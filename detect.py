#!/usr/bin/env python3
"""
Key / endpoint detection helpers shared by the tester (api_web_dashboard_v2.py) and the gateway.
Standard library only, no network calls except host_problem() (DNS + TCP connect).

  normalize_base_url(raw)        "api.x.com/v1/v1/" -> "https://api.x.com/v1" (ValueError when it can't be a URL)
  format_hint(base, key)         ("anthropic" | "openai" | None, reason) from the URL and the key prefix
  key_problem(base, key)         (key_status, message) for placeholder / malformed / wrong-provider keys, else None
  host_problem(base, timeout)    message when the host can't be resolved or reached, else None
  headers(fmt, key)              the right auth headers for "openai" (Bearer) or "anthropic" (x-api-key + version)
  same_model(asked, served)      does the response's "model" name the model that was asked for?
  genuine(fmt, ...)              {"badge": verified|suspicious|unknown, "reasons": [...], "served": name}
"""
import ipaddress
import re
import socket
import urllib.request
from urllib.parse import urlsplit

import i18n

ANTHROPIC_VERSION = "2023-06-01"
UA = "API-Test-Console/1.2"

# Endpoint paths people paste by mistake; the base URL is what's left.
_ENDPOINT_SUFFIXES = ("/chat/completions", "/completions", "/responses", "/messages/count_tokens",
                      "/messages", "/models", "/embeddings")

# Official hosts with a well-known key format: (name, host suffix, key regex, format).
KNOWN = (
    ("Anthropic", "anthropic.com", r"sk-ant-", "anthropic"),
    ("OpenRouter", "openrouter.ai", r"sk-or-", "openai"),
    ("Groq", "groq.com", r"gsk_", "openai"),
    ("xAI", "x.ai", r"xai-", "openai"),
    ("Google AI", "generativelanguage.googleapis.com", r"AIza", "openai"),
    # Many providers hand out plain "sk-" keys, so only OpenAI's project/service keys identify OpenAI.
    ("OpenAI", "openai.com", r"sk-(proj|svcacct|admin)-", "openai"),
)

_PLACEHOLDER = re.compile(
    r"your[-_ ]?(api[-_ ]?)?key|api[-_]?key[-_]?here|<[^>]*>|\{\{.*\}\}|x{4,}|\*{3,}|•|\.\.\.|…"
    r"|^(sk-)?(test|demo|example|placeholder|changeme|dummy|fake|null|none|undefined|token|key|secret)$|insert|replace[-_ ]?me",
    re.I)


def _is_local(host):
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".lan", ".internal", ".home.arpa")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified
    except ValueError:
        return "." not in host               # a bare machine name (with a port) on the LAN


def normalize_base_url(raw):
    """Generic for every provider: add the scheme, drop spaces, quotes, endpoint paths, doubled /v1 and trailing slashes."""
    s = re.sub(r"\s+", "", str(raw or "")).strip("\"'<>`")
    if not s:
        return ""
    m = re.match(r"^([a-z][a-z0-9+.-]*):/*", s, re.I)
    if m and m.group(1).lower() in ("http", "https"):
        scheme, rest = m.group(1).lower(), s[m.end():]
    elif m and "://" in s[:m.end() + 1]:
        raise ValueError(i18n.t("url.bad_scheme", scheme=m.group(1)))
    else:
        scheme, rest = None, s.lstrip("/")
    try:
        u = urlsplit("//" + rest)
        host, port = (u.hostname or ""), u.port
    except ValueError:
        raise ValueError(i18n.t("url.bad_host"))
    if not host or not re.fullmatch(r"[a-z0-9.\-_]+|[0-9a-f:]+", host) or host.startswith(".") or ".." in host:
        raise ValueError(i18n.t("url.bad_host"))
    if not (host == "localhost" or "." in host or ":" in host or port):
        raise ValueError(i18n.t("url.bad_host"))
    if scheme is None:
        scheme = "http" if port == 80 or (port != 443 and _is_local(host)) else "https"
    netloc = (f"[{host}]" if ":" in host else host) + (f":{port}" if port else "")
    path = re.sub(r"/{2,}", "/", u.path).rstrip("/")
    changed = True
    while changed and path:
        changed = False
        for suffix in _ENDPOINT_SUFFIXES:
            if path.lower().endswith(suffix):
                path, changed = path[:-len(suffix)].rstrip("/"), True
    path = re.sub(r"(/v\d+(?:alpha|beta)?\d*)(\1)+(?=/|$)", r"\1", path, flags=re.I)
    return f"{scheme}://{netloc}{path}"


def host_of(base):
    try:
        return (urlsplit(base).hostname or "").lower()
    except ValueError:
        return ""


def _known_for_host(host):
    return next((k for k in KNOWN if host == k[1] or host.endswith("." + k[1])), None)


def _known_for_key(key):
    return next((k for k in KNOWN if re.match(k[2], key)), None)


def known_key_name(key):
    k = _known_for_key(key or "")
    return k[0] if k else None


def format_hint(base, key):
    """Best guess before any request: which API style this URL + key speak."""
    host, path = host_of(base), urlsplit(base).path.lower() if base else ""
    by_host = _known_for_host(host)
    if by_host:
        return by_host[3], i18n.t("detect.by_host", name=by_host[0])
    if re.search(r"/anthropic(/|$)", path) or "claude" in host:
        return "anthropic", i18n.t("detect.by_path")
    if (key or "").startswith("sk-ant-"):
        return "anthropic", i18n.t("detect.by_key", name="Anthropic")
    return None, ""


def key_problem(base, key):
    """Fast checks that need no request: (key_status, message) or None."""
    key = key or ""
    if not key.strip():
        return "INVALID", i18n.t("key.empty")
    if re.search(r"\s", key.strip()) or any(ord(c) > 126 or ord(c) < 33 for c in key.strip()):
        return "INVALID", i18n.t("key.bad_chars")
    key = key.strip()
    if _is_local(host_of(base)):
        return None                          # Ollama, LM Studio, vLLM...: any dummy key is fine there
    if _PLACEHOLDER.search(key) or len(set(key.lower().replace("-", "").replace("_", ""))) <= 2:
        return "INVALID", i18n.t("key.placeholder")
    if re.match(r"https?://", key, re.I):
        return "INVALID", i18n.t("key.is_url")
    if len(key) < 12:
        return "INVALID", i18n.t("key.too_short", n=len(key))
    host_known = _known_for_host(host_of(base))
    key_known = _known_for_key(key)
    if host_known and key_known and host_known is not key_known:
        return "WRONG_ENDPOINT", i18n.t("key.other_provider", key_name=key_known[0], host_name=host_known[0])
    if host_known and not key_known and host_known[0] != "OpenAI":
        return "INVALID", i18n.t("key.bad_format", name=host_known[0], prefix=host_known[2].split("(")[0].replace("\\", ""))
    if host_known and host_known[0] == "OpenAI" and not key.startswith("sk-"):
        return "INVALID", i18n.t("key.bad_format", name="OpenAI", prefix="sk-")
    return None


def _proxied(scheme, host):
    proxy = urllib.request.getproxies().get(scheme)
    return bool(proxy) and not urllib.request.proxy_bypass(host)


def host_problem(base, timeout=5):
    """DNS + TCP connect only (no request). Skipped behind a proxy, which does its own resolving."""
    try:
        u = urlsplit(base)
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return i18n.t("url.bad_host")
    if not host or _proxied(u.scheme, host):
        return None
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return i18n.t("host.not_found", host=host)
    except (OSError, UnicodeError):
        return i18n.t("host.not_found", host=host)
    last = None
    for family, kind, proto, _, addr in infos[:3]:
        try:
            with socket.socket(family, kind, proto) as s:
                s.settimeout(min(timeout, 5))
                s.connect(addr)
                return None
        except ConnectionRefusedError:
            last = i18n.t("host.refused", host=host, port=port)
        except socket.timeout:
            last = i18n.t("host.timeout", host=host, port=port)
        except OSError as e:
            last = i18n.t("host.unreachable", host=host, error=e.strerror or e)
    return last


def headers(fmt, key):
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA}
    if fmt == "anthropic":
        h["x-api-key"] = key
        h["anthropic-version"] = ANTHROPIC_VERSION
    else:
        h["Authorization"] = f"Bearer {key}"
    return h


def anthropic_root(base):
    """Anthropic paths always carry /v1; accept a base with or without it."""
    return re.sub(r"/v1$", "", base.rstrip("/"), flags=re.I)


def anthropic_urls(base):
    root = anthropic_root(base)
    return root + "/v1/models", root + "/v1/messages"


def looks_anthropic_list(res):
    """Anthropic's /v1/models: {"data": [{"type": "model", "id", "display_name"}], "has_more", "first_id"}."""
    if not isinstance(res, dict) or not isinstance(res.get("data"), list):
        return False
    first = res["data"][0] if res["data"] else {}
    return "has_more" in res or (isinstance(first, dict) and first.get("type") == "model" and "display_name" in first)


def mentions_anthropic_auth(text):
    return bool(re.search(r"x-api-key|anthropic-version", str(text or ""), re.I))


# ---- choosing a cheap model for the key probe --------------------------------------------
_NOT_CHAT = re.compile(r"embed|tts|whisper|dall-?e|image|audio|realtime|moderation|rerank|transcri|"
                       r"speech|search|computer-use|vision-preview|-ocr|guard|clip|sora|veo", re.I)
_CHEAP = ((re.compile(r"nano|flash-lite|-lite|tiny|\b[1-4]b\b|[-_:]([1-9]|1[0-4])b\b"), 0),
          (re.compile(r"haiku|mini|small|flash|8b|7b|instant|turbo|air"), 1),
          (re.compile(r"opus|-pro\b|gpt-4\.5|o1-pro|o3-pro|deep-research|405b|ultra|max\b"), 5),
          (re.compile(r"reason|think|-r1\b|\bo[134]\b|o[134]-"), 4))


def is_chat_model(name):
    return not _NOT_CHAT.search(name or "")


def cheap_order(models):
    """Chat models first, the ones that are usually cheapest/fastest before the big ones."""
    def score(m):
        low = m.lower()
        s = next((v for rx, v in _CHEAP if rx.search(low)), 2)
        return (0 if is_chat_model(m) else 1, s)
    return sorted(models, key=score)


# ---- is the model genuine? ---------------------------------------------------------------
_ALLOWED_TAIL = re.compile(r"^[-:](\d{3,}|preview|exp(erimental)?|latest|v\d+([.-]\d+)*|hf|fp8|fp16|bf16|awq|gguf|int[48]|instruct)$")


def _norm_model(m):
    m = str(m or "").strip().lower().split("@")[0].rsplit("/", 1)[-1]
    m = re.sub(r":(free|beta|extended|thinking|nitro|floor|online)$", "", m)
    m = m.replace("_", "-")
    prev = None
    while prev != m:
        prev = m
        m = re.sub(r"[-.](latest|\d{8}|\d{4}-\d{2}-\d{2}|\d{4}|\d{3})$", "", m)
    return re.sub(r"(?<=\d)\.(?=\d)", "-", m)


def same_model(asked, served):
    a, b = _norm_model(asked), _norm_model(served)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return long_.startswith(short) and bool(_ALLOWED_TAIL.match(long_[len(short):]))


_ROUTER = re.compile(r"(^|/)(auto|router|default|best|any)$|openrouter/auto", re.I)
_STOP_REASONS = {"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal", "model_context_window_exceeded"}


def genuine(fmt, asked, res, resp_headers, base):
    """Consistency checks on one successful reply. Never proof: the badge says what the checks saw."""
    h = {k.lower(): v for k, v in (resp_headers or {}).items()}
    res = res if isinstance(res, dict) else {}
    served = res.get("model") if isinstance(res.get("model"), str) else ""
    bad, unsure = [], []
    if not served:
        unsure.append(i18n.t("gen.no_model_field"))
    elif not same_model(asked, served):
        (unsure if _ROUTER.search(asked) else bad).append(i18n.t("gen.model_mismatch", asked=asked, served=served))

    claims_claude = "claude" in str(asked).lower()
    official = host_of(base).endswith("anthropic.com")
    if fmt == "anthropic":
        usage = res.get("usage") if isinstance(res.get("usage"), dict) else {}
        if res.get("type") != "message" or res.get("role") != "assistant" or not isinstance(res.get("content"), list):
            bad.append(i18n.t("gen.not_message"))
        if res.get("stop_reason") not in _STOP_REASONS:
            (bad if claims_claude else unsure).append(i18n.t("gen.stop_reason", value=res.get("stop_reason")))
        if not (isinstance(usage.get("input_tokens"), int) and isinstance(usage.get("output_tokens"), int)):
            (bad if claims_claude else unsure).append(i18n.t("gen.no_usage"))
        if claims_claude:
            if not str(res.get("id") or "").startswith("msg_"):
                bad.append(i18n.t("gen.msg_id", value=res.get("id") or "—"))
            has_headers = str(h.get("request-id", "")).startswith("req_") or any(k.startswith("anthropic-") for k in h)
            if not has_headers:
                (bad if official else unsure).append(i18n.t("gen.no_anthropic_headers"))
    else:
        if not isinstance(res.get("usage"), dict):
            unsure.append(i18n.t("gen.no_usage"))
        if res.get("object") not in (None, "chat.completion"):
            unsure.append(i18n.t("gen.object", value=res.get("object")))
    if bad:
        return {"badge": "suspicious", "reasons": bad + unsure, "served": served}
    if unsure:
        return {"badge": "unknown", "reasons": unsure, "served": served}
    return {"badge": "verified", "reasons": [i18n.t("gen.consistent")], "served": served}
