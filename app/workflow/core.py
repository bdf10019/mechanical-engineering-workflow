"""Private task records and bounded handoffs; no CAD execution or network here."""
import base64
import copy
import hashlib
import io
import json
import re
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from app.photo3d.data import ROOT, MAX_BYTES, atomic, load, decode_photo, number

VERSION = '0.2.0'


class Conflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def ident(prefix):
    return prefix + uuid.uuid4().hex[:24]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError('資料欄位不符')


def text(value, limit=2000, required=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise ValueError('文字格式或長度不符')
    value = value.strip()
    if required and not value:
        raise ValueError('請填寫必要內容')
    return value


def parse(raw):
    def pairs(items):
        result = {}
        for key, val in items:
            if key in result:
                raise ValueError('JSON 重複欄位')
            result[key] = val
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('不接受非有限數值')))


def response_schema():
    string = {'type': 'string'}
    refs = {'type': 'array', 'items': string}
    observation = {'type': 'object', 'additionalProperties': False,
                   'properties': {'text': string, 'photo_ids': refs, 'dimension_ids': refs},
                   'required': ['text', 'photo_ids', 'dimension_ids']}
    properties = {'schema_version': {'type': 'integer', 'enum': [1]},
                  'task_id': string, 'prepared_id': string, 'input_sha256': string,
                  'status': {'type': 'string', 'enum': ['needs_information', 'proposal']},
                  'observations': {'type': 'array', 'items': observation},
                  'missing_information': {'type': 'array', 'items': string},
                  'proposed_steps': {'type': 'array', 'items': observation}}
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


class Workflow:
    def __init__(self, root=None):
        self.root = Path(root or ROOT / 'private/workflow')
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.active = set()

    def folder(self, tid):
        if not isinstance(tid, str) or not re.fullmatch(r't[0-9a-f]{24}', tid):
            raise ValueError('任務識別不符')
        path = self.root / tid
        if path.is_symlink() or self.root.resolve() not in path.resolve().parents:
            raise ValueError('任務路徑不符')
        return path

    def record(self, tid):
        return load(self.folder(tid) / 'task.json')

    def commit(self, task):
        # Immutable revision history. Only new files; never overwrite older revisions.
        target = self.folder(task['id']) / 'history' / f"r{task['revision']:06d}.json"
        target.parent.mkdir(exist_ok=True)
        with target.open('xb') as stream:
            stream.write(encoded(task))
        atomic(self.folder(task['id']) / 'task.json', task)
        return self.public(task)

    def check(self, tid, revision):
        task = self.record(tid)
        if type(revision) is not int or task['revision'] != revision:
            raise Conflict('任務已有新版，請重新載入後再操作')
        if tid in self.active:
            raise Conflict('此任務正在判讀，請等待結果')
        return task

    def invalidate(self, task):
        task.update(prepared=None, response=None, review=None)
        task['revision'] += 1
        task['updated_at'] = now()

    def create(self, name):
        name = text(name, 80, True)
        with self.lock:
            tid = ident('t')
            self.folder(tid).mkdir(exist_ok=False)
            task = {'id': tid, 'name': name, 'revision': 1,
                    'created_at': now(), 'updated_at': now(), 'photos': [],
                    'requirements': {'changes': '', 'keep': '', 'goal': ''}, 'dimensions': [],
                    'prepared': None, 'response': None, 'review': None,
                    'engineering_approved': False, 'model': None, 'drawing': None}
            return self.commit(task)

    def listing(self):
        with self.lock:
            return [self.public(load(p)) for p in sorted(self.root.glob('t*/task.json'), key=lambda p: p.stat().st_mtime, reverse=True)]

    def get(self, tid):
        with self.lock:
            return self.public(self.record(tid))

    def public(self, task):
        task = copy.deepcopy(task)
        for photo in task['photos']:
            photo['url'] = f"/api/tasks/{task['id']}/photos/{photo['id']}"
        has_photos = bool(task['photos'])
        has_req = bool(task['dimensions']) and bool(task['requirements']['goal'] or task['requirements']['changes'])
        prepared, response, review = task['prepared'], task['response'], task['review']
        ready = has_photos and has_req
        descriptors = [
            ('photos', '放入照片', 'done' if has_photos else 'ready', '選擇同一物件的 1–2 張照片'),
            ('requirements', '尺寸與要求', 'done' if has_req else 'ready' if has_photos else 'locked', '填已有尺寸、修改與保持不動項'),
            ('analysis', 'AI 判讀', 'done' if response else 'ready' if ready else 'locked', '先預覽資料，再選擇送出或匯出'),
            ('review', '核對建議', 'done' if review else 'ready' if response else 'locked', '檢查引用、建議及缺少資料'),
            ('model', '模型與修改', 'blocked', '照片建模執行器尚未接通'),
            ('drawing', '工程圖交付', 'blocked', '須先具備已核對模型及出圖能力')]
        task['nodes'] = [dict(id=i, title=t, status=s, reason=r) for i, t, s, r in descriptors]
        task['next_step'] = ('photos' if not has_photos else 'requirements' if not has_req else
                             'analysis' if not response else 'review' if not review else 'model')
        task['busy'] = task['id'] in self.active
        task['version'] = VERSION
        if prepared:
            attempt = self.folder(task['id']) / 'handoffs' / prepared['id'] / 'attempt.json'
            task['prepared']['attempted'] = attempt.exists()
            if attempt.exists():
                task['prepared']['attempt'] = load(attempt)
        return task

    def save(self, tid, expected_revision, name, requirements, dimensions):
        exact(requirements, ['changes', 'keep', 'goal'])
        clean_req = {key: text(value) for key, value in requirements.items()}
        if not isinstance(dimensions, list) or len(dimensions) > 30:
            raise ValueError('尺寸最多 30 筆')
        clean_dims, ids = [], set()
        reserved = {dim.get('id') for dim in dimensions if isinstance(dim, dict) and isinstance(dim.get('id'), str)}
        automatic = 1
        for index, dim in enumerate(dimensions, 1):
            exact(dim, ['id', 'label', 'value', 'unit', 'source'])
            did = dim['id']
            if did is None or did == '':
                while f'd{automatic}' in reserved or f'd{automatic}' in ids:
                    automatic += 1
                did = f'd{automatic}'
                automatic += 1
            if not isinstance(did, str) or not re.fullmatch(r'd[1-9][0-9]{0,5}', did) or did in ids:
                raise ValueError('尺寸識別重複或格式不符')
            ids.add(did)
            if dim['unit'] not in ('mm', 'cm') or dim['source'] not in ('user_provided', 'simulated', 'reference'):
                raise ValueError('尺寸單位或來源不符')
            clean_dims.append(dict(id=did, label=text(dim['label'], 160, True),
                                   value=number(dim['value'], .001, 100000), unit=dim['unit'], source=dim['source']))
        clean_name = text(name, 80, True)
        with self.lock:
            task = self.check(tid, expected_revision)
            task.update(name=clean_name, requirements=clean_req, dimensions=clean_dims)
            self.invalidate(task)
            return self.commit(task)

    def upload(self, tid, expected_revision, filename, base64_text):
        if not isinstance(base64_text, str) or len(base64_text) > MAX_BYTES * 4 // 3 + 8:
            raise ValueError('照片容量超限')
        try:
            raw = base64.b64decode(base64_text, validate=True)
        except ValueError as exc:
            raise ValueError('照片編碼不符') from exc
        working, _, size = decode_photo(raw, filename)
        with self.lock:
            task = self.check(tid, expected_revision)
            if len(task['photos']) >= 2:
                raise ValueError('本流程每次選 1–2 張照片；請先移除舊選圖')
            if any(p['original_sha256'] == digest(raw) for p in task['photos']):
                raise ValueError('此照片已在任務內')
            iid = ident('i')
            path = self.folder(tid) / 'images'
            path.mkdir(exist_ok=True)
            for suffix, content in (('.source', raw), ('.png', working)):
                with (path / (iid + suffix)).open('xb') as stream:
                    stream.write(content)
            from PIL import Image
            width, height = Image.open(io.BytesIO(working)).size
            task['photos'].append(dict(id=iid, name=filename, original_sha256=digest(raw),
                                       sha256=digest(working), width=width, height=height, original_size=size))
            self.invalidate(task)
            return self.commit(task)

    def remove_photo(self, tid, expected_revision, photo_id):
        with self.lock:
            task = self.check(tid, expected_revision)
            if photo_id not in {p['id'] for p in task['photos']}:
                raise ValueError('照片不屬於任務')
            task['photos'] = [p for p in task['photos'] if p['id'] != photo_id]
            self.invalidate(task)
            return self.commit(task)

    def photo(self, tid, iid):
        task = self.record(tid)
        photo = next((p for p in task['photos'] if p['id'] == iid), None)
        if not photo:
            raise ValueError('照片不屬於任務')
        content = (self.folder(tid) / 'images' / (iid + '.png')).read_bytes()
        if digest(content) != photo['sha256']:
            raise ValueError('照片內容已變更')
        return content

    def prepare(self, tid, expected_revision):
        with self.lock:
            task = self.check(tid, expected_revision)
            if not task['photos'] or not task['dimensions'] or not (task['requirements']['goal'] or task['requirements']['changes']):
                raise ValueError('請先放入照片，填寫尺寸及用途或修改要求')
            if task['prepared']:
                return self.public(task)  # Refresh never buys a second attempt.
            for photo in task['photos']:
                self.photo(tid, photo['id'])
            task['revision'] += 1
            prepared = dict(id=ident('h'), task_id=tid, task_revision=task['revision'],
                            photos=[{k: v for k, v in p.items() if k not in ('original_sha256', 'original_size', 'name')} for p in task['photos']],
                            requirements=copy.deepcopy(task['requirements']), dimensions=copy.deepcopy(task['dimensions']),
                            name=task['name'], created_at=now(), schema_version=1,
                            engineering_approved=False)
            prepared['sha256'] = digest(encoded(prepared))
            folder = self.folder(tid) / 'handoffs' / prepared['id']
            folder.mkdir(parents=True, exist_ok=False)
            with (folder / 'input.json').open('xb') as stream:
                stream.write(encoded(prepared))
            task.update(prepared=prepared, response=None, review=None, updated_at=now())
            return self.commit(task)

    def frozen(self, task, prepared_id=None):
        value = task['prepared']
        if not value or (prepared_id is not None and value['id'] != prepared_id):
            raise Conflict('請重新準備目前版本資料')
        target = self.folder(task['id']) / 'handoffs' / value['id'] / 'input.json'
        saved = load(target)
        if saved != value or digest(encoded({k: v for k, v in saved.items() if k != 'sha256'})) != saved['sha256']:
            raise ValueError('固定輸入內容不符')
        for photo in value['photos']:
            self.photo(task['id'], photo['id'])
        return saved

    def template(self, tid):
        with self.lock:
            task = self.record(tid)
            value = self.frozen(task)
            return dict(schema_version=1, task_id=tid, prepared_id=value['id'], input_sha256=value['sha256'],
                        status='needs_information', observations=[], missing_information=['尚未判讀：請填入有依據的觀察與待補資料'], proposed_steps=[])

    def package(self, tid):
        with self.lock:
            task = self.record(tid)
            value = self.frozen(task)
            content = io.BytesIO()
            with zipfile.ZipFile(content, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('input.json', encoded(value))
                archive.writestr('response-template.json', encoded(self.template(tid)))
                archive.writestr('response-schema.json', encoded(response_schema()))
                archive.writestr('START_HERE.md', '# 照片工程判讀資料包\n\n這是待判讀輸入，不是CAD或工程圖。請引用照片及尺寸ID，缺少資料明列，不推測內部、公差或材料。回傳只能符合response-schema.json的建議JSON，不提供可執行程式。尺寸來源simulated不是真實精度依據。\n')
                for photo in value['photos']:
                    archive.writestr(f"views/{photo['id']}.png", self.photo(tid, photo['id']))
            return content.getvalue()

    def validate_reply(self, task, reply):
        frozen = self.frozen(task)
        exact(reply, response_schema()['required'])
        if type(reply['schema_version']) is not int or reply['schema_version'] != 1:
            raise ValueError('回傳契約版本不符')
        if (reply['task_id'], reply['prepared_id'], reply['input_sha256']) != (task['id'], frozen['id'], frozen['sha256']):
            raise Conflict('回傳資料不屬於目前任務及版本')
        if reply['status'] not in ('needs_information', 'proposal'):
            raise ValueError('只能匯入待補資料或待核對建議')
        photos = {p['id'] for p in frozen['photos']}
        dimensions = {d['id'] for d in frozen['dimensions']}
        for key in ('observations', 'proposed_steps'):
            if not isinstance(reply[key], list) or len(reply[key]) > 30:
                raise ValueError('回傳項目數量不符')
            for item in reply[key]:
                exact(item, ['text', 'photo_ids', 'dimension_ids'])
                text(item['text'], 1500, True)
                for field, allowed in (('photo_ids', photos), ('dimension_ids', dimensions)):
                    refs = item[field]
                    if not isinstance(refs, list) or len(refs) > 30 or any(not isinstance(ref, str) or ref not in allowed for ref in refs) or len(refs) != len(set(refs)):
                        raise ValueError('照片或尺寸引用無效')
                if not item['photo_ids'] and not item['dimension_ids']:
                    raise ValueError('觀察與建議須引用照片或尺寸')
        missing = reply['missing_information']
        if not isinstance(missing, list) or len(missing) > 30:
            raise ValueError('待補資料格式不符')
        for item in missing:
            text(item, 1000, True)
        if reply['status'] == 'needs_information' and not missing:
            raise ValueError('需補資料時須列出缺項')
        if reply['status'] == 'proposal' and (not reply['observations'] or not reply['proposed_steps']):
            raise ValueError('建議須包含有依據的觀察及步驟')

    def import_reply(self, tid, expected_revision, prepared_id, reply, origin='manual_import'):
        with self.lock:
            task = self.check(tid, expected_revision)
            self.frozen(task, prepared_id)
            if task['response']:
                raise Conflict('此份資料已有待核對回傳；修訂工作資料後才能建立新一份')
            self.validate_reply(task, reply)
            value = copy.deepcopy(reply)
            value.update(id=ident('a'), origin=origin, received_at=now(), engineering_approved=False)
            folder = self.folder(tid) / 'responses'
            folder.mkdir(exist_ok=True)
            with (folder / (value['id'] + '.json')).open('xb') as stream:
                stream.write(encoded(value))
            task.update(response=value, review=None, updated_at=now())
            task['revision'] += 1
            return self.commit(task)

    def review(self, tid, expected_revision, response_id, decision):
        with self.lock:
            task = self.check(tid, expected_revision)
            if decision != 'acknowledge' or not task['response'] or task['response']['id'] != response_id:
                raise ValueError('請先載入目前建議')
            task['review'] = dict(response_id=response_id, decision=decision, at=now(), engineering_approved=False)
            task['revision'] += 1
            task['updated_at'] = now()
            return self.commit(task)
