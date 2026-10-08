"""Claude alias switching against scratch configs only; no installed config reads."""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import agents
import gateway
import i18n


class ClaudeTiersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        data = self.home / 'data'
        self.config = self.home / '.claude' / 'settings.json'
        self.config.parent.mkdir()
        for target, attrs in (
                (gateway, {'DATA_DIR': data}),
                (i18n, {'DATA_DIR': data, 'SETTINGS_FILE': data / 'ui-settings.json'}),
                (agents, {'STATE_FILE': data / 'agents.json', 'BACKUP_DIR': data / 'agent-backups'})):
            for name, value in attrs.items():
                p = patch.object(target, name, value)
                p.start()
                self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'HOME': str(self.home), 'CLAUDE_CONFIG_DIR': str(self.config.parent),
                                    'API_CONSOLE_HOME': str(data)})
        p.start()
        self.addCleanup(p.stop)
        self.agent = agents.ClaudeCode()

    def write(self, data):
        self.config.write_text(json.dumps(data), encoding='utf-8')

    def read(self):
        return json.loads(self.config.read_text(encoding='utf-8'))

    def enable(self, model='main'):
        self.agent.enable('http://127.0.0.1:9999', 'sk-local-test', model, [model])

    def tiers(self, on=True):
        i18n.save_routing_settings({'claudeTiersEnabled': on, 'claudeTierLarge': 'big',
                                  'claudeTierMedium': 'mid', 'claudeTierSmall': 'tiny'})

    def test_off_on_reenable_off_exact_bytes(self):
        original = b'{\r\n "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL":"original"}, "theme":"dark"\r\n}\r\n'
        self.config.write_bytes(original)
        self.enable()
        self.assertEqual(self.read()['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'], 'main')
        self.tiers()
        self.enable('next')
        env = self.read()['env']
        self.assertNotIn('CLAUDE_CODE_SUBAGENT_MODEL', env)
        for alias, value in [('OPUS', 'big'), ('SONNET', 'mid'), ('HAIKU', 'tiny')]:
            self.assertEqual(env['ANTHROPIC_DEFAULT_' + alias + '_MODEL'], value)
        self.tiers(False)
        self.enable()
        env = self.read()['env']
        self.assertEqual(env['ANTHROPIC_MODEL'], 'main')
        self.assertEqual(env['CLAUDE_CODE_SUBAGENT_MODEL'], 'main')
        for alias in ('OPUS', 'SONNET', 'HAIKU'):
            self.assertNotIn('ANTHROPIC_DEFAULT_' + alias + '_MODEL', env)
        self.assertEqual(self.agent.disable(), 'exact')
        self.assertEqual(self.config.read_bytes(), original)

    def test_user_edits_survive_reenable_and_disable(self):
        self.write({'env': {'ANTHROPIC_DEFAULT_OPUS_MODEL': 'original'}, 'theme': 'dark'})
        self.tiers()
        self.enable()
        data = self.read()
        data['theme'] = 'light'
        data['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'] = 'user-big'
        self.write(data)
        self.enable('next')
        self.assertEqual(self.read()['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'], 'big')
        self.assertEqual(self.agent.disable(), 'values')
        self.assertEqual(self.read(), {'env': {'ANTHROPIC_DEFAULT_OPUS_MODEL': 'user-big'}, 'theme': 'light'})

    def test_user_edit_after_enable_is_kept(self):
        self.write({'env': {'ANTHROPIC_MODEL': 'original'}})
        self.tiers()
        self.enable()
        data = self.read()
        data['env']['ANTHROPIC_MODEL'] = 'user-main'
        data['new'] = True
        self.write(data)
        self.assertEqual(self.agent.disable(), 'values')
        self.assertEqual(self.read(), {'env': {'ANTHROPIC_MODEL': 'user-main'}, 'new': True})

    def test_missing_file_restored(self):
        self.enable()
        self.tiers()
        self.enable()
        self.assertEqual(self.agent.disable(), 'exact')
        self.assertFalse(self.config.exists())

    def test_new_dynamic_field_original_captured(self):
        self.write({'env': {'EXTRA': 'before'}})
        self.enable()
        changes = self.agent.changes
        def expanded(*args):
            return {**changes(*args), ('env', 'EXTRA'): 'gateway'}
        with patch.object(self.agent, 'changes', expanded):
            self.enable()
        data = self.read()
        data['theme'] = 'light'
        self.write(data)
        self.assertEqual(self.agent.disable(), 'values')
        self.assertEqual(self.read(), {'env': {'EXTRA': 'before'}, 'theme': 'light'})

    def test_empty_on_tiers_use_main_model(self):
        self.write({'env': {'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'original'}})
        i18n.save_routing_settings({'claudeTiersEnabled': True})
        self.enable('gemini-3-flash')
        env = self.read()['env']
        for alias in ('OPUS', 'SONNET', 'HAIKU'):
            self.assertEqual(env['ANTHROPIC_DEFAULT_' + alias + '_MODEL'], 'gemini-3-flash')
        self.assertNotIn('CLAUDE_CODE_SUBAGENT_MODEL', env)
        self.assertEqual(self.agent.disable(), 'exact')

    def test_partial_tiers_preserve_explicit_models_and_main_effort(self):
        i18n.save_routing_settings({'claudeTiersEnabled': True,
                                   'claudeTierLarge': 'big', 'claudeTierMedium': '   '})
        self.enable('gemini-3-flash@high')
        env = self.read()['env']
        self.assertEqual(env['ANTHROPIC_DEFAULT_OPUS_MODEL'], 'big')
        for alias in ('SONNET', 'HAIKU'):
            self.assertEqual(env['ANTHROPIC_DEFAULT_' + alias + '_MODEL'], 'gemini-3-flash@high')

    def test_empty_tier_aliases_route_anthropic_http_to_main(self):
        calls = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, body['model']))
                raw = json.dumps({'id': 'msg_test', 'type': 'message', 'role': 'assistant',
                    'model': body['model'], 'content': [{'type': 'text', 'text': 'ok'}],
                    'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 1}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                gateway.handle(self, 'POST')

        servers = [ThreadingHTTPServer(('127.0.0.1', 0), cls) for cls in (Upstream, Handler)]
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
        for thread in threads:
            thread.start()
        try:
            # Store file and singleton are isolated as well as the agent config.
            with patch.object(gateway, 'STORE_FILE', self.home / 'data' / 'gateway.json'), \
                    patch.object(gateway, 'STORE', None):
                pid = gateway.save_provider({'name': 'fake', 'format': 'anthropic',
                    'base_url': 'http://127.0.0.1:%d' % servers[0].server_port,
                    'manual_models': 'gemini-3-flash'})
                gateway.add_keys(pid, 'sk-fake-test-only')
                i18n.save_routing_settings({'claudeTiersEnabled': True, 'fallbackEnabled': False})
                self.enable('gemini-3-flash')
                env = self.read()['env']
                for alias in ('OPUS', 'SONNET', 'HAIKU'):
                    model = env['ANTHROPIC_DEFAULT_' + alias + '_MODEL']
                    request = urllib.request.Request('http://127.0.0.1:%d/v1/messages' % servers[1].server_port,
                        data=json.dumps({'model': model, 'max_tokens': 1,
                            'messages': [{'role': 'user', 'content': 'hi'}]}).encode(),
                        headers={'Content-Type': 'application/json',
                                 'x-api-key': gateway.store().local_key, 'anthropic-version': '2023-06-01'})
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.load(response)['model'], 'gemini-3-flash')
                self.assertEqual(calls, [('/v1/messages', 'gemini-3-flash')] * 3)
        finally:
            for server, thread in zip(servers, threads):
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
