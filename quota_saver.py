"""Local replies for exactly fingerprinted Claude Code housekeeping.

Title and suggestion instructions were captured from installed Claude Code
2.1.292 in an isolated PTY after ordinary turns (2026-10-08). Suggestions
required the documented enable variable and warm-cache usage in the fake
response. Probe signatures were not captured. Unknown versions fail open.
"""
import hashlib
import json
import time
import uuid


TITLE_HASH = '765b5ba2fa0a315a3c749c7e54bf5cef450084a745eb01e66371b8b6359d4752'
IDENTITY_HASH = '2719b7a469d904b3281d8488976f33103944211279bddf04c05bca44d184dae6'
SUGGESTION_HASH = '848ab6d7d37cb9e01ff58d81467438a21ff26e5287bbf510e76d387df592fb0d'


def _texts(content):
    if isinstance(content, str):
        return [content]
    if isinstance(content, list) and all(isinstance(x, dict) and
            x.get('type') == 'text' and isinstance(x.get('text'), str) for x in content):
        return [x['text'] for x in content]
    return None


def _is_title(body, fmt):
    if not isinstance(body, dict) or body.get('tools') or body.get('tool_choice'):
        return False
    messages = body.get('messages')
    if not isinstance(messages, list):
        return False
    system = _texts(body.get('system', []))
    if system is None:
        return False
    users = []
    for message in messages:
        if not isinstance(message, dict):
            return False
        text = _texts(message.get('content'))
        if text is None:
            return False
        if fmt == 'openai' and message.get('role') == 'system':
            system.extend(text)
        elif message.get('role') == 'user':
            users.append(text)
        else:
            return False
    # Only the exact captured final instruction block. Other system blocks are
    # Claude's dynamic billing header and fixed CLI identity, not user content.
    return (len(users) == 1 and len(system) == 3 and
            hashlib.sha256(system[1].encode('utf-8')).hexdigest() == IDENTITY_HASH and
            hashlib.sha256(system[-1].encode('utf-8')).hexdigest() == TITLE_HASH)


def _is_suggestion(body, fmt):
    if not isinstance(body, dict) or body.get('tools') or body.get('tool_choice'):
        return False
    messages = body.get('messages')
    system = _texts(body.get('system', []))
    if not isinstance(messages, list) or not messages or system is None:
        return False
    turns = []
    for message in messages:
        if not isinstance(message, dict):
            return False
        text = _texts(message.get('content'))
        if text is None:
            return False
        if fmt == 'openai' and message.get('role') == 'system' and not turns:
            system.extend(text)
        elif message.get('role') == 'system' and turns:
            # Captured CLI inserts text-only context between conversational
            # turns. It never supplies the final suggestion instruction.
            continue
        elif message.get('role') in ('user', 'assistant'):
            turns.append((message['role'], text))
        else:
            return False
    return (len(system) == 3 and len(turns) >= 3 and
            any(role == 'assistant' for role, _ in turns[:-1]) and
            hashlib.sha256(system[1].encode('utf-8')).hexdigest() == IDENTITY_HASH and
            turns[-1][0] == 'user' and len(turns[-1][1]) == 1 and
            hashlib.sha256(turns[-1][1][0].encode('utf-8')).hexdigest() == SUGGESTION_HASH)


def maybe_handle(handler, body, client_fmt, log, start):
    """Return True only when a local reply was sent; otherwise forward normally."""
    if client_fmt not in ('anthropic', 'openai'):
        return False
    title = _is_title(body, client_fmt)
    if not title and not _is_suggestion(body, client_fmt):
        return False
    import gateway  # late import: gateway owns the caller and logging
    text = json.dumps({'title': 'Coding session'}) if title else ''
    model = str(body.get('model') or '')
    ident = uuid.uuid4().hex
    usage = {'input_tokens': 0, 'output_tokens': 0}
    message = {'id': 'msg_local_' + ident, 'type': 'message', 'role': 'assistant',
               'model': model, 'content': [{'type': 'text', 'text': text}],
               'stop_reason': 'end_turn', 'stop_sequence': None, 'usage': usage}
    completion = {'id': 'chatcmpl-local-' + ident, 'object': 'chat.completion',
                  'created': int(time.time()), 'model': model,
                  'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text},
                               'finish_reason': 'stop'}],
                  'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}
    try:
        if not body.get('stream'):
            gateway._send_json(handler, 200, message if client_fmt == 'anthropic' else completion)
        else:
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            handler.send_header('Cache-Control', 'no-cache')
            handler.send_header('Connection', 'close')
            handler.end_headers()
            if client_fmt == 'anthropic':
                events = [
                    ('message_start', {'message': {**message, 'content': [], 'stop_reason': None}}),
                    ('content_block_start', {'index': 0, 'content_block': {'type': 'text', 'text': ''}}),
                    ('content_block_delta', {'index': 0, 'delta': {'type': 'text_delta', 'text': text}}),
                    ('content_block_stop', {'index': 0}),
                    ('message_delta', {'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': usage}),
                    ('message_stop', {}),
                ]
                wire = ''.join('event: %s\ndata: %s\n\n' %
                               (name, json.dumps({'type': name, **data})) for name, data in events)
            else:
                chunk = {k: completion[k] for k in ('id', 'created', 'model')}
                chunk['object'] = 'chat.completion.chunk'
                wire = ''
                for delta, finish in (({'role': 'assistant', 'content': ''}, None),
                                      ({'content': text}, None), ({}, 'stop')):
                    data = {**chunk, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                    if finish:
                        data['usage'] = completion['usage']
                    wire += 'data: ' + json.dumps(data) + '\n\n'
                wire += 'data: [DONE]\n\n'
            handler.wfile.write(wire.encode('utf-8'))
            handler.wfile.flush()
            handler.close_connection = True
        gateway.add_log({**log, 'status': 'local', 'code': 200, 'provider': 'local',
                         'key': '', 'tokens_in': 0, 'tokens_out': 0,
                         'ms': int((time.time() - start) * 1000)})
    except (BrokenPipeError, ConnectionResetError):
        gateway.add_log({**log, 'status': 'error', 'code': 499, 'provider': 'local',
                         'key': '', 'tokens_in': 0, 'tokens_out': 0,
                         'error': 'Client closed the connection.',
                         'ms': int((time.time() - start) * 1000)})
    return True
