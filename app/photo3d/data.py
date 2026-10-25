"""Private image intake, explicit provenance and atomic local records."""
import base64
import hashlib
import io
import json
import math
import os
import re
import threading
import uuid
import warnings
from pathlib import Path
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[2]
ID = re.compile(r'^[pj][0-9a-f]{24}$')
MAX_BYTES = 20 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 50_000_000


def identifier(prefix):
    return prefix + uuid.uuid4().hex[:24]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def atomic(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('數值超出範圍或不是有限數值')
    return float(value)


def decode_photo(raw, filename):
    if not isinstance(filename, str) or not 1 <= len(filename) <= 120 or any(c in filename for c in '/\\\x00<>:'):
        raise ValueError('請提供檔名，不接受路徑或特殊控制字元')
    if any(ord(c) < 32 for c in filename): raise ValueError('檔名含控制字元')
    extension = Path(filename).suffix.lower()
    if extension not in ('.jpg', '.jpeg', '.png'): raise ValueError('目前只接受 JPEG／PNG')
    if not raw or len(raw) > MAX_BYTES: raise ValueError('每張照片須介於 1 byte 與 20 MiB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(raw))
            if image.format not in ('JPEG', 'PNG'): raise ValueError('影像內容不是 JPEG／PNG')
            if (image.format == 'PNG') != (extension == '.png'): raise ValueError('副檔名與影像格式不符')
            if image.width * image.height > Image.MAX_IMAGE_PIXELS: raise ValueError('圖片超過 50 MP')
            if getattr(image, 'n_frames', 1) != 1: raise ValueError('目前不接受動畫圖片')
            image.verify()
            image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert('RGB')
            original_size = list(image.size)
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            working = io.BytesIO(); image.save(working, 'PNG')
            image.thumbnail((240, 180), Image.Resampling.LANCZOS)
            thumb = io.BytesIO(); image.save(thumb, 'PNG')
        return working.getvalue(), thumb.getvalue(), original_size
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError('圖片無法安全解碼') from error


class Store:
    def __init__(self, root=None):
        self.root = Path(root or ROOT / 'private/photo3d')
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def folder(self, pid):
        if not isinstance(pid, str) or not ID.fullmatch(pid) or not pid.startswith('p'):
            raise ValueError('專案 ID 不合法')
        return self.root / pid

    def project(self, pid):
        return load(self.folder(pid) / 'project.json')

    def listing(self):
        return [load(p) for p in sorted(self.root.glob('p*/project.json'), key=lambda p: p.stat().st_mtime, reverse=True)]

    def create(self, name, source):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80: raise ValueError('名稱限 1–80 字')
        if source not in ('user_photos', 'synthetic_test'): raise ValueError('必須明確選擇照片來源')
        with self.lock:
            pid = identifier('p'); folder = self.folder(pid); folder.mkdir(exist_ok=False)
            (folder / 'images').mkdir()
            value = {'id': pid, 'name': name.strip(), 'source': source, 'revision': 1,
                     'images': [], 'jobs': [], 'engineering_approved': False}
            atomic(folder / 'project.json', value)
            return value

    def upload(self, pid, filename, encoded):
        if not isinstance(encoded, str) or len(encoded) > MAX_BYTES * 4 // 3 + 8: raise ValueError('照片容量超限')
        try: raw = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as error: raise ValueError('照片編碼錯誤') from error
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock:
            value = self.project(pid)
            existing = next((i for i in value['images'] if i['original_sha256'] == digest), None)
            if existing: return {'duplicate': True, 'image': existing, 'project': value}
            if len(value['images']) >= 60: raise ValueError('每個專案最多 60 張照片')
            working, thumb, size = decode_photo(raw, filename)
            iid = 'i' + uuid.uuid4().hex[:24]; folder = self.folder(pid) / 'images'
            # Content is staged under a new ID; metadata commits last. No source overwrite.
            for suffix, content in (('.source', raw), ('.png', working), ('.thumb.png', thumb)):
                with (folder / (iid + suffix)).open('xb') as stream: stream.write(content)
            image = {'id': iid, 'filename': filename, 'original_sha256': digest,
                     'working_sha256': hashlib.sha256(working).hexdigest(), 'oriented_size': size,
                     'working_size': list(Image.open(io.BytesIO(working)).size),
                     'metadata_stripped_from_working_copy': True}
            value['images'].append(image); value['revision'] += 1
            atomic(self.folder(pid) / 'project.json', value)
            return {'duplicate': False, 'image': image, 'project': value}

    def set_capture(self, pid, capture_mode, preparation):
        if capture_mode not in ('camera_moves','object_rotates'): raise ValueError('拍攝方式無效')
        if preparation not in ('none','green_diffuse_surfaces_v3'): raise ValueError('不支援的前景處理')
        if capture_mode=='object_rotates' and preparation=='none': raise ValueError('固定相機旋轉物件需明確的前景策略')
        with self.lock:
            value=self.project(pid); value.update(capture_mode=capture_mode,preparation=preparation)
            value['revision']+=1; atomic(self.folder(pid)/'project.json',value); return value

    def set_dimension_reference(self, pid, reference):
        if not isinstance(reference,dict) or set(reference)!={'kind','source_ref','dimensions'}: raise ValueError('尺寸來源欄位不符')
        if reference['kind'] not in ('simulated','provided_reference'): raise ValueError('尺寸資料來源無效')
        if not isinstance(reference['source_ref'],str) or not 1<=len(reference['source_ref'])<=160: raise ValueError('缺尺寸來源參照')
        dims=reference['dimensions']
        if not isinstance(dims,list) or not 1<=len(dims)<=30: raise ValueError('尺寸筆數無效')
        identifiers=set()
        for item in dims:
            if not isinstance(item,dict) or set(item)!={'feature_id','value','unit'}: raise ValueError('尺寸項目欄位不符')
            if not isinstance(item['feature_id'],str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}',item['feature_id']): raise ValueError('尺寸特徵ID無效')
            if item['feature_id'] in identifiers: raise ValueError('尺寸特徵重複')
            identifiers.add(item['feature_id']); number(item['value'],.001,100000)
            if item['unit'] not in ('mm','cm'): raise ValueError('尺寸單位無效')
        with self.lock:
            value=self.project(pid)
            record=json.loads(json.dumps(reference,allow_nan=False))
            record.update(applied_to_model=False,geometry_accuracy_verified=False)
            value.setdefault('dimension_references',[]).append(record); value['revision']+=1
            atomic(self.folder(pid)/'project.json',value); return value

    def image_path(self, pid, iid, thumb=False):
        value = self.project(pid)
        if iid not in {i['id'] for i in value['images']}: raise ValueError('照片不屬於專案')
        return self.folder(pid) / 'images' / (iid + ('.thumb.png' if thumb else '.png'))
