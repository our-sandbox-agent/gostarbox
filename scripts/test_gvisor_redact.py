"""Redaction must mask real secret shapes without destroying ordinary evidence."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from gvisor_redact import MASK, Redactor  # noqa: E402

KEY = 'sk-ant-api03-EXAMPLEONLYNOTREAL0123456789'


class RedactTests(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor().add(KEY)

    def test_registered_secret_is_masked_everywhere_it_appears(self):
        record = {'command': ['docker', 'create', '-e', f'ANTHROPIC_API_KEY={KEY}', 'img'],
                  'stdout': f'using {KEY} now', 'stderr': '', 'nested': [{'k': KEY}],
                  KEY: 'secret used as a key'}
        scrubbed = self.redactor.scrub(record)
        self.assertNotIn(KEY, json.dumps(scrubbed))
        self.assertEqual(self.redactor.leaks(record), [])
        self.assertEqual(scrubbed['command'][3], f'ANTHROPIC_API_KEY={MASK}')

    def test_unregistered_secret_shapes_are_masked(self):
        for value in ['sk-ant-api03-OTHERVALUE0123456789', 'ghp_' + 'a' * 36,
                      'AKIAIOSFODNN7EXAMPLE', 'xoxb-1234567890-abcdefghij',
                      'eyJhbGciOi.eyJzdWIiOi.SflKxwRJSMeKKF2QT4', 'sk-' + 'b' * 24,
                      '-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----']:
            with self.subTest(value=value[:16]):
                self.assertNotIn(value, Redactor().scrub({'stdout': value})['stdout'])

    def test_secret_named_field_masks_its_value(self):
        scrubbed = Redactor().scrub({'api_key': 'unknown-shape-but-sensitive',
                                     'password': 'hunter2horse', 'note': 'plain text'})
        self.assertEqual(scrubbed['api_key'], MASK)
        self.assertEqual(scrubbed['password'], MASK)
        self.assertEqual(scrubbed['note'], 'plain text')

    def test_ordinary_evidence_is_left_alone(self):
        record = {'command': ['docker', 'create', '--pids-limit', '64', '--cpus', '1'],
                  'stdout': 'Runtime=runsc systrap SHELL_OK', 'exit_code': 0,
                  'credential_delivery_ref': 'private-handover-note-2026-09',
                  'authorization_ref': 'user-authorized-local-vm-2026-09-26',
                  'credentials_injected': False, 'model_budget': 'none configured'}
        self.assertEqual(Redactor().scrub(record), record)

    def test_exempt_field_keeps_its_exemption_when_nested_or_prefixed(self):
        # report.json nests these under "environment", and the flat form already worked.
        for text in ['environment.credential_delivery_ref: private-handover-note-2026-09',
                     'credential_delivery_ref: private-handover-note-2026-09',
                     'workspace.authorization_ref: user-authorized-local-vm-2026-09-26']:
            with self.subTest(text=text.split(':')[0]):
                self.assertEqual(Redactor().scrub(text), text)
        nested = {'environment': {'credential_delivery_ref': 'private-handover-note-2026-09'}}
        self.assertEqual(Redactor().scrub(nested), nested)

    def test_go_struct_field_named_credential_is_not_a_secret(self):
        # runsc logs SysProcAttr; masking it hides exactly what a reviewer checks.
        text = 'sandbox.go:1433] SysProcAttr: &{Chroot: Credential:<nil> Ptrace:false Setsid:true}'
        self.assertEqual(Redactor().scrub(text), text)

    def test_go_numeric_credential_struct_preserved_but_secret_still_masked(self):
        text = 'SysProcAttr: &{Credential:&{Uid:0 Gid:0 Groups:[] NoSetGroups:false} Ptrace:false}'
        self.assertEqual(Redactor().text(text), text)
        self.assertNotIn('opaque-sensitive-value', Redactor().text('Credential:opaque-sensitive-value'))
        self.assertEqual(Redactor().text('password:&{Uid:0 Gid:0 Groups:[] NoSetGroups:false}'), 'password:'+MASK)

    def test_short_and_literal_values_are_not_masked(self):
        for text in ['credentials_injected: false', 'secret: none', 'password=1']:
            with self.subTest(text=text):
                self.assertEqual(Redactor().scrub(text), text)

    def test_environment_secrets_are_registered_but_short_values_ignored(self):
        redactor = Redactor().add_from_environ(
            {'ANTHROPIC_API_KEY': KEY, 'MY_TOKEN': 'x', 'PATH': '/usr/bin'})
        self.assertIn(KEY, redactor.values)
        self.assertNotIn('x', redactor.values)
        self.assertNotIn('/usr/bin', redactor.values)

    def test_overlapping_secrets_leave_no_tail(self):
        redactor = Redactor().add(KEY).add(KEY[:20])
        self.assertEqual(redactor.leaks({'a': KEY}), [])

    def test_registered_value_is_masked_even_without_a_known_shape(self):
        # A provider-specific or rotated credential may match none of SHAPES;
        # registering the literal must still keep it out of the bundle.
        opaque = 'Zq7Z-plain-opaque-credential-9481'
        redactor = Redactor().add(opaque)
        record = {'stdout': f'connecting with {opaque}', 'note': 'unrelated'}
        self.assertNotIn(opaque, json.dumps(redactor.scrub(record)))
        self.assertEqual(redactor.leaks(record), [])
        self.assertIn(opaque, json.dumps(Redactor().scrub(record)),
                      msg='sanity: no shape rule covers it, so registration is what masked it')

    def test_matrix_write_and_log_scrub_before_touching_disk(self):
        spec = importlib.util.spec_from_file_location(
            'matrix', Path(__file__).with_name('gvisor-no-key-matrix.py'))
        matrix = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(matrix)
        with tempfile.TemporaryDirectory() as directory:
            instance = object.__new__(matrix.Matrix)
            instance.out = Path(directory)
            instance.label = 'E05'
            instance.redactor = Redactor().add(KEY)
            instance.write('report.json', {'stdout': f'key {KEY}'})
            instance.log({'command': ['claude', '--api-key', KEY]})
            for name in ('report.json', 'E05.jsonl'):
                text = (Path(directory) / name).read_text()
                self.assertNotIn(KEY, text)
                self.assertIn(MASK, text)
