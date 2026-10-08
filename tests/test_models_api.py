"""Check the real HTTP models endpoint via curl, using only a scratch key."""
import json
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import gateway


class ModelsAPITests(unittest.TestCase):
    def test_curl_openai_and_anthropic_schema(self):
        providers = [
            {'name': 'scratch', 'enabled': True, 'models': ['claude-scratch', 'gpt-scratch']},
            {'name': 'disabled', 'enabled': False, 'models': ['hidden']},
            {'name': 'duplicate', 'models': ['claude-scratch']},
        ]
        store = Mock()
        store.providers.return_value = providers
        store.local_key = 'sk-local-scratch-test-only'

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                gateway.handle(self, 'GET')

        with patch.object(gateway, 'store', return_value=store), \
                patch.object(gateway, 'provider_models', side_effect=lambda p: p['models']), \
                patch.object(gateway, 'EXTRA_MODELS', return_value=['gpt-scratch', 'gpt-scratch@high']):
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = 'http://127.0.0.1:%d/v1/models' % server.server_port
                base = ['curl', '--silent', '--show-error', '--fail', '--noproxy', '*',
                        '--max-time', '5', url]
                openai = json.loads(subprocess.check_output(
                    base + ['-H', 'Authorization: Bearer sk-local-scratch-test-only']))
                anthropic = json.loads(subprocess.check_output(
                    base + ['-H', 'x-api-key: sk-local-scratch-test-only',
                            '-H', 'anthropic-version: 2023-06-01']))
                self.assertEqual(openai['object'], 'list')
                ids = ['claude-scratch', 'gpt-scratch', 'gpt-scratch@high']
                for result in (openai, anthropic):
                    self.assertEqual([d['id'] for d in result['data']], ids)
                    self.assertFalse(result['has_more'])
                    self.assertEqual(result['first_id'], ids[0])
                    self.assertEqual(result['last_id'], ids[-1])
                for original, model in zip(openai['data'], anthropic['data']):
                    for field, value in original.items():
                        self.assertEqual(model[field], value)
                    self.assertEqual(model['type'], 'model')
                    self.assertEqual(model['display_name'], model['id'])
                    self.assertEqual(model['created_at'], '1970-01-01T00:00:00Z')
                providers.clear()
                with patch.object(gateway, 'EXTRA_MODELS', return_value=[]):
                    empty = json.loads(subprocess.check_output(base + [
                        '-H', 'x-api-key: sk-local-scratch-test-only',
                        '-H', 'anthropic-version: 2023-06-01']))
                self.assertEqual(empty['data'], [])
                self.assertIsNone(empty['first_id'])
                self.assertIsNone(empty['last_id'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
