"""
Local AI Gateway — standard library only.

- Providers: a base URL + several real API keys each (stored on disk with
  owner-only permissions, never sent back to the browser unmasked).
- One local key: agents authenticate with it; the gateway picks a working
  real key behind it and fails over to the next key/provider on errors.
- Endpoints: OpenAI  POST /v1/chat/completions
             Anthropic POST /v1/messages (+ /v1/messages/count_tokens)
             GET /v1/models
  Both formats work against both kinds of upstream; requests, responses and
  streams are translated when the formats differ.
- Logs: model, provider, key (masked), status, timing. Never message content.
"""

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import i18n

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("API_CONSOLE_HOME") or (Path.home() / ".api-test-console"))
STORE_FILE = DATA_DIR / "gateway.json"
LOG_FILE = DATA_DIR / "gateway-logs.jsonl"
MAX_LOGS = 500

# Cool-down applied to a real key after a failure, in seconds.
COOLDOWN = {"invalid": 3600, "limited": 60, "no_credit": 1800, "error": 20}


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass


def write_private(path, text):
    """Atomic write readable only by the current user (keys live in here)."""
    ensure_data_dir()
    tmp = Path(str(path) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def mask_key(key):
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "…"
    return f"{key[:5]}…{key[-4:]}"


def new_id(prefix):
    return prefix + secrets.token_hex(4)


def new_local_key():
    return "sk-local-" + secrets.token_urlsafe(24)


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {"local_key": "", "providers": []}
        self.load()

    def load(self):
        with self.lock:
            if STORE_FILE.exists():
                try:
                    self.data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
                except Exception:
                    # Never overwrite a file we couldn't read: keep a copy aside.
                    STORE_FILE.replace(STORE_FILE.with_suffix(".broken.json"))
                    self.data = {"local_key": "", "providers": []}
            self.data.setdefault("providers", [])
            if not self.data.get("local_key"):
                self.data["local_key"] = new_local_key()
                self.save()

    def save(self):
        with self.lock:
            write_private(STORE_FILE, json.dumps(self.data, indent=2, ensure_ascii=False))

    # ---- lookups ---------------------------------------------------------
    @property
    def local_key(self):
        return self.data["local_key"]

    def provider(self, pid):
        return next((p for p in self.data["providers"] if p["id"] == pid), None)

    def providers(self):
        return self.data["providers"]

    def all_models(self):
        seen = []
        for p in self.providers():
            if not p.get("enabled", True):
                continue
            for m in provider_models(p):
                if m not in seen:
                    seen.append(m)
        return seen

    # ---- public (masked) view for the browser --------------------------
    def public_view(self):
        now = time.time()
        out = []
        with self.lock:
            for p in self.providers():
                keys = []
                for k in p.get("keys", []):
                    cooling = k.get("cooldown_until", 0) > now
                    keys.append({
                        "id": k["id"], "masked": mask_key(k["key"]), "label": k.get("label", ""),
                        "status": k.get("status", "unknown"), "last_error": k.get("last_error", ""),
                        "last_checked": k.get("last_checked"), "last_used": k.get("last_used"),
                        "cooldown_left": int(k["cooldown_until"] - now) if cooling else 0,
                        "active": k["id"] == p.get("active_key_id"),
                    })
                out.append({
                    "id": p["id"], "name": p["name"], "base_url": p["base_url"],
                    "format": p.get("format", "openai"), "enabled": p.get("enabled", True),
                    "models": provider_models(p), "manual_models": p.get("manual_models", []),
                    "keys": keys,
                })
        return out


def provider_models(p):
    models = list(p.get("manual_models") or [])
    for m in p.get("models") or []:
        if m not in models:
            models.append(m)
    return models


STORE = None
STORE_LOCK = threading.Lock()


def store():
    global STORE
    with STORE_LOCK:
        if STORE is None:
            STORE = Store()
        return STORE


# ---------------------------------------------------------------------------
# Logs (no message content, ever)
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()
_logs = []
_logs_loaded = False


def _load_logs():
    global _logs_loaded
    if _logs_loaded:
        return
    _logs_loaded = True
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-MAX_LOGS:]
            _logs.extend(json.loads(l) for l in lines if l.strip())
        except Exception:
            pass


def add_log(entry):
    entry = dict(entry)
    entry["time"] = time.time()
    with _log_lock:
        _load_logs()
        _logs.append(entry)
        del _logs[:-MAX_LOGS]
        try:
            ensure_data_dir()
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
                write_private(LOG_FILE, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in _logs))
            else:
                fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


def get_logs(limit=200):
    with _log_lock:
        _load_logs()
        return list(reversed(_logs[-limit:]))


def clear_logs():
    with _log_lock:
        _logs.clear()
        try:
            LOG_FILE.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Upstream HTTP
# ---------------------------------------------------------------------------
def upstream_url(provider, fmt):
    base = provider["base_url"].rstrip("/")
    for suffix in ("/chat/completions", "/messages"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
    if fmt == "anthropic":
        return base + "/messages" if base.lower().endswith("/v1") else base + "/v1/messages"
    return base + "/chat/completions"


def upstream_headers(provider, key, incoming_headers=None):
    h = {"Content-Type": "application/json", "User-Agent": "API-Test-Console-Gateway/1.0"}
    if provider.get("format") == "anthropic":
        h["x-api-key"] = key
        h["anthropic-version"] = (incoming_headers or {}).get("anthropic-version") or "2023-06-01"
        beta = (incoming_headers or {}).get("anthropic-beta")
        if beta:
            h["anthropic-beta"] = beta
    else:
        h["Authorization"] = f"Bearer {key}"
    return h


def open_upstream(url, headers, body, timeout):
    """Returns (status, response_or_None, error_body_dict)."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp, None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            err = json.loads(raw)
        except Exception:
            err = {"error": {"message": raw[:300]}}
        retry_after = e.headers.get("Retry-After") if e.headers else None
        if retry_after:
            err["_retry_after"] = retry_after
        return e.code, None, err
    except Exception as e:
        return 0, None, {"error": {"message": f"Connection error: {getattr(e, 'reason', e)}"}}


def short_error(err):
    if not isinstance(err, dict):
        return str(err)[:200]
    e = err.get("error", err.get("message", err))
    if isinstance(e, dict):
        e = e.get("message") or e.get("type") or e
    return str(e)[:200]


def classify(status, err):
    """What a failure means for the key: which cooldown, and should we try another key?"""
    text = short_error(err).lower()
    if status in (401, 403) or "invalid api key" in text or "unauthorized" in text:
        return "invalid", True
    if status == 402 or any(t in text for t in ("insufficient", "balance", "credit", "quota exceeded", "billing")):
        return "no_credit", True
    if status == 429 or "rate limit" in text:
        return "limited", True
    if status == 0 or status >= 500:
        return "error", True
    if status == 404 or "model" in text and ("not found" in text or "does not exist" in text or "not available" in text):
        return "model_missing", True       # try another provider, key itself is fine
    return "client", False                  # 400/422: the request itself is wrong


# ---------------------------------------------------------------------------
# Routing + failover
# ---------------------------------------------------------------------------
def resolve_targets(model):
    """[(provider, upstream_model)] in priority order for a requested model name."""
    st = store()
    enabled = [p for p in st.providers() if p.get("enabled", True) and p.get("keys")]
    if "/" in model:
        prefix, rest = model.split("/", 1)
        for p in enabled:
            if p["name"].lower() == prefix.lower() or p["id"] == prefix:
                return [(p, rest)]
    targets = [(p, model) for p in enabled if model in provider_models(p)]
    if not targets:
        # A provider with no known model list may still serve it.
        targets = [(p, model) for p in enabled if not provider_models(p)]
    return targets


def ordered_keys(provider):
    now = time.time()
    keys = list(provider.get("keys", []))
    active = provider.get("active_key_id")
    keys.sort(key=lambda k: (k["id"] != active,))
    ready = [k for k in keys if k.get("cooldown_until", 0) <= now]
    cooling = sorted((k for k in keys if k.get("cooldown_until", 0) > now), key=lambda k: k["cooldown_until"])
    return ready + cooling


def mark_key(provider, key, ok, kind=None, error="", retry_after=None):
    st = store()
    with st.lock:
        key["last_used"] = time.time()
        if ok:
            key.update(status="ok", last_error="", cooldown_until=0)
            provider["active_key_id"] = key["id"]
        elif kind in COOLDOWN:
            wait = COOLDOWN[kind]
            try:
                if retry_after:
                    wait = max(1, min(int(float(retry_after)), 3600))
            except ValueError:
                pass
            key.update(status=kind, last_error=error, cooldown_until=time.time() + wait)
            if provider.get("active_key_id") == key["id"]:
                provider["active_key_id"] = None
        st.save()


# (provider id, model) pairs that rejected the client's max_tokens: we omit it for them.
NO_MAX_TOKENS = set()


class GatewayError(Exception):
    def __init__(self, status, message, kind="api_error"):
        super().__init__(message)
        self.status, self.message, self.kind = status, message, kind


def dispatch(client_fmt, body, incoming_headers, timeout=300):
    """
    Try every (provider, key) candidate until one answers 2xx.
    Returns (provider, key, upstream_fmt, upstream_model, response, attempts).
    Only failures that happen before any byte reaches the client are retried.
    """
    model = str(body.get("model") or "")
    if not model:
        raise GatewayError(400, "The request has no model.", "invalid_request_error")
    targets = resolve_targets(model)
    if not targets:
        available = ", ".join(store().all_models()[:30]) or "none yet: add a provider and test its keys"
        raise GatewayError(404, f"Model '{model}' is not served by any provider. Available: {available}",
                           "not_found_error")

    attempts = []
    last_status, last_err = 502, "No provider answered."
    for provider, upstream_model in targets:
        up_fmt = provider.get("format", "openai")
        if client_fmt == up_fmt:
            up_body = dict(body)
        elif client_fmt == "anthropic":
            up_body = anthropic_to_openai_request(body)
        else:
            up_body = openai_to_anthropic_request(body)
        up_body["model"] = upstream_model
        if up_fmt == "openai" and up_body.get("stream"):
            up_body.setdefault("stream_options", {"include_usage": True})

        url = upstream_url(provider, up_fmt)
        if (provider["id"], upstream_model) in NO_MAX_TOKENS:
            up_body.pop("max_tokens", None)
        for key in ordered_keys(provider):
            headers = upstream_headers(provider, key["key"], incoming_headers)
            status, resp, err = open_upstream(url, headers, up_body, timeout)
            if status == 400 and up_fmt == "openai" and "max_tokens" in short_error(err).lower() \
                    and "max_tokens" in up_body:
                # Agents like Claude Code ask for very large max_tokens that many
                # OpenAI-compatible models reject; let the provider pick its own limit
                # (and remember it, so later requests don't pay for the extra round trip).
                up_body = {k: v for k, v in up_body.items() if k != "max_tokens"}
                NO_MAX_TOKENS.add((provider["id"], upstream_model))
                status, resp, err = open_upstream(url, headers, up_body, timeout)
            if resp is not None and 200 <= status < 300:
                mark_key(provider, key, True)
                return provider, key, up_fmt, upstream_model, resp, attempts
            kind, try_next = classify(status, err)
            msg = short_error(err)
            attempts.append({"provider": provider["name"], "key": mask_key(key["key"]),
                             "status": status, "reason": kind})
            last_status, last_err = (status or 502), msg
            if kind == "model_missing":
                break                              # same model on this provider's other keys: pointless
            mark_key(provider, key, False, kind, msg, (err or {}).get("_retry_after"))
            if not try_next:
                raise GatewayError(status, msg, "invalid_request_error")
    raise GatewayError(last_status if last_status in (401, 402, 403, 404, 429) else 502,
                       f"All keys failed. Last error: {last_err}")


# ---------------------------------------------------------------------------
# Format translation: requests
# ---------------------------------------------------------------------------
def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def anthropic_to_openai_request(body):
    msgs = []
    system = body.get("system")
    if system:
        msgs.append({"role": "system", "content": _text_of(system) if not isinstance(system, str) else system})
    for m in body.get("messages", []):
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        parts, tool_calls, tool_results = [], [], []
        for b in content or []:
            t = b.get("type")
            if t == "text":
                parts.append({"type": "text", "text": b.get("text", "")})
            elif t == "image":
                src = b.get("source", {})
                url = src.get("url") if src.get("type") == "url" else \
                    f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            elif t == "tool_use":
                tool_calls.append({"id": b.get("id"), "type": "function",
                                   "function": {"name": b.get("name"),
                                                "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
            elif t == "tool_result":
                rc = b.get("content")
                text = rc if isinstance(rc, str) else _text_of(rc)
                if b.get("is_error"):
                    text = "[error] " + text
                tool_results.append({"role": "tool", "tool_call_id": b.get("tool_use_id"), "content": text})
            # thinking / redacted_thinking blocks are not sent upstream
        msgs.extend(tool_results)                    # tool answers must follow the assistant tool_calls
        if role == "assistant":
            msg = {"role": "assistant", "content": "".join(p["text"] for p in parts if p["type"] == "text") or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if msg["content"] is not None or tool_calls:
                msgs.append(msg)
        elif parts:
            only_text = all(p["type"] == "text" for p in parts)
            msgs.append({"role": role, "content": "".join(p["text"] for p in parts) if only_text else parts})

    out = {"model": body.get("model"), "messages": msgs}
    if body.get("max_tokens"):
        out["max_tokens"] = body["max_tokens"]
    for k in ("temperature", "top_p", "stream"):
        if k in body:
            out[k] = body[k]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("tools"):
        out["tools"] = [{"type": "function", "function": {
            "name": t.get("name"), "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in body["tools"] if t.get("name")]
        tc = body.get("tool_choice") or {}
        mapping = {"auto": "auto", "any": "required", "none": "none"}
        if tc.get("type") == "tool":
            out["tool_choice"] = {"type": "function", "function": {"name": tc.get("name")}}
        elif tc.get("type") in mapping:
            out["tool_choice"] = mapping[tc["type"]]
    return out


def openai_to_anthropic_request(body):
    system_parts, msgs = [], []

    def push(role, blocks):
        if msgs and msgs[-1]["role"] == role:        # Anthropic wants alternating roles
            msgs[-1]["content"].extend(blocks)
        else:
            msgs.append({"role": role, "content": list(blocks)})

    for m in body.get("messages", []):
        role, content = m.get("role"), m.get("content")
        if role in ("system", "developer"):
            system_parts.append(_text_of(content) if not isinstance(content, str) else content)
            continue
        if role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id"),
                           "content": content if isinstance(content, str) else _text_of(content)}])
            continue
        blocks = []
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        else:
            for p in content or []:
                if p.get("type") == "text":
                    blocks.append({"type": "text", "text": p.get("text", "")})
                elif p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url", "")
                    if url.startswith("data:") and ";base64," in url:
                        media, data = url[5:].split(";base64,", 1)
                        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
                    else:
                        blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {"_raw": fn.get("arguments")}
            blocks.append({"type": "tool_use", "id": tc.get("id") or new_id("toolu_"),
                           "name": fn.get("name"), "input": args})
        if blocks:
            push("assistant" if role == "assistant" else "user", blocks)

    out = {"model": body.get("model"), "messages": msgs,
           "max_tokens": body.get("max_completion_tokens") or body.get("max_tokens") or 4096}
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    for k in ("temperature", "top_p", "stream"):
        if k in body:
            out[k] = body[k]
    stop = body.get("stop")
    if stop:
        out["stop_sequences"] = [stop] if isinstance(stop, str) else stop
    if body.get("tools"):
        out["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                         "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                        for t in body["tools"] if t.get("type") == "function"]
        tc = body.get("tool_choice")
        if tc == "required":
            out["tool_choice"] = {"type": "any"}
        elif tc in ("auto", "none"):
            out["tool_choice"] = {"type": tc}
        elif isinstance(tc, dict) and tc.get("function"):
            out["tool_choice"] = {"type": "tool", "name": tc["function"].get("name")}
    return out


# ---------------------------------------------------------------------------
# Format translation: complete (non-stream) responses
# ---------------------------------------------------------------------------
STOP_OA_TO_AN = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                 "function_call": "tool_use", "content_filter": "end_turn"}
STOP_AN_TO_OA = {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls",
                 "stop_sequence": "stop", "pause_turn": "stop", "refusal": "content_filter"}


def openai_to_anthropic_response(res, model):
    choice = (res.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    text = msg.get("content")
    if isinstance(text, list):
        text = _text_of(text)
    if text:
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {"_raw": fn.get("arguments")}
        content.append({"type": "tool_use", "id": tc.get("id") or new_id("toolu_"),
                        "name": fn.get("name"), "input": args})
    usage = res.get("usage") or {}
    return {"id": res.get("id") or new_id("msg_"), "type": "message", "role": "assistant", "model": model,
            "content": content or [{"type": "text", "text": ""}],
            "stop_reason": STOP_OA_TO_AN.get(choice.get("finish_reason"), "end_turn"), "stop_sequence": None,
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0)}}


def anthropic_to_openai_response(res, model):
    text, tool_calls = "", []
    for b in res.get("content") or []:
        if b.get("type") == "text":
            text += b.get("text", "")
        elif b.get("type") == "tool_use":
            tool_calls.append({"id": b.get("id"), "type": "function",
                               "function": {"name": b.get("name"),
                                            "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
    msg = {"role": "assistant", "content": text or (None if tool_calls else "")}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    usage = res.get("usage") or {}
    inp, outp = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return {"id": res.get("id") or new_id("chatcmpl-"), "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": STOP_AN_TO_OA.get(res.get("stop_reason"), "stop")}],
            "usage": {"prompt_tokens": inp, "completion_tokens": outp, "total_tokens": inp + outp}}


# ---------------------------------------------------------------------------
# Format translation: streams (SSE in, SSE out)
# ---------------------------------------------------------------------------
def sse_events(resp):
    """Yields (event_name, data_string) from an upstream SSE response."""
    event = None
    data_lines = []
    for raw in resp:
        line = raw.decode("utf-8", "ignore").rstrip("\r\n")
        if not line:
            if data_lines:
                yield event, "\n".join(data_lines)
            event, data_lines = None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event, "\n".join(data_lines)


def sse(event, obj):
    data = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return (f"event: {event}\n" if event else "") + f"data: {data}\n\n"


def passthrough_stream(resp, fmt, usage):
    """Same format both sides: forward bytes, only peek at usage for the logs."""
    for event, data in sse_events(resp):
        try:
            obj = json.loads(data) if data != "[DONE]" else None
        except Exception:
            obj = None
        if obj:
            if fmt == "openai" and isinstance(obj.get("usage"), dict):
                usage["in"] = obj["usage"].get("prompt_tokens", usage.get("in"))
                usage["out"] = obj["usage"].get("completion_tokens", usage.get("out"))
            elif fmt == "anthropic":
                u = (obj.get("message") or {}).get("usage") or obj.get("usage") or {}
                if u.get("input_tokens") is not None:
                    usage["in"] = u["input_tokens"]
                if u.get("output_tokens") is not None:
                    usage["out"] = u["output_tokens"]
        yield sse(event, data)


def openai_stream_to_anthropic(resp, model, usage):
    msg_id = new_id("msg_")
    yield sse("message_start", {"type": "message_start", "message": {
        "id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
    block_index = -1
    open_block = None            # ("text", None) or ("tool", openai_tool_index)
    finish = None

    def close():
        nonlocal open_block
        if open_block is not None:
            open_block = None
            return sse("content_block_stop", {"type": "content_block_stop", "index": block_index})
        return ""

    for _, data in sse_events(resp):
        if data.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if isinstance(chunk.get("usage"), dict):
            usage["in"] = chunk["usage"].get("prompt_tokens", usage.get("in"))
            usage["out"] = chunk["usage"].get("completion_tokens", usage.get("out"))
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                if not open_block or open_block[0] != "text":
                    yield close()
                    block_index += 1
                    open_block = ("text", None)
                    yield sse("content_block_start", {"type": "content_block_start", "index": block_index,
                                                      "content_block": {"type": "text", "text": ""}})
                yield sse("content_block_delta", {"type": "content_block_delta", "index": block_index,
                                                  "delta": {"type": "text_delta", "text": text}})
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                if not open_block or open_block != ("tool", idx):
                    yield close()
                    block_index += 1
                    open_block = ("tool", idx)
                    yield sse("content_block_start", {"type": "content_block_start", "index": block_index,
                                                      "content_block": {"type": "tool_use",
                                                                        "id": tc.get("id") or new_id("toolu_"),
                                                                        "name": fn.get("name") or "", "input": {}}})
                if fn.get("arguments"):
                    yield sse("content_block_delta", {"type": "content_block_delta", "index": block_index,
                                                      "delta": {"type": "input_json_delta",
                                                                "partial_json": fn["arguments"]}})
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    yield close()
    yield sse("message_delta", {"type": "message_delta",
                                "delta": {"stop_reason": STOP_OA_TO_AN.get(finish, "end_turn"), "stop_sequence": None},
                                "usage": {"output_tokens": usage.get("out") or 0}})
    yield sse("message_stop", {"type": "message_stop"})


def anthropic_stream_to_openai(resp, model, usage):
    cid = new_id("chatcmpl-")
    created = int(time.time())

    def chunk(delta, finish=None):
        return sse(None, {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                          "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

    yield chunk({"role": "assistant", "content": ""})
    tool_index = -1
    block_is_tool = {}
    finish = "stop"
    for event, data in sse_events(resp):
        try:
            obj = json.loads(data)
        except Exception:
            continue
        t = obj.get("type") or event
        if t == "message_start":
            u = (obj.get("message") or {}).get("usage") or {}
            usage["in"] = u.get("input_tokens", usage.get("in"))
        elif t == "content_block_start":
            cb = obj.get("content_block") or {}
            if cb.get("type") == "tool_use":
                tool_index += 1
                block_is_tool[obj.get("index")] = tool_index
                yield chunk({"tool_calls": [{"index": tool_index, "id": cb.get("id"), "type": "function",
                                             "function": {"name": cb.get("name"), "arguments": ""}}]})
        elif t == "content_block_delta":
            d = obj.get("delta") or {}
            if d.get("type") == "text_delta":
                yield chunk({"content": d.get("text", "")})
            elif d.get("type") == "input_json_delta" and obj.get("index") in block_is_tool:
                yield chunk({"tool_calls": [{"index": block_is_tool[obj["index"]],
                                             "function": {"arguments": d.get("partial_json", "")}}]})
        elif t == "message_delta":
            finish = STOP_AN_TO_OA.get((obj.get("delta") or {}).get("stop_reason"), "stop")
            u = obj.get("usage") or {}
            if u.get("output_tokens") is not None:
                usage["out"] = u["output_tokens"]
        elif t == "error":
            usage["error"] = short_error(obj)
            break
    yield chunk({}, finish)
    inp, outp = usage.get("in") or 0, usage.get("out") or 0
    yield sse(None, {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [], "usage": {"prompt_tokens": inp, "completion_tokens": outp,
                                              "total_tokens": inp + outp}})
    yield sse(None, "[DONE]")


# ---------------------------------------------------------------------------
# HTTP entry point used by the dashboard's request handler
# ---------------------------------------------------------------------------
GATEWAY_PATHS = {"/v1/chat/completions": "openai", "/chat/completions": "openai",
                 "/v1/messages": "anthropic", "/messages": "anthropic"}


def is_gateway_path(path):
    p = path.split("?", 1)[0].rstrip("/")
    return p in GATEWAY_PATHS or p in ("/v1/models", "/models", "/v1/messages/count_tokens",
                                       "/messages/count_tokens")


def _authorized(handler):
    auth = handler.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    token = token or handler.headers.get("x-api-key", "").strip()
    return bool(token) and secrets.compare_digest(token, store().local_key)


def _send_json(handler, status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_error(handler, fmt, status, message, kind="api_error"):
    if fmt == "anthropic":
        _send_json(handler, status, {"type": "error", "error": {"type": kind, "message": message}})
    else:
        _send_json(handler, status, {"error": {"message": message, "type": kind, "code": status}})


def _client_name(handler):
    ua = handler.headers.get("User-Agent", "")
    for name, needle in (("Claude Code", "claude-cli"), ("Claude Code", "claude-code"), ("opencode", "opencode"),
                         ("Hermes", "hermes"), ("curl", "curl/"), ("Python", "python")):
        if needle in ua.lower():
            return name
    return ua.split("/")[0][:24] or "unknown"


def handle(handler, method):
    path = handler.path.split("?", 1)[0].rstrip("/")
    fmt = "anthropic" if "messages" in path or handler.headers.get("anthropic-version") else "openai"

    if not _authorized(handler):
        return _send_error(handler, fmt, 401, "Invalid local gateway key.", "authentication_error")

    if path in ("/v1/models", "/models"):
        now = int(time.time())
        data = []
        for p in store().providers():
            if not p.get("enabled", True):
                continue
            for m in provider_models(p):
                if not any(d["id"] == m for d in data):
                    data.append({"id": m, "object": "model", "type": "model", "created": now,
                                 "owned_by": p["name"], "display_name": m})
        return _send_json(handler, 200, {"object": "list", "data": data, "has_more": False,
                                         "first_id": data[0]["id"] if data else None,
                                         "last_id": data[-1]["id"] if data else None})

    if method != "POST":
        return _send_error(handler, fmt, 405, "Use POST.", "invalid_request_error")

    try:
        length = int(handler.headers.get("Content-Length") or 0)
        body = json.loads(handler.rfile.read(length).decode("utf-8")) if length else {}
    except Exception:
        return _send_error(handler, fmt, 400, "Invalid JSON body.", "invalid_request_error")

    if path.endswith("/count_tokens"):
        # Rough local estimate (~4 characters per token), enough for context bookkeeping.
        chars = len(json.dumps(body.get("messages", []), ensure_ascii=False)) + \
            len(json.dumps(body.get("system", ""), ensure_ascii=False)) + \
            len(json.dumps(body.get("tools", []), ensure_ascii=False))
        return _send_json(handler, 200, {"input_tokens": max(1, chars // 4)})

    client_fmt = GATEWAY_PATHS[path]
    model = str(body.get("model") or "")
    stream = bool(body.get("stream"))
    start = time.time()
    log = {"client": _client_name(handler), "format": client_fmt, "model": model, "stream": stream}
    incoming = {k.lower(): v for k, v in handler.headers.items()}

    try:
        provider, key, up_fmt, up_model, resp, attempts = dispatch(client_fmt, body, incoming)
    except GatewayError as e:
        add_log({**log, "status": "error", "code": e.status, "provider": "", "key": "",
                 "error": e.message, "ms": int((time.time() - start) * 1000)})
        return _send_error(handler, client_fmt, e.status, e.message, e.kind)

    log.update(provider=provider["name"], key=mask_key(key["key"]), upstream_format=up_fmt,
               failovers=len(attempts))
    usage = {}
    try:
        with resp:
            if not stream:
                raw = resp.read()
                try:
                    res = json.loads(raw.decode("utf-8"))
                except Exception:
                    raise GatewayError(502, "Upstream returned a non-JSON answer.")
                if up_fmt != client_fmt:
                    res = (openai_to_anthropic_response(res, model) if client_fmt == "anthropic"
                           else anthropic_to_openai_response(res, model))
                else:
                    res["model"] = model
                u = res.get("usage") or {}
                usage["in"] = u.get("prompt_tokens", u.get("input_tokens"))
                usage["out"] = u.get("completion_tokens", u.get("output_tokens"))
                _send_json(handler, 200, res)
            else:
                handler.send_response(200)
                handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
                handler.send_header("Cache-Control", "no-cache")
                handler.send_header("Connection", "close")
                handler.end_headers()
                if up_fmt == client_fmt:
                    gen = passthrough_stream(resp, up_fmt, usage)
                elif client_fmt == "anthropic":
                    gen = openai_stream_to_anthropic(resp, model, usage)
                else:
                    gen = anthropic_stream_to_openai(resp, model, usage)
                for piece in gen:
                    if piece:
                        handler.wfile.write(piece.encode("utf-8"))
                        handler.wfile.flush()
                handler.close_connection = True
        status = "error" if usage.get("error") else "ok"
        add_log({**log, "status": status, "code": 200, "error": usage.get("error", ""),
                 "tokens_in": usage.get("in"), "tokens_out": usage.get("out"),
                 "ms": int((time.time() - start) * 1000)})
    except (BrokenPipeError, ConnectionResetError):
        add_log({**log, "status": "error", "code": 499, "error": "Client closed the connection.",
                 "ms": int((time.time() - start) * 1000)})
    except GatewayError as e:
        add_log({**log, "status": "error", "code": e.status, "error": e.message,
                 "ms": int((time.time() - start) * 1000)})
        _send_error(handler, client_fmt, e.status, e.message)
    except Exception as e:
        add_log({**log, "status": "error", "code": 502, "error": f"Stream interrupted: {e}"[:200],
                 "ms": int((time.time() - start) * 1000)})


# ---------------------------------------------------------------------------
# Admin operations (called from the dashboard's /api/* routes)
# ---------------------------------------------------------------------------
def save_provider(data):
    st = store()
    name = (data.get("name") or "").strip()
    base_url = (data.get("base_url") or "").strip().rstrip("/")
    if not name or not base_url.lower().startswith(("http://", "https://")):
        raise ValueError(i18n.t("err.provider_required"))
    if "/" in name:
        raise ValueError(i18n.t("err.provider_slash"))
    fmt = data.get("format") if data.get("format") in ("openai", "anthropic") else "openai"
    manual = [m.strip() for m in str(data.get("manual_models") or "").split(",") if m.strip()]
    with st.lock:
        dup = next((p for p in st.providers() if p["name"].lower() == name.lower() and p["id"] != data.get("id")), None)
        if dup:
            raise ValueError(i18n.t("err.provider_exists", name=name))
        p = st.provider(data.get("id")) if data.get("id") else None
        if p is None:
            p = {"id": new_id("prov_"), "keys": [], "models": [], "enabled": True}
            st.providers().append(p)
        if p.get("base_url") and p["base_url"] != base_url:
            p["models"] = []                       # different endpoint: old model list no longer applies
        p.update(name=name, base_url=base_url, format=fmt, manual_models=manual)
        if "enabled" in data:
            p["enabled"] = bool(data["enabled"])
        st.save()
        return p["id"]


def delete_provider(pid):
    st = store()
    with st.lock:
        st.data["providers"] = [p for p in st.providers() if p["id"] != pid]
        st.save()


def _split_keys(text):
    out = []
    for line in str(text or "").replace(",", "\n").split():
        key = line.strip()
        if key and key not in out:
            out.append(key)
    return out


def check_keys(text):
    """Which of the pasted keys already exist, and in which provider (masked)."""
    st = store()
    with st.lock:
        where = {k["key"]: p["name"] for p in st.providers() for k in p.get("keys", [])}
    keys = _split_keys(text)
    existing = [{"masked": mask_key(k), "provider": where[k]} for k in keys if k in where]
    return {"new": [mask_key(k) for k in keys if k not in where], "existing": existing}


def add_keys(pid, text, label=""):
    """Adds only keys that don't exist anywhere yet; reports the duplicates it skipped."""
    st = store()
    with st.lock:
        p = st.provider(pid)
        if not p:
            raise ValueError(i18n.t("err.unknown_provider"))
        report = check_keys(text)
        where = {k["key"] for q in st.providers() for k in q.get("keys", [])}
        added = 0
        for key in _split_keys(text):
            if key not in where:
                p["keys"].append({"id": new_id("key_"), "key": key, "label": label, "status": "unknown"})
                where.add(key)
                added += 1
        st.save()
        return {"added": added, "duplicates": report["existing"]}


def delete_key(pid, kid):
    st = store()
    with st.lock:
        p = st.provider(pid)
        if p:
            p["keys"] = [k for k in p["keys"] if k["id"] != kid]
            if p.get("active_key_id") == kid:
                p["active_key_id"] = None
            st.save()


def regenerate_local_key():
    st = store()
    with st.lock:
        st.data["local_key"] = new_local_key()
        st.save()
        return st.data["local_key"]


def resolve_key_ref(ref):
    """Server-side lookup so the tester can use a stored key without the browser ever seeing it."""
    st = store()
    p = st.provider((ref or {}).get("provider"))
    if not p:
        return None, None
    k = next((k for k in p["keys"] if k["id"] == ref.get("key")), None)
    return (p["base_url"], k["key"]) if k else (None, None)


def _test_one_key(p, k, tester):
    """Reuses the dashboard's own tester functions (fetch_models_list / test_model)."""
    fmt = p.get("format", "openai")
    timeout = 20
    if fmt == "openai":
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "Authorization": f"Bearer {k['key']}", "User-Agent": "API-Test-Console/1.1"}
        models, err = tester["fetch_models_list"](p["base_url"], headers, timeout, 0)
        test_models = list(p.get("manual_models") or []) + [m for m in models if m not in (p.get("manual_models") or [])]
        if not test_models:
            kind, _ = classify(0, {"error": {"message": err or ""}})
            return {"status": kind if kind in COOLDOWN else "error", "models": [],
                    "error": err or i18n.t("err.no_model_list")}
        base = p["base_url"].rstrip("/")
        r = tester["test_model"](test_models[0], base + "/chat/completions" if not base.endswith("/chat/completions")
                                 else base, headers, timeout, 0, "ping", 1, False)
        if r["status"] == "WORKING":
            return {"status": "ok", "models": models, "error": "", "tested_model": test_models[0],
                    "time": r["response_time_avg"]}
        kind = {"KEY_INVALID": "invalid", "LIMITED": "limited"}.get(r["status"], "error")
        if r["status"] == "MODEL_UNAVAILABLE" and models:
            kind = "ok"                            # key is accepted; that one model just isn't served
        return {"status": kind, "models": models, "error": r.get("error", "") if kind != "ok" else "",
                "tested_model": test_models[0]}
    # Anthropic-format provider: list models + one tiny message.
    headers = upstream_headers(p, k["key"])
    base = p["base_url"].rstrip("/")
    models_url = (base + "/models") if base.endswith("/v1") else (base + "/v1/models")
    status, res = tester["make_request"](models_url, headers, None, timeout)
    models = tester["extract_models_from_response"](res) if status == 200 else []
    test_models = list(p.get("manual_models") or []) + models
    if not test_models:
        kind = "invalid" if status in (401, 403) else "error"
        return {"status": kind, "models": [], "error": short_error(res) or i18n.t("err.no_model_list")}
    status, res = tester["make_request"](upstream_url(p, "anthropic"), headers,
                                         {"model": test_models[0], "max_tokens": 8,
                                          "messages": [{"role": "user", "content": "ping"}]}, timeout)
    if status == 200:
        return {"status": "ok", "models": models, "error": "", "tested_model": test_models[0]}
    kind, _ = classify(status, res)
    kind = kind if kind in COOLDOWN else "error"
    return {"status": kind, "models": models, "error": short_error(res), "tested_model": test_models[0]}


def test_provider_keys(pid, tester):
    st = store()
    p = st.provider(pid)
    if not p:
        raise ValueError(i18n.t("err.unknown_provider"))
    keys = list(p["keys"])
    if not keys:
        raise ValueError(i18n.t("err.no_keys"))
    with ThreadPoolExecutor(max_workers=min(8, len(keys))) as ex:
        results = list(ex.map(lambda k: _test_one_key(p, k, tester), keys))
    with st.lock:
        found = []
        for k, r in zip(keys, results):
            k["status"], k["last_error"], k["last_checked"] = r["status"], r.get("error", ""), time.time()
            k["cooldown_until"] = 0 if r["status"] == "ok" else time.time() + COOLDOWN.get(r["status"], 20)
            for m in r.get("models", []):
                if m not in found:
                    found.append(m)
        if found:
            p["models"] = found
        st.save()
    return [{"key": mask_key(k["key"]), **{x: r.get(x) for x in ("status", "error", "tested_model", "time")}}
            for k, r in zip(keys, results)]
