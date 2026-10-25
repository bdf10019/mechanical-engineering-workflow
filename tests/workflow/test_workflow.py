"""Workflow safety and persistence tests, using generated images and fake cloud replies.

All task files live in TemporaryDirectory. No CAD/COLMAP subprocess, original
user photo, provider account, real API key or live HTTPS call is used.
"""
import base64
import copy
import http.client
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, PngImagePlugin

from app.workflow import cloud as cloud_module
from app.workflow.cloud import Cloud, ENDPOINT
from app.workflow.core import Conflict, Workflow, digest, encoded, parse, response_schema
from app.workflow.server import Server, handler


SECRET = 'sk-test-not-a-real-provider-key-0123456789'
TOKEN = 'isolated-session-token-for-http-tests'


def image_bytes(colour='red', metadata=False):
    image = Image.new('RGB', (48, 32), colour)
    output = io.BytesIO()
    if metadata:
        details = PngImagePlugin.PngInfo()
        details.add_text('Author', 'PRIVATE OWNER')
        details.add_text('GPS', 'PRIVATE GPS')
        image.save(output, 'PNG', pnginfo=details)
    else:
        image.save(output, 'PNG')
    return output.getvalue()


class WorkflowCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='workflow-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # An accidental provider request or external process is a test failure.
        self.addCleanup(patch.stopall)
        patch('http.client.HTTPSConnection', side_effect=AssertionError('live HTTPS forbidden')).start()
        patch('subprocess.Popen', side_effect=AssertionError('CAD/engine subprocess forbidden')).start()
        self.workflow = Workflow(self.root / 'tasks')
        self.sender = Mock(side_effect=AssertionError('unconfigured fake sender'))
        self.cloud = Cloud(self.workflow, sender=self.sender)
        self.task = self.workflow.create('合成測試任務')

    def upload(self, colour='red', raw=None):
        raw = image_bytes(colour) if raw is None else raw
        self.task = self.workflow.upload(self.task['id'], self.task['revision'], 'phone.png',
                                         base64.b64encode(raw).decode('ascii'))
        return self.task

    def save(self, source='user_provided', dimensions=None, requirements=None):
        if dimensions is None:
            dimensions = [dict(id='d1', label='外殼寬度', value=42.5, unit='mm', source=source)]
        if requirements is None:
            requirements = dict(changes='孔距改成 30 mm，先列出缺少依據', keep='外緣不動', goal='建立待核對的零件模型')
        self.task = self.workflow.save(self.task['id'], self.task['revision'], self.task['name'],
                                       requirements, dimensions)
        return self.task

    def prepare(self):
        self.upload()
        self.save()
        self.task = self.workflow.prepare(self.task['id'], self.task['revision'])
        return self.task

    def reply(self, status='proposal'):
        value = self.workflow.template(self.task['id'])
        value.update(status=status,
                     observations=[dict(text='可見外殼表面', photo_ids=[self.task['photos'][0]['id']], dimension_ids=[])],
                     missing_information=['內部幾何尚無資料'],
                     proposed_steps=[dict(text='先核對使用者提供的外殼寬度', photo_ids=[], dimension_ids=['d1'])])
        return value

    def configure(self, model='test-model', key=SECRET):
        return self.cloud.configure('openai', model, key, 512)

    def analyze(self, config=None, **changes):
        config = config or self.cloud.public()
        args = dict(tid=self.task['id'], expected_revision=self.task['revision'],
                    prepared_id=self.task['prepared']['id'], sha256=self.task['prepared']['sha256'],
                    consent=True, expected_connection_id=config['id'])
        args.update(changes)
        return self.cloud.analyze(**args)

    def completed(self, reply=None):
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': encoded(reply or self.reply()).decode('utf-8')}]}],
                'usage': {'input_tokens': 12, 'output_tokens': 34, 'total_tokens': 46}}

    def attempt(self):
        target = self.workflow.folder(self.task['id']) / 'handoffs' / self.task['prepared']['id'] / 'attempt.json'
        return json.loads(target.read_text('utf-8'))

    def all_disk_bytes(self):
        return b'\n'.join(path.read_bytes() for path in self.root.rglob('*') if path.is_file())


class RecordTests(WorkflowCase):
    def test_create_save_reopen_and_listing_are_private_persistent(self):
        self.upload()
        self.save()
        reopened = Workflow(self.root / 'tasks').get(self.task['id'])
        self.assertEqual(reopened, self.task)
        self.assertEqual(self.workflow.listing()[0]['id'], self.task['id'])
        self.assertFalse(reopened['engineering_approved'])
        self.assertIsNone(reopened['model'])
        self.assertIsNone(reopened['drawing'])

    def test_stale_revision_conflict_does_not_mutate_record(self):
        original = self.workflow.record(self.task['id'])
        with self.assertRaises(Conflict):
            self.workflow.save(self.task['id'], 0, 'stale', original['requirements'], [])
        self.assertEqual(self.workflow.record(self.task['id']), original)
        with self.assertRaises(Conflict):
            self.workflow.remove_photo(self.task['id'], True, 'i' + '0' * 24)

    def test_dimension_sources_units_and_exact_values_preserved(self):
        dimensions = [dict(id=f'd{i}', label=f'尺寸 {i}', value=value, unit=unit, source=source)
                      for i, (value, unit, source) in enumerate([
                          (4.25, 'cm', 'user_provided'), (42.5, 'mm', 'simulated'),
                          (10, 'mm', 'reference')], 1)]
        self.save(dimensions=dimensions)
        self.assertEqual(self.task['dimensions'], dimensions)

    def test_automatic_dimension_ids_do_not_collide_with_later_explicit_ids(self):
        base = dict(label='孔距', value=30, unit='mm', source='user_provided')
        self.save(dimensions=[dict(base, id=None), dict(base, id='d1'), dict(base, id='')])
        self.assertEqual([dimension['id'] for dimension in self.task['dimensions']], ['d2', 'd1', 'd3'])
        self.save(dimensions=[dict(base, id='d9'), dict(base, id=None)])
        self.assertEqual([dimension['id'] for dimension in self.task['dimensions']], ['d9', 'd1'])

    def test_dimensions_reject_duplicate_unknown_and_invalid_values(self):
        original = self.task['revision']
        base = dict(id='d1', label='寬', value=10, unit='mm', source='user_provided')
        cases = [[base, base], [dict(base, source='verified')], [dict(base, unit='px')],
                 [dict(base, value=True)], [dict(base, value=float('nan'))],
                 [dict(base, value=0)], [dict(base, id='../d1')], [dict(base, extra='x')]]
        for dimensions in cases:
            with self.subTest(dimensions=dimensions), self.assertRaises(ValueError):
                self.save(dimensions=dimensions)
            self.assertEqual(self.workflow.get(self.task['id'])['revision'], original)

    def test_preparation_blocks_missing_photos_dimensions_or_request(self):
        with self.assertRaises(ValueError):
            self.workflow.prepare(self.task['id'], self.task['revision'])
        self.upload()
        with self.assertRaises(ValueError):
            self.workflow.prepare(self.task['id'], self.task['revision'])
        self.save(requirements=dict(changes='', keep='不動', goal=''))
        with self.assertRaises(ValueError):
            self.workflow.prepare(self.task['id'], self.task['revision'])
        self.assertIsNone(self.workflow.get(self.task['id'])['prepared'])

    def test_original_preserved_and_working_copy_strips_metadata(self):
        raw = image_bytes(metadata=True)
        self.upload(raw=raw)
        photo = self.task['photos'][0]
        original = self.workflow.folder(self.task['id']) / 'images' / (photo['id'] + '.source')
        self.assertEqual(original.read_bytes(), raw)
        self.assertEqual(photo['original_sha256'], digest(raw))
        image = Image.open(io.BytesIO(self.workflow.photo(self.task['id'], photo['id'])))
        self.assertNotIn('Author', image.info)
        self.assertNotIn('GPS', image.info)
        self.assertEqual(dict(image.getexif()), {})

    def test_two_photo_limit_remove_and_reselect_without_deleting_history(self):
        self.upload('red')
        first = self.task['photos'][0]['id']
        self.upload('green')
        with self.assertRaises(ValueError):
            self.upload('blue')
        self.task = self.workflow.remove_photo(self.task['id'], self.task['revision'], first)
        self.upload('blue')
        self.assertEqual(len(self.task['photos']), 2)
        self.assertTrue((self.workflow.folder(self.task['id']) / 'images' / (first + '.source')).exists())
        with self.assertRaises(ValueError):
            self.workflow.photo(self.task['id'], first)

    def test_duplicate_bad_encoding_and_filename_rejected(self):
        self.upload()
        with self.assertRaises(ValueError):
            self.upload()
        for name, value in [('bad.txt', base64.b64encode(image_bytes()).decode()),
                            ('../photo.png', base64.b64encode(image_bytes()).decode()),
                            ('phone.png', '!not base64!')]:
            with self.subTest(name=name, value=value[:20]), self.assertRaises(ValueError):
                self.workflow.upload(self.task['id'], self.task['revision'], name, value)

    def test_prepare_is_idempotent_and_sha_is_self_consistent(self):
        self.prepare()
        again = self.workflow.prepare(self.task['id'], self.task['revision'])
        self.assertEqual(again, self.task)
        value = self.workflow.frozen(self.workflow.record(self.task['id']))
        self.assertEqual(value['sha256'], digest(encoded({k: v for k, v in value.items() if k != 'sha256'})))

    def test_changed_input_invalidates_prepared_response_and_review(self):
        self.prepare()
        old_reply = self.reply()
        old_prepared = self.task['prepared']['id']
        self.save()
        self.assertIsNone(self.task['prepared'])
        self.assertIsNone(self.task['response'])
        self.assertIsNone(self.task['review'])
        self.task = self.workflow.prepare(self.task['id'], self.task['revision'])
        self.assertNotEqual(old_prepared, self.task['prepared']['id'])
        with self.assertRaises(Conflict):
            self.workflow.import_reply(self.task['id'], self.task['revision'], old_prepared, old_reply)

    def test_history_and_old_reply_files_not_overwritten(self):
        folder = self.workflow.folder(self.task['id'])
        first_bytes = (folder / 'history' / 'r000001.json').read_bytes()
        self.prepare()
        reply = self.reply()
        self.task = self.workflow.import_reply(self.task['id'], self.task['revision'], self.task['prepared']['id'], reply)
        response_id = self.task['response']['id']
        response_bytes = (folder / 'responses' / (response_id + '.json')).read_bytes()
        self.save()
        self.assertEqual(first_bytes, (folder / 'history' / 'r000001.json').read_bytes())
        self.assertEqual(response_bytes, (folder / 'responses' / (response_id + '.json')).read_bytes())
        revisions = sorted(folder.joinpath('history').glob('r*.json'))
        self.assertEqual(len(revisions), self.task['revision'])

    def test_package_has_only_working_photos_and_contract_not_originals_or_paths(self):
        self.upload(raw=image_bytes(metadata=True))
        self.save()
        self.task = self.workflow.prepare(self.task['id'], self.task['revision'])
        self.configure()
        with zipfile.ZipFile(io.BytesIO(self.workflow.package(self.task['id']))) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), 5)
            self.assertEqual(set(names) - {f"views/{self.task['photos'][0]['id']}.png"},
                             {'input.json', 'response-template.json', 'response-schema.json', 'START_HERE.md'})
            payload = b'\n'.join(archive.read(name) for name in names)
            for forbidden in [b'.source', b'original_sha256', b'original_size', b'phone.png',
                              b'PRIVATE OWNER', b'PRIVATE GPS', str(self.root).encode(), SECRET.encode()]:
                self.assertNotIn(forbidden, payload)
            self.assertFalse(json.loads(archive.read('input.json'))['engineering_approved'])

    def test_tampered_working_photo_and_frozen_sha_are_rejected(self):
        self.prepare()
        task = self.workflow.record(self.task['id'])
        iid = task['photos'][0]['id']
        path = self.workflow.folder(task['id']) / 'images' / (iid + '.png')
        with patch.object(Path, 'read_bytes', autospec=True, side_effect=lambda p: b'tampered' if p == path else b''):
            with self.assertRaises(ValueError):
                self.workflow.frozen(task)
        task['prepared']['sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            self.workflow.frozen(task)

    def test_path_traversal_and_other_task_photo_are_rejected(self):
        for tid in ['../task', 't' + '0' * 24 + '/../../', '', None]:
            with self.subTest(tid=tid), self.assertRaises(ValueError):
                self.workflow.get(tid)
        self.upload()
        other = self.workflow.create('另一任務')
        with self.assertRaises(ValueError):
            self.workflow.photo(other['id'], self.task['photos'][0]['id'])


class ReplyTests(WorkflowCase):
    def setUp(self):
        super().setUp()
        self.prepare()

    def test_schema_rejects_unknown_fields_nested_fields_and_bad_references(self):
        for location in ['top', 'observation', 'unknown_photo', 'unknown_dimension', 'duplicate_ref', 'no_ref']:
            reply = self.reply()
            if location == 'top':
                reply['engineering_approved'] = True
            elif location == 'observation':
                reply['observations'][0]['script'] = 'dangerous()'
            elif location == 'unknown_photo':
                reply['observations'][0]['photo_ids'] = ['i' + '0' * 24]
            elif location == 'unknown_dimension':
                reply['proposed_steps'][0]['dimension_ids'] = ['d999']
            elif location == 'duplicate_ref':
                reply['observations'][0]['photo_ids'] *= 2
            else:
                reply['observations'][0]['photo_ids'] = []
            with self.subTest(location=location), self.assertRaises(ValueError):
                self.workflow.import_reply(self.task['id'], self.task['revision'], self.task['prepared']['id'], reply)
        self.assertIsNone(self.workflow.get(self.task['id'])['response'])

    def test_other_task_prepared_and_sha_rejected(self):
        for field, value in [('task_id', 't' + '0' * 24), ('prepared_id', 'h' + '0' * 24), ('input_sha256', '0' * 64)]:
            reply = self.reply()
            reply[field] = value
            with self.subTest(field=field), self.assertRaises(Conflict):
                self.workflow.import_reply(self.task['id'], self.task['revision'], self.task['prepared']['id'], reply)

    def test_empty_proposal_and_missing_information_are_not_silent_success(self):
        cases = [dict(status='proposal', observations=[]), dict(status='proposal', proposed_steps=[]),
                 dict(status='needs_information', missing_information=[]), dict(status='engineering_approved'),
                 dict(schema_version=True)]
        for changes in cases:
            reply = self.reply()
            reply.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.workflow.validate_reply(self.workflow.record(self.task['id']), reply)

    def test_review_acknowledges_only_and_never_creates_cad_or_engineering_approval(self):
        self.task = self.workflow.import_reply(self.task['id'], self.task['revision'], self.task['prepared']['id'], self.reply())
        rid = self.task['response']['id']
        for decision in ['approve', 'engineering_approved', True]:
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                self.workflow.review(self.task['id'], self.task['revision'], rid, decision)
        with self.assertRaises(ValueError):
            self.workflow.review(self.task['id'], self.task['revision'], 'a' + '0' * 24, 'acknowledge')
        self.task = self.workflow.review(self.task['id'], self.task['revision'], rid, 'acknowledge')
        self.assertFalse(self.task['engineering_approved'])
        self.assertFalse(self.task['response']['engineering_approved'])
        self.assertFalse(self.task['review']['engineering_approved'])
        self.assertIsNone(self.task['model'])
        self.assertIsNone(self.task['drawing'])
        self.assertEqual([n['status'] for n in self.task['nodes'][-2:]], ['blocked', 'blocked'])

    def test_parse_duplicate_fields_nonfinite_and_bad_json_rejected(self):
        for raw in ['{"a":1,"a":2}', '{"value":NaN}', '{"value":Infinity}', '{not json']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse(raw)
        schema = response_schema()
        self.assertFalse(schema['additionalProperties'])
        for name in ['observations', 'proposed_steps']:
            self.assertFalse(schema['properties'][name]['items']['additionalProperties'])


class CloudTests(WorkflowCase):
    def setUp(self):
        super().setUp()
        self.prepare()

    def test_configuration_is_memory_only_public_view_never_returns_key(self):
        config = self.configure()
        self.assertTrue(config['configured'])
        self.assertFalse(config['verified'])
        self.assertFalse(config['live_call_tested'])
        self.assertEqual(config['endpoint'], ENDPOINT)
        self.assertNotIn(SECRET, encoded(config).decode())
        self.assertNotIn(SECRET.encode(), self.all_disk_bytes())
        self.assertFalse(Cloud(Workflow(self.root / 'tasks'), sender=self.sender).public()['configured'])
        self.cloud.disconnect()
        self.assertIsNone(self.cloud.config)

    def test_configuration_rejects_custom_endpoints_bad_models_keys_and_token_limits(self):
        cases = [('other', 'test-model', SECRET, 512), ('openai', 'https://other.invalid', SECRET, 512),
                 ('openai', 'bad model', SECRET, 512), ('openai', 'test-model', 'short', 512),
                 ('openai', 'test-model', SECRET + '\n', 512), ('openai', 'test-model', SECRET, True),
                 ('openai', 'test-model', SECRET, 4097)]
        # A URL-looking model uses characters permitted in model IDs, not an endpoint.
        cases = [case for case in cases if case[1] != 'https://other.invalid']
        for args in cases:
            with self.subTest(args=args[:2]), self.assertRaises(ValueError):
                self.cloud.configure(*args)
        self.sender.assert_not_called()

    def test_payload_is_finite_structured_and_uses_stripped_working_images(self):
        self.configure()
        payload = self.cloud.payload(self.workflow.record(self.task['id']), self.cloud.config)
        self.assertFalse(payload['store'])
        self.assertEqual(payload['max_output_tokens'], 512)
        self.assertNotIn('tools', payload)
        self.assertTrue(payload['text']['format']['strict'])
        content = payload['input'][0]['content']
        photos = [item for item in content if item['type'] == 'input_image']
        self.assertEqual(len(photos), 1)
        image = base64.b64decode(photos[0]['image_url'].split(',', 1)[1])
        self.assertEqual(image, self.workflow.photo(self.task['id'], self.task['photos'][0]['id']))
        self.assertNotIn(SECRET, encoded(payload).decode())

    def test_missing_consent_configuration_or_sha_never_consumes_attempt(self):
        with self.assertRaises(ValueError):
            self.analyze()
        config = self.configure()
        for changes in [dict(consent=False), dict(consent='yes'), dict(sha256='0' * 64)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.analyze(config, **changes)
        self.sender.assert_not_called()
        self.assertFalse(self.workflow.get(self.task['id'])['prepared']['attempted'])

    def test_configuration_switch_invalidates_old_connection_consent(self):
        old_config = self.configure()
        new_config = self.configure('another-test-model')
        self.assertNotEqual(old_config['id'], new_config['id'])
        with self.assertRaises(Conflict):
            self.analyze(old_config)
        self.sender.assert_not_called()
        self.assertFalse(self.workflow.get(self.task['id'])['prepared']['attempted'])

    def test_success_one_call_and_one_receipt_without_engineering_promotion(self):
        config = self.configure()
        self.sender.side_effect = None
        self.sender.return_value = self.completed()
        value = self.analyze(config)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(value['response']['origin'], 'openai_responses')
        self.assertEqual(self.attempt()['status'], 'completed')
        self.assertFalse(self.attempt()['retry_allowed'])
        self.assertEqual(self.attempt()['usage']['total_tokens'], 46)
        self.assertFalse(value['engineering_approved'])
        self.assertNotIn(SECRET.encode(), self.all_disk_bytes())
        self.task = value
        with self.assertRaises(Conflict):
            self.analyze(config)
        self.assertEqual(self.sender.call_count, 1)

    def test_failed_call_does_not_reflect_secret_and_never_retries_after_reopen(self):
        config = self.configure()
        self.sender.side_effect = RuntimeError('Authorization: Bearer ' + SECRET)
        with self.assertRaises(ValueError) as caught:
            self.analyze(config)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(self.attempt()['status'], 'failed')
        self.assertIsNone(self.workflow.get(self.task['id'])['response'])
        self.assertFalse(self.cloud.busy)
        self.assertNotIn(self.task['id'], self.workflow.active)
        self.assertNotIn(SECRET.encode(), self.all_disk_bytes())
        other = Cloud(Workflow(self.root / 'tasks'), sender=self.sender)
        cfg = other.configure('openai', 'test-model', SECRET, 512)
        with self.assertRaises(Conflict):
            other.analyze(self.task['id'], self.task['revision'], self.task['prepared']['id'],
                          self.task['prepared']['sha256'], True, cfg['id'])
        self.assertEqual(self.sender.call_count, 1)

    def test_refusal_and_incomplete_are_failures_without_partial_import(self):
        config = self.configure()
        self.sender.side_effect = None
        self.sender.return_value = {'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'not allowed'}]}]}
        with self.assertRaises(ValueError):
            self.analyze(config)
        self.assertEqual(self.attempt()['status'], 'failed')
        self.assertIsNone(self.workflow.get(self.task['id'])['response'])
        # A new input revision is needed for another explicit attempt.
        self.save()
        self.task = self.workflow.prepare(self.task['id'], self.task['revision'])
        self.sender.return_value = self.completed()
        self.sender.return_value['status'] = 'incomplete'
        with self.assertRaises(ValueError):
            self.analyze(config)
        self.assertEqual(self.attempt()['status'], 'failed')
        self.assertIsNone(self.workflow.get(self.task['id'])['response'])

    def test_malformed_or_stale_cloud_reply_is_quarantined(self):
        config = self.configure()
        reply = self.reply()
        reply['prepared_id'] = 'h' + '0' * 24
        self.sender.side_effect = None
        self.sender.return_value = self.completed(reply)
        with self.assertRaises(ValueError):
            self.analyze(config)
        self.assertEqual(self.attempt()['status'], 'failed')
        self.assertIsNone(self.workflow.get(self.task['id'])['response'])

    def test_busy_task_config_and_edits_are_locked_during_sender(self):
        config = self.configure()
        good = self.completed()
        def inspect(body, key):
            self.assertTrue(self.cloud.busy)
            self.assertIn(self.task['id'], self.workflow.active)
            with self.assertRaises(Conflict):
                self.cloud.disconnect()
            with self.assertRaises(Conflict):
                self.configure('changed')
            with self.assertRaises(Conflict):
                self.save()
            return good
        self.sender.side_effect = inspect
        self.analyze(config)
        self.assertFalse(self.cloud.busy)
        self.assertNotIn(self.task['id'], self.workflow.active)

    def test_concurrent_duplicate_submission_cannot_start_second_provider_call(self):
        config = self.configure()
        entered, release = threading.Event(), threading.Event()
        good = self.completed()
        values, failures = [], []

        def waiting_sender(body, key):
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError('test did not release fake provider')
            return good

        def first_submission():
            try:
                values.append(self.analyze(config))
            except BaseException as error:
                failures.append(error)

        self.sender.side_effect = waiting_sender
        worker = threading.Thread(target=first_submission, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            with self.assertRaises(Conflict):
                self.analyze(config)
            self.assertEqual(self.sender.call_count, 1)
            self.assertEqual(self.attempt()['status'], 'started')
        finally:
            release.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(values), 1)
        self.assertEqual(self.attempt()['status'], 'completed')
        self.assertFalse(self.cloud.busy)


class TransportTests(WorkflowCase):
    """Use inert HTTP/socket/timer objects; never open a provider connection."""

    def connection(self, status=200, chunks=None):
        connection = Mock()
        response = Mock(status=status)
        response.read.side_effect = chunks if chunks is not None else [b'{"status":"completed"}', b'']
        connection.getresponse.return_value = response
        return connection, response

    def invoke(self, connection, clock=None, timer_factory=None):
        clock = clock if clock is not None else [0.0]
        timer_factory = timer_factory or Mock(return_value=Mock())
        with patch('http.client.HTTPSConnection', return_value=connection) as factory, \
                patch.object(cloud_module.ssl, 'create_default_context', return_value=Mock()), \
                patch.object(cloud_module.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(cloud_module.threading, 'Timer', side_effect=timer_factory):
            result = cloud_module.transport({'model': 'test-model'}, SECRET)
        return result, factory

    def test_one_fixed_endpoint_request_and_timer_cancelled_on_success(self):
        connection, response = self.connection()
        timer = Mock()
        result, factory = self.invoke(connection, timer_factory=Mock(return_value=timer))
        self.assertEqual(result, {'status': 'completed'})
        self.assertEqual(factory.call_args.args, ('api.openai.com',))
        self.assertEqual(connection.auto_open, 0, 'watchdog-close must not allow HTTPConnection to reconnect')
        self.assertEqual(connection.request.call_count, 1)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ('POST', '/v1/responses'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer ' + SECRET)
        self.assertNotIn(SECRET.encode(), kwargs['body'])
        timer.start.assert_called_once()
        timer.cancel.assert_called_once()
        self.assertTrue(connection.close.called)

    def test_non_success_and_redirect_never_read_reflect_or_retry_provider_body(self):
        for status in [307, 401, 429, 500]:
            connection, response = self.connection(status=status)
            response.read.side_effect = AssertionError('provider error body must not be read or reflected: ' + SECRET)
            with self.subTest(status=status), self.assertRaises(ValueError) as caught:
                self.invoke(connection)
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertEqual(connection.request.call_count, 1)
            response.read.assert_not_called()
            self.assertTrue(connection.close.called)

    def test_response_limit_is_one_mebibyte_plus_detection_byte(self):
        connection, response = self.connection(chunks=[b'x' * 65536] * 16 + [b'x'])
        with self.assertRaisesRegex(ValueError, '容量超限'):
            self.invoke(connection)
        self.assertEqual(response.read.call_count, 17)
        self.assertEqual(response.read.call_args.args, (1,))
        self.assertEqual(connection.request.call_count, 1)
        self.assertTrue(connection.close.called)

    def test_connect_expiring_budget_never_sends_body_or_reads_response(self):
        connection, response = self.connection()
        clock = [0.0]
        connection.connect.side_effect = lambda: clock.__setitem__(0, 61.0)
        with self.assertRaises(TimeoutError):
            self.invoke(connection, clock=clock)
        connection.request.assert_not_called()
        connection.getresponse.assert_not_called()
        self.assertTrue(connection.close.called)

    def test_watchdog_shutdown_during_headers_rejects_late_success(self):
        connection, response = self.connection()
        callbacks, timers = [], []

        def timer_factory(interval, callback):
            self.assertLessEqual(interval, 60)
            callbacks.append(callback)
            timer = Mock()
            timers.append(timer)
            return timer

        def delayed_headers():
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            return response

        connection.getresponse.side_effect = delayed_headers
        with self.assertRaises(TimeoutError):
            self.invoke(connection, timer_factory=timer_factory)
        self.assertTrue(connection.sock.shutdown.called)
        self.assertEqual(connection.request.call_count, 1)
        response.read.assert_not_called()
        self.assertTrue(connection.close.called)
        timers[0].cancel.assert_called_once()


class HttpTests(WorkflowCase):
    def setUp(self):
        super().setUp()
        self.server = Server(('127.0.0.1', 0), http.client.HTTPConnection if False else __import__('http.server', fromlist=['BaseHTTPRequestHandler']).BaseHTTPRequestHandler)
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = handler(self.workflow, self.cloud, self.port, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method='GET', path='/api/health', body=None, headers=None):
        base = {'Host': f'127.0.0.1:{self.port}'}
        if method == 'POST':
            base.update({'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}', 'X-Session-Token': TOKEN})
        base.update(headers or {})
        content = encoded(body) if isinstance(body, (dict, list)) else body
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request(method, path, content, base)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_health_session_static_files_no_false_completion(self):
        status, headers, raw = self.request()
        self.assertEqual(status, 200)
        health = parse(raw)
        self.assertFalse(health['photo_to_cad'])
        self.assertFalse(health['drawing_output'])
        self.assertFalse(health['live_cloud_verified'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(parse(self.request(path='/api/session')[2])['token'], TOKEN)
        for path, mime in [('/', 'text/html'), ('/workflow.css', 'text/css'),
                           ('/workflow.js', 'text/javascript'), ('/workflow-state.mjs', 'text/javascript')]:
            with self.subTest(path=path):
                status, headers, raw = self.request(path=path)
                self.assertEqual(status, 200)
                self.assertTrue(headers['Content-Type'].startswith(mime))
                self.assertGreater(len(raw), 10)

    def test_host_origin_token_and_cross_site_are_rejected(self):
        cases = [('GET', '/api/session', None, {'Host': 'evil.invalid'}),
                 ('GET', '/api/session', None, {'Sec-Fetch-Site': 'cross-site'}),
                 ('POST', '/api/tasks', {'name': 'x'}, {'Origin': 'https://evil.invalid'}),
                 ('POST', '/api/tasks', {'name': 'x'}, {'X-Session-Token': ''}),
                 ('POST', '/api/tasks', {'name': 'x'}, {'Sec-Fetch-Site': 'cross-site'})]
        for method, path, body, headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(method, path, body, headers)[0], 403)
        self.assertEqual(len(self.workflow.listing()), 1)

    def test_invalid_json_errors_do_not_reflect_input_secret(self):
        for raw in ['{"api_key":"' + SECRET, '{"name":"x","name":"y"}', '{"name":NaN}']:
            status, _, body = self.request('POST', '/api/tasks', raw)
            self.assertEqual(status, 400)
            self.assertNotIn(SECRET.encode(), body)
        self.assertEqual(self.request('POST', '/api/tasks', {'name': 'x', 'unknown': SECRET})[0], 400)

    def test_invalid_content_length_never_reflects_header_or_creates_task(self):
        status, _, body = self.request('POST', '/api/tasks', b'', {'Content-Length': SECRET})
        self.assertEqual(status, 400)
        self.assertNotIn(SECRET.encode(), body)
        self.assertEqual(parse(body), {'error': '內容長度不符'})
        self.assertEqual(len(self.workflow.listing()), 1)

    def test_wrong_content_type_body_limit_unknown_route_and_methods(self):
        self.assertEqual(self.request('POST', '/api/tasks', '{}', {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', '/api/tasks', b'')[0], 413)
        self.assertEqual(self.request(path='/api/health?key=' + SECRET)[0], 400)
        self.assertEqual(self.request(path='/no-such-file')[0], 404)
        self.assertEqual(self.request(path='/../core.py')[0], 404)
        self.assertEqual(self.request('PUT', '/api/tasks', '{}')[0], 405)

    def test_create_save_conflict_photo_download_package_and_import_routes(self):
        self.prepare()
        base = '/api/tasks/' + self.task['id']
        status, _, body = self.request(path=base)
        self.assertEqual(status, 200)
        self.assertEqual(parse(body)['id'], self.task['id'])
        status, headers, body = self.request(path=base + '/photos/' + self.task['photos'][0]['id'])
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/png')
        self.assertEqual(body, image_bytes())
        status, headers, body = self.request(path=base + '/package')
        self.assertEqual(status, 200)
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertEqual(headers['Content-Type'], 'application/zip')
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(body)))
        self.assertEqual(self.request(path=base + '/response-template')[0], 200)
        stale = dict(expected_revision=0, name='過期資料', requirements=self.task['requirements'], dimensions=self.task['dimensions'])
        self.assertEqual(self.request('POST', base + '/save', stale)[0], 409)
        status, _, body = self.request('POST', base + '/import', dict(expected_revision=self.task['revision'],
                                               prepared_id=self.task['prepared']['id'], reply=self.reply()))
        self.assertEqual(status, 200)
        self.assertFalse(parse(body)['engineering_approved'])
        self.sender.assert_not_called()

    def test_connection_routes_do_not_expose_key_or_use_provider(self):
        status, _, body = self.request('POST', '/api/connection', dict(provider='openai', model='test-model',
                                      api_key=SECRET, max_output_tokens=512))
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET.encode(), body)
        self.assertNotIn(SECRET.encode(), self.request(path='/api/connection')[2])
        self.assertNotIn(SECRET.encode(), self.all_disk_bytes())
        self.assertFalse(parse(self.request('POST', '/api/disconnect', {})[2])['configured'])
        self.sender.assert_not_called()


if __name__ == '__main__':
    unittest.main()
