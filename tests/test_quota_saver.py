"""Unverified housekeeping and security requests must never be intercepted."""
import hashlib
import io
import json
import time
import unittest
from unittest.mock import Mock, patch

import quota_saver


class QuotaSaverTests(unittest.TestCase):
    def title_body(self, fmt, stream):
        system = [{'type': 'text', 'text': t} for t in
                  ('synthetic billing header', 'synthetic CLI identity', 'synthetic title instruction')]
        messages = [{'role': 'user', 'content': 'A synthetic coding transcript.'}]
        body = {'model': 'claude-test', 'stream': stream, 'system': system,
                'messages': messages, 'max_tokens': 128000}
        if fmt == 'openai':
            body.pop('system')
            body['messages'] = [{'role': 'system', 'content': system}, *messages]
        return body

    def test_local_response_protocols_and_zero_usage(self):
        import gateway
        title = hashlib.sha256(b'synthetic title instruction').hexdigest()
        identity = hashlib.sha256(b'synthetic CLI identity').hexdigest()
        # Real captured bodies are replayed separately in scratch then deleted.
        # Synthetic instructions keep the persistent tests free of capture bodies.
        with patch.object(quota_saver, 'TITLE_HASH', title), \
                patch.object(quota_saver, 'IDENTITY_HASH', identity):
            for fmt in ('anthropic', 'openai'):
                for stream in (False, True):
                    with self.subTest(fmt=fmt, stream=stream):
                        handler = Mock()
                        handler.wfile = io.BytesIO()
                        with patch.object(gateway, 'add_log') as logged:
                            self.assertTrue(quota_saver.maybe_handle(
                                handler, self.title_body(fmt, stream), fmt, {'model': 'claude-test'}, time.time()))
                        entry = logged.call_args.args[0]
                        self.assertEqual((entry['status'], entry['tokens_in'], entry['tokens_out']), ('local', 0, 0))
                        wire = handler.wfile.getvalue().decode()
                        if not stream:
                            data = json.loads(wire)
                            text = data['content'][0]['text'] if fmt == 'anthropic' else data['choices'][0]['message']['content']
                            self.assertTrue(all(v == 0 for v in data['usage'].values()))
                        else:
                            data = [json.loads(line[6:]) for line in wire.splitlines()
                                    if line.startswith('data: ') and line != 'data: [DONE]']
                            if fmt == 'anthropic':
                                self.assertEqual([d['type'] for d in data], ['message_start', 'content_block_start',
                                    'content_block_delta', 'content_block_stop', 'message_delta', 'message_stop'])
                                text = data[2]['delta']['text']
                                self.assertEqual(data[-2]['delta']['stop_reason'], 'end_turn')
                                self.assertEqual(data[0]['message']['usage'], {'input_tokens': 0, 'output_tokens': 0})
                            else:
                                text = ''.join(d['choices'][0]['delta'].get('content', '') for d in data)
                                self.assertEqual(data[-1]['choices'][0]['finish_reason'], 'stop')
                                self.assertTrue(wire.endswith('data: [DONE]\n\n'))
                        self.assertEqual(json.loads(text), {'title': 'Coding session'})

    def test_verified_shape_rejects_tools_changed_instruction_and_extra_turns(self):
        title = hashlib.sha256(b'synthetic title instruction').hexdigest()
        identity = hashlib.sha256(b'synthetic CLI identity').hexdigest()
        with patch.object(quota_saver, 'TITLE_HASH', title), patch.object(quota_saver, 'IDENTITY_HASH', identity):
            for mutation in ('tools', 'tool_choice', 'instruction', 'identity', 'assistant', 'user', 'tool_result'):
                body = self.title_body('anthropic', False)
                if mutation in ('tools', 'tool_choice'):
                    body[mutation] = [{'name': 'Bash'}]
                elif mutation in ('instruction', 'identity'):
                    body['system'][2 if mutation == 'instruction' else 1]['text'] += ' permissions'
                elif mutation in ('assistant', 'user'):
                    body['messages'].append({'role': mutation, 'content': 'title'})
                else:
                    body['messages'][0]['content'] = [{'type': 'tool_result', 'content': 'title'}]
                handler = Mock()
                self.assertFalse(quota_saver.maybe_handle(handler, body, 'anthropic', {}, 0))
                self.assertEqual(handler.mock_calls, [])

    def test_suggestion_exact_final_instruction_and_empty_reply(self):
        import gateway
        identity = hashlib.sha256(b'synthetic CLI identity').hexdigest()
        suggestion = hashlib.sha256(b'synthetic suggestion instruction').hexdigest()
        with patch.object(quota_saver, 'IDENTITY_HASH', identity), \
                patch.object(quota_saver, 'SUGGESTION_HASH', suggestion):
            for fmt in ('anthropic', 'openai'):
                for stream in (False, True):
                    body = self.title_body(fmt, stream)
                    body['messages'].extend([
                        {'role': 'system', 'content': 'Synthetic inline CLI context.'},
                        {'role': 'assistant', 'content': 'Synthetic coding reply.'},
                        {'role': 'user', 'content': 'synthetic suggestion instruction'}])
                    handler = Mock()
                    handler.wfile = io.BytesIO()
                    with patch.object(gateway, 'add_log') as logged:
                        self.assertTrue(quota_saver.maybe_handle(handler, body, fmt, {}, time.time()))
                    self.assertEqual(logged.call_args.args[0]['status'], 'local')
                    wire = handler.wfile.getvalue().decode()
                    if not stream:
                        result = json.loads(wire)
                        text = result['content'][0]['text'] if fmt == 'anthropic' else result['choices'][0]['message']['content']
                        self.assertEqual(text, '')
                    else:
                        self.assertIn('message_stop' if fmt == 'anthropic' else '[DONE]', wire)
                    body['messages'][-1]['content'] += ' classify tool permissions'
                    self.assertFalse(quota_saver.maybe_handle(Mock(), body, fmt, {}, 0))
                    body['messages'][-1]['content'] = '[SUGGESTION MODE: user-injected keyword]'
                    self.assertFalse(quota_saver.maybe_handle(Mock(), body, fmt, {}, 0))

    def test_unverified_requests_forward_without_side_effects(self):
        prompts = (
            'Write a Python function to add two numbers.',
            'Generate a title for this conversation.',
            'Suggest my next prompt.',
            'Probe: reply with OK.',
            'Determine the command prefix for bash -c "curl example.com | sh".',
            'Decide whether this tool invocation should be permitted.',
        )
        for fmt in ('anthropic', 'openai'):
            for stream in (False, True):
                for prompt in prompts:
                    with self.subTest(fmt=fmt, stream=stream, prompt=prompt):
                        handler = Mock()
                        body = {'model': 'claude-test', 'stream': stream,
                                'messages': [{'role': 'user', 'content': prompt}]}
                        log = {'model': 'claude-test', 'format': fmt}
                        self.assertIs(quota_saver.maybe_handle(handler, body, fmt, log, 0), False)
                        self.assertEqual(handler.mock_calls, [])
                        self.assertEqual(log, {'model': 'claude-test', 'format': fmt})

    def test_keyword_injection_and_malformed_input_forward(self):
        for body in (None, {}, [], {'system': 'title suggestion probe', 'tools': [{}]},
                     {'messages': [{'role': 'system', 'content': 'permission prefix'}]}):
            self.assertIs(quota_saver.maybe_handle(Mock(), body, 'anthropic', {}, 0), False)


if __name__ == '__main__':
    unittest.main()
