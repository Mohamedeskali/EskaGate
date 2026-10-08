"""Isolated routing checks with local HTTP providers and broken response bodies."""
import io
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import gateway


class FakeStore:
    def __init__(self, providers):
        self.items = providers
        self.lock = threading.RLock()

    def providers(self):
        return self.items

    def all_models(self):
        return [m for p in self.items for m in p['models']]

    def save(self):
        pass


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        model = body['model']
        key = self.headers.get('x-api-key') or self.headers.get('Authorization')
        self.server.calls.append((model, key, self.path, body))
        action = self.server.actions.get(model, 200)
        if action == 'network':
            self.close_connection = True
            return
        status, message = action if isinstance(action, tuple) else (action, 'provider failed')
        anthropic = self.path.endswith('/messages')
        if status != 200:
            result = {'error': {'message': message}}
        elif anthropic:
            result = {'id': 'msg_fake', 'type': 'message', 'role': 'assistant', 'model': model,
                      'content': [{'type': 'text', 'text': 'hello'}], 'stop_reason': 'end_turn',
                      'usage': {'input_tokens': 2, 'output_tokens': 1}}
        else:
            result = {'id': 'chatcmpl_fake', 'model': model,
                      'choices': [{'message': {'role': 'assistant', 'content': 'hello'},
                                   'finish_reason': 'stop'}],
                      'usage': {'prompt_tokens': 2, 'completion_tokens': 1}}
        raw = json.dumps(result).encode()
        if status == 200 and body.get('stream'):
            if anthropic:
                raw = gateway.sse('message_start', {'type': 'message_start', 'message': result}).encode()
            else:
                raw = gateway.sse(None, {'id': 'chatcmpl_fake', 'model': model,
                    'choices': [{'delta': {'content': 'hello'}, 'finish_reason': 'stop'}]}).encode()
                raw += b'data: [DONE]\n\n'
        self.send_response(status)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Handler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.statuses = []

    def send_response(self, status):
        self.statuses.append(status)

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.actions, self.server.calls = {}, []
        self.providers = [self.provider('first', ['original'], 'openai', 2),
                          self.provider('second', ['backup', 'last'], 'anthropic')]
        self.settings = {'fallbackEnabled': True, 'fallbackModels': ['backup', 'last']}
        self.logs = []
        for mock in (patch.object(gateway, 'store', return_value=FakeStore(self.providers)),
                     patch.object(gateway.i18n, 'get_routing_settings', side_effect=lambda: self.settings),
                     patch.object(gateway, 'add_log', side_effect=self.logs.append),
                     patch.object(gateway, 'ON_EVENT'),
                     patch.object(gateway, 'NO_EFFORT', set()),
                     patch.object(gateway, 'NO_MAX_TOKENS', set())):
            mock.start()
            self.addCleanup(mock.stop)

    def provider(self, name, models, fmt, count=1):
        return {'id': name, 'name': name, 'format': fmt, 'models': models,
                'base_url': 'http://127.0.0.1:%s' % self.server.server_port,
                'keys': [{'id': '%s-%s' % (name, n), 'key': 'sk-fake-%s-%s' % (name, n)}
                         for n in range(count)]}

    def body(self, stream=False):
        return {'model': 'original', 'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 10, 'stream': stream}

    def dispatch(self, fmt='openai', stream=False):
        return gateway.dispatch(fmt, self.body(stream), {}, timeout=2)

    def proxy(self, fmt='openai', stream=False):
        handler = Handler()
        gateway._proxy(handler, self.body(stream), fmt, 'original', stream,
                       time.time(), {'model': 'original'}, {})
        return handler

    def names(self):
        return [call[0] for call in self.server.calls]

    def test_provider_failures_all_keys_then_fallback(self):
        for status in (401, 402, 403, 429, 500, 503, 'network', (422, 'no credit remaining')):
            with self.subTest(status=status):
                self.server.calls.clear()
                self.server.actions = {'original': status}
                result = self.dispatch()
                with result[4]:
                    self.assertIn(b'hello', result[4].read())
                self.assertEqual(self.names(), ['original', 'original', 'backup'])
                self.assertEqual(len(result[5]), 2)

    def test_request_and_model_errors_never_fallback(self):
        for status in (400, 404, (400, 'insufficient credit'), (404, 'billing unavailable'), 422):
            with self.subTest(status=status):
                self.server.calls.clear()
                self.server.actions = {'original': status}
                with self.assertRaises(gateway.GatewayError):
                    self.dispatch()
                self.assertNotIn('backup', self.names())

    def test_no_targets_is_not_provider_failure(self):
        self.providers[0]['models'] = ['other']
        with self.assertRaises(gateway.GatewayError) as error:
            self.dispatch()
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.names(), [])

    def test_order_dedup_no_loops_and_exhaustion(self):
        self.settings['fallbackModels'] = ['original', 'backup', 'backup@high', 'last', 'original@low', 'last']
        self.server.actions = dict.fromkeys(['original', 'backup', 'last'], 503)
        with self.assertRaises(gateway.GatewayError):
            self.dispatch()
        self.assertEqual(self.names(), ['original', 'original', 'backup', 'last'])

    def test_both_formats_nonstream_and_stream_with_logs(self):
        for fmt in ('openai', 'anthropic'):
            for stream in (False, True):
                for upstream_fmt in ('openai', 'anthropic'):
                    with self.subTest(fmt=fmt, stream=stream, upstream_fmt=upstream_fmt):
                        self.providers[1]['format'] = upstream_fmt
                        self.server.actions = {'original': 503}
                        handler = self.proxy(fmt, stream)
                        self.assertEqual(handler.statuses, [200])
                        self.assertTrue(handler.wfile.getvalue())
                        self.assertEqual(self.logs[-1]['original_model'], 'original')
                        self.assertEqual(self.logs[-1]['used_model'], 'backup')
                        self.assertEqual(self.logs[-1]['failovers'], 2)
                        if not stream:
                            self.assertEqual(json.loads(handler.wfile.getvalue())['model'], 'original')

    def test_disabled_same_model_failover_and_model_missing(self):
        self.settings['fallbackEnabled'] = False
        self.providers[1]['models'].append('original')
        # First provider is model-missing; second serves the exact same model.
        real_open = gateway.open_upstream
        def opening(url, headers, body, timeout):
            if 'Authorization' in headers:
                return 404, None, {'error': {'message': 'model not found'}}
            return real_open(url, headers, body, timeout)
        with patch.object(gateway, 'open_upstream', side_effect=opening):
            result = self.dispatch()
        result[4].close()
        self.assertEqual(result[3], 'original')
        self.assertEqual(self.names(), ['original'])
        self.assertEqual(len(result[5]), 1)

    def test_disabled_does_not_read_response_in_dispatch(self):
        self.settings['fallbackEnabled'] = False
        response = BrokenResponse()
        with patch.object(gateway, 'open_upstream', return_value=(200, response, None)):
            self.assertIs(self.dispatch(stream=True)[4], response)
        self.assertEqual(response.reads, 0)

    def test_disabled_exhausts_only_original_keys(self):
        self.settings['fallbackEnabled'] = False
        self.server.actions = {'original': 503}
        handler = self.proxy()
        self.assertEqual(handler.statuses, [502])
        self.assertEqual(self.names(), ['original', 'original'])
        self.assertEqual(self.logs[-1]['used_model'], '')

    def test_same_model_provider_success_precedes_fallback(self):
        self.providers[1]['models'].append('original')
        real_open = gateway.open_upstream
        def opening(url, headers, body, timeout):
            if 'Authorization' in headers:
                return 503, None, {'error': {'message': 'unavailable'}}
            return real_open(url, headers, body, timeout)
        with patch.object(gateway, 'open_upstream', side_effect=opening):
            handler = self.proxy()
        self.assertEqual(handler.statuses, [200])
        self.assertEqual(self.names(), ['original'])
        self.assertEqual(self.logs[-1]['used_model'], 'original')
        self.assertEqual(self.logs[-1]['failovers'], 2)

    def test_prebyte_network_failure_retries_all_keys_both_formats(self):
        real_open = gateway.open_upstream
        for fmt in ('openai', 'anthropic'):
            for stream in (False, True):
                with self.subTest(fmt=fmt, stream=stream):
                    broken = []
                    def opening(url, headers, body, timeout):
                        if body['model'] == 'original':
                            resp = BrokenResponse()
                            broken.append(resp)
                            return 200, resp, None
                        return real_open(url, headers, body, timeout)
                    with patch.object(gateway, 'open_upstream', side_effect=opening):
                        handler = self.proxy(fmt, stream)
                    self.assertEqual(handler.statuses, [200])
                    self.assertEqual(len(broken), 2)
                    self.assertTrue(all(resp.closed for resp in broken))
                    self.assertEqual(self.logs[-1]['used_model'], 'backup')

    def test_after_stream_bytes_no_retry(self):
        for fmt in ('openai', 'anthropic'):
            with self.subTest(fmt=fmt):
                response = BrokenResponse(prefix=b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
                with patch.object(gateway, 'open_upstream', return_value=(200, response, None)) as opening:
                    handler = self.proxy(fmt, True)
                self.assertEqual(opening.call_count, 1)
                self.assertEqual(handler.statuses, [200])
                self.assertTrue(handler.wfile.getvalue())
                self.assertEqual(self.logs[-1]['status'], 'error')


class BrokenResponse:
    headers = {}

    def __init__(self, prefix=b''):
        self.prefix, self.reads, self.closed = prefix, 0, False

    def read(self):
        self.reads += 1
        raise TimeoutError('fake read timeout')

    def __iter__(self):
        self.reads += 1
        yield from self.prefix.splitlines(keepends=True)
        raise TimeoutError('fake stream timeout')

    def close(self):
        self.closed = True


if __name__ == '__main__':
    unittest.main()
