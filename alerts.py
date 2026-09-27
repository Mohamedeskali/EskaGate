"""
Telegram alerts from the gateway.

gateway.py reports events through gateway.ON_EVENT (set here, so the gateway
never imports this module). Messages name the provider, a masked key and the
agent, never prompts, message content or full keys. The same cause for the
same provider is sent at most once every DEDUP seconds.
"""

import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request

import gateway

CONFIG_FILE = gateway.DATA_DIR / "telegram.json"   # {bot_token, chat_id, enabled, idle_minutes}
API = os.environ.get("ESKAGATE_TELEGRAM_API", "https://api.telegram.org")   # overridable for tests
DEDUP = 300
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
CHAT_RE = re.compile(r"^(-?\d{3,}|@[A-Za-z0-9_]{5,})$")

REASON = {"invalid": "مرفوض (المفتاح غالط ولا تحيد)", "no_credit": "سالا الرصيد",
          "limited": "وصل للحد (rate limit)", "error": "المزود طايح ولا ما جاوبش"}

_lock = threading.RLock()
_queue = queue.Queue()
_last_sent = {}          # (scope, cause) -> time sent
_failed_keys = {}        # (provider_id, key_id) -> cause, until that key works again
_down = set()            # provider ids reported as fully down
_clients = {}            # client name -> {"last": t, "active": n, "idle": bool}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def load():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {"bot_token": data.get("bot_token", ""), "chat_id": str(data.get("chat_id", "")),
            "enabled": bool(data.get("enabled", True)), "idle_minutes": int(data.get("idle_minutes", 10))}


def public():
    c = load()
    return {"configured": bool(c["bot_token"] and c["chat_id"]), "enabled": c["enabled"],
            "bot_token": gateway.mask_key(c["bot_token"]), "chat_id": gateway.mask_key(c["chat_id"]),
            "idle_minutes": c["idle_minutes"]}


def _merged(data):
    """Saved settings with the fields typed on the page on top (an empty field keeps the saved value)."""
    c = load()
    token = (data.get("bot_token") or "").strip()
    chat = str(data.get("chat_id") or "").strip()
    if token:
        if not TOKEN_RE.match(token):
            raise ValueError("الـ Bot token ماشي صحيح. خودو كامل من @BotFather (بحال 123456:ABC...).")
        c["bot_token"] = token
    if chat:
        if not CHAT_RE.match(chat):
            raise ValueError("الـ Chat ID خاصو يكون رقم (ولا @channel).")
        c["chat_id"] = chat
    if "enabled" in data:
        c["enabled"] = bool(data["enabled"])
    if "idle_minutes" in data:
        try:
            c["idle_minutes"] = max(0, min(int(data["idle_minutes"]), 1440))
        except (TypeError, ValueError):
            raise ValueError("عدد الدقايق خاصو يكون رقم.")
    return c


def save(data):
    c = _merged(data)
    gateway.write_private(CONFIG_FILE, json.dumps(c, indent=2, ensure_ascii=False))
    return public()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def _call(token, method, payload=None):
    """Telegram Bot API call. Errors never include the token (it is part of the URL)."""
    req = urllib.request.Request(f"{API}/bot{token}/{method}",
                                 data=json.dumps(payload or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode("utf-8")).get("description", "")
        except Exception:
            desc = ""
        raise ValueError(f"Telegram: {desc or 'HTTP ' + str(e.code)}")
    except Exception as e:
        raise ValueError(f"ما قدرتش نوصل لـ Telegram ({type(e).__name__}).")


def send_now(text, cfg=None):
    c = cfg or load()
    if not (c["bot_token"] and c["chat_id"]):
        raise ValueError("عمّر الـ Bot token والـ Chat ID بعدا.")
    _call(c["bot_token"], "sendMessage", {"chat_id": c["chat_id"], "text": text,
                                           "disable_web_page_preview": True})


def test(data):
    send_now("✅ EskaGate: رسالة تجريبية. التنبيهات ديال الـ gateway غادي يوصلو هنا.", _merged(data))


def find_chat_id(data):
    """Chat ID of the last message sent to the bot (the user writes /start to it first)."""
    c = _merged(data)
    if not c["bot_token"]:
        raise ValueError("كتب الـ Bot token بعدا.")
    res = _call(c["bot_token"], "getUpdates", {"limit": 20})
    for upd in reversed(res.get("result") or []):
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            return {"chat_id": str(chat["id"]),
                    "name": chat.get("title") or chat.get("first_name") or chat.get("username") or ""}
    raise ValueError("ما لقيت حتى رسالة. صيفط /start للبوت ديالك فـ Telegram، وعاود.")


def notify(scope, cause, text):
    """Queue an alert unless the same cause for the same scope was sent in the last DEDUP seconds."""
    c = load()
    if not (c["enabled"] and c["bot_token"] and c["chat_id"]):
        return False
    now = time.time()
    with _lock:
        if now - _last_sent.get((scope, cause), 0) < DEDUP:
            return False
        _last_sent[(scope, cause)] = now
    _queue.put(text)
    return True


def _sender():
    while True:
        text = _queue.get()
        try:
            send_now(text)
        except Exception as e:
            sys.stderr.write(f"[alerts] {e}\n")


# ---------------------------------------------------------------------------
# Gateway events
# ---------------------------------------------------------------------------
def on_event(kind, **i):
    try:
        _handle(kind, i)
    except Exception as e:           # alerts must never break a request
        sys.stderr.write(f"[alerts] {type(e).__name__}: {e}\n")


def _handle(kind, i):
    now = time.time()
    if kind == "key_failed":
        pid, name = i["provider_id"], i["provider"]
        with _lock:
            _failed_keys[(pid, i["key_id"])] = i["reason"]
        code = f" (HTTP {i['status']})" if i.get("status") else ""
        nxt = (f"↪️ دوزت للمفتاح {i['next_key']}." if i.get("next_key")
               else f"↪️ دوزت للمزود {i['next_provider']}." if i.get("next_provider")
               else "↪️ ما بقا حتى مفتاح آخر نجربو.")
        notify(pid, i["reason"], f"⚠️ EskaGate · {name}\nالمفتاح {i['key']}: {REASON.get(i['reason'], i['reason'])}{code}.\n{nxt}")
    elif kind == "provider_down":
        pid = i["provider_id"]
        with _lock:
            _down.add(pid)
        notify(pid, "down", f"🔴 EskaGate · {i['provider']}\nجميع المفاتيح ({i['keys']}) طايحين. "
                            "الطلبات ما غاديش تدوز من هاد المزود حتى يرجع واحد فيهم.")
    elif kind == "key_ok":
        pid, key = i["provider_id"], (i["provider_id"], i["key_id"])
        with _lock:
            was_down = pid in _down
            was_failed = _failed_keys.pop(key, None)
            _down.discard(pid)
        if was_down or was_failed:
            notify(pid, "recovered", f"✅ EskaGate · {i['provider']}\nرجع كلشي عادي: المفتاح {i['key']} خدام.")
    elif kind == "request":
        name = i.get("client") or "unknown"
        if name in ("curl", "unknown"):
            return                    # manual tests are not agents
        with _lock:
            c = _clients.setdefault(name, {"last": now, "active": 0, "idle": False})
            c["last"] = now
            c["active"] = max(0, c["active"] + (1 if i.get("phase") == "start" else -1))
            woke = c["idle"]
            c["idle"] = False
        if woke:
            notify("agent:" + name, "active", f"✅ EskaGate · {name}\nرجع كيبعث الطلبات.")


def _idle_watch():
    while True:
        time.sleep(30)
        check_idle()


def check_idle(now=None):
    """Alert once for each agent that had traffic and then went quiet for idle_minutes."""
    minutes = load()["idle_minutes"]
    if minutes <= 0:
        return
    now = now or time.time()
    with _lock:
        quiet = [(n, c["last"]) for n, c in _clients.items()
                 if not c["idle"] and c["active"] == 0 and now - c["last"] >= minutes * 60]
        for n, _ in quiet:
            _clients[n]["idle"] = True
    for n, last in quiet:
        notify("agent:" + n, "idle", f"💤 EskaGate · {n}\nما بعث حتى طلب هادي {minutes} دقيقة "
                                     f"(آخر طلب {time.strftime('%H:%M', time.localtime(last))}).")


def start():
    gateway.ON_EVENT = on_event
    threading.Thread(target=_sender, daemon=True, name="alerts-send").start()
    threading.Thread(target=_idle_watch, daemon=True, name="alerts-idle").start()
