"""Offline checks for the legacy updater's local credential boundary."""
import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'update' / 'bilibili.py'


class UpdateCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        syntax = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        helpers = ast.Module(body=[node for node in syntax.body
                                  if isinstance(node, ast.FunctionDef)
                                  and node.name in ('_load_cookies', '_clean_cookie', 'get_common_headers')],
                             type_ignores=[])
        self.scope = {'os': os, 'json': json, 'List': List, 'Dict': Dict,
                      'Optional': Optional, '__file__': str(self.root / 'update' / 'bilibili.py'),
                      'COOKIES': []}
        exec(compile(helpers, str(SOURCE), 'exec'), self.scope)
        self.environment = patch.dict(os.environ, {'BILIBILI_COOKIE': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def load(self):
        return self.scope['_load_cookies']()

    def write(self, value):
        (self.root / 'cookies.json').write_text(json.dumps(value), encoding='utf-8-sig')

    def test_missing_file_does_not_supply_any_credentials(self):
        self.assertEqual(self.load(), [])
        with self.assertRaisesRegex(RuntimeError, 'cookies.json'):
            self.scope['get_common_headers']()

    def test_local_array_and_whitespace(self):
        self.write([' SESSDATA=LOCAL_TEST; x=1 ', '', 'unrelated=value'])
        self.assertEqual(self.load(), ['SESSDATA=LOCAL_TEST; x=1'])

    def test_environment_takes_priority_without_opening_local_file(self):
        os.environ['BILIBILI_COOKIE'] = ' SESSDATA=ENV_TEST '
        (self.root / 'cookies.json').write_text('invalid JSON', encoding='utf-8')
        self.assertEqual(self.load(), ['SESSDATA=ENV_TEST'])

    def test_object_format_is_rejected_by_legacy_tool(self):
        self.write({'SESSDATA': 'OBJECT_TEST'})
        with self.assertRaisesRegex(RuntimeError, '字符串数组'):
            self.load()

    def test_non_string_array_is_rejected(self):
        self.write(['SESSDATA=LOCAL_TEST', 123])
        with self.assertRaisesRegex(RuntimeError, '字符串数组'):
            self.load()

    def test_malformed_file_error_does_not_echo_contents(self):
        content = 'PRIVATE_TEST_VALUE not valid JSON'
        (self.root / 'cookies.json').write_text(content, encoding='utf-8')
        with self.assertRaises(RuntimeError) as raised:
            self.load()
        self.assertNotIn(content, str(raised.exception))

    def test_explicit_cookie_remains_supported(self):
        headers = self.scope['get_common_headers']('SESSDATA=EXPLICIT_TEST')
        self.assertEqual(headers['Cookie'], 'SESSDATA=EXPLICIT_TEST')


if __name__ == '__main__':
    unittest.main()
