#!/usr/bin/env python3
"""
API Testing & OpenCode Config Generator — Web Edition
---------------------------------------------------------------------
هاد النسخة كتحول السكريبت الأصلي (الطلبات، parallel testing، retry،
capabilities، إلخ) لسيرفر محلي صغير:

  - منطق الاختبار (make_request / retry / test_model / probes) هو
    بالضبط نفسه لي كان فالسكريبت الأصلي — ما تبدلش، غير تلبس فوقو
    طبقة سيرفر.
  - الإدخال (Base URL, API Key, ...) كيدخل من صفحة الويب، ماشي من
    الطرفية (input()).
  - السيرفر كيدير الاختبارات الحقيقية، وكيبعث كل نتيجة لصفحة الويب
    بشكل حي (streaming NDJSON) بمجرد ما توصل — ماشي كتسنى الكل يسالي.
  - غير Python standard library، حتى pip install واحد.

طريقة التشغيل:
    python3 api_web_dashboard.py --port 8000
    وبعدها حل المتصفح على: http://localhost:8000
"""

import html
import json
import re
import sys
import time
import argparse
import statistics
import webbrowser
import urllib.request
import urllib.error
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gateway  # noqa: E402  (local AI gateway: providers, local key, /v1 endpoints)
import i18n     # noqa: E402  (translations: i18n/<lang>.json + the saved UI language)
import agents   # noqa: E402  (Claude Code / opencode / pi / Hermes config switching)
import alerts   # noqa: E402  (Telegram alerts from gateway events)
import phone    # noqa: E402  (phone access over the home Wi-Fi, token-protected)
import qr       # noqa: E402  (QR code for the phone link)
from http.cookies import SimpleCookie  # noqa: E402


class StopStreaming(Exception):
    """Raised when the browser tab/connection closed mid-stream."""
    pass


# ===========================================================================
# 1) نفس منطق الاختبار الأصلي (ماشي مبدل) — request / retry / probes
# ===========================================================================
def make_request(url, headers, payload=None, timeout=15):
    data = json.dumps(payload).encode('utf-8') if payload else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                  method='POST' if payload else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"error": {"message": body}}
    except urllib.error.URLError as e:
        return 0, {"error": {"message": f"Connection error: {e.reason}"}}
    except Exception as e:
        return 0, {"error": {"message": str(e)}}


def is_timeout(status, res):
    return status == 0 and "timed out" in error_message(res).lower()


def timed_request_with_retry(url, headers, payload=None, timeout=15,
                             max_retries=2, backoff_base=1.5, retry_timeouts=True):
    """
    Same retry policy as before, but also returns the duration of the LAST
    attempt only, so backoff sleeps and failed attempts don't inflate the
    measured response time.
    """
    status, res, elapsed = 0, {"error": {"message": "Unknown"}}, 0.0
    for attempt in range(max_retries + 1):
        start = time.time()
        status, res = make_request(url, headers, payload, timeout)
        elapsed = time.time() - start
        transient = status == 0 or status == 429 or status >= 500
        # A timeout already cost a full `timeout`; retrying it multiplies the wait.
        if transient and not retry_timeouts and is_timeout(status, res):
            transient = False
        if not transient or attempt == max_retries:
            return status, res, elapsed
        time.sleep(backoff_base ** attempt)
    return status, res, elapsed


def make_request_with_retry(url, headers, payload=None, timeout=15,
                             max_retries=2, backoff_base=1.5, retry_timeouts=True):
    status, res, _ = timed_request_with_retry(url, headers, payload, timeout,
                                              max_retries, backoff_base, retry_timeouts)
    return status, res


def probe_streaming(model, chat_url, headers, timeout):
    """
    Checks streaming support and, when it works, measures real generation
    speed: time to first token (ttft) and tokens/s counted from the first
    token to the end of the stream (so network latency doesn't skew it).
    """
    payload = {"model": model,
               "messages": [{"role": "user", "content": "Count from 1 to 40 separated by spaces."}],
               "max_tokens": 80, "stream": True,
               "stream_options": {"include_usage": True}}
    out = {"supported": False, "ttft": None, "tokens_per_sec": None}
    req = urllib.request.Request(chat_url, data=json.dumps(payload).encode(),
                                  headers=headers, method="POST")
    start = time.time()
    first_at = last_at = None
    chunks, usage_tokens = 0, None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "ignore").strip()
                if not line.startswith("data"):
                    continue
                out["supported"] = True
                data = line.split(":", 1)[1].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if isinstance(obj.get("usage"), dict) and obj["usage"].get("completion_tokens"):
                    usage_tokens = obj["usage"]["completion_tokens"]
                choices = obj.get("choices") or []
                delta = choices[0].get("delta") if choices and isinstance(choices[0], dict) else None
                if isinstance(delta, dict) and (delta.get("content") or delta.get("reasoning_content")):
                    now = time.time()
                    if first_at is None:
                        first_at = now
                    last_at = now
                    chunks += 1
    except Exception:
        # A stream that started but broke mid-way still proves support.
        pass

    if first_at is not None:
        out["ttft"] = round(first_at - start, 3)
        generated = usage_tokens or chunks
        span = (last_at or first_at) - first_at
        # Need a few tokens over a measurable span, otherwise the number is noise.
        if generated >= 5 and span > 0.05:
            out["tokens_per_sec"] = round((generated - 1) / span, 1)
    return out


def probe_tool_calling(model, chat_url, headers, timeout):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "What is the weather in Paris?"}],
        "max_tokens": 30,
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a city",
                "parameters": {"type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"]},
            },
        }],
    }
    status, res = make_request(chat_url, headers, payload, timeout)
    if status != 200:
        return False
    try:
        return bool(res["choices"][0]["message"].get("tool_calls"))
    except Exception:
        return False


def probe_json_mode(model, chat_url, headers, timeout):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": 'Return {"ok": true} as JSON.'}],
        "max_tokens": 40,
        "response_format": {"type": "json_object"},
    }
    status, res = make_request(chat_url, headers, payload, timeout)
    if status != 200:
        return False
    # Many gateways silently ignore response_format and still answer 200,
    # so only count it when the reply actually parses as a JSON object.
    try:
        content = res["choices"][0]["message"]["content"]
    except Exception:
        return False
    if isinstance(content, list):
        content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        return False
    text = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        return isinstance(json.loads(text), dict)
    except Exception:
        return False


def error_message(res):
    """Get a provider error without assuming one particular JSON format."""
    if not isinstance(res, dict):
        return str(res or "Unknown error")
    err = res.get("error", res.get("message", "Unknown error"))
    if isinstance(err, dict):
        return str(err.get("message") or err.get("detail") or err.get("code") or err)
    return str(err)


def diagnose_failure(code, res):
    """Classify failures so a temporary provider issue is not called a bad model."""
    raw = error_message(res)
    text = raw.lower()
    if code in (401, 403) or any(term in text for term in ("invalid api key", "invalid key", "unauthorized", "authentication", "forbidden")):
        return "KEY_INVALID", i18n.t("srv.diag.KEY_INVALID"), raw
    if code == 429 or any(term in text for term in ("rate limit", "too many requests", "quota", "insufficient balance", "credit")):
        return "LIMITED", i18n.t("srv.diag.LIMITED"), raw
    if code == 0:
        if "timed out" in text or "timeout" in text:
            return "UNAVAILABLE", i18n.t("srv.diag.TIMEOUT"), raw
        return "UNAVAILABLE", i18n.t("srv.diag.UNREACHABLE"), raw
    if code >= 500:
        return "UNAVAILABLE", i18n.t("srv.diag.SERVER"), raw
    if code == 404 or any(term in text for term in ("model not found", "unknown model", "does not exist", "not available")):
        return "MODEL_UNAVAILABLE", i18n.t("srv.diag.MODEL_UNAVAILABLE"), raw
    if code == 400:
        return "INCOMPATIBLE", i18n.t("srv.diag.INCOMPATIBLE"), raw
    if code == 200:
        return "INVALID_RESPONSE", i18n.t("srv.diag.INVALID_RESPONSE"), raw
    return "FAILED", i18n.t("srv.diag.FAILED", code=code), raw


def response_has_text(choice):
    """Support text and multipart content without treating a valid reply as a crash."""
    if not isinstance(choice, dict):
        return False
    message = choice.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(part, dict) and str(part.get("text") or part.get("content") or "").strip()
                   for part in content)
    return bool(message.get("tool_calls"))


def test_model(model, chat_url, headers, timeout, max_retries, test_prompt,
                repeat, check_capabilities):
    payload = {"model": model, "messages": [{"role": "user", "content": test_prompt}],
               "max_tokens": 16}
    times, quality_ok = [], True
    last_res, last_code = None, 0

    for _ in range(max(1, repeat)):
        # Only the successful attempt is timed (no retry sleeps included).
        code, res, elapsed = timed_request_with_retry(chat_url, headers, payload,
                                                      timeout=timeout, max_retries=max_retries)
        # Some newer OpenAI-compatible gateways only accept this token field.
        if code == 400:
            alternate_payload = dict(payload)
            alternate_payload.pop("max_tokens", None)
            alternate_payload["max_completion_tokens"] = 16
            code, res, elapsed = timed_request_with_retry(chat_url, headers, alternate_payload,
                                                          timeout=timeout, max_retries=max_retries)
        last_res, last_code = res, code

        if code == 200 and isinstance(res, dict) and isinstance(res.get("choices"), list) and res["choices"]:
            times.append(elapsed)
            quality_ok = quality_ok and response_has_text(res["choices"][0])
        else:
            break

    if not times:
        status, message, technical_error = diagnose_failure(last_code, last_res)
        return {"model": model, "status": status, "code": last_code,
                "error": message, "technical_error": technical_error}

    avg_time = round(statistics.mean(times), 3)
    result = {
        "model": model, "status": "WORKING", "code": 200,
        "response_time_avg": avg_time,
        "response_time_min": round(min(times), 3),
        "response_time_max": round(max(times), 3),
        "runs": len(times), "quality_ok": quality_ok,
        # Real generation speed needs a streamed answer; it is measured by the
        # streaming probe when "capabilities" is enabled, otherwise unknown.
        "tokens_per_sec": None, "ttft": None,
    }
    if check_capabilities:
        # The three probes are independent: run them together instead of one by one.
        with ThreadPoolExecutor(max_workers=3) as probes:
            f_stream = probes.submit(probe_streaming, model, chat_url, headers, timeout)
            f_tools = probes.submit(probe_tool_calling, model, chat_url, headers, timeout)
            f_json = probes.submit(probe_json_mode, model, chat_url, headers, timeout)
            stream = f_stream.result()
            result["capabilities"] = {
                "streaming": stream["supported"],
                "tool_calling": f_tools.result(),
                "json_mode": f_json.result(),
            }
        result["tokens_per_sec"] = stream["tokens_per_sec"]
        result["ttft"] = stream["ttft"]
    return result


def build_opencode_config(provider_id, base_url, working_sorted):
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            provider_id: {
                "npm": "@ai-sdk/openai-compatible",
                "name": provider_id.capitalize(),
                "options": {"baseURL": base_url},
                "models": {m: {"name": m} for m in working_sorted},
            }
        },
        "model": f"{provider_id}/{working_sorted[0]}" if working_sorted else "",
    }


def extract_models_from_response(res):
    """Extract model IDs from common OpenAI-compatible response shapes."""
    def read_list(items):
        if not isinstance(items, list):
            return []
        found = []
        for item in items:
            if isinstance(item, str) and item.strip():
                found.append(item.strip())
            elif isinstance(item, dict):
                # ``id`` is the OpenAI field. Other gateways commonly use one
                # of these alternatives when returning a models catalogue.
                for field in ("id", "model", "model_id", "name"):
                    value = item.get(field)
                    if isinstance(value, str) and value.strip():
                        found.append(value.strip())
                        break
        return found

    if isinstance(res, list):
        return list(dict.fromkeys(read_list(res)))
    if not isinstance(res, dict):
        return []

    # Providers use different container names, and some nest them once.
    for key in ("data", "models", "results", "items"):
        value = res.get(key)
        models = read_list(value)
        if models:
            return list(dict.fromkeys(models))
        if isinstance(value, dict):
            for nested_key in ("data", "models", "results", "items"):
                models = read_list(value.get(nested_key))
                if models:
                    return list(dict.fromkeys(models))
    return []


def model_endpoint_candidates(base_url):
    """Return the usual model-list endpoints without duplicate requests."""
    base = base_url.rstrip('/')
    # A frequent configuration mistake is pasting the complete chat endpoint.
    for suffix in ("/chat/completions", "/responses", "/models"):
        if base.lower().endswith(suffix):
            base = base[:-len(suffix)].rstrip('/')
            break

    candidates = [f"{base}/models"]
    if base.lower().endswith("/v1"):
        candidates.append(f"{base[:-3].rstrip('/')}/models")
    else:
        candidates.append(f"{base}/v1/models")
    return list(dict.fromkeys(candidates))


def fetch_models_list(base_url, headers, timeout, retries, emit=None):
    """
    يحاول روابط القائمة الشائعة لدى موفري OpenAI-compatible، ويشرح
    الرابط وحالة كل محاولة عند الفشل.
    كترجع (models_list, error_message_or_None).
    """
    candidates = model_endpoint_candidates(base_url)

    errors = []
    for url in candidates:
        if emit:
            emit("status", {"message": i18n.t("srv.trying_list", url=url)})
        # Retries still cover 429/5xx and dropped connections, but a timeout is
        # not retried: a dead host would otherwise cost (retries+1) x timeout.
        status, res = make_request_with_retry(
            url, headers, timeout=timeout, max_retries=max(retries, 3), backoff_base=1.7,
            retry_timeouts=False,
        )
        if status == 200:
            models = extract_models_from_response(res)
            if models:
                return models, None
            errors.append(i18n.t("srv.list_unreadable", url=url))
        else:
            if isinstance(res, dict):
                err = res.get("error", "Unknown error")
                message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            else:
                message = f"HTTP {status}"
            errors.append(f"{url}: {message}")
            if status == 0:
                # Same host for every candidate: if it can't be reached at all,
                # trying the next path would just repeat the same failure.
                break

    return [], " | ".join(errors) if errors else "Unknown error"


# ===========================================================================
# 2) الـ pipeline — كيدير نفس خطوات main() الأصلية، غير بدل print
#    كيبعث "emit(event_type, data)" لكل خطوة، باش تتبعث حية لصفحة الويب
# ===========================================================================
def guess_provider_id(base_url):
    """api.openai.com -> openai, openrouter.ai -> openrouter, IP/localhost -> custom_provider."""
    match = re.search(r'https?://([^/:]+)', base_url)
    host = match.group(1).lower() if match else ""
    if not host or host == "localhost" or re.fullmatch(r"[\d.]+", host):
        return "custom_provider"
    labels = [l for l in host.split('.') if l and l != "www"]
    if len(labels) >= 2:
        return labels[-2]
    return labels[0] if labels else "custom_provider"


def run_pipeline(params, emit):
    base_url = (params.get("base_url") or "").strip().rstrip('/')
    api_key = (params.get("api_key") or "").strip()

    if not base_url or not api_key:
        emit("error", {"message": i18n.t("srv.need_url_key")})
        return

    if not re.match(r"^https?://[^/]+", base_url, re.IGNORECASE):
        emit("error", {"message": i18n.t("srv.bad_scheme")})
        return

    # Accept a complete chat URL too; internally the dashboard always needs
    # the API base URL in order to call both /models and /chat/completions.
    if base_url.lower().endswith("/chat/completions"):
        base_url = base_url[:-len("/chat/completions")].rstrip('/')

    default_provider = guess_provider_id(base_url)
    provider_id = (params.get("provider_id") or "").strip() or default_provider

    try:
        timeout = float(params.get("timeout") or 15)
        retries = int(params.get("retries") or 2)
        workers = int(params.get("workers") or 8)
        repeat = int(params.get("repeat") or 1)
    except (TypeError, ValueError):
        emit("error", {"message": i18n.t("srv.not_numbers")})
        return

    if timeout <= 0 or retries < 0 or workers <= 0 or repeat <= 0:
        emit("error", {"message": i18n.t("srv.bad_ranges")})
        return
    # Prevent an accidental value in the form from exhausting the local machine.
    workers = min(workers, 32)
    check_capabilities = bool(params.get("capabilities"))
    test_prompt = params.get("prompt") or "ping"
    manual_models = params.get("models") or ""
    filter_regex = params.get("filter") or ""
    fallback_models = params.get("fallback_models") or []

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "API-Test-Console/1.1",
    }

    # ---- خطوة 1: جلب الموديلات (بقوة — عدة محاولات وعدة طرق) ----
    models_list = []
    if manual_models.strip():
        models_list = list(dict.fromkeys(m.strip() for m in manual_models.split(",") if m.strip()))
        emit("status", {"message": i18n.t("srv.manual_models", n=len(models_list))})
    else:
        emit("status", {"message": i18n.t("srv.fetching")})
        models_list, fetch_err = fetch_models_list(base_url, headers, timeout, retries, emit=emit)

        if models_list:
            emit("status", {"message": i18n.t("srv.found", n=len(models_list))})
        elif fallback_models:
            models_list = [m for m in fallback_models if isinstance(m, str) and m.strip()]
            emit("status", {"message": i18n.t("srv.fallback", error=fetch_err, n=len(models_list))})
        else:
            emit("error", {"message": i18n.t("srv.fetch_failed", error=fetch_err)})
            return

    if filter_regex:
        try:
            pattern = re.compile(filter_regex)
            before = len(models_list)
            models_list = [m for m in models_list if pattern.search(m)]
            emit("status", {"message": i18n.t("srv.filter_matched", filter=filter_regex, n=len(models_list), before=before)})
        except re.error as e:
            emit("error", {"message": i18n.t("srv.bad_regex", error=e)})
            return

    if not models_list:
        emit("error", {"message": i18n.t("srv.no_models")})
        return

    emit("models_found", {"count": len(models_list), "models": models_list})

    # ---- خطوة 2: اختبار متوازي، كل نتيجة كتبعث فالحين ----
    chat_url = f"{base_url}/chat/completions"
    t0 = time.time()
    results = []

    executor = ThreadPoolExecutor(max_workers=min(workers, len(models_list)))
    futures = {
        executor.submit(test_model, m, chat_url, headers, timeout, retries,
                         test_prompt, repeat, check_capabilities): m
        for m in models_list
    }
    try:
        for future in as_completed(futures):
            model = futures[future]
            try:
                r = future.result()
            except Exception as e:
                # A bad provider response for one model must not abort the full batch.
                r = {"model": model, "status": "FAILED", "code": 0,
                     "error": i18n.t("srv.model_crash"),
                     "technical_error": str(e)}
            results.append(r)
            emit("model_result", r)  # <-- كتبعث لصفحة الويب فالحين، بلا ما تسنى الباقي
    except StopStreaming:
        # The page was closed or the user pressed "stop": drop the models that
        # haven't started yet instead of spending API calls nobody will see.
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    executor.shutdown(wait=True)

    total_time = round(time.time() - t0, 2)
    working = sorted([r for r in results if r["status"] == "WORKING"],
                      key=lambda r: r["response_time_avg"])
    failed = [r for r in results if r["status"] != "WORKING"]
    working_sorted = [r["model"] for r in working]

    if working:
        key_status = "VALID"
        key_message = i18n.t("srv.key.VALID")
    elif any(r["status"] == "KEY_INVALID" for r in failed):
        key_status = "INVALID"
        key_message = i18n.t("srv.key.INVALID")
    elif any(r["status"] == "LIMITED" for r in failed):
        key_status = "LIMITED"
        key_message = i18n.t("srv.key.LIMITED")
    else:
        key_status = "UNCONFIRMED"
        key_message = i18n.t("srv.key.UNCONFIRMED")

    config_data = build_opencode_config(provider_id, base_url, working_sorted) if working_sorted else None

    emit("summary", {
        "base_url": base_url, "provider_id": provider_id,
        "total_time_seconds": total_time,
        "total": len(models_list), "working": len(working), "failed": len(failed),
        "key_status": key_status, "key_message": key_message,
        "config": config_data,
    })
    emit("done", {})


# ===========================================================================
# 3) صفحة الويب (HTML/CSS/JS) — كتبعث الإدخال، وكتقرا الـ stream حي
# ===========================================================================
INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="{{i18n:lang}}" dir="{{i18n:dir}}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EskaGate</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%2064%2064%22%3E%20%3Cdefs%3E%20%3ClinearGradient%20id%3D%22bg%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%221%22%20y2%3D%221%22%3E%20%3Cstop%20offset%3D%220%22%20stop-color%3D%22%236D8BFF%22/%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%239B6DFF%22/%3E%20%3C/linearGradient%3E%20%3C/defs%3E%20%3Crect%20width%3D%2264%22%20height%3D%2264%22%20rx%3D%2215%22%20fill%3D%22url%28%23bg%29%22/%3E%20%3Cg%20fill%3D%22%23fff%22%3E%20%3Crect%20x%3D%2215%22%20y%3D%2214%22%20width%3D%228%22%20height%3D%2236%22%20rx%3D%222.5%22/%3E%20%3Crect%20x%3D%2215%22%20y%3D%2214%22%20width%3D%2232%22%20height%3D%228%22%20rx%3D%222.5%22/%3E%20%3Crect%20x%3D%2215%22%20y%3D%2242%22%20width%3D%2232%22%20height%3D%228%22%20rx%3D%222.5%22/%3E%20%3C/g%3E%20%3Cg%20fill%3D%22%233EE6A8%22%3E%20%3Crect%20x%3D%2226%22%20y%3D%2228%22%20width%3D%2212%22%20height%3D%228%22%20rx%3D%222.5%22/%3E%20%3Cpath%20d%3D%22M36%2023.5%20L49.5%2032%20L36%2040.5%20Z%22%20stroke%3D%22%233EE6A8%22%20stroke-width%3D%222%22%20stroke-linejoin%3D%22round%22/%3E%20%3C/g%3E%20%3C/svg%3E">
<style>
  /* ============================================================
     THEME TOKENS — dark (default) + light
  ============================================================ */
  :root {
    --bg: #0A0C12;
    --bg-grad-1: #0d1018;
    --bg-grad-2: #0a0c12;
    --surface: #12151D;
    --surface-2: #181C26;
    --surface-3: #1F2430;
    --line: #262B38;
    --line-soft: #1E2230;
    --text: #EDEDEA;
    --muted: #8B93A3;
    --faint: #5C6475;
    --ok: #34D399;
    --ok-soft: rgba(52,211,153,0.13);
    --bad: #F87171;
    --bad-soft: rgba(248,113,113,0.13);
    --warn: #FBBF24;
    --warn-soft: rgba(251,191,36,0.14);
    --accent: #6D8BFF;
    --accent-2: #9B6DFF;
    --accent-soft: rgba(109,139,255,0.15);
    --radius: 16px;
    --radius-sm: 10px;
    --radius-xs: 7px;
    --shadow: 0 12px 40px rgba(0,0,0,0.45);
    --shadow-sm: 0 4px 14px rgba(0,0,0,0.30);
    --glass: rgba(18,21,29,0.72);
    --font-ui: -apple-system, "Segoe UI", Inter, "Helvetica Neue", Arial, sans-serif;
    --font-mono: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
    --trans: .18s cubic-bezier(.4,0,.2,1);
  }
  html[data-theme="light"] {
    --bg: #F2F4F9;
    --bg-grad-1: #F6F8FC;
    --bg-grad-2: #EDEFF6;
    --surface: #FFFFFF;
    --surface-2: #F4F6FB;
    --surface-3: #EAEDF4;
    --line: #DDE1EA;
    --line-soft: #E8EBF2;
    --text: #161922;
    --muted: #6B7280;
    --faint: #9AA1B0;
    --ok: #059669;
    --ok-soft: rgba(5,150,105,0.12);
    --bad: #DC2626;
    --bad-soft: rgba(220,38,38,0.10);
    --warn: #D97706;
    --warn-soft: rgba(217,119,6,0.12);
    --accent: #4F6BFF;
    --accent-2: #8B5CF6;
    --accent-soft: rgba(79,107,255,0.12);
    --shadow: 0 12px 40px rgba(30,41,80,0.12);
    --shadow-sm: 0 4px 14px rgba(30,41,80,0.08);
    --glass: rgba(255,255,255,0.82);
  }

  * { box-sizing: border-box; }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--line); border-radius: 10px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--faint); }

  body {
    margin: 0; color: var(--text);
    font-family: var(--font-ui); font-size: 14px; line-height: 1.55;
    background: radial-gradient(1200px 700px at 100% -10%, var(--accent-soft), transparent 60%),
                radial-gradient(1000px 600px at 0% 0%, var(--accent-soft), transparent 55%),
                linear-gradient(160deg, var(--bg-grad-1), var(--bg-grad-2));
    background-attachment: fixed;
    min-height: 100vh;
  }
  .mono { font-family: var(--font-mono); }
  ::selection { background: var(--accent-soft); }

  /* ---------- TOP BAR ---------- */
  header {
    display: flex; align-items: center; gap: 14px;
    padding: 12px clamp(16px, 3vw, 28px);
    border-bottom: 1px solid var(--line);
    background: var(--glass); backdrop-filter: blur(14px) saturate(1.2);
    position: sticky; top: 0; z-index: 60;
  }
  header .logo {
    width: 40px; height: 40px; border-radius: 12px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    box-shadow: var(--shadow-sm); overflow: hidden;
  }
  header .titles h1 { font-size: 16px; font-weight: 750; margin: 0; letter-spacing: .2px; }
  header .titles p { margin: 1px 0 0; color: var(--muted); font-size: 12px; }
  header .top-actions { margin-inline-start: auto; display: flex; gap: 8px; align-items: center; }

  .icon-btn {
    width: 38px; height: 38px; border-radius: 10px; border: 1px solid var(--line);
    background: var(--surface-2); color: var(--text); cursor: pointer;
    font-size: 16px; display: inline-flex; align-items: center; justify-content: center;
    transition: var(--trans); position: relative;
  }
  .icon-btn:hover { border-color: var(--accent); color: var(--accent); transform: translateY(-1px); }
  .icon-btn.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
  .icon-btn .dot { position: absolute; top: -4px; inset-inline-end: -4px; width: 12px; height: 12px; border-radius: 50%; background: var(--bad); border: 2px solid var(--surface); display: none; }
  .icon-btn.has-alert .dot { display: block; }
  .lang-select { height: 38px; border-radius: 10px; border: 1px solid var(--line); background: var(--surface-2); color: var(--text);
    font: inherit; font-size: 12.5px; padding: 0 8px; cursor: pointer; transition: var(--trans); }
  .lang-select:hover, .lang-select:focus { border-color: var(--accent); outline: none; }
  @media (max-width: 700px) { .lang-select { display: none; } }   /* still in ⚙️ settings */

  /* ---------- APP LAYOUT ---------- */
  .app { max-width: 1600px; margin: 0 auto; padding: 18px clamp(12px, 2.5vw, 26px) 60px; }

  /* ---------- DASHBOARD STAT CARDS (top) ---------- */
  .stat-strip {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px; margin-bottom: 18px;
  }
  .stat {
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 16px 18px; position: relative; overflow: hidden;
    transition: var(--trans);
  }
  .stat:hover { transform: translateY(-2px); box-shadow: var(--shadow-sm); border-color: var(--faint); }
  .stat::before { content:''; position:absolute; top:0; right:0; width:100%; height:3px; background: var(--line); transition: var(--trans); }
  .stat.ok::before { background: linear-gradient(90deg, var(--ok), transparent); }
  .stat.bad::before { background: linear-gradient(90deg, var(--bad), transparent); }
  .stat.accent::before { background: linear-gradient(90deg, var(--accent), var(--accent-2)); }
  .stat .num { font-size: 30px; font-weight: 800; font-family: var(--font-mono); line-height: 1.1; }
  .stat .label { color: var(--muted); font-size: 12px; margin-top: 4px; display:flex; align-items:center; gap:6px; }
  .stat.ok .num { color: var(--ok); }
  .stat.bad .num { color: var(--bad); }
  .stat.accent .num { color: var(--accent); }
  .stat .trend { font-size: 11px; font-weight: 700; }
  .trend.up { color: var(--ok); } .trend.down { color: var(--bad); }

  /* ---------- LIVE MONITOR BAR ---------- */
  .live-bar {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 12px 16px; margin-bottom: 16px;
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  }
  .live-bar .pulse {
    width: 11px; height: 11px; border-radius: 50%; background: var(--ok); flex-shrink:0;
    box-shadow: 0 0 0 0 var(--ok); animation: pulse 1.8s infinite;
  }
  .live-bar .pulse.paused { background: var(--faint); animation: none; }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(52,211,153,0.5); }
    70% { box-shadow: 0 0 0 9px rgba(52,211,153,0); }
    100% { box-shadow: 0 0 0 0 rgba(52,211,153,0); }
  }
  .live-bar .live-label { font-weight: 700; font-size: 13px; }
  .live-bar .countdown { color: var(--muted); font-size: 12.5px; font-family: var(--font-mono); }
  .live-bar .spacer { flex: 1; }
  .seg { display: inline-flex; background: var(--surface-2); border: 1px solid var(--line); border-radius: 9px; padding: 3px; gap: 2px; }
  .seg button { border:none; background: transparent; color: var(--muted); font: inherit; font-size: 12px; padding: 6px 12px; border-radius: 6px; cursor: pointer; transition: var(--trans); }
  .seg button.on { background: var(--surface); color: var(--text); box-shadow: var(--shadow-sm); font-weight: 700; }

  /* ---------- MAIN GRID ---------- */
  .grid { display: grid; grid-template-columns: 360px 1fr; gap: 16px; align-items: start; }
  @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }

  .card {
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
    overflow: hidden;
  }
  .card-head {
    display: flex; align-items: center; gap: 10px;
    padding: 14px 16px; border-bottom: 1px solid var(--line);
    background: var(--surface-2);
  }
  .card-head h3 { font-size: 13.5px; font-weight: 750; margin: 0; display:flex; align-items:center; gap:8px; }
  .card-head .spacer { flex:1; }
  .card-body { padding: 16px; }

  /* ---------- FORM FIELDS ---------- */
  .field { margin-bottom: 14px; }
  .field:last-child { margin-bottom: 0; }
  .field label { display: block; margin-bottom: 6px; color: var(--muted); font-size: 12px; font-weight: 600; }
  .field input, .field select, .field textarea {
    width: 100%; background: var(--surface-2); border: 1px solid var(--line); color: var(--text);
    padding: 10px 11px; border-radius: var(--radius-sm); font: inherit; font-size: 13px;
    transition: var(--trans);
  }
  .field input:focus, .field select:focus, .field textarea:focus {
    outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft);
  }
  .field input::placeholder { color: var(--faint); }
  .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .checkbox { display: flex; align-items: center; gap: 9px; cursor: pointer; font-size: 13px; }
  .checkbox input { width: auto; accent-color: var(--accent); }

  .input-with-btn { display: flex; gap: 6px; }
  .input-with-btn input { flex: 1; min-width: 0; }
  .btn-icon {
    flex-shrink: 0; min-width: 38px; height: auto; background: var(--surface-2); border: 1px solid var(--line);
    color: var(--text); border-radius: var(--radius-sm); cursor: pointer; font-size: 14px; padding: 0 10px;
    transition: var(--trans);
  }
  .btn-icon:hover { border-color: var(--accent); color: var(--accent); }
  .btn-icon.copied { border-color: var(--ok); color: var(--ok); background: var(--ok-soft); }
  input[dir="ltr"], textarea[dir="ltr"] { font-family: var(--font-mono); text-align: left; direction: ltr; }

  .side-section {
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 15px; margin-bottom: 14px;
  }
  .side-section-title {
    display: flex; align-items: center; gap: 7px;
    font-size: 12px; font-weight: 750; color: var(--muted); margin-bottom: 12px; letter-spacing:.3px;
  }
  details.side-section > summary {
    cursor: pointer; list-style: none; display: flex; align-items: center;
    justify-content: space-between; font-size: 12px; font-weight: 750; color: var(--muted);
  }
  details.side-section > summary::-webkit-details-marker { display: none; }
  details.side-section > summary::after { content: '⌄'; font-size: 15px; transition: transform .15s; }
  details.side-section[open] > summary::after { transform: rotate(180deg); }
  details.side-section[open] > summary { margin-bottom: 12px; }

  /* ---------- WORKSPACE CONTROLS ---------- */
  .workspace-controls {
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    gap: 14px; align-items: stretch; margin-bottom: 16px;
  }
  .control-panel {
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 15px; min-width: 0;
  }
  .control-panel.connection-panel {
    border-color: color-mix(in srgb, var(--accent) 42%, var(--line));
    background: linear-gradient(135deg, var(--accent-soft), transparent 42%), var(--surface);
  }
  .workspace-controls > .connection-panel { grid-column: span 5; order: 1; }
  .workspace-controls > .run-panel { grid-column: span 2; order: 2; }
  .workspace-controls > #advancedSection { grid-column: span 3; order: 3; }
  .workspace-controls > section:last-child { grid-column: span 2; order: 4; }
  .control-panel-title {
    display:flex; align-items:center; justify-content:space-between; gap:10px;
    color:var(--muted); font-size:12px; font-weight:800; letter-spacing:.3px; margin-bottom:12px;
  }
  .connection-fields { display:grid; grid-template-columns: 1.25fr 1fr; gap:10px; }
  .connection-fields .provider-field { grid-column: span 2; }
  .workspace-controls .field { margin-bottom: 0; }
  .advanced-fields { display:grid; grid-template-columns: 1fr 1fr; gap:10px; }
  .advanced-fields .wide { grid-column:span 2; }
  .advanced-fields .row2 { gap:10px; }
  .profile-actions { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:10px; }
  .run-panel { display:flex; flex-direction:column; justify-content:center; gap:9px; }
  .run-panel .primary { min-height:52px; font-size:15px; }
  .run-panel .action-grid { margin-top:auto; }
  .run-panel .dd-menu { bottom:auto; top:calc(100% + 8px); }
  .results-area { min-width:0; }

  @media (max-width: 1180px) {
    .workspace-controls { grid-template-columns: minmax(0, 1fr) minmax(250px, .85fr); }
    .workspace-controls > .connection-panel, .workspace-controls > #advancedSection,
    .workspace-controls > section:last-child { grid-column:auto; }
    .run-panel { grid-column:span 2; display:grid; grid-template-columns:minmax(220px, 1fr) 1fr; align-items:center; }
    .run-panel .primary { grid-row:span 2; }
    .run-panel .dd { width:100%; }
    .run-panel .action-grid { margin-top:0; }
  }
  @media (max-width: 700px) {
    .workspace-controls, .connection-fields, .advanced-fields { grid-template-columns:1fr; }
    .connection-fields .provider-field, .advanced-fields .wide { grid-column:span 1; }
    .workspace-controls > .run-panel { grid-column:auto; display:flex; }
    .run-panel .primary { min-height:48px; }
    .profile-actions { grid-template-columns:1fr 1fr; }
  }

  /* ---------- BUTTONS ---------- */
  button { font-family: var(--font-ui); cursor: pointer; }
  button.primary {
    width: 100%; padding: 14px; border: none; border-radius: var(--radius-sm);
    background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: #fff;
    font-weight: 800; font-size: 14px; box-shadow: var(--shadow-sm);
    transition: var(--trans); letter-spacing: .3px;
  }
  button.primary:hover:not(:disabled) { transform: translateY(-1px); filter: brightness(1.08); box-shadow: var(--shadow); }
  button.primary:active:not(:disabled) { transform: translateY(0) scale(.99); }
  button.primary:disabled { opacity: .5; cursor: not-allowed; }
  button.ghost {
    width: 100%; padding: 9px 8px; border: 1px solid var(--line); border-radius: var(--radius-sm);
    background: var(--surface-2); color: var(--text); font: inherit; font-size: 12.5px; transition: var(--trans);
  }
  button.ghost:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  button.ghost:disabled { opacity: .4; cursor: not-allowed; }
  button.ghost.sm { padding: 7px 8px; font-size: 11.5px; }
  .action-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }

  /* ---------- DROPDOWN (smart copy) ---------- */
  .dd { position: relative; }
  .dd-btn { width:100%; display:flex; align-items:center; justify-content:center; gap:7px;
    padding: 11px; border: 1px solid var(--accent); background: var(--accent-soft);
    color: var(--accent); border-radius: var(--radius-sm); font-weight: 800; font-size: 13px; transition: var(--trans); }
  .dd-btn:hover { filter: brightness(1.1); }
  .dd-btn .caret { font-size: 10px; transition: transform .15s; }
  .dd.open .dd-btn .caret { transform: rotate(180deg); }
  .dd-menu {
    position: absolute; bottom: calc(100% + 8px); left: 0; right: 0; z-index: 30;
    background: var(--surface-3); border: 1px solid var(--line); border-radius: var(--radius-sm);
    box-shadow: var(--shadow); padding: 6px; display: none;
  }
  .dd.open .dd-menu { display: block; animation: pop .14s ease; }
  @keyframes pop { from { opacity: 0; transform: translateY(6px); } to { opacity:1; transform: none; } }
  .dd-item { display:flex; align-items:center; gap:9px; width:100%; padding: 9px 10px; border:none;
    background: transparent; color: var(--text); border-radius: var(--radius-xs); font-size: 13px; transition: var(--trans); }
  .dd-item:hover { background: var(--accent-soft); color: var(--accent); }
  .dd-item:disabled { opacity: .4; cursor: not-allowed; }
  .dd-item .em { font-size: 14px; }

  /* ---------- STATUS LINE ---------- */
  .status-line {
    display: flex; align-items: center; gap: 10px;
    color: var(--muted); font-size: 13px; margin-bottom: 16px;
    padding: 11px 15px; background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius-sm); border-inline-start: 3px solid var(--line);
  }
  .status-line.err { border-inline-start-color: var(--bad); color: var(--bad); background: var(--bad-soft); }
  .status-line .spin { width:14px; height:14px; border:2px solid var(--line); border-top-color: var(--accent); border-radius:50%; animation: rot .7s linear infinite; display:none; }
  .status-line.busy .spin { display: inline-block; }
  @keyframes rot { to { transform: rotate(360deg); } }

  /* ---------- RESULTS TABLE ---------- */
  table { width: 100%; border-collapse: collapse; }
  thead th {
    text-align: start; color: var(--muted); font-weight: 700; font-size: 12px;
    padding: 12px 14px; border-bottom: 1px solid var(--line); white-space: nowrap;
    position: sticky; top: 0; background: var(--surface-2); z-index: 2;
  }
  tbody td { padding: 11px 14px; border-bottom: 1px solid var(--line-soft); white-space: nowrap; font-size: 13px; }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: var(--surface-2); }
  tbody td:first-child { font-family: var(--font-mono); }
  tbody tr.flash { animation: flash 1s ease; }
  @keyframes flash { 0% { background: var(--accent-soft); } 100% { background: transparent; } }

  .pill { display: inline-block; padding: 4px 11px; border-radius: 20px; font-size: 11.5px; font-weight: 750; }
  .pill.ok { background: var(--ok-soft); color: var(--ok); }
  .pill.bad { background: var(--bad-soft); color: var(--bad); }
  .pill.pending { background: var(--accent-soft); color: var(--accent); }
  .bar-cell { display: flex; align-items: center; gap: 8px; min-width: 130px; }
  .bar-cell span:first-child { font-family: var(--font-mono); font-size: 12px; }
  .bar-track { flex: 1; height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; }
  .bar-fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent-2)); border-radius: 3px; transition: width .4s ease; }
  .result-diagnosis { display:flex; flex-direction:column; gap:2px; min-width:190px; max-width:420px; white-space:normal; }
  .result-diagnosis strong { font-size:12px; color:var(--bad); }
  .result-diagnosis span { font-size:11.5px; color:var(--muted); overflow-wrap:anywhere; }
  .result-diagnosis small { color:var(--faint); font-family:var(--font-mono); font-size:10px; }
  .cap { color: var(--faint); font-size: 11px; font-weight: 600; }
  .cap.on { color: var(--warn); }

  /* ---------- CHART ---------- */
  .chart-wrap { padding: 8px 14px 16px; }
  .chart { display: flex; align-items: stretch; gap: 6px; height: 150px; }
  /* Each column fills the chart height; the bar lives in a flexible slot so its
     percentage height has something to resolve against. */
  .chart .col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 6px; min-width: 0; height: 100%; }
  .chart .col .slot { flex: 1; width: 100%; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; min-height: 0; }
  .chart .col .val { font-size: 10px; color: var(--muted); font-family: var(--font-mono); margin-bottom: 3px; }
  .chart .col .bar { width: 100%; max-width: 34px; border-radius: 5px 5px 0 0;
    background: linear-gradient(180deg, var(--accent), var(--accent-2)); transition: height .5s ease; }
  .chart .col:first-child .bar { background: linear-gradient(180deg, var(--ok), color-mix(in srgb, var(--ok) 55%, var(--accent))); }
  .chart .col .bar.fail { background: var(--bad); opacity:.55; }
  .chart .col .lab { font-size: 9.5px; color: var(--muted); font-family: var(--font-mono);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
  .chart-empty { color: var(--faint); text-align:center; padding: 24px 0; font-size: 12.5px; }

  /* ---------- MODALS ---------- */
  .modal-overlay {
    display: none; position: fixed; inset: 0; background: rgba(5,6,10,0.66); backdrop-filter: blur(3px);
    align-items: flex-start; justify-content: center; padding: 5vh 16px; z-index: 200; overflow-y: auto;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
    box-shadow: var(--shadow); width: min(1000px, 100%); max-height: 90vh; overflow: auto; padding: 22px;
    animation: pop .16s ease;
  }
  .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; }
  .modal-header h2 { font-size: 16px; margin: 0; font-weight: 800; display:flex; align-items:center; gap:9px; }
  .modal-close {
    background: var(--surface-2); border: 1px solid var(--line); color: var(--text);
    border-radius: var(--radius-sm); padding: 7px 14px; font: inherit; font-size: 12.5px; transition: var(--trans);
  }
  .modal-close:hover { border-color: var(--bad); color: var(--bad); }

  /* ---------- ARCHIVE CARDS (compact glass) ---------- */
  .archive-grid-view {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .key-card {
    background: color-mix(in srgb, var(--surface-2) 55%, transparent);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    border: 1px solid color-mix(in srgb, var(--line) 60%, transparent);
    border-radius: 12px;
    padding: 10px 12px;
    transition: var(--trans);
    position: relative;
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }
  .key-card::before {
    content: '';
    position: absolute;
    inset-block: 0; inset-inline-end: 0;
    width: 3px;
    background: var(--faint);
    border-start-end-radius: 12px; border-end-end-radius: 12px;
  }
  .key-card.ok::before { background: var(--ok); }
  .key-card.bad::before { background: var(--bad); }
  .key-card:hover {
    background: color-mix(in srgb, var(--surface-2) 75%, transparent);
    border-color: var(--faint);
    box-shadow: var(--shadow-sm);
  }

  /* Small status icon */
  .kc-icon {
    flex: 0 0 30px;
    width: 30px; height: 30px;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 15px;
    background: var(--surface-3);
    border: 1px solid var(--line-soft);
  }
  .key-card.ok .kc-icon { background: var(--ok-soft); border-color: color-mix(in srgb, var(--ok) 30%, transparent); }
  .key-card.bad .kc-icon { background: var(--bad-soft); border-color: color-mix(in srgb, var(--bad) 30%, transparent); }

  /* Body */
  .kc-body {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  /* Row 1: name + badge + meta */
  .kc-row1 {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    flex-wrap: wrap;
  }
  .kc-name {
    font-weight: 700;
    font-size: 13px;
    color: var(--text);
    letter-spacing: -0.2px;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .kc-status { flex-shrink: 0; }
  .kc-meta-right {
    display: flex;
    align-items: center;
    gap: 5px;
    margin-inline-start: auto;
    font-size: 10px;
    color: var(--muted);
    white-space: nowrap;
  }
  .kc-meta-right .dot-sep { width: 2px; height: 2px; border-radius: 50%; background: var(--faint); }

  /* Row 2: URL + Key on one line */
  .kc-row2 {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 6px;
    min-width: 0;
  }
  .kc-pair {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    background: color-mix(in srgb, var(--surface) 50%, transparent);
    border: 1px solid var(--line-soft);
    border-radius: 7px;
    padding: 5px 8px;
  }
  .kc-pair .k {
    font-size: 9px;
    font-weight: 700;
    color: var(--muted);
    letter-spacing: .3px;
    text-transform: uppercase;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .kc-pair .k::before {
    content: '';
    width: 4px; height: 4px;
    border-radius: 50%;
  }
  .kc-pair.url .k::before { background: var(--accent); }
  .kc-pair.key .k::before { background: var(--accent-2); }
  .kc-pair .v {
    flex: 1;
    min-width: 0;
    font-family: var(--font-mono);
    font-size: 10.5px;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .kc-pair .v.empty { color: var(--faint); font-style: italic; }
  .mini-copy {
    flex-shrink: 0;
    width: 22px; height: 22px;
    border-radius: 5px;
    border: 1px solid var(--line);
    background: var(--surface-2);
    color: var(--muted);
    font-size: 10px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: var(--trans);
    padding: 0;
    cursor: pointer;
  }
  .mini-copy:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
    background: var(--accent-soft);
  }
  .mini-copy:disabled { opacity: .3; cursor: not-allowed; }
  .mini-copy.copied { border-color: var(--ok); color: var(--ok); background: var(--ok-soft); }

  /* Row 3: source + models + copy-dropdown + actions */
  .kc-row3 {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    min-width: 0;
  }
  .kc-source-inline {
    display: flex;
    align-items: center;
    gap: 4px;
    min-width: 0;
  }
  .kc-source-inline .k {
    font-size: 9px;
    font-weight: 700;
    color: var(--muted);
    letter-spacing: .3px;
    display: flex;
    align-items: center;
    gap: 3px;
  }
  .kc-source-inline .k::before { content:''; width:4px; height:4px; border-radius:50%; background:var(--ok); }
  .kc-source-inline input {
    background: transparent;
    border: none;
    border-bottom: 1px dashed var(--line);
    color: var(--text);
    font-size: 10px;
    padding: 2px 4px;
    width: 110px;
  }
  .kc-source-inline input:focus { outline: none; border-color: var(--accent); }
  .kc-models {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
    min-width: 0;
    flex: 1;
  }
  .kc-models .lbl {
    font-size: 9px;
    font-weight: 700;
    color: var(--muted);
    letter-spacing: .3px;
  }
  .model-tags { display: flex; flex-wrap: wrap; gap: 3px; }
  .model-tag {
    font-family: var(--font-mono);
    font-size: 9.5px;
    padding: 2px 7px;
    border-radius: 4px;
    background: var(--surface);
    border: 1px solid var(--line);
    color: var(--text);
  }
  .diff-add { color: var(--ok); font-weight: 700; font-size: 10px; }
  .diff-remove { color: var(--bad); font-weight: 700; font-size: 10px; }

  /* Smart copy dropdown */
  .copy-dd {
    position: relative;
    flex-shrink: 0;
  }
  .copy-dd > button {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 5px 10px;
    font-size: 11px;
    font-weight: 600;
    border-radius: 7px;
    border: 1px solid var(--line);
    background: color-mix(in srgb, var(--accent) 12%, var(--surface-2));
    color: var(--accent);
    cursor: pointer;
    transition: var(--trans);
    white-space: nowrap;
  }
  .copy-dd > button:hover {
    background: color-mix(in srgb, var(--accent) 22%, var(--surface-2));
    border-color: var(--accent);
  }
  .copy-dd > button::after {
    content: '▾';
    font-size: 9px;
    opacity: .7;
  }
  .copy-dd .dd-menu {
    display: none;
    position: absolute;
    inset-inline-start: 0;
    top: calc(100% + 4px);
    min-width: 170px;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    box-shadow: var(--shadow);
    z-index: 100;
    padding: 5px;
    flex-direction: column;
    gap: 2px;
  }
  .copy-dd.open .dd-menu { display: flex; }
  .copy-dd .dd-item {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 7px 10px;
    font-size: 11.5px;
    border-radius: 6px;
    border: none;
    background: transparent;
    color: var(--text);
    cursor: pointer;
    text-align: start;
    transition: var(--trans);
  }
  .copy-dd .dd-item:hover:not(:disabled) {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .copy-dd .dd-item:disabled { opacity: .4; cursor: not-allowed; }
  .copy-dd .dd-item .em { font-size: 13px; }
  .copy-dd .dd-sep {
    height: 1px;
    background: var(--line-soft);
    margin: 3px 4px;
  }

  /* Actions */
  .kc-actions {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
  }
  .kc-actions .ghost {
    padding: 5px 9px;
    font-size: 10.5px;
    border-radius: 6px;
    white-space: nowrap;
  }

  /* Responsive */
  @media (max-width: 700px) {
    .kc-row2 { grid-template-columns: 1fr; }
    .kc-row3 { flex-direction: column; align-items: flex-start; }
    .kc-source-inline input { width: 140px; }
  }
  .model-tag .t { color: var(--muted); }
  .diff-line { font-size: 12px; margin-top: 6px; }
  .diff-add { color: var(--ok); font-weight:700; }
  .diff-remove { color: var(--bad); font-weight:700; }

  .empty-state { color: var(--muted); padding: 40px 12px; text-align: center; font-size: 13px; }
  .empty-state .em { font-size: 40px; display:block; margin-bottom: 10px; opacity:.6; }

  /* ---------- TOASTS ---------- */
  #toasts { position: fixed; bottom: 18px; inset-inline-end: 18px; z-index: 300; display: flex; flex-direction: column; gap: 10px; width: min(340px, calc(100vw - 36px)); }
  .toast {
    background: var(--surface-3); border: 1px solid var(--line); border-inline-start: 4px solid var(--accent);
    border-radius: var(--radius-sm); padding: 12px 14px; box-shadow: var(--shadow);
    animation: slidein .25s ease; font-size: 12.5px;
  }
  @keyframes slidein { from { opacity:0; transform: translateX(var(--slide-x)); } to { opacity:1; transform:none; } }
  .toast.ok { border-inline-start-color: var(--ok); }
  .toast.bad { border-inline-start-color: var(--bad); }
  .toast.warn { border-inline-start-color: var(--warn); }
  /* toasts sit on the inline-end side and slide in from the page edge */
  :root { --slide-x: -16px; }
  [dir="ltr"] { --slide-x: 16px; }
  .toast .t-title { font-weight: 800; font-size: 13px; margin-bottom: 3px; display:flex; align-items:center; gap:7px; }
  .toast .t-body { color: var(--muted); line-height: 1.4; }

  .modal-desc { color: var(--muted); font-size: 13px; margin: 0 0 16px; line-height: 1.6; }
  .setting-row { display:flex; align-items:center; justify-content:space-between; gap:12px; padding: 12px 0; border-bottom: 1px solid var(--line-soft); }
  .setting-row:last-child { border-bottom: none; }
  .setting-row .s-label { font-weight: 700; font-size: 13.5px; }
  .setting-row .s-sub { color: var(--muted); font-size: 11.5px; }
  .toggle { width: 46px; height: 26px; border-radius: 20px; background: var(--surface-3); border:1px solid var(--line); position:relative; cursor:pointer; transition: var(--trans); flex-shrink:0; }
  .toggle::after { content:''; position:absolute; top:2px; inset-inline-start:2px; width:20px; height:20px; border-radius:50%; background: var(--muted); transition: var(--trans); }
  .toggle.on { background: var(--accent-soft); border-color: var(--accent); }
  .toggle.on::after { background: var(--accent); inset-inline-start: calc(100% - 22px); }
  .theme-seg { display:inline-flex; background: var(--surface-2); border:1px solid var(--line); border-radius:9px; padding:3px; gap:2px; }
  .theme-seg button { border:none; background:transparent; color:var(--muted); font:inherit; font-size:12px; padding:6px 14px; border-radius:6px; cursor:pointer; transition:var(--trans); }
  .theme-seg button.on { background: var(--surface); color: var(--text); font-weight:700; box-shadow: var(--shadow-sm); }

  /* ---------- TABS (side bar: open = icon + name, closed = icons only) ---------- */
  :root { --nav-open: 224px; --nav-closed: 68px; --nav-w: var(--nav-open); --header-h: 65px; }
  body.nav-collapsed { --nav-w: var(--nav-closed); }
  .tabs { position: fixed; top: var(--header-h); bottom: 0; inset-inline-start: 0; z-index: 55;
    width: var(--nav-w); display: flex; flex-direction: column; gap: 4px; padding: 10px;
    background: var(--glass); backdrop-filter: blur(14px) saturate(1.2);
    border-inline-end: 1px solid var(--line); overflow-x: hidden; overflow-y: auto;
    transition: width .28s cubic-bezier(.4,0,.2,1), box-shadow .28s; }
  .nav-toggle { align-self: flex-start; width: 46px; height: 40px; margin-bottom: 6px; border: 1px solid var(--line);
    border-radius: 10px; background: var(--surface-2); color: var(--muted); cursor: pointer; font-size: 17px;
    display: flex; align-items: center; justify-content: center; transition: var(--trans); flex-shrink: 0; }
  .nav-toggle:hover { color: var(--text); border-color: var(--accent); }
  .tab-btn { position: relative; border: none; background: transparent; color: var(--muted); font: inherit; font-size: 13.5px; font-weight: 700;
    padding: 10px 12px; border-radius: 10px; cursor: pointer; transition: var(--trans);
    display: flex; align-items: center; gap: 12px; width: 100%; white-space: nowrap; text-align: start; flex-shrink: 0; }
  .tab-btn:hover { color: var(--text); background: var(--surface-2); }
  .tab-btn.on { background: var(--accent-soft); color: var(--accent); }
  .tab-btn .ic { width: 24px; flex-shrink: 0; text-align: center; font-size: 17px; }
  .tab-btn .lbl { flex: 1; overflow: hidden; text-overflow: ellipsis; transition: opacity .2s; }
  .tab-btn .count { font-family: var(--font-mono); font-size: 11px; background: var(--surface-3); color: var(--muted);
    padding: 1px 8px; border-radius: 10px; transition: var(--trans); }
  .tab-btn.on .count { background: var(--accent); color: #fff; }
  body.nav-collapsed .tab-btn .lbl { opacity: 0; pointer-events: none; }
  body.nav-collapsed .tab-btn .count { position: absolute; top: 3px; inset-inline-start: 30px;
    font-size: 9.5px; padding: 0 5px; line-height: 15px; }
  .tab-panel[hidden] { display: none !important; }
  body:not(.nav-ready) .tabs, body:not(.nav-ready) .app { transition: none; }   /* no slide on page load */
  .app { max-width: calc(1600px + var(--nav-w)); padding-inline-start: calc(var(--nav-w) + clamp(12px, 2.5vw, 26px));
    transition: padding .28s cubic-bezier(.4,0,.2,1), max-width .28s cubic-bezier(.4,0,.2,1); }
  /* Small screens: the bar stays icons-only beside the page and opens over it. */
  @media (max-width: 700px) {
    :root { --nav-closed: 58px; }
    .app { max-width: none; padding-inline-start: calc(var(--nav-closed) + 10px); }
    body:not(.nav-collapsed) .tabs { box-shadow: var(--shadow-lg, 0 10px 40px rgba(0,0,0,.35)); }
    .tabs { padding: 8px 6px; }
  }

  /* ---------- TEST TAB LAYOUT ---------- */
  .test-layout { display: grid; grid-template-columns: minmax(320px, 430px) minmax(0, 1fr); gap: 16px; align-items: start; }
  .setup-col { display: flex; flex-direction: column; gap: 14px; position: sticky; top: 84px; }
  .setup-col .field { margin-bottom: 12px; }
  .setup-col .btn-icon { min-width: 34px; padding: 0 8px; font-size: 13px; }
  .setup-col .connection-panel .primary { margin-top: 4px; min-height: 50px; font-size: 15px; }
  .setup-col .hint { color: var(--faint); font-size: 11px; text-align: center; margin-top: 7px; }
  .setup-col .hint kbd { font-family: var(--font-mono); background: var(--surface-3); border: 1px solid var(--line);
    border-radius: 4px; padding: 0 5px; font-size: 10.5px; }
  button.primary.stop { background: linear-gradient(135deg, var(--bad), #f59e0b); }
  details.control-panel > summary { cursor: pointer; list-style: none; display: flex; align-items: center; justify-content: space-between;
    color: var(--muted); font-size: 12px; font-weight: 800; letter-spacing: .3px; }
  details.control-panel > summary::-webkit-details-marker { display: none; }
  details.control-panel > summary::after { content: '⌄'; font-size: 15px; transition: transform .15s; }
  details.control-panel[open] > summary { margin-bottom: 12px; }
  details.control-panel[open] > summary::after { transform: rotate(180deg); }
  .summary-note { color: var(--faint); font-weight: 600; font-size: 11px; margin-inline-start: auto; margin-inline-end: 8px; }
  @media (max-width: 700px) { header .titles p { display: none; } .search-input { width: 100%; } }
  @media (max-width: 980px) {
    .test-layout { grid-template-columns: 1fr; }
    .setup-col { position: static; }
  }

  /* ---------- RESULT SUMMARY BANNER ---------- */
  .result-banner { display: none; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 16px; padding: 14px 16px;
    border-radius: var(--radius); border: 1px solid color-mix(in srgb, var(--ok) 40%, var(--line));
    background: linear-gradient(135deg, var(--ok-soft), transparent 60%), var(--surface); }
  .result-banner.show { display: flex; animation: pop .2s ease; }
  .result-banner.bad { border-color: color-mix(in srgb, var(--bad) 40%, var(--line));
    background: linear-gradient(135deg, var(--bad-soft), transparent 60%), var(--surface); }
  .result-banner .big { font-size: 26px; font-weight: 800; font-family: var(--font-mono); color: var(--ok); line-height: 1; }
  .result-banner.bad .big { color: var(--bad); }
  .result-banner .txt { display: flex; flex-direction: column; gap: 3px; min-width: 0; flex: 1; }
  .result-banner .txt strong { font-size: 14px; }
  .result-banner .txt span { color: var(--muted); font-size: 12.5px; overflow-wrap: anywhere; }
  .result-banner .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .result-banner .actions .ghost, .result-banner .actions .dd-btn { width: auto; padding: 9px 14px; }
  .result-banner .dd-menu { bottom: auto; top: calc(100% + 8px); inset-inline-end: 0; inset-inline-start: auto; min-width: 220px; }

  /* ---------- RESULT FILTERS ---------- */
  .filters { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .chip { border: 1px solid var(--line); background: var(--surface); color: var(--muted); font: inherit; font-size: 12px;
    font-weight: 700; padding: 5px 12px; border-radius: 20px; cursor: pointer; transition: var(--trans); }
  .chip:hover { color: var(--text); border-color: var(--faint); }
  .chip.on { background: var(--accent-soft); color: var(--accent); border-color: var(--accent); }
  .chip .n { font-family: var(--font-mono); margin-inline-start: 4px; opacity: .8; }
  .search-input { background: var(--surface); border: 1px solid var(--line); color: var(--text); border-radius: 20px;
    padding: 6px 12px; font: inherit; font-size: 12px; width: 170px; font-family: var(--font-mono); }
  .search-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  .model-cell { display: flex; align-items: center; gap: 7px; }
  .model-cell .mini-copy { opacity: 0; }
  tbody tr:hover .model-cell .mini-copy, .model-cell .mini-copy.copied { opacity: 1; }
  .rank { font-family: var(--font-mono); font-size: 10.5px; color: var(--faint); min-width: 18px; }
  .sub-metric { display: block; color: var(--faint); font-size: 10.5px; font-family: var(--font-mono); }
  .empty-row td { text-align: center; color: var(--faint); padding: 34px 12px !important; white-space: normal; }

  /* ---------- KEYS TAB ---------- */
  .keys-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
  .keys-head .modal-desc { margin: 0; flex: 1; min-width: 240px; }

  /* ---------- GATEWAY TABS ---------- */
  .gw-grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); gap: 16px; margin-bottom: 16px; }
  @media (max-width: 980px) { .gw-grid { grid-template-columns: 1fr; } }
  .kv-row { display: flex; align-items: center; gap: 8px; padding: 8px 10px; margin-bottom: 8px; border-radius: var(--radius-sm);
    background: var(--surface-2); border: 1px solid var(--line-soft); min-width: 0; }
  .kv-row .k { font-size: 11px; font-weight: 700; color: var(--muted); min-width: 104px; }
  .kv-row .v { flex: 1; min-width: 0; font-family: var(--font-mono); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .prov-card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); margin-bottom: 14px; overflow: hidden; }
  .prov-card.off { opacity: .6; }
  .prov-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 13px 16px; background: var(--surface-2); border-bottom: 1px solid var(--line); }
  .prov-head .name { font-weight: 800; font-size: 14px; font-family: var(--font-mono); }
  .prov-head .url { color: var(--muted); font-size: 11.5px; font-family: var(--font-mono); overflow-wrap: anywhere; }
  .prov-head .acts { display: flex; gap: 6px; margin-inline-start: auto; flex-wrap: wrap; }
  .prov-head .acts .ghost { width: auto; padding: 7px 12px; }
  .prov-body { padding: 12px 16px 16px; }
  .fmt-badge { font-size: 10.5px; font-weight: 800; padding: 2px 8px; border-radius: 6px; background: var(--accent-soft); color: var(--accent); }
  .key-row { display: grid; grid-template-columns: 22px minmax(120px, 1.1fr) 110px minmax(0, 2fr) auto; gap: 10px; align-items: center;
    padding: 8px 4px; border-bottom: 1px solid var(--line-soft); font-size: 12.5px; }
  .key-row:last-child { border-bottom: none; }
  .key-row .mk { font-family: var(--font-mono); }
  .key-row .err { color: var(--muted); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .key-row .acts { display: flex; gap: 4px; }
  .key-row .acts .ghost { width: auto; padding: 5px 9px; font-size: 11px; }
  .star { color: var(--warn); }
  .add-keys { display: flex; gap: 8px; margin-top: 12px; align-items: stretch; }
  .add-keys textarea { flex: 1; min-height: 42px; background: var(--surface-2); border: 1px solid var(--line); color: var(--text);
    border-radius: var(--radius-sm); padding: 9px 11px; font-family: var(--font-mono); font-size: 12px; resize: vertical; }
  .add-keys .ghost { width: auto; padding: 0 16px; }
  .add-keys-box { margin-top: 14px; padding: 12px; border-radius: var(--radius-sm); border: 1px dashed color-mix(in srgb, var(--accent) 45%, var(--line));
    background: color-mix(in srgb, var(--accent-soft) 45%, transparent); }
  .add-keys-box > label { display: block; font-size: 12px; font-weight: 800; color: var(--accent); margin-bottom: 8px; }
  .add-keys-box .hint-inline { color: var(--muted); font-weight: 600; }
  .add-keys-box .add-keys { margin-top: 0; }
  .add-keys .add-btn { width: auto; padding: 0 18px; white-space: nowrap; font-size: 13px; }
  .add-keys .btn-icon { min-width: 38px; }
  .add-keys textarea.masked { -webkit-text-security: disc; text-security: disc; }
  .add-keys-msg { font-size: 12px; margin-top: 8px; min-height: 0; }
  .add-keys-msg:empty { display: none; }
  .add-keys-msg.ok { color: var(--ok); }
  .add-keys-msg.warn { color: var(--warn); }
  .add-keys-msg.bad { color: var(--bad); font-weight: 700; }
  .rotation-help { color: var(--faint); font-size: 11.5px; margin: 10px 0 0; line-height: 1.6; }
  .agent-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
  .agent-card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 16px; display: flex; flex-direction: column; gap: 10px; }
  .agent-card.on { border-color: color-mix(in srgb, var(--ok) 45%, var(--line)); background: linear-gradient(135deg, var(--ok-soft), transparent 55%), var(--surface); }
  .agent-card .top { display: flex; align-items: center; gap: 10px; }
  .agent-card .top .ic { width: 38px; height: 38px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 19px; background: var(--surface-3); }
  .agent-card .top .nm { font-weight: 800; font-size: 15px; }
  .agent-card .top .ver { color: var(--muted); font-size: 11px; font-family: var(--font-mono); }
  .agent-card .top .pill { margin-inline-start: auto; white-space: nowrap; }
  .agent-card .meta { color: var(--muted); font-size: 11.5px; font-family: var(--font-mono); overflow-wrap: anywhere; }
  .agent-card .note { color: var(--warn); font-size: 12px; }
  .agent-card .row { display: flex; gap: 8px; align-items: center; }
  .agent-card select { flex: 1; min-width: 0; background: var(--surface-2); border: 1px solid var(--line); color: var(--text);
    border-radius: var(--radius-sm); padding: 9px 10px; font-family: var(--font-mono); font-size: 12px; }
  .agent-card .row .ghost, .agent-card .row .primary { width: auto; padding: 10px 16px; }
  input.from-provider { color: var(--accent) !important; }
  /* ---------- KEYS TAB (redesign) ---------- */
  .stat.warn::before { background: linear-gradient(90deg, var(--warn), transparent); }
  .stat.warn .num { color: var(--warn); }
  .monitor-panel { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }
  .monitor-panel .mon-title { display: flex; align-items: center; gap: 12px; flex: 1; min-width: 240px; }
  .monitor-panel .live-label { font-weight: 800; font-size: 14px; }
  .monitor-panel .countdown { color: var(--muted); font-size: 12px; font-family: var(--font-mono); }
  .monitor-panel .pulse { width: 12px; height: 12px; border-radius: 50%; background: var(--ok); flex-shrink: 0; animation: pulse 1.8s infinite; }
  .monitor-panel .pulse.paused { background: var(--faint); animation: none; }
  .monitor-panel .mon-controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .monitor-panel .mon-lbl { color: var(--muted); font-size: 12px; }
  .monitor-panel .ghost { width: auto; padding: 9px 14px; }
  .keys-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }
  .keys-toolbar .filters { flex: 1; }
  .keys-toolbar .toolbar-actions { display: flex; gap: 8px; }
  .keys-toolbar .toolbar-actions .ghost, .keys-toolbar .toolbar-actions .primary { width: auto; padding: 9px 16px; }
  .key-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; }
  @media (max-width: 700px) { .key-grid { grid-template-columns: 1fr; } }
  .kcard { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); display: flex; flex-direction: column;
    transition: var(--trans); position: relative; }
  .kcard:hover { border-color: var(--faint); box-shadow: var(--shadow-sm); }
  .kcard::before { content: ''; position: absolute; top: 0; right: 0; left: 0; height: 3px; border-radius: var(--radius) var(--radius) 0 0; background: var(--line); }
  .kcard.ok::before { background: linear-gradient(90deg, var(--ok), transparent); }
  .kcard.warn::before { background: linear-gradient(90deg, var(--warn), transparent); }
  .kcard.bad::before { background: linear-gradient(90deg, var(--bad), transparent); }
  .kcard-head { display: flex; align-items: center; gap: 10px; padding: 14px 16px 10px; }
  .kcard-head .kc-ic { width: 34px; height: 34px; border-radius: 10px; display: flex; align-items: center; justify-content: center;
    font-size: 16px; background: var(--surface-3); flex-shrink: 0; }
  .kcard.ok .kc-ic { background: var(--ok-soft); } .kcard.warn .kc-ic { background: var(--warn-soft); } .kcard.bad .kc-ic { background: var(--bad-soft); }
  .kcard-head .kc-title { flex: 1; min-width: 0; }
  .kcard-head .kc-name { font-weight: 800; font-size: 14px; overflow-wrap: anywhere; }
  .kc-link { color: inherit; text-decoration: none; border-bottom: 1px dashed transparent; transition: var(--trans); }
  .kc-link:hover { color: var(--accent); border-bottom-color: var(--accent); }
  .kc-link .ext { font-size: 11px; color: var(--accent); opacity: .7; }
  .kcard-head .kc-sub { color: var(--muted); font-size: 11.5px; }
  .kcard-head .pill { white-space: nowrap; }
  .kcard-body { padding: 0 16px 12px; display: flex; flex-direction: column; gap: 0; }
  .kcard-body .kv-row { margin-bottom: 6px; padding: 6px 9px; }
  .kcard-body .kv-row .k { min-width: 64px; }
  .kcard-body .kv-row input { flex: 1; min-width: 0; background: transparent; border: none; color: var(--text); font: inherit; font-size: 12px; padding: 0; }
  .kcard-body .kv-row input:focus { outline: none; }
  .kcard-body .kv-row input::placeholder { color: var(--faint); }
  .kc-models-block { margin-top: 6px; }
  .kc-models-block .lbl { font-size: 11px; font-weight: 700; color: var(--muted); margin-bottom: 6px; }
  .kc-models-block .model-tag { font-size: 10.5px; padding: 3px 8px; }
  .model-tag[hidden] { display: none; }
  .models-scroll { max-height: 230px; overflow-y: auto; padding: 6px; border: 1px solid var(--line);
    border-radius: 8px; background: var(--surface-2); align-content: flex-start; }
  .models-search { width: 100%; margin-bottom: 6px; padding: 6px 10px; font: inherit; font-size: 12px;
    border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--text); }
  .models-search:focus { outline: none; border-color: var(--accent); }
  .models-none { font-size: 11px; color: var(--faint); padding: 6px 2px; }
  .models-more { margin-top: 6px; background: none; border: none; padding: 2px 0; font: inherit; font-size: 11.5px;
    font-weight: 700; color: var(--accent); cursor: pointer; }
  .models-more:hover { text-decoration: underline; }
  .to-top { position: fixed; bottom: 22px; inset-inline-end: 22px; z-index: 50; width: 44px; height: 44px;
    border-radius: 50%; border: 1px solid var(--line); background: var(--surface-2); color: var(--text);
    font-size: 18px; cursor: pointer; box-shadow: var(--shadow-sm); opacity: 0; transform: translateY(12px);
    pointer-events: none; transition: var(--trans); }
  .to-top.show { opacity: 1; transform: none; pointer-events: auto; }
  .to-top:hover { border-color: var(--accent); color: var(--accent); }
  .kc-error { color: var(--bad); font-size: 12px; margin-top: 6px; overflow-wrap: anywhere; }
  .kcard-foot { display: flex; align-items: center; gap: 6px; padding: 10px 16px; border-top: 1px solid var(--line-soft); margin-top: auto;
    background: var(--surface-2); border-radius: 0 0 var(--radius) var(--radius); flex-wrap: wrap; }
  .kcard-foot .spacer { flex: 1; }
  .kcard-foot .icon-act { width: 34px; height: 34px; border-radius: 9px; border: 1px solid var(--line); background: var(--surface);
    color: var(--text); cursor: pointer; transition: var(--trans); font-size: 14px; }
  .kcard-foot .icon-act:hover { border-color: var(--accent); color: var(--accent); }
  .kcard-foot .icon-act.danger:hover { border-color: var(--bad); color: var(--bad); }
  .kcard-foot .copy-dd > button { padding: 8px 12px; font-size: 12px; }
  .kcard-foot .copy-dd .dd-menu { top: auto; bottom: calc(100% + 6px); }
  .keys-empty { grid-column: 1 / -1; }

  /* ---------- AGENTS: custom agents + icons ---------- */
  .agents-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
  .agents-head .modal-desc { margin: 0; flex: 1; min-width: 240px; }
  .agents-head .primary { width: auto; padding: 10px 18px; }
  .agent-ic { width: 44px; height: 44px; border-radius: 12px; border: 1px solid var(--line); background: var(--surface-3); cursor: pointer;
    display: flex; align-items: center; justify-content: center; font-size: 21px; padding: 0; position: relative; overflow: hidden; flex-shrink: 0; transition: var(--trans); }
  .agent-ic img { width: 100%; height: 100%; object-fit: cover; }
  .agent-ic::after { content: '✏️'; position: absolute; inset: 0; background: rgba(0,0,0,.55); display: flex; align-items: center; justify-content: center;
    font-size: 14px; opacity: 0; transition: var(--trans); }
  .agent-ic:hover::after { opacity: 1; }
  .agent-ic:hover { border-color: var(--accent); }
  .icon-menu { position: fixed; z-index: 400; display: none; flex-direction: column; gap: 2px; min-width: 180px; padding: 6px;
    background: var(--surface-3); border: 1px solid var(--line); border-radius: var(--radius-sm); box-shadow: var(--shadow); }
  .icon-menu.open { display: flex; animation: pop .14s ease; }
  .icon-menu button { border: none; background: transparent; color: var(--text); font: inherit; font-size: 13px; text-align: start;
    padding: 9px 10px; border-radius: var(--radius-xs); cursor: pointer; }
  .icon-menu button:hover:not(:disabled) { background: var(--accent-soft); color: var(--accent); }
  .icon-menu button:disabled { opacity: .4; cursor: not-allowed; }
  .agent-card .top .card-acts { display: flex; gap: 4px; }
  .agent-card .top .card-acts button { width: 30px; height: 30px; border-radius: 8px; border: 1px solid var(--line); background: var(--surface-2);
    color: var(--text); cursor: pointer; font-size: 12px; }
  .agent-card .top .card-acts button:hover { border-color: var(--accent); }
  .agent-card .kind { font-size: 10.5px; font-weight: 800; padding: 2px 8px; border-radius: 6px; background: var(--accent-soft); color: var(--accent); }
  .agent-card .fields { display: grid; grid-template-columns: auto 1fr; gap: 3px 10px; font-size: 11.5px; font-family: var(--font-mono); }
  .agent-card .fields .fk { color: var(--muted); }
  .agent-card .fields .fv { overflow-wrap: anywhere; }
  .desktop-guide { margin-top: 16px; }
  .agent-preview { font-family: var(--font-mono); font-size: 11.5px; color: var(--muted); background: var(--surface-2); border: 1px solid var(--line-soft);
    border-radius: var(--radius-sm); padding: 10px 12px; margin: 4px 0 14px; white-space: pre-wrap; }
  /* ---------- AGENTS — model dropdown (search + ☑ which models show in the agent) ---------- */
  .agent-model-block { display:flex; flex-direction:column; gap:6px; }
  .mdd { position: relative; }
  .mdd-field { display:flex; align-items:center; gap:8px; width:100%; background:var(--surface-2); border:1px solid var(--line);
    color:var(--text); border-radius:var(--radius-sm); padding:9px 10px; font-family:var(--font-mono); font-size:12px;
    cursor:pointer; text-align:start; transition:var(--trans); }
  .mdd-field:hover:not(:disabled), .mdd.open .mdd-field { border-color:var(--accent); }
  .mdd-field:disabled { opacity:.5; cursor:not-allowed; }
  .mdd-field .mdd-val { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .mdd-field .mdd-badge { font-size:10.5px; color:var(--accent); background:var(--accent-soft); padding:1px 7px; border-radius:6px; white-space:nowrap; }
  .mdd-field .mdd-chev { width:14px; height:14px; color:var(--muted); flex-shrink:0; transition:transform .15s; }
  .mdd.open .mdd-chev { transform:rotate(180deg); }
  .mdd-panel { display:none; position:absolute; top:calc(100% + 4px); inset-inline:0; z-index:60; background:var(--surface);
    border:1px solid var(--accent); border-radius:var(--radius-sm); box-shadow:0 12px 32px rgba(0,0,0,.35); overflow:hidden; }
  .mdd.open .mdd-panel { display:block; }
  .mdd-search { display:block; width:100%; box-sizing:border-box; background:var(--surface-2); border:none; border-bottom:1px solid var(--line);
    color:var(--text); padding:9px 11px; font-family:var(--font-mono); font-size:12.5px; outline:none; }
  .mdd-search::placeholder { color:var(--faint); }
  .mdd-list { max-height:280px; overflow:auto; }
  .mdd-row { display:flex; align-items:center; gap:9px; padding:7px 11px; cursor:pointer; font-family:var(--font-mono); font-size:12px; }
  .mdd-row:hover { background:var(--surface-3); }
  .mdd-row.sel { background:var(--accent-soft); color:var(--accent); font-weight:700; }
  .mdd-row input { width:14px; height:14px; margin:0; accent-color:var(--accent); flex-shrink:0; cursor:pointer; }
  .mdd-row .mdd-name { flex:1; min-width:0; overflow-wrap:anywhere; text-align:start; }
  .mdd-empty { padding:12px; color:var(--muted); font-size:12px; text-align:center; }
  .mdd-foot { display:flex; align-items:center; gap:6px; padding:7px 10px; border-top:1px solid var(--line); background:var(--surface-2); font-size:11px; color:var(--muted); }
  .mdd-foot .mdd-count { flex:1; }
  .mdd-foot button { background:none; border:1px solid var(--line); color:var(--text); border-radius:6px; padding:3px 8px; font:inherit; font-size:11px; cursor:pointer; }
  .mdd-foot button:hover { border-color:var(--accent); color:var(--accent); }
  .agent-filter-info { font-size:11px; color:var(--accent); font-weight:700; }
  .effort-row { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--muted); }
  .effort-row span { white-space:nowrap; }
  .agent-card .effort-row select { font-family:inherit; }
  .effort-na { font-size:11px; color:var(--faint); }
  /* ---------- PHONE ACCESS (QR) + TELEGRAM ---------- */
  .qr-box { background:#fff; border-radius:var(--radius-sm); padding:10px; width:min(260px, 100%); margin:0 auto 12px; }
  .qr-box svg { display:block; width:100%; height:auto; }
  .phone-url { display:flex; align-items:center; gap:6px; background:var(--surface-2); border:1px solid var(--line);
    border-radius:var(--radius-sm); padding:6px 8px; margin-bottom:12px; }
  .phone-url code { flex:1; min-width:0; font-size:11.5px; overflow-wrap:anywhere; color:var(--text); }
  .phone-help { font-size:12px; color:var(--muted); margin:0 0 14px; }
  .phone-help summary { cursor:pointer; }
  .phone-help code { font-size:11px; overflow-wrap:anywhere; }
  .phone-actions { display:flex; gap:8px; flex-wrap:wrap; }
  .phone-actions button { width:auto; padding:10px 16px; }
  button.ghost.danger { color:var(--bad); border-color:color-mix(in srgb, var(--bad) 45%, var(--line)); }
  .tg-box { border-top:1px solid var(--line-soft); margin-top:4px; }
  .tg-box .setting-row { border-bottom:none; }
  .tg-saved { color:var(--ok); font-weight:600; font-family:var(--font-mono); font-size:11px; }
  .tg-inline { display:flex; gap:8px; }
  .tg-inline input { flex:1; min-width:0; }
  .tg-inline button { width:auto; padding:0 14px; white-space:nowrap; }
  .tg-actions { display:flex; gap:8px; margin-bottom:12px; }
  .tg-actions button { width:auto; padding:10px 18px; }
  .tg-help { font-size:12px; margin:0; }
  .tg-sub { border-bottom:none; padding-top:0; }
  .tg-sub input[type=time] { background:var(--surface-2); border:1px solid var(--line); color:var(--text);
    border-radius:var(--radius-sm); padding:6px 8px; font-family:var(--font-mono); font-size:12px; }
  .key-row .quota { grid-column: 2 / -1; font-size:11px; color:var(--muted); margin-top:-4px; }
  .key-row .quota b { font-family:var(--font-mono); font-weight:600; color:var(--text); }
  .key-row .quota .low, .key-row .quota .low b { color:var(--warn); font-weight:700; }
  .key-row .quota .q-at { color:var(--faint); }
  /* ---------- ALERT HISTORY ---------- */
  .ah-card { margin-top:16px; }
  .ah-filters { display:flex; gap:6px; flex-wrap:wrap; }
  .ah-filters select { background:var(--surface-2); border:1px solid var(--line); color:var(--text);
    border-radius:var(--radius-sm); padding:6px 8px; font:inherit; font-size:12px; }
  .ah-counts { margin-bottom:10px; }
  .ah-counts td, .ah-counts th { text-align:center; white-space:nowrap; }
  .ah-counts td:first-child { text-align:start; font-weight:700; }
  .ah-row { display:grid; grid-template-columns: 150px auto 120px minmax(0, 1fr); gap:10px; align-items:center;
    padding:8px 16px; border-top:1px solid var(--line-soft); font-size:12px; }
  .ah-time { font-size:11px; color:var(--muted); }
  .ah-src { font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ah-msg { color:var(--muted); overflow-wrap:anywhere; }
  .ah-more { padding:8px 16px; color:var(--faint); font-size:12px; }
  @media (max-width: 700px) { .key-row .acts { grid-column: 3; grid-row: 1; } .key-row .quota { grid-column: 2 / 4; margin-top: 0; } }
  @media (max-width: 700px) { .ah-row { grid-template-columns: auto minmax(0, 1fr); } .ah-row .ah-msg { grid-column: 1 / -1; } }
  /* ---------- PHONE LAYOUT (≤520px): compact header, nothing wider than the screen ---------- */
  .key-grid > *, .agent-grid > * { min-width: 0; }
  @media (max-width: 700px) { .key-grid, .agent-grid { grid-template-columns: minmax(0, 1fr); } }
  @media (max-width: 520px) {
    header { gap: 8px; padding: 10px 12px; }
    header .logo { width: 34px; height: 34px; border-radius: 10px; }
    header .logo svg { width: 34px; height: 34px; }
    header .titles { min-width: 0; }
    header .titles h1 { font-size: 14px; }
    header .top-actions { gap: 4px; }
    .icon-btn { width: 32px; height: 32px; font-size: 14px; border-radius: 9px; }
    .kcard-head { flex-wrap: wrap; padding: 12px 12px 8px; }
    .kcard-head .pill { white-space: normal; }
    .kcard-body { padding: 0 12px 10px; }
    .kcard-foot { padding: 10px 12px; }
    .modal { padding: 16px; }
    .tg-actions { flex-wrap: wrap; }
    .stat-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .stat { padding: 12px 14px; }
    .stat .num { font-size: 24px; }
  }
  @media (max-width: 400px) { header .titles { display: none; } }

  @media (max-width: 700px) { .key-row { grid-template-columns: 22px 1fr auto; } .key-row .err, .key-row .st { grid-column: 2 / 4; } }
</style>
</head>
<body>

<header>
  <div class="logo"><svg width="40" height="40" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"> <defs> <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"> <stop offset="0" stop-color="#6D8BFF"/><stop offset="1" stop-color="#9B6DFF"/> </linearGradient> </defs> <rect width="64" height="64" rx="15" fill="url(#bg)"/> <g fill="#fff"> <rect x="15" y="14" width="8" height="36" rx="2.5"/> <rect x="15" y="14" width="32" height="8" rx="2.5"/> <rect x="15" y="42" width="32" height="8" rx="2.5"/> </g> <g fill="#3EE6A8"> <rect x="26" y="28" width="12" height="8" rx="2.5"/> <path d="M36 23.5 L49.5 32 L36 40.5 Z" stroke="#3EE6A8" stroke-width="2" stroke-linejoin="round"/> </g> </svg></div>
  <div class="titles">
    <h1>EskaGate</h1>
    <p>{{t:app.tagline}}</p>
  </div>
  <div class="top-actions">
    <select class="lang-select" id="langSelect" title="{{t:settings.lang}}" aria-label="{{t:settings.lang}}" onchange="setLang(this.value)">
      <option value="ar">{{t:lang.ar}}</option><option value="en">{{t:lang.en}}</option><option value="fr">{{t:lang.fr}}</option>
    </select>
    <button class="icon-btn" id="toastToggleBtn" title="{{t:hdr.toasts}}" onclick="toggleToasts()">🔔</button>
    <button class="icon-btn" id="themeToggleBtn" title="{{t:hdr.theme}}" onclick="toggleTheme()">🌙</button>
    <button class="icon-btn" title="{{t:nav.keys}}" onclick="showTab('keys')">🔑</button>
    <button class="icon-btn" title="{{t:hdr.settings}}" onclick="openSettings()">⚙️</button>
    <button class="icon-btn" id="phoneBtn" title="{{t:phone.btn_title}}" onclick="openPhone()">📱</button>
  </div>
</header>

<button class="to-top" id="toTop" title="{{t:app.to_top}}" onclick="window.scrollTo({ top: 0, behavior: 'smooth' })">⬆</button>
<div class="app">

  <!-- ============ TABS ============ -->
  <nav class="tabs" role="tablist" aria-orientation="vertical">
    <button class="nav-toggle" id="navToggle" type="button" onclick="toggleNav()" title="{{t:nav.toggle}}" aria-expanded="true">☰</button>
    <button class="tab-btn on" id="tabBtn-test" role="tab" title="{{t:nav.test}}" onclick="showTab('test')"><span class="ic">🧪</span><span class="lbl">{{t:nav.test}}</span></button>
    <button class="tab-btn" id="tabBtn-keys" role="tab" title="{{t:nav.keys}}" onclick="showTab('keys')"><span class="ic">🔑</span><span class="lbl">{{t:nav.keys_short}}</span><span class="count" id="statKeys">0</span></button>
    <button class="tab-btn" id="tabBtn-providers" role="tab" title="{{t:nav.providers}}" onclick="showTab('providers')"><span class="ic">🔌</span><span class="lbl">{{t:nav.providers}}</span></button>
    <button class="tab-btn" id="tabBtn-gateway" role="tab" title="{{t:nav.gateway}}" onclick="showTab('gateway')"><span class="ic">🚪</span><span class="lbl">{{t:nav.gateway}}</span></button>
    <button class="tab-btn" id="tabBtn-agents" role="tab" title="{{t:nav.agents}}" onclick="showTab('agents')"><span class="ic">🤖</span><span class="lbl">{{t:nav.agents}}</span></button>
  </nav>

  <!-- ================================================================
       TAB 1 — TEST
  ================================================================= -->
  <div class="tab-panel" id="tab-test">

    <!-- TOP STATS -->
    <div class="stat-strip">
      <div class="stat ok"><div class="num" id="statWorking">0</div><div class="label">{{t:test.stat_working}}</div></div>
      <div class="stat bad"><div class="num" id="statFailed">0</div><div class="label">{{t:test.stat_failed}}</div></div>
      <div class="stat accent"><div class="num" id="statTotal">0</div><div class="label">{{t:test.stat_total}}</div></div>
      <div class="stat"><div class="num" id="statTime">0s</div><div class="label">{{t:test.stat_time}}</div></div>
    </div>

    <div class="test-layout">
      <!-- ---------- SETUP COLUMN ---------- -->
      <aside class="setup-col">
        <section class="control-panel connection-panel">
          <div class="control-panel-title"><span>{{t:test.connection}}</span></div>
          <div class="field">
            <label>Base URL</label>
            <div class="input-with-btn">
              <input id="base_url" dir="ltr" placeholder="https://api.example.com/v1">
              <button type="button" class="btn-icon" title="{{t:common.paste}}" onclick="pasteInto('base_url')">📥</button>
              <button type="button" class="btn-icon" title="{{t:common.copy}}" onclick="copyField('base_url', this)">📋</button>
            </div>
          </div>
          <div class="field">
            <label>API Key</label>
            <div class="input-with-btn">
              <input id="api_key" dir="ltr" type="password" placeholder="sk-..." autocomplete="off">
              <button type="button" class="btn-icon" title="{{t:common.show_hide}}" onclick="toggleKeyField(this)">👁</button>
              <button type="button" class="btn-icon" title="{{t:common.paste}}" onclick="pasteInto('api_key')">📥</button>
              <button type="button" class="btn-icon" title="{{t:common.copy}}" onclick="copyField('api_key', this)">📋</button>
            </div>
          </div>
          <div class="field">
            <label>{{t:test.provider_id}}</label>
            <input id="provider_id" dir="ltr" placeholder="{{t:test.provider_id_ph}}">
          </div>
          <button class="primary" id="runBtn" onclick="runOrStop()">{{t:test.run}}</button>
          <div class="hint">{{t:test.shortcut}} <kbd>Ctrl</kbd> + <kbd>Enter</kbd></div>
        </section>

        <details class="control-panel" id="advancedSection">
          <summary><span>{{t:test.advanced}}</span><span class="summary-note" id="advancedNote"></span></summary>
          <div class="advanced-fields">
            <div class="field wide">
              <label>{{t:test.manual_models}}</label>
              <input id="models" dir="ltr" placeholder="gpt-4o-mini, llama-3-70b">
            </div>
            <div class="field wide">
              <label>{{t:test.filter}}</label>
              <input id="filter" dir="ltr" placeholder="gpt|claude">
            </div>
            <div class="field"><label>Timeout (s)</label><input id="timeout" type="number" min="1" value="15"></div>
            <div class="field"><label>Retries</label><input id="retries" type="number" min="0" value="2"></div>
            <div class="field"><label>Workers</label><input id="workers" type="number" min="1" max="32" value="8"></div>
            <div class="field"><label>Repeat</label><input id="repeat" type="number" min="1" value="1"></div>
            <div class="checkbox wide">
              <input id="capabilities" type="checkbox">
              <label for="capabilities" style="margin:0;">{{t:test.capabilities}}</label>
            </div>
          </div>
        </details>

        <section class="control-panel">
          <div class="control-panel-title">{{t:test.saved_profiles}}</div>
          <div class="field">
            <select id="savedProfiles" onchange="applyProfile()">
              <option value="">{{t:test.pick_profile}}</option>
            </select>
          </div>
          <div class="field">
            <label>{{t:test.source}}</label>
            <input id="source" placeholder="{{t:test.source_ph}}">
          </div>
          <div class="profile-actions">
            <button class="ghost" onclick="saveProfile()">{{t:test.save_current}}</button>
            <button class="ghost" onclick="deleteProfile()">{{t:test.delete_selected}}</button>
            <button class="ghost" onclick="openFormats()">{{t:test.formats}}</button>
            <button class="ghost" onclick="clearAllFields()">{{t:test.clear_fields}}</button>
          </div>
        </section>
      </aside>

      <!-- ---------- RESULTS COLUMN ---------- -->
      <main class="results-area">
        <div class="status-line" id="statusLine">
          <span class="spin"></span>
          <span id="statusText">{{t:test.ready}}</span>
        </div>

        <!-- SUMMARY (shows after a run) -->
        <div class="result-banner" id="resultBanner">
          <div class="big" id="bannerBig">0/0</div>
          <div class="txt">
            <strong id="bannerTitle"></strong>
            <span id="bannerSub"></span>
          </div>
          <div class="actions">
            <div class="dd" id="copyDD">
              <button class="dd-btn" onclick="toggleCopyDD(event)">{{t:test.copy_config}} <span class="caret">⌄</span></button>
              <div class="dd-menu" id="copyDDMenu"></div>
            </div>
            <button class="ghost" id="dlReport" disabled onclick="downloadReport()">⬇ report.json</button>
          </div>
        </div>

        <!-- TABLE -->
        <div class="card" style="margin-bottom:16px;">
          <div class="card-head">
            <h3>{{t:test.results}}</h3>
            <div class="spacer"></div>
            <div class="filters">
              <button class="chip on" data-filter="all" onclick="setResultFilter('all')">{{t:common.all}}<span class="n" id="fcAll">0</span></button>
              <button class="chip" data-filter="ok" onclick="setResultFilter('ok')">{{t:common.ok_filter}}<span class="n" id="fcOk">0</span></button>
              <button class="chip" data-filter="bad" onclick="setResultFilter('bad')">{{t:test.f_bad}}<span class="n" id="fcBad">0</span></button>
              <input class="search-input" id="resultSearch" dir="ltr" placeholder="{{t:test.search_ph}}" oninput="applyResultFilter()">
            </div>
          </div>
          <div style="overflow-x:auto;">
            <table>
              <thead>
                <tr>
                  <th>{{t:test.th_model}}</th><th>{{t:test.th_status}}</th><th>{{t:test.th_time}}</th><th title="{{t:test.th_tps}}">tokens/s</th><th>{{t:test.th_caps}}</th>
                </tr>
              </thead>
              <tbody id="tbody">
                <tr class="empty-row"><td colspan="5">{{t:test.empty_rows}}</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- CHART -->
        <div class="card">
          <div class="card-head">
            <h3>{{t:test.chart_title}}</h3>
            <div class="spacer"></div>
            <span style="color:var(--muted); font-size:11.5px;">{{t:test.chart_note}}</span>
          </div>
          <div class="chart-wrap">
            <div class="chart" id="chart"><div class="chart-empty" style="width:100%;">{{t:test.chart_empty}}</div></div>
          </div>
        </div>
      </main>
    </div>
  </div>

  <!-- ================================================================
       TAB 2 — KEYS + MONITOR
  ================================================================= -->
  <div class="tab-panel" id="tab-keys" hidden>
    <div class="stat-strip">
      <div class="stat accent"><div class="num" id="ksTotal">0</div><div class="label">{{t:keys.stat_saved}}</div></div>
      <div class="stat ok"><div class="num" id="ksOk">0</div><div class="label">{{t:keys.stat_ok}}</div></div>
      <div class="stat warn"><div class="num" id="ksWarn">0</div><div class="label">{{t:keys.stat_warn}}</div></div>
      <div class="stat bad"><div class="num" id="ksBad">0</div><div class="label">{{t:keys.stat_bad}}</div></div>
    </div>

    <section class="control-panel monitor-panel">
      <div class="mon-title">
        <span class="pulse paused" id="livePulse"></span>
        <div>
          <div class="live-label" id="liveLabel">{{t:keys.monitor}}</div>
          <div class="countdown" id="monitorNextRun">{{t:keys.monitor_desc}}</div>
        </div>
      </div>
      <div class="mon-controls">
        <span class="mon-lbl">{{t:keys.every}}</span>
        <div class="seg" id="intervalSeg">
          <button data-min="1" onclick="setMonitorInterval(1)">1 {{t:unit.min}}</button>
          <button data-min="5" class="on" onclick="setMonitorInterval(5)">5 {{t:unit.min}}</button>
          <button data-min="10" onclick="setMonitorInterval(10)">10 {{t:unit.min}}</button>
          <button data-min="30" onclick="setMonitorInterval(30)">30 {{t:unit.min}}</button>
        </div>
        <button class="ghost" id="liveToggleBtn" onclick="toggleMonitor()">{{t:keys.monitor_start}}</button>
        <button class="ghost" id="checkAllBtn" onclick="checkAllProfiles()">{{t:keys.check_all}}</button>
      </div>
    </section>

    <div class="keys-toolbar">
      <div class="filters">
        <button class="chip on" data-kf="all" onclick="setKeyFilter('all')">{{t:common.all}}<span class="n" id="kfAll">0</span></button>
        <button class="chip" data-kf="ok" onclick="setKeyFilter('ok')">{{t:common.ok_filter}}<span class="n" id="kfOk">0</span></button>
        <button class="chip" data-kf="warn" onclick="setKeyFilter('warn')">{{t:keys.f_warn}}<span class="n" id="kfWarn">0</span></button>
        <button class="chip" data-kf="bad" onclick="setKeyFilter('bad')">{{t:keys.f_bad}}<span class="n" id="kfBad">0</span></button>
        <button class="chip" data-kf="idle" onclick="setKeyFilter('idle')">{{t:keys.f_idle}}<span class="n" id="kfIdle">0</span></button>
        <input class="search-input" id="keySearch" placeholder="{{t:keys.search_ph}}" oninput="renderArchive()">
      </div>
      <div class="toolbar-actions">
        <button class="ghost" onclick="openFormats()">{{t:keys.custom_formats}}</button>
        <button class="primary" onclick="goAddKey()">{{t:keys.add_key}}</button>
      </div>
    </div>
    <div id="archiveBody"></div>
  </div>

  <!-- ================================================================
       TAB 3 — PROVIDERS (gateway)
  ================================================================= -->
  <div class="tab-panel" id="tab-providers" hidden>
    <div class="test-layout">
      <aside class="setup-col">
        <section class="control-panel connection-panel">
          <div class="control-panel-title"><span id="provFormTitle">{{t:prov.new}}</span>
            <button class="ghost sm" id="provFormCancel" style="width:auto; display:none;" onclick="resetProviderForm()">{{t:common.cancel}}</button></div>
          <input type="hidden" id="prov_id">
          <div class="field"><label>{{t:prov.name_label}}</label><input id="prov_name" dir="ltr" placeholder="openrouter"></div>
          <div class="field"><label>Base URL</label><input id="prov_base" dir="ltr" placeholder="https://api.example.com/v1"></div>
          <div class="field"><label>{{t:prov.format_label}}</label>
            <select id="prov_format">
              <option value="openai">OpenAI-compatible (/chat/completions)</option>
              <option value="anthropic">Anthropic (/v1/messages)</option>
            </select></div>
          <div class="field"><label>{{t:test.manual_models}}</label><input id="prov_models" dir="ltr" placeholder="{{t:prov.models_ph}}"></div>
          <button class="primary" onclick="saveProviderForm()">{{t:prov.save}}</button>
        </section>
        <section class="control-panel">
          <div class="control-panel-title">{{t:prov.import}}</div>
          <p class="modal-desc" style="margin:0 0 10px;">{{t:prov.import_desc}}</p>
          <button class="ghost" onclick="importProfilesToGateway()">{{t:prov.import_btn}}</button>
        </section>
      </aside>
      <main class="results-area" id="providersList"></main>
    </div>
  </div>

  <!-- ================================================================
       TAB 4 — GATEWAY (local key + endpoints + logs)
  ================================================================= -->
  <div class="tab-panel" id="tab-gateway" hidden>
    <div class="gw-grid">
      <section class="control-panel connection-panel">
        <div class="control-panel-title"><span>{{t:gw.local_key_title}}</span>
          <button class="ghost sm" style="width:auto;" onclick="regenerateLocalKey()">{{t:gw.new_key}}</button></div>
        <div class="kv-row"><span class="k">{{t:gw.local_key}}</span><code class="v" id="gwLocalKey" dir="ltr">…</code>
          <button class="mini-copy" title="{{t:common.show_hide}}" onclick="toggleLocalKey()">👁</button>
          <button class="mini-copy" title="{{t:common.copy}}" onclick="copyRaw(gwState && gwState.local_key, this)">📋</button></div>
        <div class="kv-row"><span class="k">OpenAI base</span><code class="v" id="gwOpenAI" dir="ltr">…</code>
          <button class="mini-copy" title="{{t:common.copy}}" onclick="copyRaw(gwState && gwState.openai_base, this)">📋</button></div>
        <div class="kv-row"><span class="k">Anthropic base</span><code class="v" id="gwAnthropic" dir="ltr">…</code>
          <button class="mini-copy" title="{{t:common.copy}}" onclick="copyRaw(gwState && gwState.anthropic_base, this)">📋</button></div>
        <p class="modal-desc" style="margin:10px 0 0;">{{t:gw.desc}}</p>
      </section>
      <section class="control-panel">
        <div class="control-panel-title"><span>{{t:gw.models_title}}</span><span id="gwModelCount" class="summary-note"></span></div>
        <div class="model-tags" id="gwModels"></div>
        <p class="modal-desc" style="margin:10px 0 0;">{{h:gw.models_hint}}</p>
      </section>
    </div>
    <div class="card">
      <div class="card-head">
        <h3>{{t:gw.log}}</h3>
        <div class="spacer"></div>
        <div class="filters">
          <button class="chip on" data-lf="all" onclick="setLogFilter('all')">{{t:common.all}}</button>
          <button class="chip" data-lf="ok" onclick="setLogFilter('ok')">{{t:gw.f_ok}}</button>
          <button class="chip" data-lf="error" onclick="setLogFilter('error')">{{t:gw.f_err}}</button>
          <button class="ghost sm" style="width:auto;" onclick="clearGatewayLogs()">{{t:gw.clear}}</button>
        </div>
      </div>
      <div style="overflow-x:auto;">
        <table>
          <thead><tr><th>{{t:gw.th_time}}</th><th>{{t:gw.th_client}}</th><th>{{t:test.th_model}}</th><th>{{t:gw.th_provkey}}</th><th>{{t:test.th_status}}</th><th>tokens</th><th>{{t:gw.th_duration}}</th></tr></thead>
          <tbody id="logsBody"><tr class="empty-row"><td colspan="7">{{t:gw.no_requests}}</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card ah-card">
      <div class="card-head">
        <h3>{{t:ah.title}}</h3>
        <div class="spacer"></div>
        <div class="ah-filters">
          <select id="ahSource" onchange="renderAlertHistory()"><option value="">{{t:ah.all_sources}}</option></select>
          <select id="ahType" onchange="renderAlertHistory()"><option value="">{{t:ah.all_types}}</option></select>
          <select id="ahPeriod" onchange="renderAlertHistory()">
            <option value="month">{{t:ah.month}}</option><option value="7">{{t:ah.days7}}</option>
            <option value="30">{{t:ah.days30}}</option><option value="all">{{t:ah.all}}</option></select>
        </div>
      </div>
      <div id="ahCounts"></div>
      <div id="ahList"></div>
    </div>
  </div>

  <!-- ================================================================
       TAB 5 — AGENTS
  ================================================================= -->
  <div class="tab-panel" id="tab-agents" hidden>
    <div class="agents-head">
      <p class="modal-desc">{{t:agents.desc}}</p>
      <button class="primary" onclick="openAgentForm()">{{t:agents.add}}</button>
    </div>
    <div class="agent-grid" id="agentsList"></div>
    <section class="control-panel desktop-guide">
      <div class="control-panel-title"><span>{{t:agents.desktop_title}}</span><span class="summary-note">{{t:agents.desktop_note}}</span></div>
      <ol class="modal-desc" style="margin:0 0 10px; padding-inline-start:18px; line-height:1.9;">
        <li>{{h:agents.dg1}}</li>
        <li>{{h:agents.dg2}}</li>
        <li>{{h:agents.dg3}}</li>
        <li>{{t:agents.dg4}}</li>
      </ol>
      <div class="kv-row"><span class="k">Gateway base URL</span><code class="v" id="dgBase" dir="ltr">…</code>
        <button class="mini-copy" title="{{t:common.copy}}" onclick="copyRaw(gwState && gwState.anthropic_base, this)">📋</button></div>
      <div class="kv-row"><span class="k">Gateway API key</span><code class="v" id="dgKey" dir="ltr">…</code>
        <button class="mini-copy" title="{{t:common.copy}}" onclick="copyRaw(gwState && gwState.local_key, this)">📋</button></div>
      <div class="kv-row"><span class="k">Auth scheme</span><code class="v" dir="ltr">bearer</code></div>
      <p class="modal-desc" style="margin:8px 0 0;">{{t:agents.dg_note}}</p>
    </section>
  </div>
</div>


<!-- ============ CUSTOM AGENT MODAL ============ -->
<div class="modal-overlay" id="agentOverlay" onclick="if(event.target===this) closeAgentForm()">
  <div class="modal" style="width: min(620px, 100%);">
    <div class="modal-header">
      <h2 id="agentFormTitle">{{t:agents.form_add}}</h2>
      <button class="modal-close" onclick="closeAgentForm()">{{t:common.close}}</button>
    </div>
    <p class="modal-desc">{{t:agents.form_desc}}</p>
    <input type="hidden" id="ag_id">
    <div class="row2">
      <div class="field"><label>{{t:agents.name}}</label><input id="ag_name" placeholder="{{t:agents.name_ph}}"></div>
      <div class="field"><label>{{t:agents.kind}}</label>
        <select id="ag_kind" onchange="updateAgentFormHints()">
          <option value="json">{{t:agents.kind_json}}</option>
          <option value="env">.env (KEY=VALUE)</option>
        </select></div>
    </div>
    <div class="field"><label>{{t:agents.path}}</label><input id="ag_path" dir="ltr" placeholder="~/.config/tool/config.json"></div>
    <div class="field"><label>{{t:agents.format}}</label>
      <select id="ag_format" onchange="updateAgentFormHints()">
        <option value="openai">{{t:agents.fmt_openai}}</option>
        <option value="anthropic">{{t:agents.fmt_anthropic}}</option>
      </select></div>
    <div class="field"><label>{{t:agents.f_base}}</label><input id="ag_f_base" dir="ltr"></div>
    <div class="field"><label>{{t:agents.f_key}}</label><input id="ag_f_key" dir="ltr"></div>
    <div class="field"><label>{{t:agents.f_model}}</label><input id="ag_f_model" dir="ltr"></div>
    <div class="agent-preview" id="agentPreview" dir="ltr"></div>
    <button class="primary" onclick="saveAgentForm()">{{t:agents.save}}</button>
  </div>
</div>

<!-- icon picker -->
<div class="icon-menu" id="iconMenu">
  <button onclick="pickIconFile()">{{t:icon.file}}</button>
  <button onclick="pickIconEmoji()">{{t:icon.emoji}}</button>
  <button id="iconResetBtn" onclick="resetIcon()">{{t:icon.reset}}</button>
</div>
<input type="file" id="iconFile" accept="image/*" style="display:none" onchange="iconFileChosen(this)">

<!-- ============ FORMATS MODAL ============ -->
<div class="modal-overlay" id="formatsOverlay" onclick="if(event.target===this) closeFormats()">
  <div class="modal">
    <div class="modal-header">
      <h2>{{t:formats.title}}</h2>
      <button class="modal-close" onclick="closeFormats()">{{t:common.close}}</button>
    </div>
    <p class="modal-desc">{{t:formats.desc}}</p>
    <div class="field">
      <label>{{t:formats.name}}</label>
      <input id="formatName" placeholder="hermes">
    </div>
    <div class="field">
      <label>{{t:formats.example}}</label>
      <textarea id="formatTemplate" rows="10" dir="ltr"
        style="width:100%; background: var(--surface-2); border:1px solid var(--line); color: var(--text);
               padding:9px 10px; border-radius:6px; font:inherit; resize:vertical;"
        placeholder='{"providers": {"agentrouter": {"options": {"baseURL": "...", "apiKey": "..."}, "models": {...}}}}'></textarea>
    </div>
    <button class="primary" onclick="saveFormat()">{{t:formats.save}}</button>
    <div id="formatsListBody" style="margin-top:20px;"></div>
  </div>
</div>

<!-- ============ SETTINGS MODAL ============ -->
<div class="modal-overlay" id="phoneOverlay" onclick="if(event.target===this) closePhone()">
  <div class="modal" style="width: min(440px, 100%);">
    <div class="modal-header">
      <h2>{{t:phone.title}}</h2>
      <button class="modal-close" onclick="closePhone()">{{t:common.close}}</button>
    </div>
    <div id="phoneBody"></div>
  </div>
</div>

<div class="modal-overlay" id="settingsOverlay" onclick="if(event.target===this) closeSettings()">
  <div class="modal" style="width: min(520px, 100%);">
    <div class="modal-header">
      <h2>{{t:settings.title}}</h2>
      <button class="modal-close" onclick="closeSettings()">{{t:common.close}}</button>
    </div>
    <div class="setting-row">
      <div>
        <div class="s-label">{{t:settings.lang}}</div>
        <div class="s-sub">{{t:settings.lang_sub}}</div>
      </div>
      <div class="seg" id="langSeg">
        <button data-lang="ar" onclick="setLang('ar')">{{t:lang.ar}}</button>
        <button data-lang="en" onclick="setLang('en')">{{t:lang.en}}</button>
        <button data-lang="fr" onclick="setLang('fr')">{{t:lang.fr}}</button>
      </div>
    </div>
    <div class="setting-row">
      <div>
        <div class="s-label">{{t:settings.theme}}</div>
        <div class="s-sub">{{t:settings.theme_sub}}</div>
      </div>
      <div class="theme-seg" id="themeSeg">
        <button data-theme="dark" onclick="setTheme('dark')">{{t:settings.dark}}</button>
        <button data-theme="light" onclick="setTheme('light')">{{t:settings.light}}</button>
      </div>
    </div>
    <div class="setting-row">
      <div>
        <div class="s-label">{{t:settings.toasts}}</div>
        <div class="s-sub">{{t:settings.toasts_sub}}</div>
      </div>
      <div class="toggle" id="toastToggle" onclick="toggleToasts()"></div>
    </div>
    <div class="setting-row">
      <div>
        <div class="s-label">{{t:settings.monitor}}</div>
        <div class="s-sub">{{t:settings.monitor_sub}}</div>
      </div>
      <div class="toggle" id="monitorToggle" onclick="toggleMonitor()"></div>
    </div>
    <div class="setting-row">
      <div>
        <div class="s-label">{{t:settings.interval}}</div>
        <div class="s-sub">{{t:settings.interval_sub}}</div>
      </div>
      <div class="seg" id="settingsIntervalSeg">
        <button data-min="1" onclick="setMonitorInterval(1)">1</button>
        <button data-min="5" class="on" onclick="setMonitorInterval(5)">5</button>
        <button data-min="10" onclick="setMonitorInterval(10)">10</button>
        <button data-min="30" onclick="setMonitorInterval(30)">30</button>
      </div>
    </div>
    <div class="tg-box">
      <div class="setting-row">
        <div>
          <div class="s-label">{{t:tg.title}}</div>
          <div class="s-sub">{{t:tg.desc}}</div>
        </div>
        <div class="toggle" id="tgToggle" title="{{t:tg.toggle_title}}" onclick="toggleTelegram()"></div>
      </div>
      <div class="field"><label>Bot token <span class="tg-saved" id="tgTokenSaved"></span></label>
        <input id="tgToken" type="password" dir="ltr" autocomplete="off" placeholder="123456789:AA..."></div>
      <div class="field"><label>Chat ID <span class="tg-saved" id="tgChatSaved"></span></label>
        <div class="tg-inline"><input id="tgChat" dir="ltr" autocomplete="off" placeholder="123456789">
          <button class="ghost" title="{{t:tg.find_title}}" onclick="findTelegramChat()">{{t:tg.find}}</button></div></div>
      <div class="field"><label>{{t:tg.idle_label}}</label>
        <input id="tgIdle" type="number" min="0" max="1440" dir="ltr" placeholder="10"></div>
      <div class="setting-row tg-sub">
        <div>
          <div class="s-label">{{t:tg.summary}}</div>
          <div class="s-sub">{{t:tg.summary_desc}}</div>
        </div>
        <input id="tgSummaryTime" type="time" dir="ltr" value="09:00" title="{{t:tg.summary_time_title}}">
        <div class="toggle" id="tgSummaryToggle" title="{{t:tg.summary_toggle_title}}" onclick="toggleSummary()"></div>
      </div>
      <div class="tg-actions">
        <button class="primary" onclick="saveTelegram()">{{t:tg.save}}</button>
        <button class="ghost" onclick="testTelegram()">{{t:tg.test}}</button>
      </div>
      <p class="modal-desc tg-help">{{h:tg.help}}</p>
    </div>
  </div>
</div>

<div id="toasts"></div>

<script>
/* =========================================================================
   LANGUAGE — the server fills in the dictionary of the saved language (i18n/<lang>.json)
   ========================================================================= */
const I18N = {{i18n:json}};
const LANG = '{{i18n:lang}}';
const RTL = document.documentElement.dir === 'rtl';
// T('key', {name: value}) -> text with {name} filled in. Escape user data before it goes into HTML.
function T(key, params) {
  const s = I18N[key] ?? key;
  return params ? s.replace(/\{(\w+)\}/g, (m, k) => k in params ? params[k] : m) : s;
}
async function setLang(lang) {
  if (lang === LANG) return;
  if (currentRun && !confirm(T('settings.lang_confirm_stop'))) { syncLangUI(); return; }
  try {
    await api('/api/settings', { lang });
    await Promise.all(Object.keys(STORE_KEYS).map(flushStore));   // unsaved key changes first
    location.reload();
  } catch (e) { toast(T('settings.lang_failed'), 'bad', e.message); syncLangUI(); }
}
function syncLangUI() {
  const sel = document.getElementById('langSelect'); if (sel) sel.value = LANG;
  document.querySelectorAll('#langSeg button').forEach(b => b.classList.toggle('on', b.dataset.lang === LANG));
}

/* =========================================================================
   STORAGE KEYS + STATE  (same keys as before → backward compatible)
   ========================================================================= */
const PROFILES_KEY = 'api_test_console_profiles';
const FORMATS_KEY = 'api_test_console_formats';
const MONITOR_KEY = 'api_test_console_monitor';
const PREFS_KEY = 'api_test_console_prefs';   // theme + toasts (new, additive)

let lastConfig = null;
let lastResults = { working: [], failed: [] };
let maxTime = 1;
let monitorTimerId = null;
let monitorNextRunAt = null;
let monitorBusy = false;       // prevents overlapping "check all" runs
let currentRun = null;         // AbortController of the running test
let resultFilter = 'all';

/* =========================================================================
   TABS
   ========================================================================= */
function showTab(name) {
  const TABS = ['test', 'keys', 'providers', 'gateway', 'agents'];
  if (!TABS.includes(name)) name = 'test';
  TABS.forEach(t => {
    const panel = document.getElementById('tab-' + t);
    const btn = document.getElementById('tabBtn-' + t);
    if (panel) panel.hidden = (t !== name);
    if (btn) { btn.classList.toggle('on', t === name); btn.setAttribute('aria-selected', t === name); }
  });
  if (name === 'keys') renderArchive();
  onTabShown(name);
  const p = getPrefs(); p.tab = name; setPrefs(p);
  // On a phone the open bar covers the page, so close it once a tab is picked.
  if (window.matchMedia('(max-width: 700px)').matches) setNavCollapsed(true, false);
}

// Side bar: open shows icon + name, closed shows icons only. The choice is remembered.
function setNavCollapsed(collapsed, save = true) {
  document.body.classList.toggle('nav-collapsed', collapsed);
  const btn = document.getElementById('navToggle');
  if (btn) btn.setAttribute('aria-expanded', String(!collapsed));
  if (save) { const p = getPrefs(); p.navCollapsed = collapsed; setPrefs(p); }
}
function toggleNav() { setNavCollapsed(!document.body.classList.contains('nav-collapsed')); }
function initNav() {
  const small = window.matchMedia('(max-width: 700px)').matches;
  setNavCollapsed(small || !!getPrefs().navCollapsed, false);
  // The bar starts right under the sticky header, whatever its height.
  const header = document.querySelector('header');
  const fit = () => document.documentElement.style.setProperty('--header-h', header.offsetHeight + 'px');
  fit();
  if (window.ResizeObserver) new ResizeObserver(fit).observe(header);
  requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.add('nav-ready')));
}

/* =========================================================================
   PREFERENCES (theme + toasts)
   ========================================================================= */
function getPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch(e) { return {}; }
}
function setPrefs(p) { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); }

function applyTheme() {
  const p = getPrefs();
  const t = p.theme || 'dark';
  document.documentElement.setAttribute('data-theme', t);
  const btn = document.getElementById('themeToggleBtn');
  if (btn) btn.textContent = (t === 'light') ? '☀️' : '🌙';
  const seg = document.getElementById('themeSeg');
  if (seg) seg.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.theme === t));
}
function setTheme(t) {
  const p = getPrefs(); p.theme = t; setPrefs(p);
  applyTheme();
}
function toggleTheme() {
  const p = getPrefs();
  setTheme((p.theme || 'dark') === 'light' ? 'dark' : 'light');
}

function toastsEnabled() { return getPrefs().toasts !== false; } // default on
function applyToastUI() {
  const on = toastsEnabled();
  const btn = document.getElementById('toastToggleBtn');
  if (btn) { btn.classList.toggle('active', on); btn.textContent = on ? '🔔' : '🔕'; }
  const t = document.getElementById('toastToggle');
  if (t) t.classList.toggle('on', on);
}
function toggleToasts() {
  const p = getPrefs();
  p.toasts = !(p.toasts !== false);
  setPrefs(p);
  applyToastUI();
  toast(p.toasts ? T('toast.notif_on') : T('toast.notif_off'), p.toasts ? 'ok' : 'warn');
}

/* =========================================================================
   TOASTS
   ========================================================================= */
function toast(title, type='ok', body='') {
  if (!toastsEnabled()) return;
  const wrap = document.getElementById('toasts');
  if (!wrap) return;
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  const em = type === 'bad' ? '❌' : type === 'warn' ? '⚠️' : type === 'info' ? 'ℹ️' : '✅';
  el.innerHTML = `<div class="t-title">${em} ${escapeHtml(title)}</div>` +
                 (body ? `<div class="t-body">${escapeHtml(body)}</div>` : '');
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s, transform .3s';
    el.style.opacity = '0'; el.style.transform = RTL ? 'translateX(-16px)' : 'translateX(16px)';
    setTimeout(() => el.remove(), 320);
  }, 5200);
  // flash header bell
  const bell = document.getElementById('toastToggleBtn');
  if (bell) { bell.classList.add('has-alert'); setTimeout(() => bell.classList.remove('has-alert'), 600); }
}
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

/* =========================================================================
   PROFILES (localStorage) — same as before + 'source' field
   ========================================================================= */
/* Saved keys and formats live in the app's data folder (not in the browser), so they
   show up in every browser and Chrome profile. localStorage is kept as a backup copy. */
const serverStore = { profiles: null, formats: null };
function readLocal(key) { try { return JSON.parse(localStorage.getItem(key)) || []; } catch (e) { return []; } }
function writeLocal(key, list) { try { localStorage.setItem(key, JSON.stringify(list)); } catch (e) {} }
/* Tabs send only what they changed (per entry, by name), never the whole list, so a tab with an
   old copy can't bring back a deleted key or drop one added elsewhere. The server merges and
   returns the full list, which the tab then takes. Unsent changes wait in outbox and retry. */
const STORE_KEYS = { profiles: PROFILES_KEY, formats: FORMATS_KEY };
const outbox = { profiles: [], formats: [] };
const storeQueue = { profiles: Promise.resolve(), formats: Promise.resolve() };
function storeOps(before, after, saved) {
  const old = new Map((before || []).map(x => [x.name, x])), ops = [];
  for (const x of after) {
    const b = old.get(x.name); old.delete(x.name);
    if (!b || x.name === saved) { ops.push({ name: x.name, create: true, set: x }); continue; }
    const set = {}, unset = Object.keys(b).filter(k => !(k in x));
    for (const k in x) if (JSON.stringify(x[k]) !== JSON.stringify(b[k])) set[k] = x[k];
    if (Object.keys(set).length || unset.length) ops.push({ name: x.name, set, unset });
  }
  for (const name of old.keys()) ops.push({ name, delete: true });
  return ops;
}
function saveStore(name, list, saved) {
  outbox[name].push(...storeOps(serverStore[name], list, saved));
  serverStore[name] = list; writeLocal(STORE_KEYS[name], list);
  flushStore(name);
}
function flushStore(name) {
  storeQueue[name] = storeQueue[name].then(async () => {
    const ops = outbox[name].slice();
    if (!ops.length) return;
    try {
      const { value } = await api('/api/store/' + name, { ops });
      outbox[name].splice(0, ops.length);
      if (!outbox[name].length) adoptStore(name, value);   // newer local changes still on the way: wait for them
    } catch (e) {
      toast(T('store.save_failed'), 'bad', e.message);
      setTimeout(() => flushStore(name), 5000);
    }
  });
  return storeQueue[name];
}
function adoptStore(name, list) {
  if (!Array.isArray(list) || JSON.stringify(list) === JSON.stringify(serverStore[name])) return;
  serverStore[name] = list; writeLocal(STORE_KEYS[name], list);
  if (name === 'profiles') {
    refreshProfileSelect(document.getElementById('savedProfiles').value); renderArchive(); updateKeyStats();
  } else { renderFormatsList(); renderArchive(); refreshCopyDD(); }
}
// Picks up changes other tabs made (on load and whenever this tab comes back into view).
async function refreshStore(name) {
  await storeQueue[name];
  if (outbox[name].length) return flushStore(name);
  try { const { value } = await api('/api/store/' + name); if (!outbox[name].length) adoptStore(name, value || []); }
  catch (e) {}
}
async function initServerStore() {
  for (const [name, key] of Object.entries(STORE_KEYS)) {
    // The browser copy is only a fallback for when the server can't be reached; it is never merged
    // back in, because it may still hold keys that were deleted since.
    try { serverStore[name] = (await api('/api/store/' + name)).value || []; writeLocal(key, serverStore[name]); }
    catch (e) { serverStore[name] = readLocal(key); }
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') Object.keys(STORE_KEYS).forEach(refreshStore);
  });
  window.addEventListener('beforeunload', e => {
    if (outbox.profiles.length || outbox.formats.length) { e.preventDefault(); e.returnValue = ''; }
  });
}
function getProfiles() { return JSON.parse(JSON.stringify(serverStore.profiles ?? readLocal(PROFILES_KEY))); }
// saved: name the user just saved on purpose (added or replaced even if another tab deleted it).
function setProfiles(list, saved) { saveStore('profiles', list, saved); }

function refreshProfileSelect(selectName) {
  const sel = document.getElementById('savedProfiles');
  const profiles = getProfiles();
  sel.innerHTML = `<option value="">${escapeHtml(T('test.pick_profile'))}</option>` +
    profiles.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join('');
  if (selectName) sel.value = selectName;
}

function currentFormValues() {
  return {
    base_url: document.getElementById('base_url').value,
    api_key: document.getElementById('api_key').value,
    provider_id: document.getElementById('provider_id').value,
    models: document.getElementById('models').value,
    filter: document.getElementById('filter').value,
    timeout: document.getElementById('timeout').value,
    retries: document.getElementById('retries').value,
    workers: document.getElementById('workers').value,
    repeat: document.getElementById('repeat').value,
    capabilities: document.getElementById('capabilities').checked,
    source: document.getElementById('source').value,
  };
}

function saveProfile() {
  if (testerKeyRef) { toast(T('profile.provider_key'), 'warn'); return; }
  const values = currentFormValues();
  if (!values.base_url || !values.api_key) {
    toast(T('profile.fill_first'), 'bad'); return;
  }
  const suggested = values.provider_id || (values.base_url.replace(/^https?:\/\//, '').split('/')[0]);
  const existing = getProfiles().find(p => p.name === suggested);
  const name = prompt(T('profile.name_prompt'), suggested) || suggested;
  if (!name) return;

  const profiles = getProfiles();
  const idx = profiles.findIndex(p => p.name === name);
  const entry = { name, ...values };
  if (idx >= 0) profiles[idx] = entry; else profiles.push(entry);
  setProfiles(profiles, name);
  refreshProfileSelect(name);
  updateKeyStats();
  toast(T('common.saved_name', { name }), 'ok');
}

function applyProfile() {
  const name = document.getElementById('savedProfiles').value;
  if (!name) return;
  clearTesterKeyRef();
  const profile = getProfiles().find(p => p.name === name);
  if (!profile) return;
  document.getElementById('base_url').value = profile.base_url || '';
  document.getElementById('api_key').value = profile.api_key || '';
  document.getElementById('provider_id').value = profile.provider_id || '';
  document.getElementById('models').value = profile.models || '';
  document.getElementById('filter').value = profile.filter || '';
  document.getElementById('timeout').value = profile.timeout || 15;
  document.getElementById('retries').value = profile.retries || 2;
  document.getElementById('workers').value = profile.workers || 8;
  document.getElementById('repeat').value = profile.repeat || 1;
  document.getElementById('capabilities').checked = !!profile.capabilities;
  document.getElementById('source').value = profile.source || '';
  updateAdvancedNote();
  toast(T('profile.loaded', { name }), 'ok');
}

function deleteProfile() {
  const name = document.getElementById('savedProfiles').value;
  if (!name) { toast(T('profile.pick_delete'), 'warn'); return; }
  if (!confirm(T('common.confirm_delete', { name }))) return;
  setProfiles(getProfiles().filter(p => p.name !== name));
  refreshProfileSelect();
  updateKeyStats();
  toast(T('common.deleted_name', { name }), 'warn');
}

/* =========================================================================
   QUICK COPY / PASTE
   ========================================================================= */
async function copyToClipboard(text) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch(e) {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); return true; } catch(e2) { return false; }
    finally { document.body.removeChild(ta); }
  }
}
function flashCopied(btn) {
  if (!btn) return;
  const orig = btn.innerHTML;
  btn.classList.add('copied'); btn.innerHTML = '✓';
  setTimeout(() => { btn.classList.remove('copied'); btn.innerHTML = orig; }, 1300);
}
function copyField(fieldId, btn) {
  const el = document.getElementById(fieldId);
  if (!el || !el.value) { toast(T('copy.empty_field'), 'warn'); return; }
  copyToClipboard(el.value.trim()).then(ok => {
    if (ok) { flashCopied(btn); }
    else toast(T('copy.failed'), 'bad');
  });
}
async function pasteInto(fieldId) {
  const input = document.getElementById(fieldId);
  if (!input) return;
  if (fieldId === 'api_key' || fieldId === 'base_url') clearTesterKeyRef();
  try {
    const text = await navigator.clipboard.readText();
    if (!text) { toast(T('paste.empty'), 'warn'); return; }
    input.value = text.trim();
    input.dispatchEvent(new Event('input'));
  } catch (e) {
    toast(T('paste.failed'), 'warn');
    input.focus();
  }
}

function toggleKeyField(btn) {
  const input = document.getElementById('api_key');
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  btn.textContent = show ? '🙈' : '👁';
}
function updateAdvancedNote() {
  const note = document.getElementById('advancedNote');
  if (!note) return;
  const bits = [];
  if (document.getElementById('models').value.trim()) bits.push(T('adv.manual'));
  if (document.getElementById('filter').value.trim()) bits.push(T('adv.filter'));
  if (document.getElementById('capabilities').checked) bits.push(T('adv.caps'));
  note.textContent = bits.join(' · ');
}

function clearAllFields() {
  clearTesterKeyRef();
  ['base_url','api_key','provider_id','models','filter','source'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  document.getElementById('timeout').value = 15;
  document.getElementById('retries').value = 2;
  document.getElementById('workers').value = 8;
  document.getElementById('repeat').value = 1;
  document.getElementById('capabilities').checked = false;
  document.getElementById('savedProfiles').value = '';
  updateAdvancedNote();
  setStatus(T('test.cleared'));
}

/* =========================================================================
   STATUS LINE
   ========================================================================= */
function setStatus(msg, isErr, busy) {
  const el = document.getElementById('statusLine');
  document.getElementById('statusText').textContent = msg;
  el.className = 'status-line' + (isErr ? ' err' : '') + (busy ? ' busy' : '');
}

/* =========================================================================
   KEY-LEVEL HELPERS (archive / monitor)
   ========================================================================= */
function maskKey(key) {
  if (!key) return '—';
  if (key.length <= 8) return '•'.repeat(key.length);
  return key.slice(0,4) + '•'.repeat(Math.max(4, key.length - 8)) + key.slice(-4);
}
function timeAgo(ts) {
  if (!ts) return T('time.never');
  const diff = Math.round((Date.now() - ts) / 1000);
  if (diff < 5) return T('time.now');
  if (diff < 60) return T('time.secs', { n: diff });
  if (diff < 3600) return T('time.mins', { n: Math.round(diff/60) });
  if (diff < 86400) return T('time.hours', { n: Math.round(diff/3600) });
  return T('time.days', { n: Math.round(diff/86400) });
}
function normalizeWorkingEntry(w) { return (typeof w === 'string') ? { model: w, time: null } : w; }

function statusBadge(p) {
  if (p.checking) return `<span class="pill pending">${T('badge.checking')}</span>`;
  if (!p.lastCheck) return `<span class="pill pending">${T('time.never')}</span>`;
  if (!p.lastCheck.keyValid) return `<span class="pill bad">${T('badge.key_down')}</span>`;
  if (!p.lastCheck.working || p.lastCheck.working.length === 0)
    return `<span class="pill bad">${T('badge.none_working')}</span>`;
  const changed = (p.lastCheck.added.length || p.lastCheck.removed.length) && p.lastCheck.hadPrevious;
  if (changed) return `<span class="pill pending">${T('badge.changed')}</span>`;
  return `<span class="pill ok">${T('badge.all_ok')}</span>`;
}

// Long lists (a key can have thousands of models): a card shows the fastest few,
// "show all" opens a scroll box with its own search. Open state survives re-renders.
const MODELS_PREVIEW = 12;
const modelsOpen = new Set();   // names of keys whose full list is open
const modelsQuery = {};         // key name -> search text inside its list
function modelTags(list, key) {
  if (!list || list.length === 0) return '<span style="color:var(--faint);">—</span>';
  const sorted = list.map(normalizeWorkingEntry).sort((a,b) => (a.time ?? Infinity) - (b.time ?? Infinity));
  const tag = (w, i) =>
    `<span class="model-tag" data-m="${escapeHtml(String(w.model).toLowerCase())}">${i+1}. ${escapeHtml(w.model)}${w.time != null ? ` <span class="t">· ${w.time}s</span>` : ''}</span>`;
  if (key == null || sorted.length <= MODELS_PREVIEW) return `<div class="model-tags">${sorted.map(tag).join('')}</div>`;
  if (!modelsOpen.has(key)) {
    return `<div class="model-tags">${sorted.slice(0, MODELS_PREVIEW).map(tag).join('')}</div>
      <button class="models-more" onclick="toggleModels('${esc(key)}')">${T('models.show_all', { n: sorted.length })}</button>`;
  }
  return `<input class="models-search" dir="ltr" data-key="${escapeHtml(key)}" placeholder="${escapeHtml(T('models.search_ph', { n: sorted.length }))}"
      value="${escapeHtml(modelsQuery[key] || '')}" oninput="filterModels(this)">
    <div class="model-tags models-scroll">${sorted.map(tag).join('')}</div>
    <div class="models-none" hidden>${T('models.none')}</div>
    <button class="models-more" onclick="toggleModels('${esc(key)}')">${T('models.collapse')}</button>`;
}
function toggleModels(key) {
  if (modelsOpen.has(key)) modelsOpen.delete(key); else modelsOpen.add(key);
  renderArchive();
}
function filterModels(input) {
  modelsQuery[input.dataset.key] = input.value;
  const q = input.value.trim().toLowerCase();
  const box = input.nextElementSibling;
  let shown = 0;
  for (const t of box.children) { const hit = !q || t.dataset.m.includes(q); t.hidden = !hit; if (hit) shown++; }
  box.nextElementSibling.hidden = shown > 0;
}
function shortList(arr, max = 8) {
  const head = arr.slice(0, max).map(escapeHtml).join(', ');
  return arr.length > max ? `${head} … (+${arr.length - max})` : head;
}
function diffLine(p) {
  if (!p.lastCheck || !p.lastCheck.hadPrevious) return '';
  const parts = [];
  if (p.lastCheck.added.length) parts.push(`<span class="diff-add" title="${escapeHtml(p.lastCheck.added.join(', '))}">${T('diff.added', { list: shortList(p.lastCheck.added) })}</span>`);
  if (p.lastCheck.removed.length) parts.push(`<span class="diff-remove" title="${escapeHtml(p.lastCheck.removed.join(', '))}">${T('diff.removed', { list: shortList(p.lastCheck.removed) })}</span>`);
  if (!parts.length) return '';
  return `<div class="diff-line">${parts.join(' &middot; ')}</div>`;
}

/* =========================================================================
   ARCHIVE (grid of key-cards)
   ========================================================================= */
let keyFilter = 'all';
function profileState(p) {
  if (p.checking) return 'idle';
  if (!p.lastCheck) return 'idle';
  if (!p.lastCheck.keyValid || !p.lastCheck.working || !p.lastCheck.working.length) return 'bad';
  if (p.lastCheck.hadPrevious && (p.lastCheck.added.length || p.lastCheck.removed.length)) return 'warn';
  return 'ok';
}
function setKeyFilter(f) {
  keyFilter = f;
  document.querySelectorAll('[data-kf]').forEach(c => c.classList.toggle('on', c.dataset.kf === f));
  renderArchive();
}
function goAddKey() {
  showTab('test');
  clearTesterKeyRef();
  setStatus(T('keys.go_add'));
  document.getElementById('base_url').focus();
}
async function sendProfileToGateway(name) {
  const profile = getProfiles().find(p => p.name === name);
  if (!profile) return;
  try {
    const r = await api('/api/providers/import', { profiles: [profile] });
    gwState = r;
    toast(r.imported ? T('keys.sent_gw', { name }) : T('keys.already_gw', { name }), r.imported ? 'ok' : 'warn');
  } catch (e) { toast(T('keys.not_added'), 'bad', e.message); }
}
function renderArchive() {
  const body = document.getElementById('archiveBody');
  const profiles = getProfiles();
  const counts = { all: profiles.length, ok: 0, warn: 0, bad: 0, idle: 0 };
  profiles.forEach(p => counts[profileState(p)]++);
  const setN = (id, n) => { const el = document.getElementById(id); if (el) el.textContent = n; };
  setN('ksTotal', counts.all); setN('ksOk', counts.ok); setN('ksWarn', counts.warn); setN('ksBad', counts.bad);
  setN('kfAll', counts.all); setN('kfOk', counts.ok); setN('kfWarn', counts.warn); setN('kfBad', counts.bad); setN('kfIdle', counts.idle);
  updateKeyStats();

  if (profiles.length === 0) {
    body.className = '';
    body.innerHTML = `<div class="card"><div class="empty-state"><span class="em">🔑</span>${T('keys.empty')}</div></div>`;
    return;
  }
  const q = (document.getElementById('keySearch')?.value || '').trim().toLowerCase();
  const formats = getFormats();
  const icons = { ok: '✅', warn: '⚠️', bad: '⛔', idle: '⏳' };
  const cards = profiles.map((p, i) => ({ p, i, st: profileState(p) }))
    .filter(({ p, st }) => (keyFilter === 'all' || st === keyFilter)
      && (!q || (p.name || '').toLowerCase().includes(q) || (p.base_url || '').toLowerCase().includes(q) || (p.source || '').toLowerCase().includes(q)
        || ((p.lastCheck && p.lastCheck.working) || []).some(w => String(normalizeWorkingEntry(w).model).toLowerCase().includes(q))))
    .map(({ p, i, st }) => {
      const workingCount = p.lastCheck && p.lastCheck.working ? p.lastCheck.working.length : 0;
      const canCopy = !!(p.lastCheck && p.lastCheck.config);
      const hasWorking = workingCount > 0;
      const fmtItems = formats.map(f =>
        `<button class="dd-item" ${hasWorking ? '' : 'disabled'} onclick="copyCustomFormat('${esc(p.name)}', '${esc(f.name)}', 'copybtn-${i}')"><span class="em">🧩</span> ${escapeHtml(f.name)}</button>`).join('');
      const err = p.lastCheck && !p.lastCheck.keyValid && p.lastCheck.errorMessage
        ? `<div class="kc-error">${escapeHtml(p.lastCheck.errorMessage)}</div>` : '';
      return `
      <div class="kcard ${st}">
        <div class="kcard-head">
          <span class="kc-ic">${icons[st]}</span>
          <div class="kc-title">
            <div class="kc-name">${sourceUrl(p.source)
              ? `<a class="kc-link" href="${escapeHtml(sourceUrl(p.source))}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(T('keys.open_url', { url: sourceUrl(p.source) }))}">${escapeHtml(p.name)} <span class="ext">↗</span></a>`
              : `<span title="${escapeHtml(T('keys.link_hint'))}">${escapeHtml(p.name)}</span>`}</div>
            <div class="kc-sub">⏱ ${timeAgo(p.lastCheck && p.lastCheck.timestamp)} · ${T('keys.working_count', { n: workingCount })}</div>
          </div>
          ${statusBadge(p)}
        </div>
        <div class="kcard-body">
          <div class="kv-row"><span class="k">URL</span>
            <span class="v" dir="ltr" title="${esc(p.base_url || '')}">${p.base_url ? escapeHtml(p.base_url) : '—'}</span>
            <button class="mini-copy" title="${escapeHtml(T('keys.copy_url'))}" ${p.base_url ? '' : 'disabled'} onclick="copyRaw('${esc(p.base_url)}', this)">📋</button></div>
          <div class="kv-row"><span class="k">${T('keys.lbl_key')}</span>
            <span class="v" id="akey-${i}" dir="ltr">${maskKey(p.api_key)}</span>
            <button class="mini-copy" title="${escapeHtml(T('common.show_hide'))}" onclick="toggleKey(${i}, '${esc(p.api_key)}')">👁</button>
            <button class="mini-copy" title="${escapeHtml(T('keys.copy_key'))}" ${p.api_key ? '' : 'disabled'} onclick="copyRaw('${esc(p.api_key)}', this)">📋</button></div>
          <div class="kv-row"><span class="k">${T('keys.lbl_source')}</span>
            <input value="${esc(p.source)}" placeholder="${escapeHtml(T('keys.source_ph'))}" onchange="updateSource('${esc(p.name)}', this.value)"></div>
          <div class="kc-models-block">
            <div class="lbl">${T('keys.models_label')}</div>
            ${modelTags(p.lastCheck && p.lastCheck.working, p.name)}
            ${diffLine(p)}
            ${err}
          </div>
        </div>
        <div class="kcard-foot">
          <div class="copy-dd" id="copydd-${i}">
            <button id="copybtn-${i}" ${canCopy ? '' : 'disabled style="opacity:.4;cursor:not-allowed"'} onclick="toggleCopyMenu(event, ${i})">${T('test.copy_config')}</button>
            <div class="dd-menu">
              <button class="dd-item" ${canCopy ? '' : 'disabled'} onclick="copyProfileConfig('${esc(p.name)}', ${i}, 'opencode')"><span class="em">🧪</span> opencode</button>
              <button class="dd-item" ${canCopy ? '' : 'disabled'} onclick="copyProfileConfig('${esc(p.name)}', ${i}, 'piagent')"><span class="em">🪶</span> pi agent</button>
              <button class="dd-item" ${canCopy ? '' : 'disabled'} onclick="copyProfileConfig('${esc(p.name)}', ${i}, 'hermes')"><span class="em">🪽</span> hermes</button>
              ${fmtItems ? '<div class="dd-sep"></div>' + fmtItems : ''}
              <div class="dd-sep"></div>
              <button class="dd-item" ${canCopy ? '' : 'disabled'} onclick="copyProfileConfig('${esc(p.name)}', ${i}, 'custom')"><span class="em">✏️</span> ${T('keys.custom_tpl')}</button>
            </div>
          </div>
          <span class="spacer"></span>
          <button class="icon-act" title="${escapeHtml(T('keys.open_tester'))}" onclick="loadFromArchive('${esc(p.name)}')">⬆</button>
          <button class="icon-act" title="${escapeHtml(T('keys.check_now'))}" onclick="checkProfileNow('${esc(p.name)}')">🔄</button>
          <button class="icon-act" title="${escapeHtml(T('keys.to_gw'))}" onclick="sendProfileToGateway('${esc(p.name)}')">🚪</button>
          <button class="icon-act danger" title="${escapeHtml(T('common.delete'))}" onclick="deleteFromArchive('${esc(p.name)}')">🗑</button>
        </div>
      </div>`;
    });
  const act = document.activeElement;
  const typing = act && act.classList.contains('models-search')
    ? { key: act.dataset.key, a: act.selectionStart, b: act.selectionEnd } : null;
  body.className = 'key-grid';
  body.innerHTML = cards.join('') || '<div class="card keys-empty"><div class="empty-state">' + T('keys.no_match') + '</div></div>';
  body.querySelectorAll('.models-search').forEach(inp => {
    if (inp.value) filterModels(inp);
    if (typing && inp.dataset.key === typing.key) { inp.focus(); inp.setSelectionRange(typing.a, typing.b); }
  });
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\r?\n/g, '\\n')
    .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function copyRaw(text, btn) {
  if (!text) return;
  copyToClipboard(text).then(ok => { if (ok) flashCopied(btn); });
}
function toggleKey(i, fullKey) {
  const el = document.getElementById('akey-' + i);
  const showing = el.dataset.showing === '1';
  el.textContent = showing ? maskKey(fullKey) : (fullKey || '—');
  el.dataset.showing = showing ? '0' : '1';
}
function updateSource(name, val) {
  const profiles = getProfiles();
  const p = profiles.find(x => x.name === name);
  if (p) { p.source = val; setProfiles(profiles); renderArchive(); }
}
// The "source" field as a link: a full http(s) URL, or a bare domain like openrouter.ai/keys. Anything else is plain text.
function sourceUrl(src) {
  const t = String(src || '').trim();
  if (!t || /\s/.test(t)) return '';
  const withScheme = /^https?:\/\//i.test(t) ? t
    : /^[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}(:\d+)?(\/\S*)?$/i.test(t) ? 'https://' + t : '';
  try { const u = new URL(withScheme); return /^https?:$/.test(u.protocol) ? u.href : ''; } catch (e) { return ''; }
}

function openArchive() { showTab('keys'); }
function closeArchive() { showTab('test'); }
function loadFromArchive(name) {
  refreshProfileSelect(name); applyProfile(); closeArchive();
}
function deleteFromArchive(name) {
  if (!confirm(T('common.confirm_delete', { name }))) return;
  setProfiles(getProfiles().filter(p => p.name !== name));
  refreshProfileSelect(); renderArchive(); updateKeyStats();
  toast(T('common.deleted_name', { name }), 'warn');
}
async function copyProfileConfig(name, i, fmt) {
  const profile = getProfiles().find(p => p.name === name);
  if (!profile || !profile.lastCheck || !profile.lastCheck.config) return;
  let text = JSON.stringify(profile.lastCheck.config, null, 2);
  if (fmt && fmt !== 'opencode') {
    const workingList = (profile.lastCheck && profile.lastCheck.working) || [];
    if (workingList.length) {
      const models = workingList.map(normalizeWorkingEntry).map(w => w.model);
      if (fmt === 'custom') {
        const tpl = prompt(T('copy.tpl_prompt'),
          '{\n  "url": "{{url}}",\n  "key": "{{key}}",\n  "models": {{models}}\n}');
        if (!tpl) return;
        text = buildConfigFromTemplate(tpl, profile, models);
      } else {
        text = JSON.stringify({ url: profile.base_url, key: profile.api_key, models: models }, null, 2);
      }
    }
  }
  const ok = await copyToClipboard(text);
  if (ok) {
    const dd = document.getElementById('copydd-' + i);
    const btn = dd && dd.querySelector(':scope > button');
    if (btn) flashCopied(btn);
    toast(T('copy.done', { what: fmt || 'opencode' }), 'ok');
  }
}
function toggleCopyMenu(e, i) {
  e.stopPropagation();
  const dd = document.getElementById('copydd-' + i);
  if (!dd) return;
  const wasOpen = dd.classList.contains('open');
  document.querySelectorAll('.copy-dd.open').forEach(d => d.classList.remove('open'));
  if (!wasOpen) dd.classList.add('open');
}
document.addEventListener('click', () => {
  document.querySelectorAll('.copy-dd.open').forEach(d => d.classList.remove('open'));
});

/* =========================================================================
   PROFILE CHECK (live) — same logic as before
   ========================================================================= */
async function runProfileCheck(profile) {
  const knownWorking = ((profile.lastCheck && profile.lastCheck.working) || [])
    .map(normalizeWorkingEntry).map(w => w.model);
  const payload = {
    base_url: profile.base_url, api_key: profile.api_key, provider_id: profile.provider_id,
    models: profile.models, filter: profile.filter,
    timeout: profile.timeout || 15, retries: profile.retries || 2, workers: profile.workers || 8,
    repeat: 1, capabilities: false, fallback_models: knownWorking,
  };
  const result = { working: [], failed: [], errorMessage: null, config: null, keyStatus: null };
  try {
    const resp = await fetch('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Console': '1' }, body: JSON.stringify(payload) });
    const reader = resp.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n'); buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const evt = JSON.parse(line);
        if (evt.type === 'model_result') {
          if (evt.data.status === 'WORKING') result.working.push({ model: evt.data.model, time: evt.data.response_time_avg });
          else result.failed.push(evt.data.model);
        } else if (evt.type === 'summary') {
          result.config = evt.data.config;
          result.keyStatus = evt.data.key_status;
          // A rejected key doesn't raise an "error" event, only a summary.
          if (evt.data.key_status === 'INVALID') result.errorMessage = evt.data.key_message;
        }
        else if (evt.type === 'error') result.errorMessage = evt.data.message;
      }
    }
  } catch (e) { result.errorMessage = e.message; }
  return result;
}

function markChecking(name, val) {
  const profiles = getProfiles();
  const p = profiles.find(x => x.name === name);
  if (p) { p.checking = val; setProfiles(profiles); }
}

async function checkProfile(name) {
  const profile = getProfiles().find(p => p.name === name);
  if (!profile) return;
  const result = await runProfileCheck(profile);
  result.working.sort((a, b) => (a.time ?? Infinity) - (b.time ?? Infinity));

  const fresh = getProfiles();
  const target = fresh.find(p => p.name === name);
  if (!target) return;

  // Must be read BEFORE lastCheck is overwritten below.
  const hadBefore = !!(target.lastCheck && target.lastCheck.working);
  const previousWorking = ((target.lastCheck && target.lastCheck.working) || []).map(normalizeWorkingEntry).map(w => w.model);
  const currentWorkingNames = result.working.map(w => w.model);
  const added = currentWorkingNames.filter(m => !previousWorking.includes(m));
  const removed = previousWorking.filter(m => !currentWorkingNames.includes(m));

  target.checking = false;
  target.lastCheck = {
    timestamp: Date.now(), working: result.working, failed: result.failed,
    config: result.config, keyValid: !result.errorMessage, errorMessage: result.errorMessage,
    added, removed, hadPrevious: hadBefore,
  };
  setProfiles(fresh);

  // ---- LIVE NOTIFICATION on change ----
  if (hadBefore) {
    if (!result.errorMessage && removed.length)
      toast(T('notify.model_stopped', { name }), 'bad', T('notify.stopped_list', { list: removed.join(', ') }));
    if (!result.errorMessage && added.length)
      toast(T('notify.new_model', { name }), 'ok', T('notify.added_list', { list: added.join(', ') }));
    if (result.errorMessage)
      toast(T('notify.key_down', { name }), 'bad', result.errorMessage);
    if (!result.errorMessage && !added.length && !removed.length && result.working.length)
      toast(T('notify.all_good', { name }), 'ok');
  } else if (result.working.length) {
    toast(T('notify.working', { name, n: result.working.length }), 'ok');
  }
  updateKeyStats();
}

async function checkProfileNow(name) {
  markChecking(name, true); renderArchive();
  await checkProfile(name); renderArchive();
}
async function checkAllProfiles() {
  if (monitorBusy) { toast(T('monitor.busy'), 'warn'); return; }
  const names = getProfiles().map(p => p.name);
  if (names.length === 0) { toast(T('monitor.nothing'), 'warn'); return; }
  monitorBusy = true;
  const btn = document.getElementById('checkAllBtn');
  if (btn) btn.disabled = true;
  try {
    names.forEach(n => markChecking(n, true)); renderArchive();
    // Check keys two at a time: faster than one by one, gentle on the providers.
    const queue = [...names];
    const worker = async () => {
      while (queue.length) { const name = queue.shift(); await checkProfile(name); renderArchive(); }
    };
    await Promise.all([worker(), worker()]);
    toast(T('monitor.checked', { n: names.length }), 'info');
  } finally {
    monitorBusy = false;
    if (btn) btn.disabled = false;
  }
}

/* =========================================================================
   MONITOR (auto periodic)
   ========================================================================= */
function getMonitorSettings() {
  try { return JSON.parse(localStorage.getItem(MONITOR_KEY)) || { enabled: false, intervalMinutes: 5 }; }
  catch(e) { return { enabled: false, intervalMinutes: 5 }; }
}
function setMonitorSettings(s) { localStorage.setItem(MONITOR_KEY, JSON.stringify(s)); }

function syncIntervalSeg(min) {
  ['intervalSeg','settingsIntervalSeg'].forEach(id => {
    const seg = document.getElementById(id); if (!seg) return;
    seg.querySelectorAll('button').forEach(b => b.classList.toggle('on', parseInt(b.dataset.min) === min));
  });
}
function setMonitorInterval(minutes) {
  const s = getMonitorSettings();
  s.intervalMinutes = minutes; setMonitorSettings(s);
  syncIntervalSeg(minutes);
  if (s.enabled) { startMonitor(minutes); }
}
function toggleMonitor() {
  const s = getMonitorSettings();
  s.enabled = !s.enabled; setMonitorSettings(s);
  syncMonitorUI();
  if (s.enabled) startMonitor(s.intervalMinutes); else stopMonitor();
  toast(s.enabled ? T('monitor.started', { n: s.intervalMinutes }) : T('monitor.stopped'), s.enabled ? 'ok' : 'warn');
}
function startMonitor(minutes) {
  stopMonitor();
  monitorNextRunAt = Date.now() + minutes * 60000;
  updateNextRunLabel();
  monitorTimerId = setInterval(async () => {
    if (!monitorBusy) await checkAllProfiles();
    monitorNextRunAt = Date.now() + minutes * 60000;
    updateNextRunLabel();
  }, minutes * 60000);
}
const MONITOR_IDLE_TEXT = T('monitor.idle');
function stopMonitor() {
  if (monitorTimerId) clearInterval(monitorTimerId);
  monitorTimerId = null; monitorNextRunAt = null;
  const el = document.getElementById('monitorNextRun'); if (el) el.textContent = MONITOR_IDLE_TEXT;
}
function updateNextRunLabel() {
  const el = document.getElementById('monitorNextRun'); if (!el) return;
  if (!monitorNextRunAt) { el.textContent = MONITOR_IDLE_TEXT; return; }
  const secsLeft = Math.max(0, Math.round((monitorNextRunAt - Date.now()) / 1000));
  const mins = Math.floor(secsLeft / 60), secs = secsLeft % 60;
  el.textContent = T('monitor.next', { time: `${mins}:${secs.toString().padStart(2,'0')}` });
}
function syncMonitorUI() {
  const s = getMonitorSettings();
  const pulse = document.getElementById('livePulse');
  const btn = document.getElementById('liveToggleBtn');
  const tgl = document.getElementById('monitorToggle');
  if (pulse) pulse.classList.toggle('paused', !s.enabled);
  if (btn) btn.textContent = s.enabled ? T('keys.monitor_stop') : T('keys.monitor_start');
  if (tgl) tgl.classList.toggle('on', s.enabled);
  syncIntervalSeg(s.intervalMinutes);
  if (s.enabled && !monitorTimerId) startMonitor(s.intervalMinutes);
}
function initMonitorUI() { syncMonitorUI(); }

/* =========================================================================
   KEY STAT (top dashboard)
   ========================================================================= */
function updateKeyStats() {
  const el = document.getElementById('statKeys');
  if (el) el.textContent = getProfiles().length;
}

/* =========================================================================
   FORMATS (custom)
   ========================================================================= */
function getFormats() { return JSON.parse(JSON.stringify(serverStore.formats ?? readLocal(FORMATS_KEY))); }
function setFormats(list, saved) { saveStore('formats', list, saved); }
function openFormats() { renderFormatsList(); refreshCopyDD(); document.getElementById('formatsOverlay').classList.add('open'); }
function closeFormats() { document.getElementById('formatsOverlay').classList.remove('open'); }

function saveFormat() {
  const name = document.getElementById('formatName').value.trim();
  const raw = document.getElementById('formatTemplate').value.trim();
  if (!name) { toast(T('formats.need_name'), 'warn'); return; }
  let template;
  try { template = JSON.parse(raw); } catch(e) { toast(T('formats.bad_json', { error: e.message }), 'bad'); return; }
  const formats = getFormats();
  const idx = formats.findIndex(f => f.name === name);
  const entry = { name, template };
  if (idx >= 0) formats[idx] = entry; else formats.push(entry);
  setFormats(formats, name);
  document.getElementById('formatName').value = '';
  document.getElementById('formatTemplate').value = '';
  renderFormatsList(); refreshCopyDD();
  toast(T('formats.saved', { name }), 'ok');
}
function deleteFormat(name) {
  if (!confirm(T('formats.confirm_delete', { name }))) return;
  setFormats(getFormats().filter(f => f.name !== name));
  renderFormatsList(); renderArchive(); refreshCopyDD();
}
function renderFormatsList() {
  const body = document.getElementById('formatsListBody');
  const formats = getFormats();
  if (formats.length === 0) { body.innerHTML = `<div class="empty-state">${T('formats.empty')}</div>`; return; }
  body.innerHTML = formats.map(f => `
    <div class="side-section" style="margin-bottom:10px;">
      <div class="side-section-title">🧩 ${escapeHtml(f.name)}</div>
      <div class="action-grid"><button class="ghost" onclick="deleteFormat('${esc(f.name)}')">${T('common.delete_btn')}</button></div>
    </div>`).join('');
}

/* ---- conversion engine (same as before) ---- */
function cloneDeep(o) { return JSON.parse(JSON.stringify(o)); }
function findProviderContainerKey(obj) {
  if (obj && typeof obj === 'object') { if ('provider' in obj) return 'provider'; if ('providers' in obj) return 'providers'; }
  return null;
}
function buildConfigFromTemplate(template, profile, workingModels) {
  const config = cloneDeep(template);
  const providerId = (profile.provider_id || '').trim();
  function walkReplace(node) {
    if (Array.isArray(node)) node.forEach(walkReplace);
    else if (node && typeof node === 'object') {
      for (const key of Object.keys(node)) {
        if (key === 'baseURL' && typeof node[key] === 'string') node[key] = profile.base_url;
        else if (key === 'apiKey' && typeof node[key] === 'string') node[key] = profile.api_key;
        else if (key === 'models' && node[key] && typeof node[key] === 'object' && !Array.isArray(node[key])) {
          const fresh = {}; workingModels.forEach(m => { fresh[m] = { name: m }; }); node[key] = fresh;
        } else walkReplace(node[key]);
      }
    }
  }
  walkReplace(config);
  const containerKey = findProviderContainerKey(config);
  if (containerKey) {
    const container = config[containerKey];
    const oldKeys = Object.keys(container);
    if (providerId) {
      const renamed = {};
      oldKeys.forEach((k, idx) => { const newKey = oldKeys.length === 1 ? providerId : `${providerId}_${idx+1}`; renamed[newKey] = container[k]; });
      config[containerKey] = renamed;
      if (oldKeys.length === 1 && typeof config.model === 'string' && workingModels.length) config.model = `${providerId}/${workingModels[0]}`;
    } else if (oldKeys.length === 1 && typeof config.model === 'string' && workingModels.length)
      config.model = `${oldKeys[0]}/${workingModels[0]}`;
  }
  return config;
}
async function copyCustomFormat(profileName, formatName, elId) {
  const profile = getProfiles().find(p => p.name === profileName);
  const format = getFormats().find(f => f.name === formatName);
  if (!profile || !format) return;
  const workingList = (profile.lastCheck && profile.lastCheck.working) || [];
  if (!workingList.length) { toast(T('copy.no_working_copy'), 'warn'); return; }
  const models = workingList.map(normalizeWorkingEntry).map(w => w.model);
  const config = buildConfigFromTemplate(format.template, profile, models);
  const ok = await copyToClipboard(JSON.stringify(config, null, 2));
  if (ok) { const b = document.getElementById(elId); flashCopied(b); toast(T('copy.done', { what: formatName }), 'ok'); }
}

/* =========================================================================
   SMART COPY DROPDOWN (single button → pick target)
   ========================================================================= */
function toggleCopyDD(e) {
  e.stopPropagation();
  document.getElementById('copyDD').classList.toggle('open');
}
document.addEventListener('click', () => { const dd = document.getElementById('copyDD'); if (dd) dd.classList.remove('open'); });

function refreshCopyDD() {
  const menu = document.getElementById('copyDDMenu');
  const formats = getFormats();
  // keep built-in items, add custom formats
  const ready = !!lastConfig;
  let html = `
    <button class="dd-item" ${ready ? '' : 'disabled'} onclick="pickCopy('opencode')"><span class="em">🧪</span> ${T('copy.opencode')}</button>
    <button class="dd-item" ${ready ? '' : 'disabled'} onclick="pickCopy('names')"><span class="em">📝</span> ${T('copy.names')}</button>`;
  formats.forEach(f => {
    html += `<button class="dd-item" ${ready ? '' : 'disabled'} onclick="pickCopy('fmt:${esc(f.name)}')"><span class="em">🧩</span> ${T('copy.fmt', { name: escapeHtml(f.name) })}</button>`;
  });
  menu.innerHTML = html;
}
async function pickCopy(target) {
  document.getElementById('copyDD').classList.remove('open');
  if (target === 'opencode') {
    if (lastConfig) { await copyToClipboard(JSON.stringify(lastConfig, null, 2)); toast(T('copy.done', { what: 'opencode config' }), 'ok'); }
    else toast(T('copy.no_config'), 'warn');
    return;
  }
  const workingNames = [...lastResults.working]
    .sort((a,b) => a.response_time_avg - b.response_time_avg).map(r => r.model);
  if (target === 'names') {
    if (!workingNames.length) { toast(T('copy.no_working'), 'warn'); return; }
    await copyToClipboard(workingNames.join(', ')); toast(T('copy.n_models', { n: workingNames.length }), 'ok');
    return;
  }
  if (target.startsWith('fmt:')) {
    // Uses the current form + the last run directly: no need to save a profile first.
    const fname = target.slice(4);
    const format = getFormats().find(f => f.name === fname);
    if (!format) return;
    if (!workingNames.length) { toast(T('copy.run_first'), 'warn'); return; }
    const profile = currentFormValues();
    const config = buildConfigFromTemplate(format.template, profile, workingNames);
    const ok = await copyToClipboard(JSON.stringify(config, null, 2));
    if (ok) toast(T('copy.done', { what: fname }), 'ok');
  }
}

/* =========================================================================
   CAPABILITIES BADGES + ROWS + CHART
   ========================================================================= */
function capBadges(caps) {
  if (!caps) return '<span class="cap">—</span>';
  const names = { streaming: 'stream', tool_calling: 'tools', json_mode: 'json' };
  return Object.entries(names).map(([k, label]) =>
    `<span class="cap ${caps[k] ? 'on' : ''}">${label}</span>`).join(' &middot; ');
}
function rowId(model) { return 'row-' + btoa(unescape(encodeURIComponent(model))).replace(/[^a-zA-Z0-9]/g, ''); }

function addRow(model) {
  const empty = document.querySelector('#tbody .empty-row');
  if (empty) empty.remove();
  const tr = document.createElement('tr');
  tr.id = rowId(model);
  tr.dataset.model = model;
  tr.dataset.state = 'pending';
  tr.innerHTML = `<td>${modelCell(model, '')}</td><td><span class="pill pending">${T('row.testing')}</span></td><td>—</td><td>—</td><td>—</td>`;
  document.getElementById('tbody').appendChild(tr);
  applyResultFilter();
  return tr;
}
function modelCell(model, rank) {
  return `<div class="model-cell"><span class="rank">${rank}</span><span>${escapeHtml(model)}</span>` +
         `<button class="mini-copy" title="${escapeHtml(T('row.copy_model'))}" onclick="copyRaw('${esc(model)}', this)">📋</button></div>`;
}
function updateRow(result) {
  const id = rowId(result.model);
  let tr = document.getElementById(id);
  if (!tr) tr = addRow(result.model);
  const isOk = result.status === 'WORKING';
  if (isOk) { maxTime = Math.max(maxTime, result.response_time_avg); lastResults.working.push(result); }
  else lastResults.failed.push(result);
  const labels = {
    KEY_INVALID: T('st.KEY_INVALID'), MODEL_UNAVAILABLE: T('st.MODEL_UNAVAILABLE'),
    LIMITED: T('st.LIMITED'), UNAVAILABLE: T('st.UNAVAILABLE'),
    INCOMPATIBLE: T('st.INCOMPATIBLE'), INVALID_RESPONSE: T('st.INVALID_RESPONSE'), FAILED: T('st.FAILED')
  };
  const transient = ['LIMITED', 'UNAVAILABLE', 'INCOMPATIBLE'].includes(result.status);
  const statusLabel = isOk ? T('st.WORKING') : (labels[result.status] || T('st.FAILED'));
  tr.dataset.state = isOk ? 'ok' : 'bad';
  tr.classList.remove('flash'); void tr.offsetWidth; tr.classList.add('flash');
  const extra = [];
  if (isOk && result.runs > 1) extra.push(`min ${result.response_time_min}s · max ${result.response_time_max}s`);
  if (isOk && result.ttft != null) extra.push(T('row.first_token', { t: result.ttft }));
  tr.innerHTML = `
    <td>${modelCell(result.model, '')}</td>
    <td><span class="pill ${isOk ? 'ok' : (transient ? 'pending' : 'bad')}">${statusLabel}</span></td>
    <td>${isOk
        ? `<div class="bar-cell"><span>${result.response_time_avg}s</span><div class="bar-track"><div class="bar-fill" data-time="${result.response_time_avg}"></div></div></div>${extra.length ? `<span class="sub-metric">${escapeHtml(extra.join(' · '))}</span>` : ''}`
        : `<div class="result-diagnosis"><strong>${escapeHtml(result.error || statusLabel)}</strong><small>HTTP ${escapeHtml(result.code || 0)}</small>${result.technical_error ? `<span title="${escapeHtml(result.technical_error)}">${T('row.tech_details')}</span>` : ''}</div>`}</td>
    <td>${isOk && result.tokens_per_sec ? result.tokens_per_sec : `<span title="${isOk ? escapeHtml(T('row.enable_caps')) : ''}">—</span>`}</td>
    <td>${isOk ? capBadges(result.capabilities) : '—'}</td>`;
  refreshBars();
  updateChart();
  updateLiveCounts();
  applyResultFilter();
}
// Bars are relative to the slowest model so far, so refresh all of them.
function refreshBars() {
  document.querySelectorAll('#tbody .bar-fill[data-time]').forEach(el => {
    el.style.width = Math.min(100, Math.round((parseFloat(el.dataset.time) / maxTime) * 100)) + '%';
  });
}
function updateLiveCounts() {
  const ok = lastResults.working.length, bad = lastResults.failed.length;
  const total = document.querySelectorAll('#tbody tr[data-state]').length;
  document.getElementById('statWorking').textContent = ok;
  document.getElementById('statFailed').textContent = bad;
  document.getElementById('statTotal').textContent = total;
  document.getElementById('fcAll').textContent = total;
  document.getElementById('fcOk').textContent = ok;
  document.getElementById('fcBad').textContent = bad;
}
function setResultFilter(f) {
  resultFilter = f;
  document.querySelectorAll('.filters .chip').forEach(c => c.classList.toggle('on', c.dataset.filter === f));
  applyResultFilter();
}
function applyResultFilter() {
  const q = (document.getElementById('resultSearch').value || '').trim().toLowerCase();
  document.querySelectorAll('#tbody tr[data-state]').forEach(tr => {
    const st = tr.dataset.state;
    const passState = resultFilter === 'all' || st === resultFilter || (resultFilter === 'bad' && st === 'bad');
    const passText = !q || tr.dataset.model.toLowerCase().includes(q);
    tr.style.display = (passState && passText) ? '' : 'none';
  });
}
// At the end of a run: fastest working models first (with rank), then failures.
function sortResultRows() {
  const tbody = document.getElementById('tbody');
  const timeOf = {}; lastResults.working.forEach(r => { timeOf[r.model] = r.response_time_avg; });
  const rows = [...tbody.querySelectorAll('tr[data-state]')];
  rows.sort((a, b) => {
    const ta = timeOf[a.dataset.model], tb = timeOf[b.dataset.model];
    return (ta ?? Infinity) - (tb ?? Infinity);
  });
  rows.forEach((tr, i) => {
    tbody.appendChild(tr);
    const rank = tr.querySelector('.rank');
    if (rank) rank.textContent = timeOf[tr.dataset.model] != null ? (i + 1) : '';
  });
}
function updateChart() {
  const wrap = document.getElementById('chart');
  if (!wrap) return;
  if (!lastResults.working.length) {
    wrap.innerHTML = `<div class="chart-empty" style="width:100%;">${T('test.chart_empty')}</div>`;
    return;
  }
  const data = [...lastResults.working].sort((a,b) => a.response_time_avg - b.response_time_avg);
  const max = Math.max(...data.map(d => d.response_time_avg), 0.01);
  wrap.innerHTML = data.map(d => {
    const h = Math.max(4, Math.round((d.response_time_avg / max) * 100));
    return `<div class="col" title="${escapeHtml(d.model)}: ${d.response_time_avg}s">
      <div class="slot"><span class="val">${d.response_time_avg}s</span><div class="bar" style="height:${h}%"></div></div>
      <div class="lab">${escapeHtml(d.model.length > 12 ? d.model.slice(0,11)+'…' : d.model)}</div>
    </div>`;
  }).join('');
}

/* =========================================================================
   RUN TEST (streaming) — the same button starts and stops a run
   ========================================================================= */
function runOrStop() {
  if (currentRun) { currentRun.abort(); return; }
  runTest();
}
function setRunButton(running) {
  const btn = document.getElementById('runBtn');
  btn.classList.toggle('stop', running);
  btn.textContent = running ? T('test.stop') : T('test.run');
}
function showBanner(data) {
  const banner = document.getElementById('resultBanner');
  const fastest = lastResults.working.length
    ? [...lastResults.working].sort((a,b) => a.response_time_avg - b.response_time_avg)[0] : null;
  banner.classList.toggle('bad', !data.working);
  document.getElementById('bannerBig').textContent = `${data.working}/${data.total}`;
  document.getElementById('bannerTitle').textContent = data.working
    ? T('banner.n_working', { working: data.working, total: data.total }) : T('banner.none');
  const parts = [data.key_message];
  if (fastest) parts.push(T('banner.fastest', { model: fastest.model, t: fastest.response_time_avg }));
  parts.push(T('banner.duration', { t: data.total_time_seconds }));
  document.getElementById('bannerSub').textContent = parts.join(' · ');
  banner.classList.add('show');
}
async function runTest() {
  if (!document.getElementById('base_url').value.trim() || !document.getElementById('api_key').value.trim()) {
    setStatus(T('run.fill'), true);
    toast(T('run.fill_toast'), 'warn');
    return;
  }
  currentRun = new AbortController();
  setRunButton(true);
  document.getElementById('dlReport').disabled = true;
  document.getElementById('resultBanner').classList.remove('show');
  document.getElementById('tbody').innerHTML = '';
  lastResults = { working: [], failed: [] }; maxTime = 0.001; lastConfig = null;
  refreshCopyDD();
  updateLiveCounts();
  document.getElementById('statTime').textContent = '…';
  updateChart();
  const startedAt = performance.now();

  const payload = {
    base_url: document.getElementById('base_url').value,
    api_key: testerKeyRef ? '' : document.getElementById('api_key').value,
    key_ref: testerKeyRef || undefined,
    provider_id: document.getElementById('provider_id').value,
    models: document.getElementById('models').value,
    filter: document.getElementById('filter').value,
    timeout: document.getElementById('timeout').value,
    retries: document.getElementById('retries').value,
    workers: document.getElementById('workers').value,
    repeat: document.getElementById('repeat').value,
    capabilities: document.getElementById('capabilities').checked,
    fallback_models: [],
  };

  try {
    setStatus(T('run.connecting'), false, true);
    const resp = await fetch('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Console': '1' },
                                           body: JSON.stringify(payload), signal: currentRun.signal });
    const reader = resp.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n'); buffer = lines.pop();
      for (const line of lines) { if (line.trim()) handleEvent(JSON.parse(line)); }
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      const secs = ((performance.now() - startedAt) / 1000).toFixed(1);
      document.getElementById('statTime').textContent = secs + 's';
      document.querySelectorAll('#tbody tr[data-state="pending"]').forEach(tr => tr.remove());
      sortResultRows(); updateLiveCounts();
      setStatus(T('run.stopped', { ok: lastResults.working.length, bad: lastResults.failed.length }), false, false);
      if (lastResults.working.length || lastResults.failed.length) document.getElementById('dlReport').disabled = false;
    } else {
      setStatus(T('run.conn_error', { error: e.message }), true);
    }
  } finally {
    currentRun = null;
    setRunButton(false);
    if (document.getElementById('statTime').textContent === '…') document.getElementById('statTime').textContent = '0s';
  }
}

function handleEvent(evt) {
  if (evt.type === 'status') setStatus(evt.data.message, false, true);
  else if (evt.type === 'error') setStatus(evt.data.message, true);
  else if (evt.type === 'models_found') {
    setStatus(T('run.testing_n', { n: evt.data.count }), false, true);
    evt.data.models.forEach(addRow);
    updateLiveCounts();
  }
  else if (evt.type === 'model_result') {
    updateRow(evt.data);
    const done = lastResults.working.length + lastResults.failed.length;
    const total = document.querySelectorAll('#tbody tr[data-state]').length;
    setStatus(T('run.progress', { done, total }), false, true);
  }
  else if (evt.type === 'summary') {
    document.getElementById('statWorking').textContent = evt.data.working;
    document.getElementById('statFailed').textContent = evt.data.failed;
    document.getElementById('statTotal').textContent = evt.data.total;
    document.getElementById('statTime').textContent = evt.data.total_time_seconds + 's';
    lastConfig = evt.data.config;
    document.getElementById('dlReport').disabled = false;
    refreshCopyDD();
    sortResultRows();
    showBanner(evt.data);
    // The banner already carries the details; keep the status line short.
    setStatus(evt.data.key_status === 'INVALID' ? evt.data.key_message
              : T('run.finished', { t: evt.data.total_time_seconds }), evt.data.key_status === 'INVALID', false);
    if (evt.data.working > 0) toast(T('run.done_toast', { working: evt.data.working, total: evt.data.total }), 'ok');
    else toast(T('run.none_toast'), 'warn');
  }
  else if (evt.type === 'done') { /* end */ }
}

/* =========================================================================
   DOWNLOADS
   ========================================================================= */
function downloadBlob(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}
function downloadReport() { downloadBlob(lastResults, 'test_report.json'); }

/* =========================================================================
   SETTINGS MODAL
   ========================================================================= */
function openSettings() { applyTheme(); applyToastUI(); syncMonitorUI(); syncLangUI(); loadTelegram(); document.getElementById('settingsOverlay').classList.add('open'); }
function closeSettings() { document.getElementById('settingsOverlay').classList.remove('open'); }

/* ---- phone access: LAN listener + QR, token in the link (cookie after the first visit) ---- */
let phoneState = null;
function setPhoneBtn() { document.getElementById('phoneBtn').classList.toggle('active', !!(phoneState && phoneState.on)); }
async function syncPhone() {
  try { phoneState = await api('/api/phone'); setPhoneBtn(); } catch (e) {}
}
async function openPhone() {
  document.getElementById('phoneOverlay').classList.add('open');
  const body = document.getElementById('phoneBody');
  body.innerHTML = `<p class="modal-desc">${T('phone.opening')}</p>`;
  try { phoneState = await api('/api/phone/start', {}); }
  catch (e) { body.innerHTML = `<p class="modal-desc" style="color:var(--bad)">${escapeHtml(e.message)}</p>`; return; }
  setPhoneBtn(); renderPhone();
}
function closePhone() { document.getElementById('phoneOverlay').classList.remove('open'); }
function renderPhone() {
  const st = phoneState || {}, body = document.getElementById('phoneBody');
  if (!st.on) {
    body.innerHTML = `<p class="modal-desc">${T('phone.closed')}</p>
      <div class="phone-actions"><button class="primary" onclick="openPhone()">${T('phone.reopen')}</button></div>`;
    return;
  }
  body.innerHTML = `${st.here ? `<p class="modal-desc">${T('phone.here')}</p>` : ''}
    <p class="modal-desc">${T('phone.scan')}</p>
    <div class="qr-box">${st.qr}</div>
    <div class="phone-url"><code dir="ltr">${escapeHtml(st.url)}</code>
      <button class="mini-copy" title="${escapeHtml(T('common.copy'))}" onclick="copyRaw(phoneState && phoneState.url, this)">📋</button></div>
    <p class="phone-help">${T('phone.secret')}</p>
    <details class="phone-help"><summary>${T('phone.trouble')}</summary>
      <p>${T('phone.trouble_body')}</p>
      <code dir="ltr">sudo ufw allow from ${escapeHtml(st.ip.split('.').slice(0, 2).join('.'))}.0.0/16 to any port ${st.port} proto tcp</code></details>
    <div class="phone-actions"><button class="ghost danger" onclick="stopPhone()">${T('phone.stop')}</button></div>`;
}
async function stopPhone() {
  try { phoneState = await api('/api/phone/stop', {}); }
  catch (e) { toast(T('phone.stop_failed'), 'bad', e.message); return; }
  setPhoneBtn(); renderPhone();
  toast(T('phone.stopped'), 'ok', T('phone.stopped_body'));
}

/* ---- Telegram alerts (settings saved on the server, token/chat shown masked) ---- */
let tgState = null;
async function loadTelegram() {
  try { tgState = await api('/api/telegram'); } catch (e) { return; }
  renderTelegram();
}
function renderTelegram() {
  const t = tgState || {};
  document.getElementById('tgToggle').classList.toggle('on', !!(t.enabled && t.configured));
  document.getElementById('tgTokenSaved').innerHTML = t.bot_token ? T('tg.saved_value', { value: `<bdi dir="ltr">${escapeHtml(t.bot_token)}</bdi>` }) : '';
  document.getElementById('tgChatSaved').innerHTML = t.chat_id ? T('tg.saved_value', { value: `<bdi dir="ltr">${escapeHtml(t.chat_id)}</bdi>` }) : '';
  document.getElementById('tgIdle').value = t.idle_minutes ?? 10;
  document.getElementById('tgSummaryToggle').classList.toggle('on', !!t.summary);
  document.getElementById('tgSummaryTime').value = t.summary_time || '09:00';
}
function toggleSummary() {
  if (!(tgState || {}).configured) return toast(T('tg.fill_first'), 'warn');
  saveTelegram({ summary: !tgState.summary });
}
function tgForm() {
  return { bot_token: document.getElementById('tgToken').value.trim(), chat_id: document.getElementById('tgChat').value.trim(),
    idle_minutes: Number(document.getElementById('tgIdle').value || 0),
    summary_time: document.getElementById('tgSummaryTime').value || '09:00' };
}
async function saveTelegram(extra) {
  try { tgState = await api('/api/telegram/save', { ...tgForm(), ...(extra || {}) }); }
  catch (e) { toast(T('common.not_saved'), 'bad', e.message); return false; }
  document.getElementById('tgToken').value = ''; document.getElementById('tgChat').value = '';
  renderTelegram();
  toast(tgState.configured ? T('tg.saved') : T('tg.saved_incomplete'), tgState.configured ? 'ok' : 'warn');
  return true;
}
function toggleTelegram() {
  const t = tgState || {};
  if (!t.configured) return toast(T('tg.fill_first'), 'warn');
  saveTelegram({ enabled: !t.enabled });
}
async function testTelegram() {
  try { await api('/api/telegram/test', tgForm()); toast(T('tg.sent'), 'ok'); }
  catch (e) { toast(T('tg.send_failed'), 'bad', e.message); }
}
async function findTelegramChat() {
  try {
    const r = await api('/api/telegram/chat-id', { bot_token: document.getElementById('tgToken').value.trim() });
    document.getElementById('tgChat').value = r.chat_id;
    toast(T('tg.chat_found') + (r.name ? ` (${r.name})` : ''), 'ok', T('tg.press_save'));
  } catch (e) { toast(T('tg.not_found'), 'bad', e.message); }
}


/* =========================================================================
   LOCAL AI GATEWAY — providers, local key, logs, agents
   ========================================================================= */
let gwState = null;
let testerKeyRef = null;      // {provider, key}: tester uses a stored key server-side
let logFilter = 'all';
let logsTimer = null;
let showLocalKey = false;
const KEY_STATUS = {
  ok: ['ok', T('ks.ok')], unknown: ['pending', T('ks.unknown')], invalid: ['bad', T('ks.invalid')],
  limited: ['pending', T('ks.limited')], no_credit: ['bad', T('ks.no_credit')], error: ['bad', T('common.error')],
};

async function api(path, body) {
  const opts = { headers: { 'X-Console': '1', 'Content-Type': 'application/json' } };
  if (body !== undefined) { opts.method = 'POST'; opts.body = JSON.stringify(body); }
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch (e) {}
  if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
  return data;
}

async function loadGateway() {
  try { gwState = await api('/api/gateway/state'); }
  catch (e) { toast(T('gw.state_failed'), 'bad', e.message); return; }
  renderProviders(); renderGatewayInfo();
}

/* ---------------- providers ---------------- */
// Remaining quota, only as the provider reported it in its rate-limit headers (never guessed).
const QUOTA_LABEL = { requests: T('quota.requests'), tokens: T('quota.tokens'), requests_day: T('quota.requests_day'), tokens_day: T('quota.tokens_day') };
function fmtCount(n) { return n == null ? '?' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e4 ? (n / 1e3).toFixed(1) + 'k' : String(n); }
function quotaHtml(q) {
  const b = q && q.buckets ? Object.entries(q.buckets) : [];
  if (!b.length) return `<span class="quota" title="${escapeHtml(T('quota.not_reported_title'))}">${T('quota.not_reported')}</span>`;
  const parts = b.map(([name, v]) => {
    const low = v.limit && v.remaining < v.limit * 0.1;
    return `<span class="${low ? 'low' : ''}" title="${v.reset ? escapeHtml(T('quota.resets', { reset: v.reset })) : ''}">${QUOTA_LABEL[name] || escapeHtml(name)} <b dir="ltr">${fmtCount(v.remaining)}/${fmtCount(v.limit)}</b>${low ? ' ⚠️' : ''}</span>`;
  });
  return `<span class="quota">📊 ${parts.join(' · ')} <span class="q-at">${timeAgo(q.at * 1000)}</span></span>`;
}
function renderProviders() {
  const wrap = document.getElementById('providersList');
  if (!wrap || !gwState) return;
  const list = gwState.providers;
  if (!list.length) {
    wrap.innerHTML = `<div class="card"><div class="empty-state"><span class="em">🔌</span>${T('prov.empty')}</div></div>`;
    return;
  }
  wrap.innerHTML = list.map(p => {
    const ok = p.keys.filter(k => k.status === 'ok').length;
    const keys = p.keys.map(k => {
      const [cls, label] = KEY_STATUS[k.status] || ['pending', k.status];
      const cool = k.cooldown_left > 0 ? ` · ${fmtSecs(k.cooldown_left)}` : '';
      return `<div class="key-row">
        <span class="star" title="${escapeHtml(T('prov.active_key'))}">${k.active ? '★' : ''}</span>
        <span class="mk" dir="ltr" title="${escapeHtml(k.label || '')}">${escapeHtml(k.masked)}</span>
        <span class="st"><span class="pill ${cls}">${label}${cool}</span></span>
        <span class="err" title="${escapeHtml(k.last_error || '')}">${escapeHtml(k.last_error || (k.last_checked ? T('prov.tested_ago', { ago: timeAgo(k.last_checked * 1000) }) : ''))}</span>
        <span class="acts">
          <button class="ghost" title="${escapeHtml(T('prov.full_test'))}" onclick="openKeyInTester('${esc(p.id)}', '${esc(k.id)}')">🔬</button>
          <button class="ghost" title="${escapeHtml(T('prov.delete_key'))}" onclick="deleteProviderKey('${esc(p.id)}', '${esc(k.id)}')">🗑</button>
        </span>
        ${quotaHtml(k.quota)}
      </div>`;
    }).join('') || `<div class="empty-state" style="padding:14px;">${T('prov.no_keys')}</div>`;
    const models = p.models.length
      ? `<div class="model-tags" style="margin:0 0 10px;">${p.models.slice(0, 24).map(m => `<span class="model-tag">${escapeHtml(m)}</span>`).join('')}${p.models.length > 24 ? `<span class="model-tag">+${p.models.length - 24}</span>` : ''}</div>`
      : `<p class="modal-desc" style="margin:0 0 10px;">${T('prov.models_later')}</p>`;
    return `<div class="prov-card ${p.enabled ? '' : 'off'}">
      <div class="prov-head">
        <span class="name">${escapeHtml(p.name)}</span>
        <span class="fmt-badge">${p.format === 'anthropic' ? 'Anthropic' : 'OpenAI'}</span>
        <span class="pill ${ok ? 'ok' : 'pending'}">${T('prov.n_ok', { ok, n: p.keys.length })}</span>
        <span class="url" dir="ltr">${escapeHtml(p.base_url)}</span>
        <span class="acts">
          <button class="ghost" id="test-${escapeHtml(p.id)}" onclick="testProvider('${esc(p.id)}')">${T('prov.test_keys')}</button>
          <button class="ghost" title="${escapeHtml(T('common.edit'))}" onclick="editProvider('${esc(p.id)}')">✏️</button>
          <button class="ghost" title="${escapeHtml(T(p.enabled ? 'prov.pause' : 'prov.resume'))}" onclick="toggleProvider('${esc(p.id)}')">${p.enabled ? '⏸' : '▶'}</button>
          <button class="ghost" title="${escapeHtml(T('common.delete'))}" onclick="deleteProvider('${esc(p.id)}')">🗑</button>
        </span>
      </div>
      <div class="prov-body">
        ${models}
        ${keys}
        <div class="add-keys-box">
          <label for="addkeys-${escapeHtml(p.id)}">${T('prov.add_more', { name: escapeHtml(p.name) })} <span class="hint-inline">${T('prov.add_more_hint')}</span></label>
          <div class="add-keys">
            <textarea id="addkeys-${escapeHtml(p.id)}" class="masked" dir="ltr" rows="1" spellcheck="false" autocomplete="off"
              placeholder="${escapeHtml(T('prov.paste_ph'))}" oninput="checkNewKeys('${esc(p.id)}')"
              onkeydown="if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault();event.stopPropagation();addProviderKeys('${esc(p.id)}');}"></textarea>
            <button type="button" class="btn-icon" title="${escapeHtml(T('common.show_hide'))}" onclick="toggleAddKeysMask('${esc(p.id)}', this)">👁</button>
            <button class="primary add-btn" id="addkeys-btn-${escapeHtml(p.id)}" disabled onclick="addProviderKeys('${esc(p.id)}')">${T('prov.add_btn')}</button>
          </div>
          <div class="add-keys-msg" id="addkeys-msg-${escapeHtml(p.id)}"></div>
        </div>
        <p class="rotation-help">${T('prov.rotation_help')}</p>
      </div>
    </div>`;
  }).join('');
}
function fmtSecs(s) { return s >= 60 ? `${Math.round(s / 60)} ${T('unit.min')}` : `${s} ${T('unit.sec')}`; }

function resetProviderForm() {
  ['prov_id', 'prov_name', 'prov_base', 'prov_models'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('prov_format').value = 'openai';
  document.getElementById('provFormTitle').textContent = T('prov.new');
  document.getElementById('provFormCancel').style.display = 'none';
}
function editProvider(id) {
  const p = gwState.providers.find(x => x.id === id); if (!p) return;
  document.getElementById('prov_id').value = p.id;
  document.getElementById('prov_name').value = p.name;
  document.getElementById('prov_base').value = p.base_url;
  document.getElementById('prov_format').value = p.format;
  document.getElementById('prov_models').value = (p.manual_models || []).join(', ');
  document.getElementById('provFormTitle').textContent = T('common.edit_title', { name: p.name });
  document.getElementById('provFormCancel').style.display = '';
  document.getElementById('prov_name').focus();
}
async function saveProviderForm() {
  const body = {
    id: document.getElementById('prov_id').value || undefined,
    name: document.getElementById('prov_name').value.trim(),
    base_url: document.getElementById('prov_base').value.trim(),
    format: document.getElementById('prov_format').value,
    manual_models: document.getElementById('prov_models').value,
  };
  try { gwState = await api('/api/providers/save', body); }
  catch (e) { toast(T('prov.save_failed'), 'bad', e.message); return; }
  toast(T('common.saved_name', { name: body.name }), 'ok');
  resetProviderForm(); renderProviders(); renderGatewayInfo();
}
async function toggleProvider(id) {
  const p = gwState.providers.find(x => x.id === id); if (!p) return;
  try { gwState = await api('/api/providers/save', { id, name: p.name, base_url: p.base_url, format: p.format,
          manual_models: (p.manual_models || []).join(','), enabled: !p.enabled }); }
  catch (e) { toast(T('common.error'), 'bad', e.message); return; }
  renderProviders(); renderGatewayInfo();
}
async function deleteProvider(id) {
  const p = gwState.providers.find(x => x.id === id); if (!p) return;
  if (!confirm(T('prov.confirm_delete', { name: p.name, n: p.keys.length }))) return;
  gwState = await api('/api/providers/delete', { id });
  renderProviders(); renderGatewayInfo();
}
const keyCheckTimers = {};
function dupText(list) {
  return list.map(d => T('dup.item', { masked: d.masked, provider: d.provider })).join(T('common.list_sep'));
}
function dupHtml(list) {
  return list.map(d => T('dup.item', { masked: `<bdi dir="ltr">${escapeHtml(d.masked)}</bdi>`, provider: escapeHtml(d.provider) })).join(T('common.list_sep'));
}
function setAddKeysMsg(id, html, kind) {
  const el = document.getElementById('addkeys-msg-' + id);
  if (el) { el.className = 'add-keys-msg ' + (kind || ''); el.innerHTML = html; }
}
// Checks the pasted keys against every provider while you type.
function checkNewKeys(id) {
  const ta = document.getElementById('addkeys-' + id);
  const btn = document.getElementById('addkeys-btn-' + id);
  ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 160) + 'px';
  clearTimeout(keyCheckTimers[id]);
  if (!ta.value.trim()) { btn.disabled = true; setAddKeysMsg(id, ''); return; }
  keyCheckTimers[id] = setTimeout(async () => {
    let r;
    try { r = await api('/api/providers/keys/check', { keys: ta.value }); } catch (e) { return; }
    btn.disabled = !r.new.length;
    if (r.existing.length && !r.new.length)
      setAddKeysMsg(id, T('addkeys.exists_only', { list: dupHtml(r.existing) }), 'bad');
    else if (r.existing.length)
      setAddKeysMsg(id, T('addkeys.some_new', { n: r.new.length, list: dupHtml(r.existing) }), 'warn');
    else
      setAddKeysMsg(id, r.new.length === 1 ? T('addkeys.one_new') : T('addkeys.many_new', { n: r.new.length }), 'ok');
  }, 250);
}
function toggleAddKeysMask(id, btn) {
  const ta = document.getElementById('addkeys-' + id);
  ta.classList.toggle('masked');
  btn.textContent = ta.classList.contains('masked') ? '👁' : '🙈';
}
async function addProviderKeys(id) {
  const ta = document.getElementById('addkeys-' + id);
  if (!ta.value.trim()) { setAddKeysMsg(id, T('addkeys.paste_first'), 'warn'); ta.focus(); return; }
  let r;
  try { r = await api('/api/providers/keys/add', { provider_id: id, keys: ta.value }); }
  catch (e) { setAddKeysMsg(id, '⛔ ' + escapeHtml(e.message), 'bad'); return; }
  gwState = r;
  if (!r.added) {
    // Keep what was typed so the message stays next to it.
    setAddKeysMsg(id, T('addkeys.none_added', { list: dupHtml(r.duplicates) }), 'bad');
    toast(T('addkeys.exists_toast'), 'warn', dupText(r.duplicates));
    return;
  }
  renderProviders();
  if (r.duplicates.length) setAddKeysMsg(id, T('addkeys.added_skipped', { added: r.added, n: r.duplicates.length, list: dupHtml(r.duplicates) }), 'warn');
  else setAddKeysMsg(id, escapeHtml(T('addkeys.added', { n: r.added })), 'ok');
  toast(T('addkeys.added_toast', { n: r.added }), 'ok');
}
async function deleteProviderKey(pid, kid) {
  if (!confirm(T('prov.confirm_delete_key'))) return;
  gwState = await api('/api/providers/keys/delete', { provider_id: pid, key_id: kid });
  renderProviders();
}
async function testProvider(id) {
  const btn = document.getElementById('test-' + id);
  if (btn) { btn.disabled = true; btn.textContent = T('prov.testing'); }
  try {
    const r = await api('/api/providers/test', { id });
    gwState = r;
    const ok = r.results.filter(x => x.status === 'ok').length;
    toast(T('prov.test_result', { ok, n: r.results.length }), ok ? 'ok' : 'bad');
  } catch (e) { toast(T('prov.test_failed'), 'bad', e.message); }
  renderProviders(); renderGatewayInfo();
}
async function importProfilesToGateway() {
  const profiles = getProfiles();
  if (!profiles.length) { toast(T('import.none'), 'warn'); return; }
  try {
    const r = await api('/api/providers/import', { profiles });
    gwState = r;
    toast(T('import.done', { n: r.imported, total: profiles.length }), r.imported ? 'ok' : 'warn');
  } catch (e) { toast(T('import.failed'), 'bad', e.message); return; }
  renderProviders(); renderGatewayInfo();
}

/* Full test of a stored key in the tester tab: the real key stays on the server. */
function openKeyInTester(pid, kid) {
  const p = gwState.providers.find(x => x.id === pid);
  const k = p && p.keys.find(x => x.id === kid);
  if (!k) return;
  testerKeyRef = { provider: pid, key: kid };
  document.getElementById('base_url').value = p.base_url;
  const input = document.getElementById('api_key');
  input.type = 'text'; input.readOnly = true; input.classList.add('from-provider');
  input.value = `🔒 ${k.masked} (${p.name})`;
  document.getElementById('provider_id').value = p.name;
  document.getElementById('savedProfiles').value = '';
  showTab('test');
  setStatus(T('tester.from_provider'));
}
function clearTesterKeyRef() {
  if (!testerKeyRef) return;
  testerKeyRef = null;
  const input = document.getElementById('api_key');
  input.readOnly = false; input.classList.remove('from-provider'); input.type = 'password'; input.value = '';
}

/* ---------------- local key, models, logs ---------------- */
function renderGatewayInfo() {
  if (!gwState) return;
  document.getElementById('gwLocalKey').textContent = showLocalKey ? gwState.local_key : maskKey(gwState.local_key);
  const dgB = document.getElementById('dgBase'), dgK = document.getElementById('dgKey');
  if (dgB) dgB.textContent = gwState.anthropic_base;
  if (dgK) dgK.textContent = maskKey(gwState.local_key);
  document.getElementById('gwOpenAI').textContent = gwState.openai_base;
  document.getElementById('gwAnthropic').textContent = gwState.anthropic_base;
  document.getElementById('gwModelCount').textContent = gwState.models.length;
  document.getElementById('gwModels').innerHTML = gwState.models.length
    ? gwState.models.map(m => `<span class="model-tag">${escapeHtml(m)}</span>`).join('')
    : `<span class="modal-desc" style="margin:0;">${T('gw.no_models')}</span>`;
}
function toggleLocalKey() { showLocalKey = !showLocalKey; renderGatewayInfo(); }
async function regenerateLocalKey() {
  if (!confirm(T('gw.confirm_regen'))) return;
  try {
    const r = await api('/api/gateway/regenerate', {});
    gwState = r; renderGatewayInfo();
    toast(T('gw.regenerated'), 'ok', r.agents_updated.length ? T('gw.updated_agents', { list: r.agents_updated.join(', ') }) : '');
  } catch (e) { toast(T('common.error'), 'bad', e.message); }
}
function setLogFilter(f) {
  logFilter = f;
  document.querySelectorAll('[data-lf]').forEach(c => c.classList.toggle('on', c.dataset.lf === f));
  loadLogs();
}
async function loadLogs() {
  let logs = [];
  try { logs = (await api('/api/logs?limit=200')).logs; } catch (e) { return; }
  if (logFilter !== 'all') logs = logs.filter(l => l.status === logFilter);
  const body = document.getElementById('logsBody');
  if (!logs.length) { body.innerHTML = `<tr class="empty-row"><td colspan="7">${T('gw.no_requests')}</td></tr>`; return; }
  body.innerHTML = logs.map(l => {
    const t = new Date(l.time * 1000);
    const ok = l.status === 'ok';
    const tokens = (l.tokens_in != null || l.tokens_out != null) ? `${l.tokens_in ?? '?'} → ${l.tokens_out ?? '?'}` : '—';
    const fo = l.failovers ? ` <span class="sub-metric">${T('log.failovers', { n: l.failovers })}</span>` : '';
    return `<tr>
      <td class="mono" style="font-size:11.5px;">${t.toLocaleTimeString()}</td>
      <td>${escapeHtml(l.client || '')}<span class="sub-metric">${escapeHtml(l.format || '')}${l.stream ? ' · stream' : ''}</span></td>
      <td class="mono">${escapeHtml(l.model || '')}</td>
      <td>${escapeHtml(l.provider || '—')}<span class="sub-metric" dir="ltr">${escapeHtml(l.key || '')}</span>${fo}</td>
      <td><span class="pill ${ok ? 'ok' : 'bad'}">${ok ? T('log.ok') : T('common.error')} ${l.code || ''}</span>${l.error ? `<span class="sub-metric" style="white-space:normal; max-width:320px;" title="${escapeHtml(l.error)}">${escapeHtml(l.error.slice(0, 90))}</span>` : ''}</td>
      <td class="mono">${tokens}</td>
      <td class="mono">${l.ms != null ? (l.ms / 1000).toFixed(2) + 's' : '—'}</td>
    </tr>`;
  }).join('');
}
/* ---- alert history (every Telegram alert sent, from alert-history.jsonl) ---- */
const AH_TYPES = { no_credit: T('ah.type.no_credit'), invalid: T('ah.type.invalid'), limited: T('ah.type.limited'), error: T('ah.type.error'),
  provider_down: T('ah.type.provider_down'), recovered: T('ah.type.recovered'), quota_low: T('ah.type.quota_low'), idle: T('ah.type.idle'),
  active: T('ah.type.active'), summary: T('ah.type.summary') };
let ahEntries = [];
async function loadAlertHistory() {
  try { ahEntries = (await api('/api/alerts/history')).entries || []; } catch (e) { return; }
  const fill = (id, values, label) => {
    const el = document.getElementById(id), cur = el.value;
    el.innerHTML = el.options[0].outerHTML + values.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(label(v))}</option>`).join('');
    el.value = values.includes(cur) ? cur : '';
  };
  fill('ahSource', [...new Set(ahEntries.map(ahSource))].sort(), v => v);
  fill('ahType', [...new Set(ahEntries.map(e => e.type))], v => AH_TYPES[v] || v);
  renderAlertHistory();
}
function ahSource(e) { return e.provider || (e.agent ? '🤖 ' + e.agent : '—'); }
function renderAlertHistory() {
  const src = document.getElementById('ahSource').value, type = document.getElementById('ahType').value;
  const period = document.getElementById('ahPeriod').value, now = new Date();
  const from = period === 'month' ? new Date(now.getFullYear(), now.getMonth(), 1).getTime() / 1000
    : period === 'all' ? 0 : now.getTime() / 1000 - Number(period) * 86400;
  const list = ahEntries.filter(e => e.time >= from && (!src || ahSource(e) === src) && (!type || e.type === type));
  // Counts: one row per provider/agent, one column per alert type (e.g. how often a provider ran out of credit).
  const types = Object.keys(AH_TYPES).filter(t => list.some(e => e.type === t));
  const rows = {};
  list.forEach(e => { const r = rows[ahSource(e)] = rows[ahSource(e)] || {}; r[e.type] = (r[e.type] || 0) + 1; });
  document.getElementById('ahCounts').innerHTML = list.length ? `<div style="overflow-x:auto;"><table class="ah-counts">
    <thead><tr><th></th>${types.map(t => `<th>${AH_TYPES[t]}</th>`).join('')}<th>${T('ah.total')}</th></tr></thead>
    <tbody>${Object.entries(rows).sort((a, b) => a[0].localeCompare(b[0])).map(([name, r]) => `<tr><td>${escapeHtml(name)}</td>
      ${types.map(t => `<td class="mono">${r[t] || '·'}</td>`).join('')}<td class="mono"><b>${Object.values(r).reduce((a, b) => a + b, 0)}</b></td></tr>`).join('')}</tbody>
    </table></div>` : `<div class="empty-state" style="padding:14px;">${T('ah.none')}</div>`;
  document.getElementById('ahList').innerHTML = list.slice(0, 100).map(e => `<div class="ah-row">
      <span class="mono ah-time">${new Date(e.time * 1000).toLocaleString()}</span>
      <span class="pill ${e.sent === false ? 'bad' : 'pending'}">${AH_TYPES[e.type] || escapeHtml(e.type)}${e.sent === false ? T('ah.not_sent') : ''}</span>
      <span class="ah-src">${escapeHtml(ahSource(e))}</span>
      <span class="ah-msg" title="${escapeHtml(e.error || '')}">${escapeHtml(String(e.message || '').split('\n').slice(1).join(' · '))}</span>
    </div>`).join('') + (list.length > 100 ? `<div class="ah-more">${T('ah.more', { n: list.length - 100 })}</div>` : '');
}
async function clearGatewayLogs() {
  if (!confirm(T('gw.confirm_clear'))) return;
  await api('/api/logs/clear', {}); loadLogs();
}

/* ---------------- agents ---------------- */
const AGENT_ICON = { claude: '✳️', opencode: '⌨️', pi: 'π', hermes: '🪽' };
let agentsCache = [];
let iconTarget = null;
async function loadAgents() {
  let r;
  try { r = await api('/api/agents'); } catch (e) { toast(T('agents.load_failed'), 'bad', e.message); return; }
  reasoningModels = new Set(r.reasoning_models || []);
  renderAgents(r.agents, r.models);
}
function agentIconHtml(a) {
  return a.icon ? `<img src="${escapeHtml(a.icon)}" alt="">` : (AGENT_ICON[a.id] || '🤖');
}
// Models an agent shows: the ones picked in 🎯 (that still exist), otherwise all of them.
function agentVisibleModels(a, models) {
  const picked = new Set(a.model_filter || []);
  const shown = models.filter(m => picked.has(m));
  return shown.length ? shown : models;
}
function renderAgents(list, models) {
  agentsCache = list;
  agentModelsAll = models || [];
  const wrap = document.getElementById('agentsList');
  wrap.innerHTML = list.map(a => {
    const shown = agentVisibleModels(a, models);
    const filtered = shown.length < models.length;
    const want = agentPick[a.id] || a.model;
    const cur = shown.includes(want) ? want : (shown[0] || '');
    const pill = !a.installed ? `<span class="pill bad">${T('agents.not_installed')}</span>`
      : a.enabled ? `<span class="pill ok">${T('agents.enabled')}</span>` : `<span class="pill pending">${T('agents.original')}</span>`;
    const warn = a.id === 'claude'
      ? `<div class="modal-desc" style="margin:0;">${a.enabled ? '' : T('agents.claude_new')}${T('agents.claude_note')}</div>` : '';
    let custom = '';
    if (a.spec) {
      const f = a.spec.fields, cur = a.current || {};
      const row = (label, field, key) => field
        ? `<span class="fk">${label}</span><span class="fv">${escapeHtml(field)} = ${cur[key] == null ? `<i>${T('agents.field_missing')}</i>` : escapeHtml(cur[key])}</span>` : '';
      custom = `<div class="fields" dir="ltr">${row('URL', f.base_url, 'base_url')}${row('Key', f.api_key, 'api_key')}${row('Model', f.model, 'model')}</div>`;
    }
    const acts = a.builtin ? '' : `<span class="card-acts">
        <button title="${escapeHtml(T('common.edit'))}" onclick="openAgentForm('${esc(a.id)}')">✏️</button>
        <button title="${escapeHtml(T('common.delete'))}" onclick="deleteCustomAgent('${esc(a.id)}')">🗑</button></span>`;
    return `<div class="agent-card ${a.enabled ? 'on' : ''}">
      <div class="top">
        <button class="agent-ic" title="${escapeHtml(T('agents.change_icon'))}" onclick="openIconMenu(event, '${esc(a.id)}')">${agentIconHtml(a)}</button>
        <div style="min-width:0;"><div class="nm">${escapeHtml(a.name)}</div>
          <div class="ver">${a.spec ? `<span class="kind">${a.spec.kind === 'env' ? '.env' : 'JSON'} · ${a.spec.format === 'anthropic' ? 'Anthropic' : 'OpenAI'}</span>` : escapeHtml(a.version || '')}</div></div>
        ${pill}${acts}</div>
      <div class="meta" dir="ltr">${escapeHtml(a.config_path)}${a.config_exists ? '' : ' ' + T('agents.will_create')}</div>
      ${custom}
      ${a.enabled && a.backup ? `<div class="meta" dir="ltr">backup: ${escapeHtml(a.backup)}</div>` : ''}
      ${a.note ? `<div class="note">⚠️ ${escapeHtml(a.note)}</div>` : ''}
      ${warn}
      <div class="agent-model-block">
        <div class="mdd" id="mdd-${a.id}">
          <input type="hidden" id="agentModel-${a.id}" value="${escapeHtml(cur)}">
          <button type="button" class="mdd-field" ${a.installed && models.length ? '' : 'disabled'} onclick="toggleModelDropdown('${esc(a.id)}')">
            <span class="mdd-val" dir="ltr" id="mddVal-${a.id}">${models.length ? escapeHtml(cur) : T('agents.no_models')}</span>
            ${filtered ? `<span class="mdd-badge">☑ ${shown.length}</span>` : ''}
            <svg class="mdd-chev" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6l4 4 4-4"/></svg>
          </button>
          <div class="mdd-panel">
            <input class="mdd-search" id="mddSearch-${a.id}" dir="auto" placeholder="${escapeHtml(T('agents.search_ph'))}" autocomplete="off"
              oninput="renderModelDropdown()" onkeydown="modelDropdownKey(event)">
            <div class="mdd-list" id="mddList-${a.id}"></div>
            <div class="mdd-foot"><span class="mdd-count" id="mddCount-${a.id}"></span>
              <button type="button" onclick="markShownModels(true)" title="${escapeHtml(T('agents.mark_shown_title'))}">${T('agents.mark_shown')}</button>
              <button type="button" onclick="markShownModels(false)" title="${escapeHtml(T('agents.unmark_title'))}">${T('agents.unmark')}</button></div>
          </div>
        </div>
        <div id="effortRow-${a.id}">${effortSelectHtml(a.id, cur, a.installed && models.length)}</div>
        ${filtered ? `<div class="agent-filter-info">${T('agents.filter_info', { shown: shown.length, total: models.length })}</div>` : ''}
      </div>
      <div class="row">
        <button class="primary" ${a.installed && models.length ? '' : 'disabled'} onclick="enableAgent('${esc(a.id)}')">${a.enabled ? T('agents.update') : T('agents.enable')}</button>
        <button class="ghost" ${a.enabled ? '' : 'disabled'} onclick="disableAgent('${esc(a.id)}')">${T('agents.disable')}</button>
      </div>
    </div>`;
  }).join('');
}
// Every word typed must appear in the model name (case-insensitive): "gpt 4o mini".
function modelMatches(m, query) {
  const name = m.toLowerCase();
  return query.toLowerCase().split(/\s+/).filter(Boolean).every(w => name.includes(w));
}
/* ---- thinking level: sent as "model@level", the gateway turns it into the provider's own parameter ---- */
const EFFORT_LEVELS = [['', T('effort.default')], ['none', T('effort.none')], ['minimal', T('effort.minimal')],
  ['low', T('effort.low')], ['medium', T('effort.medium')], ['high', T('effort.high')], ['xhigh', T('effort.xhigh')], ['max', T('effort.max')]];
let reasoningModels = new Set();  // models whose name says they can think (from the server)
let agentEffortPick = {};         // level chosen in the select, kept across re-renders
function agentEffort(id) {
  if (id in agentEffortPick) return agentEffortPick[id];
  const a = agentsCache.find(x => x.id === id);
  return (a && a.effort) || '';
}
function effortSelectHtml(id, model, usable) {
  if (!model) return '';
  if (!reasoningModels.has(model))
    return `<div class="effort-na">${T('effort.na')}</div>`;
  const cur = agentEffort(id);
  return `<label class="effort-row" title="${escapeHtml(T('effort.title'))}">
    <span>${T('effort.label')}</span>
    <select id="agentEffort-${id}" ${usable ? '' : 'disabled'} onchange="agentEffortPick['${esc(id)}'] = this.value">
      ${EFFORT_LEVELS.map(([v, t]) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${t}</option>`).join('')}
    </select></label>`;
}
// The level to send: none when the chosen model doesn't support thinking.
function effortValue(id) {
  const el = document.getElementById('agentEffort-' + id);
  return el ? el.value : '';
}
/* ---- model dropdown: click → search; click a name = the agent's model; ☑ = shows in the agent ---- */
let agentModelsAll = [];
let agentPick = {};              // model chosen in the dropdown, kept across re-renders
let mdd = null;                  // { id, marked:Set, dirty }
function toggleModelDropdown(id) {
  if (mdd && mdd.id === id) return closeModelDropdown();
  closeModelDropdown();
  const a = agentsCache.find(x => x.id === id);
  if (!a) return;
  mdd = { id, marked: new Set((a.model_filter || []).filter(m => agentModelsAll.includes(m))), dirty: false };
  document.getElementById('mdd-' + id).classList.add('open');
  const search = document.getElementById('mddSearch-' + id);
  search.value = '';
  renderModelDropdown();
  search.focus();
  const sel = document.querySelector(`#mddList-${CSS.escape(id)} .mdd-row.sel`);
  if (sel) sel.scrollIntoView({ block: 'nearest' });
}
function mddHits() {
  const q = document.getElementById('mddSearch-' + mdd.id).value.trim();
  return q ? agentModelsAll.filter(m => modelMatches(m, q)) : agentModelsAll;
}
function renderModelDropdown() {
  if (!mdd) return;
  const cur = document.getElementById('agentModel-' + mdd.id).value;
  document.getElementById('mddList-' + mdd.id).innerHTML = mddHits().map(m => {
    const i = agentModelsAll.indexOf(m);
    return `<div class="mdd-row ${m === cur ? 'sel' : ''}" onclick="chooseDropdownModel(${i})">
      <input type="checkbox" title="${escapeHtml(T('agents.show_in_agent'))}" ${mdd.marked.has(m) ? 'checked' : ''}
        onclick="event.stopPropagation()" onchange="markDropdownModel(${i}, this.checked)">
      <span class="mdd-name" dir="ltr">${escapeHtml(m)}</span></div>`;
  }).join('') || `<div class="mdd-empty">${T('agents.none_found')}</div>`;
  updateDropdownCount();
}
function updateDropdownCount() {
  const n = mdd.marked.size;
  document.getElementById('mddCount-' + mdd.id).textContent = n
    ? T('agents.count_marked', { n, total: agentModelsAll.length }) : T('agents.count_none');
}
function setDropdownValue(m) {
  document.getElementById('agentModel-' + mdd.id).value = m;
  document.getElementById('mddVal-' + mdd.id).textContent = m;
  agentPick[mdd.id] = m;
  document.getElementById('effortRow-' + mdd.id).innerHTML = effortSelectHtml(mdd.id, m, true);
}
// When models are marked, the agent's model has to be one of them.
function keepValueInMarked() {
  const cur = document.getElementById('agentModel-' + mdd.id).value;
  if (mdd.marked.size && !mdd.marked.has(cur)) setDropdownValue(agentModelsAll.find(m => mdd.marked.has(m)));
}
function markDropdownModel(i, on) {
  const m = agentModelsAll[i];
  if (on) mdd.marked.add(m); else mdd.marked.delete(m);
  mdd.dirty = true;
  keepValueInMarked();
  renderModelDropdown();
}
function markShownModels(on) {
  mddHits().forEach(m => on ? mdd.marked.add(m) : mdd.marked.delete(m));
  mdd.dirty = true;
  keepValueInMarked();
  renderModelDropdown();
}
function chooseDropdownModel(i) {
  const m = agentModelsAll[i];
  if (mdd.marked.size && !mdd.marked.has(m)) { mdd.marked.add(m); mdd.dirty = true; }
  setDropdownValue(m);
  closeModelDropdown();
}
function modelDropdownKey(e) {
  if (e.key === 'Enter') {
    e.preventDefault();
    const first = mddHits()[0];
    if (first) chooseDropdownModel(agentModelsAll.indexOf(first));
  } else if (e.key === 'Escape') { e.stopPropagation(); closeModelDropdown(); }
}
async function closeModelDropdown() {
  if (!mdd) return;
  const { id, marked, dirty } = mdd;
  mdd = null;
  const box = document.getElementById('mdd-' + id);
  if (box) box.classList.remove('open');
  if (!dirty) return;
  const models = agentModelsAll.filter(m => marked.has(m));
  try {
    const r = await api('/api/agents/models', { agent: id, models, model: document.getElementById('agentModel-' + id).value,
      effort: effortValue(id) });
    renderAgents(r.agents, agentModelsAll);
    const a = r.agents.find(x => x.id === id);
    toast(models.length ? T('agents.marked_toast', { n: models.length, name: a ? a.name : T('agents.the_agent') }) : T('agents.all_shown'), 'ok',
      a && a.enabled ? T('agents.restart_hint') : '');
  } catch (e) { toast(T('common.not_saved'), 'bad', e.message); }
}
document.addEventListener('mousedown', e => {
  if (mdd && !e.target.closest('#mdd-' + CSS.escape(mdd.id))) closeModelDropdown();
});

async function enableAgent(id) {
  const model = document.getElementById('agentModel-' + id).value;
  try {
    const r = await api('/api/agents/enable', { agent: id, model, effort: effortValue(id) });
    renderAgents(r.agents, gwState ? gwState.models : []);
    toast(T('agents.enabled_toast'), 'ok');
  } catch (e) { toast(T('agents.enable_failed'), 'bad', e.message); }
  loadAgents();
}
async function disableAgent(id) {
  try {
    const r = await api('/api/agents/disable', { agent: id });
    toast(r.restored === 'exact' ? T('agents.restored_exact') : T('agents.restored_values'), 'ok');
  } catch (e) { toast(T('common.error'), 'bad', e.message); }
  loadAgents();
}

/* ---- custom agents ---- */
function openAgentForm(id) {
  const a = id ? agentsCache.find(x => x.id === id) : null;
  const s = a && a.spec;
  document.getElementById('ag_id').value = s ? s.id : '';
  document.getElementById('ag_name').value = s ? s.name : '';
  document.getElementById('ag_kind').value = s ? s.kind : 'json';
  document.getElementById('ag_path').value = s ? s.path : '';
  document.getElementById('ag_format').value = s ? s.format : 'openai';
  document.getElementById('ag_f_base').value = s ? s.fields.base_url : '';
  document.getElementById('ag_f_key').value = s ? s.fields.api_key : '';
  document.getElementById('ag_f_model').value = s ? (s.fields.model || '') : '';
  document.getElementById('agentFormTitle').textContent = s ? T('common.edit_title', { name: s.name }) : T('agents.form_add');
  updateAgentFormHints();
  document.getElementById('agentOverlay').classList.add('open');
  document.getElementById('ag_name').focus();
}
function closeAgentForm() { document.getElementById('agentOverlay').classList.remove('open'); }
function updateAgentFormHints() {
  const env = document.getElementById('ag_kind').value === 'env';
  const fmt = document.getElementById('ag_format').value;
  document.getElementById('ag_path').placeholder = env ? '~/.config/tool/.env' : '~/.config/tool/config.json';
  document.getElementById('ag_f_base').placeholder = env ? (fmt === 'anthropic' ? 'ANTHROPIC_BASE_URL' : 'OPENAI_BASE_URL') : 'provider.myproxy.options.baseURL';
  document.getElementById('ag_f_key').placeholder = env ? (fmt === 'anthropic' ? 'ANTHROPIC_API_KEY' : 'OPENAI_API_KEY') : 'provider.myproxy.options.apiKey';
  document.getElementById('ag_f_model').placeholder = env ? 'MODEL' : 'model';
  const root = gwState ? gwState.root : 'http://127.0.0.1:8000';
  const url = fmt === 'openai' ? root + '/v1' : root;
  const b = document.getElementById('ag_f_base').value || document.getElementById('ag_f_base').placeholder;
  const k = document.getElementById('ag_f_key').value || document.getElementById('ag_f_key').placeholder;
  const m = document.getElementById('ag_f_model').value;
  document.getElementById('agentPreview').textContent =
    `${T('agents.preview')}\n  ${b} = ${url}\n  ${k} = sk-local-…` + (m ? `\n  ${m} = ${T('agents.preview_model')}` : '');
}
['ag_f_base', 'ag_f_key', 'ag_f_model'].forEach(id => document.addEventListener('input', e => { if (e.target.id === id) updateAgentFormHints(); }));
async function saveAgentForm() {
  const body = {
    id: document.getElementById('ag_id').value || undefined,
    name: document.getElementById('ag_name').value,
    kind: document.getElementById('ag_kind').value,
    path: document.getElementById('ag_path').value,
    format: document.getElementById('ag_format').value,
    fields: { base_url: document.getElementById('ag_f_base').value, api_key: document.getElementById('ag_f_key').value,
              model: document.getElementById('ag_f_model').value },
  };
  try {
    const r = await api('/api/agents/custom/save', body);
    renderAgents(r.agents, gwState ? gwState.models : []);
    closeAgentForm();
    toast(T('agents.saved', { name: body.name }), 'ok');
  } catch (e) { toast(T('common.not_saved'), 'bad', e.message); }
}
async function deleteCustomAgent(id) {
  const a = agentsCache.find(x => x.id === id);
  if (!confirm(T('agents.confirm_delete', { name: a ? a.name : '' }))) return;
  try {
    const r = await api('/api/agents/custom/delete', { agent: id });
    renderAgents(r.agents, gwState ? gwState.models : []);
  } catch (e) { toast(T('agents.delete_failed'), 'bad', e.message); }
}

/* ---- agent icons ---- */
function openIconMenu(e, id) {
  e.stopPropagation();
  iconTarget = id;
  const a = agentsCache.find(x => x.id === id);
  document.getElementById('iconResetBtn').disabled = !(a && a.icon);
  const menu = document.getElementById('iconMenu');
  const r = e.currentTarget.getBoundingClientRect();
  menu.classList.add('open');
  const w = menu.offsetWidth;
  menu.style.top = Math.min(window.innerHeight - menu.offsetHeight - 8, r.bottom + 6) + 'px';
  menu.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, RTL ? r.right - w : r.left)) + 'px';
}
document.addEventListener('click', () => document.getElementById('iconMenu')?.classList.remove('open'));
function pickIconFile() { document.getElementById('iconFile').click(); }
function renderIconToDataUrl(drawFn) {
  const c = document.createElement('canvas'); c.width = c.height = 96;
  const ctx = c.getContext('2d'); drawFn(ctx);
  return c.toDataURL('image/png');
}
function iconFileChosen(input) {
  const file = input.files && input.files[0];
  input.value = '';
  if (!file || !iconTarget) return;
  if (!file.type.startsWith('image/')) { toast(T('icon.pick_image'), 'warn'); return; }
  const img = new Image();
  img.onload = () => {
    // Center-crop to a square and shrink to 96x96 so it stays small on disk.
    const side = Math.min(img.width, img.height);
    const data = renderIconToDataUrl(ctx => ctx.drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side, 0, 0, 96, 96));
    URL.revokeObjectURL(img.src);
    saveIcon(iconTarget, data);
  };
  img.onerror = () => toast(T('icon.read_failed'), 'bad');
  img.src = URL.createObjectURL(file);
}
function pickIconEmoji() {
  const em = prompt(T('icon.emoji_prompt'), '🤖');
  if (!em || !iconTarget) return;
  const data = renderIconToDataUrl(ctx => {
    ctx.font = '72px "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(em.trim().slice(0, 8), 48, 54);
  });
  saveIcon(iconTarget, data);
}
async function resetIcon() { if (iconTarget) saveIcon(iconTarget, ''); }
async function saveIcon(id, image) {
  try {
    const r = await api('/api/agents/icon', { agent: id, image });
    renderAgents(r.agents, gwState ? gwState.models : []);
    toast(image ? T('icon.changed') : T('icon.restored'), 'ok');
  } catch (e) { toast(T('icon.failed'), 'bad', e.message); }
}

function onTabShown(name) {
  if (logsTimer) { clearInterval(logsTimer); logsTimer = null; }
  if (name === 'providers') loadGateway();
  if (name === 'gateway') { loadGateway(); loadLogs(); loadAlertHistory(); logsTimer = setInterval(loadLogs, 4000); }
  if (name === 'agents') { loadGateway(); loadAgents(); }
}

/* =========================================================================
   INIT
   ========================================================================= */
document.addEventListener('DOMContentLoaded', async () => {
  applyTheme(); applyToastUI(); initNav(); syncLangUI(); syncPhone();
  await initServerStore();
  refreshProfileSelect();
  updateKeyStats();
  initMonitorUI();
  refreshCopyDD();
  updateAdvancedNote();
  ['models', 'filter', 'capabilities'].forEach(id =>
    document.getElementById(id).addEventListener('input', updateAdvancedNote));
  document.getElementById('capabilities').addEventListener('change', updateAdvancedNote);
  showTab(getPrefs().tab || 'test');
  // Typing a key by hand (or pasting/clearing) stops using a stored provider key.
  document.getElementById('api_key').addEventListener('focus', clearTesterKeyRef);
  document.getElementById('base_url').addEventListener('input', clearTesterKeyRef);
  setInterval(() => { if (monitorNextRunAt) updateNextRunLabel(); }, 1000);
  const toTop = document.getElementById('toTop');
  window.addEventListener('scroll', () => toTop.classList.toggle('show', window.scrollY > 500), { passive: true });
});

// Keyboard: Ctrl/Cmd+Enter runs (or stops) the test, Esc closes any open window.
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); showTab('test'); runOrStop(); }
  else if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
    document.querySelectorAll('.dd.open, .copy-dd.open').forEach(d => d.classList.remove('open'));
  }
});
</script>
</body>
</html>
"""



# ===========================================================================
# 4) السيرفر نفسه
# ===========================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "APITestConsole/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[server] " + (fmt % args) + "\n")

    # ---- helpers -----------------------------------------------------------
    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _admin_ok(self):
        """
        The dashboard API can read stored keys' status and rewrite agent configs,
        so only the page itself may call it: local Host (blocks DNS rebinding),
        and a custom header, which other websites can't send without a CORS
        preflight this server never approves.
        """
        allowed = ((self.server.server_address[0],) if getattr(self.server, "lan", False)
                   else ("127.0.0.1", "localhost", "::1"))   # phone access: only the LAN address itself
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in allowed:
            return False
        return self.headers.get("X-Console") == "1"

    def _lan_gate(self):
        """Requests on the phone (LAN) listener need the token: ?token= once (saved as a cookie), then the cookie.
        Returns True when the request may continue; otherwise the answer was already sent."""
        if not getattr(self.server, "lan", False):
            return True
        if not phone.is_private(self.client_address[0]):
            self._deny(403, i18n.t("phone.deny_lan"))
            return False
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if self.command == "GET" and phone.token_ok((query.get("token") or [""])[0]):
            rest = "&".join(f"{k}={v}" for k, vals in query.items() if k != "token" for v in vals)
            self.send_response(303)
            self.send_header("Set-Cookie", f"{phone.COOKIE}={phone.token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            self.send_header("Location", url.path + ("?" + rest if rest else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        try:
            jar = SimpleCookie(self.headers.get("Cookie") or "")
        except Exception:
            jar = {}
        if phone.token_ok(jar[phone.COOKIE].value if phone.COOKIE in jar else ""):
            return True
        self._deny(401, i18n.t("phone.deny_token"))
        return False

    def _deny(self, status, message):
        if self.path.startswith(("/api/", "/v1/")):
            return self._json(status, {"error": message})
        lang = i18n.get_lang()
        body = (f'<!doctype html><html lang="{lang}" dir="{i18n.direction(lang)}"><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>EskaGate</title>'
                f'<body style="font-family:system-ui,sans-serif;background:#0f1220;color:#e8eaf6;display:grid;'
                f'place-items:center;min-height:90vh;margin:0;padding:16px;text-align:center">'
                f'<p style="font-size:18px;line-height:1.7">{html.escape(message)}</p></body></html>').encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _phone_state(self):
        st = phone.status()
        if st.get("on"):
            st["qr"] = qr.svg(st["url"])
        st["here"] = bool(getattr(self.server, "lan", False))   # page opened from the phone itself
        return st

    def _gateway_root(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def _state(self):
        st = gateway.store()
        root = self._gateway_root()
        models = st.all_models()
        return {"local_key": st.local_key, "root": root, "openai_base": root + "/v1",
                "anthropic_base": root, "providers": st.public_view(), "models": models,
                "reasoning_models": [m for m in models if gateway.supports_reasoning(m)],
                "data_dir": str(gateway.DATA_DIR)}

    # ---- routes ------------------------------------------------------------
    def do_GET(self):
        if not self._lan_gate():
            return
        path = urlparse(self.path).path
        if gateway.is_gateway_path(path):
            return gateway.handle(self, "GET")
        if path in ("/", "/index.html"):
            body = render_index(i18n.get_lang())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/"):
            if not self._admin_ok():
                return self._json(403, {"error": "Forbidden"})
            if path == "/api/gateway/state":
                return self._json(200, self._state())
            if path == "/api/phone":
                return self._json(200, self._phone_state())
            if path == "/api/telegram":
                return self._json(200, alerts.public())
            if path == "/api/alerts/history":
                return self._json(200, {"entries": alerts.history()})
            if path == "/api/logs":
                limit = int((parse_qs(urlparse(self.path).query).get("limit") or ["200"])[0])
                return self._json(200, {"logs": gateway.get_logs(min(limit, gateway.MAX_LOGS))})
            if path.startswith("/api/store/") and path.rsplit("/", 1)[1] in UI_STORES:
                return self._json(200, {"value": ui_store_read(path.rsplit("/", 1)[1])})
            if path == "/api/agents":
                models = gateway.store().all_models()
                return self._json(200, {"agents": agents.list_agents(self._gateway_root()), "models": models,
                                        "reasoning_models": [m for m in models if gateway.supports_reasoning(m)]})
        self.send_error(404, "Not found")

    def do_POST(self):
        if not self._lan_gate():
            return
        path = urlparse(self.path).path
        if gateway.is_gateway_path(path):
            return gateway.handle(self, "POST")
        if not path.startswith("/api/"):
            return self.send_error(404, "Not found")
        if not self._admin_ok():
            return self._json(403, {"error": "Forbidden"})
        try:
            params = self._body()
        except Exception:
            return self._json(400, {"error": "Invalid JSON body"})
        if path == "/api/run":
            return self._run_tester(params)
        if path == "/api/settings":
            try:
                i18n.set_lang(params.get("lang"))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, {"ok": True, "lang": i18n.get_lang()})
        if path.startswith("/api/store/"):
            # Whole-list writes come from a page opened before this version; they would undo other tabs' changes.
            if "ops" not in params:
                return self._json(409, {"error": i18n.t("srv.page_old")})
            try:
                value = ui_store_apply(path.rsplit("/", 1)[1], params.get("ops"))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, {"ok": True, "value": value})
        try:
            return self._json(200, self._admin(path, params))
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _admin(self, path, p):
        root = self._gateway_root()
        if path == "/api/providers/save":
            gateway.save_provider(p)
        elif path == "/api/providers/delete":
            gateway.delete_provider(p.get("id"))
        elif path == "/api/providers/keys/add":
            result = gateway.add_keys(p.get("provider_id"), p.get("keys"), p.get("label", ""))
            return {**self._state(), **result}
        elif path == "/api/providers/keys/check":
            return gateway.check_keys(p.get("keys"))
        elif path == "/api/providers/keys/delete":
            gateway.delete_key(p.get("provider_id"), p.get("key_id"))
        elif path == "/api/providers/test":
            results = gateway.test_provider_keys(p.get("id"), TESTER)
            return {**self._state(), "results": results}
        elif path == "/api/providers/import":
            return {**self._state(), "imported": import_profiles(p.get("profiles") or [])}
        elif path == "/api/gateway/regenerate":
            gateway.regenerate_local_key()
            return {**self._state(), "agents_updated": agents.refresh_enabled(root)}
        elif path == "/api/phone/start":
            phone.start(Handler, self.server.server_address[1])
            return self._phone_state()
        elif path == "/api/phone/stop":
            phone.stop()
            return self._phone_state()
        elif path == "/api/telegram/save":
            return alerts.save(p)
        elif path == "/api/telegram/test":
            alerts.test(p)
            return {"ok": True}
        elif path == "/api/telegram/chat-id":
            return alerts.find_chat_id(p)
        elif path == "/api/logs/clear":
            gateway.clear_logs()
            return {"ok": True}
        elif path == "/api/agents/enable":
            agents.enable(p.get("agent"), root, p.get("model"), p.get("effort") or "")
            return {"agents": agents.list_agents(root)}
        elif path == "/api/agents/models":
            agents.set_model_filter(p.get("agent"), p.get("models"), root, p.get("model"), p.get("effort"))
            return {"agents": agents.list_agents(root)}
        elif path == "/api/agents/disable":
            restored = agents.disable(p.get("agent"))
            return {"agents": agents.list_agents(root), "restored": restored}
        elif path == "/api/agents/custom/save":
            agents.save_custom(p)
            return {"agents": agents.list_agents(root)}
        elif path == "/api/agents/custom/delete":
            agents.delete_custom(p.get("agent"))
            return {"agents": agents.list_agents(root)}
        elif path == "/api/agents/icon":
            agents.set_icon(p.get("agent"), p.get("image") or "")
            return {"agents": agents.list_agents(root)}
        else:
            raise ValueError(i18n.t("srv.unknown_action"))
        return self._state()

    def _run_tester(self, params):
        # A stored provider key can be tested without it ever reaching the browser.
        if params.get("key_ref"):
            base, key = gateway.resolve_key_ref(params["key_ref"])
            if not key:
                return self._json(400, {"error": i18n.t("srv.stored_key_missing")})
            params["api_key"] = key
            params["base_url"] = params.get("base_url") or base

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event_type, data):
            line = json.dumps({"type": event_type, "data": data}, ensure_ascii=False) + "\n"
            try:
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                raise StopStreaming

        try:
            run_pipeline(params, emit)
        except StopStreaming:
            pass  # المستخدم سد الصفحة قبل ما يسالي الاختبار
        except Exception as e:
            try:
                emit("error", {"message": i18n.t("srv.unexpected", error=e)})
            except StopStreaming:
                pass

        self.close_connection = True


# The page in one language: {{t:key}} -> escaped text, {{h:key}} -> trusted HTML from the
# translation file, {{i18n:lang|dir|json}} -> <html> attributes and the dictionary for the JS T().
_pages = {}


def render_index(lang):
    if lang not in _pages:
        words = {**i18n.load(i18n.DEFAULT_LANG), **i18n.load(lang)}
        extra = {"lang": lang, "dir": i18n.direction(lang),
                 "json": json.dumps(words, ensure_ascii=False).replace("</", "<\\/")}

        def sub(m):
            kind, key = m.groups()
            if kind == "i18n":
                return extra[key]
            text = i18n.t(key, lang)
            return text if kind == "h" else html.escape(text)
        _pages[lang] = re.sub(r"\{\{(t|h|i18n):([\w.]+)\}\}", sub, INDEX_HTML).encode("utf-8")
    return _pages[lang]


# Saved keys ("مفاتيحي") and custom formats, kept next to the gateway data with
# owner-only permissions instead of in one browser's localStorage.
UI_STORES = ("profiles", "formats")


def ui_store_read(name):
    if name not in UI_STORES:
        raise ValueError("Unknown store.")
    try:
        return json.loads((gateway.DATA_DIR / f"ui-{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def ui_store_write(name, value):
    if name not in UI_STORES or not isinstance(value, list):
        raise ValueError("Unknown store or bad value.")
    gateway.write_private(gateway.DATA_DIR / f"ui-{name}.json", json.dumps(value, ensure_ascii=False, indent=1))


_ui_store_lock = threading.Lock()


def ui_store_apply(name, ops):
    """Applies one tab's changes, entry by entry (keyed by name), to the current store, so
    tabs never overwrite each other. Ops: {name, delete: true} | {name, create: true, set}
    (add or replace the whole entry) | {name, set, unset} (change fields). A field change to
    an entry that is gone is dropped, so a key deleted in one tab never comes back."""
    if name not in UI_STORES or not isinstance(ops, list):
        raise ValueError("Unknown store or bad ops.")
    with _ui_store_lock:
        items = [x for x in ui_store_read(name) if isinstance(x, dict)]
        for op in ops:
            key = op.get("name") if isinstance(op, dict) else None
            if not isinstance(key, str) or not key:
                continue
            i = next((j for j, x in enumerate(items) if x.get("name") == key), -1)
            if op.get("delete"):
                if i >= 0:
                    items.pop(i)
            elif op.get("create"):
                entry = {**(op.get("set") or {}), "name": key}
                if i >= 0:
                    items[i] = entry
                else:
                    items.append(entry)
            elif i >= 0:
                items[i].update({k: v for k, v in (op.get("set") or {}).items() if k != "name"})
                for k in op.get("unset") or []:
                    if k != "name":
                        items[i].pop(k, None)
        ui_store_write(name, items)
        return items


# The gateway reuses the tester's own request/probe functions.
TESTER = {"fetch_models_list": fetch_models_list, "test_model": test_model,
          "make_request": make_request, "extract_models_from_response": extract_models_from_response}


def import_profiles(profiles):
    """Saved tester profiles (browser) -> gateway providers, one key each, grouped by base URL."""
    imported = 0
    st = gateway.store()
    for prof in profiles:
        base = (prof.get("base_url") or "").strip().rstrip("/")
        key = (prof.get("api_key") or "").strip()
        if not base or not key:
            continue
        existing = next((p for p in st.providers() if p["base_url"].rstrip("/") == base), None)
        if existing:
            pid = existing["id"]
        else:
            name = re.sub(r"[^A-Za-z0-9_.-]", "-", (prof.get("provider_id") or guess_provider_id(base)))[:40] or "provider"
            candidate, n = name, 2
            while any(p["name"].lower() == candidate.lower() for p in st.providers()):
                candidate, n = f"{name}-{n}", n + 1
            pid = gateway.save_provider({"name": candidate, "base_url": base, "format": "openai",
                                         "manual_models": prof.get("models") or ""})
        imported += gateway.add_keys(pid, key, prof.get("name", ""))["added"]
    return imported


def main():
    parser = argparse.ArgumentParser(description="Local web console for testing OpenAI-compatible APIs.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser automatically")
    args = parser.parse_args()

    # Avoid UnicodeEncodeError on Windows terminals (cp1252) for Arabic output.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    alerts.start()
    url = f"http://{args.host}:{args.port}"
    print(i18n.t("cli.running", url=url))
    print(i18n.t("cli.stop_hint"))
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n" + i18n.t("cli.stopped"))
        server.shutdown()


if __name__ == "__main__":
    main()
