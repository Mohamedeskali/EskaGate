"""
Telegram alerts, daily summary and bot commands for the gateway.

gateway.py reports events through gateway.ON_EVENT (set here, so the gateway
never imports this module). Messages name the provider, a masked key and the
agent, never prompts, message content or full keys. The same cause for the
same provider is sent at most once every DEDUP seconds. Every alert that goes
out is appended to alert-history.jsonl.

Bot commands (/status, /switch <provider>) are read with long polling and only
accepted from the configured chat.
"""

import datetime
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request

import balance
import gateway
import i18n

CONFIG_FILE = gateway.DATA_DIR / "telegram.json"        # settings (see load())
STATS_FILE = gateway.DATA_DIR / "alert-stats.json"      # per-agent counters since the last summary, quota warnings
HISTORY_FILE = gateway.DATA_DIR / "alert-history.jsonl"  # every alert sent: time, provider, type, message
MAX_HISTORY = 5000
API = os.environ.get("ESKAGATE_TELEGRAM_API", "https://api.telegram.org")   # overridable for tests
DEDUP = 300
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
CHAT_RE = re.compile(r"^(-?\d{3,}|@[A-Za-z0-9_]{5,})$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PROFILE_BALANCES = lambda: []    # set by the server: [(key name, balance)] from the saved keys ("مفاتيحي")



def reason_text(kind):
    """Why a key failed, in the page's language (messages follow the language picked in the app)."""
    return {"invalid": i18n.t("alert.reason.invalid"), "no_credit": i18n.t("alert.reason.no_credit"),
            "limited": i18n.t("alert.reason.limited"), "error": i18n.t("alert.reason.error")}.get(kind, kind)


_lock = threading.RLock()
_queue = queue.Queue()
_last_sent = {}          # (scope, cause) -> time sent
_failed_keys = {}        # (provider_id, key_id) -> cause, until that key works again
_down = set()            # provider ids reported as fully down
_clients = {}            # client name -> {"last": t, "active": n, "idle": bool}
_last_chat = {}          # last chat that wrote to the bot (seen by the command poller)
_poller = {"running": False}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def load():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {"bot_token": data.get("bot_token", ""), "chat_id": str(data.get("chat_id", "")),
            "enabled": bool(data.get("enabled", True)), "idle_minutes": int(data.get("idle_minutes", 10)),
            "summary": bool(data.get("summary", True)), "summary_time": data.get("summary_time") or "09:00",
            "balance_min": _min(data.get("balance_min", 1))}


def _min(v):
    try:
        return max(0.0, min(float(v), 1e6))
    except (TypeError, ValueError):
        return 1.0


def public():
    c = load()
    return {"configured": bool(c["bot_token"] and c["chat_id"]), "enabled": c["enabled"],
            "bot_token": gateway.mask_key(c["bot_token"]), "chat_id": gateway.mask_key(c["chat_id"]),
            "idle_minutes": c["idle_minutes"], "summary": c["summary"], "summary_time": c["summary_time"],
            "balance_min": c["balance_min"]}


def _ready(c=None):
    c = c or load()
    return bool(c["enabled"] and c["bot_token"] and c["chat_id"])


def _merged(data):
    """Saved settings with the fields typed on the page on top (an empty field keeps the saved value)."""
    c = load()
    token = (data.get("bot_token") or "").strip()
    chat = str(data.get("chat_id") or "").strip()
    if token:
        if not TOKEN_RE.match(token):
            raise ValueError(i18n.t("tg.err_token"))
        c["bot_token"] = token
    if chat:
        if not CHAT_RE.match(chat):
            raise ValueError(i18n.t("tg.err_chat"))
        c["chat_id"] = chat
    for k in ("enabled", "summary"):
        if k in data:
            c[k] = bool(data[k])
    if "idle_minutes" in data:
        try:
            c["idle_minutes"] = max(0, min(int(data["idle_minutes"]), 1440))
        except (TypeError, ValueError):
            raise ValueError(i18n.t("tg.err_minutes"))
    if data.get("balance_min") not in (None, ""):
        try:
            c["balance_min"] = max(0.0, min(float(data["balance_min"]), 1e6))
        except (TypeError, ValueError):
            raise ValueError(i18n.t("tg.err_balance"))
    if data.get("summary_time"):
        m = TIME_RE.match(str(data["summary_time"]).strip())
        if not m:
            raise ValueError(i18n.t("tg.err_time"))
        c["summary_time"] = f"{int(m.group(1)):02d}:{m.group(2)}"
    return c


def save(data):
    c = _merged(data)
    gateway.write_private(CONFIG_FILE, json.dumps(c, indent=2, ensure_ascii=False))
    return public()


# ---------------------------------------------------------------------------
# Counters kept between restarts (daily summary, one quota warning per key per day)
# ---------------------------------------------------------------------------
_stats = None


def _load_stats():
    global _stats
    if _stats is None:
        try:
            _stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except Exception:
            _stats = {}
        _stats.setdefault("since", time.time())
        _stats.setdefault("last_summary", "")
        _stats.setdefault("agents", {})
        _stats.setdefault("quota_warned", {})
        _stats.setdefault("balance_warned", {})
        _stats["dirty"] = False
    return _stats


_saved_at = [0.0]


def _save_stats(force=False):
    with _lock:
        st = _load_stats()
        if not (st.get("dirty") or force):
            return
        st["dirty"] = False
        _saved_at[0] = time.time()
        data = {k: v for k, v in st.items() if k != "dirty"}
    gateway.write_private(STATS_FILE, json.dumps(data, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Sending + history
# ---------------------------------------------------------------------------
def _call(token, method, payload=None, timeout=15):
    """Telegram Bot API call. Errors never include the token (it is part of the URL)."""
    req = urllib.request.Request(f"{API}/bot{token}/{method}",
                                 data=json.dumps(payload or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode("utf-8")).get("description", "")
        except Exception:
            desc = ""
        raise ValueError(f"Telegram: {desc or 'HTTP ' + str(e.code)}")
    except Exception as e:
        raise ValueError(i18n.t("tg.err_unreachable", error=type(e).__name__))


def send_now(text, cfg=None):
    c = cfg or load()
    if not (c["bot_token"] and c["chat_id"]):
        raise ValueError(i18n.t("tg.err_missing"))
    _call(c["bot_token"], "sendMessage", {"chat_id": c["chat_id"], "text": text,
                                           "disable_web_page_preview": True})


def test(data):
    send_now(i18n.t("tg.test_msg"), _merged(data))


def find_chat_id(data):
    """Chat ID of the last message sent to the bot (the user writes /start to it first)."""
    c = _merged(data)
    if not c["bot_token"]:
        raise ValueError(i18n.t("tg.err_token_first"))
    if _poller["running"] and _ready(c):
        # The command poller already reads the updates (Telegram allows one reader at a time).
        if _last_chat:
            return dict(_last_chat)
        raise ValueError(i18n.t("tg.err_start_retry"))
    res = _call(c["bot_token"], "getUpdates", {"limit": 20})
    for upd in reversed(res.get("result") or []):
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            return {"chat_id": str(chat["id"]),
                    "name": chat.get("title") or chat.get("first_name") or chat.get("username") or ""}
    raise ValueError(i18n.t("tg.err_no_message"))


def _log_history(item, sent, error=""):
    entry = {"time": time.time(), "provider": item.get("provider", ""), "agent": item.get("agent", ""),
             "type": item["type"], "message": item["text"], "sent": sent}
    if error:
        entry["error"] = error[:200]
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with _lock:
        try:
            gateway.ensure_data_dir()
            if HISTORY_FILE.exists() and HISTORY_FILE.stat().st_size > 2_000_000:
                lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines(True)[-MAX_HISTORY:]
                gateway.write_private(HISTORY_FILE, "".join(lines) + line)
            else:
                fd = os.open(HISTORY_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            pass


def history(limit=MAX_HISTORY):
    """Alerts sent, newest first."""
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return sorted(out, key=lambda e: e.get("time", 0), reverse=True)


def _enqueue(text, kind, provider="", agent=""):
    _queue.put({"text": text, "type": kind, "provider": provider, "agent": agent})


def notify(scope, cause, text, provider="", agent="", kind=None):
    """Queue an alert unless the same cause for the same scope was sent in the last DEDUP seconds."""
    if not _ready():
        return False
    now = time.time()
    with _lock:
        if now - _last_sent.get((scope, cause), 0) < DEDUP:
            return False
        _last_sent[(scope, cause)] = now
    _enqueue(text, kind or cause, provider, agent)
    return True


def _sender():
    while True:
        item = _queue.get()
        try:
            send_now(item["text"])
            _log_history(item, True)
        except Exception as e:
            _log_history(item, False, str(e))
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
        nxt = (i18n.t("alert.next_key", key=i["next_key"]) if i.get("next_key")
               else i18n.t("alert.next_provider", provider=i["next_provider"]) if i.get("next_provider")
               else i18n.t("alert.no_next"))
        quota = i18n.t("alert.quota_line", quota=i["quota"]) if i.get("quota") else ""
        notify(pid, i["reason"], i18n.t("alert.key_failed", name=name, key=i["key"], reason=reason_text(i["reason"]),
                                        code=code, quota=quota, next=nxt), provider=name)
    elif kind == "provider_down":
        pid = i["provider_id"]
        with _lock:
            _down.add(pid)
        notify(pid, "down", i18n.t("alert.provider_down", provider=i["provider"], keys=i["keys"]),
               provider=i["provider"], kind="provider_down")
    elif kind == "key_ok":
        pid, key = i["provider_id"], (i["provider_id"], i["key_id"])
        with _lock:
            was_down = pid in _down
            was_failed = _failed_keys.pop(key, None)
            _down.discard(pid)
        if was_down or was_failed:
            notify(pid, "recovered", i18n.t("alert.recovered", provider=i["provider"], key=i["key"]),
                   provider=i["provider"])
    elif kind == "quota_low":
        today = datetime.date.today().isoformat()
        with _lock:
            st = _load_stats()
            if st["quota_warned"].get(i["key_id"]) == today or not _ready():
                return
            st["quota_warned"][i["key_id"]] = today
            st["dirty"] = True
        reset = i18n.t("alert.reset", reset=i["reset"]) if i.get("reset") else ""
        pct = int(i["remaining"] * 100 / i["limit"]) if i.get("limit") else 0
        _enqueue(i18n.t("alert.quota_low", provider=i["provider"], key=i["key"], bucket=i["bucket"],
                        remaining=i["remaining"], limit=i["limit"], pct=pct, reset=reset),
                 "quota_low", provider=i["provider"])
        _save_stats()
    elif kind == "balance":
        b, c = i["balance"], load()
        if not balance.low(b, c["balance_min"]):
            return
        today = datetime.date.today().isoformat()
        with _lock:
            st = _load_stats()
            if st["balance_warned"].get(i["key_id"]) == today or not _ready(c):
                return
            st["balance_warned"][i["key_id"]] = today
            st["dirty"] = True
        left = balance.text(b, i18n.t("bal.used"))
        _enqueue(i18n.t("alert.balance_low", name=i["label"], key=i["key"], left=left,
                        min=balance.money(c["balance_min"], b["currency"])), "balance_low", provider=i["label"])
        _save_stats()
    elif kind == "request_logged":
        name = i.get("client") or "unknown"
        if name in ("curl", "unknown"):
            return
        with _lock:
            st = _load_stats()
            a = st["agents"].setdefault(name, {"requests": 0, "tokens": 0, "switches": 0, "errors": 0})
            a["requests"] += 1
            a["tokens"] += int(i.get("tokens_in") or 0) + int(i.get("tokens_out") or 0)
            a["switches"] += int(i.get("failovers") or 0)
            a["errors"] += 1 if i.get("status") == "error" else 0
            st["dirty"] = True
            due = now - _saved_at[0] > 5
        if due:                          # at most every 5 s, so a restart loses little
            _save_stats()
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
            notify("agent:" + name, "active", i18n.t("alert.active", name=name), agent=name)


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
        notify("agent:" + n, "idle", i18n.t("alert.idle", name=n, minutes=minutes,
                                            last=time.strftime("%H:%M", time.localtime(last))), agent=n)


# ---------------------------------------------------------------------------
# Daily summary: one message per agent that had traffic since the last one
# ---------------------------------------------------------------------------
def _fmt_tokens(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e4 else str(n)


def check_summary(now=None):
    """Sends the summary once a day, at or after summary_time (local), if it wasn't sent today."""
    c = load()
    if not (c["summary"] and _ready(c)):
        return False
    dt = datetime.datetime.fromtimestamp(now or time.time())
    if dt.strftime("%H:%M") < c["summary_time"]:
        return False
    today = dt.date().isoformat()
    with _lock:
        st = _load_stats()
        if st["last_summary"] == today:
            return False
        agents, since = st["agents"], st["since"]
        st.update(agents={}, since=dt.timestamp(), last_summary=today, dirty=True)
    start = time.strftime("%d/%m %H:%M", time.localtime(since))
    for name, a in sorted(agents.items(), key=lambda x: -x[1]["requests"]):
        if not a["requests"]:
            continue
        errors = i18n.t("alert.summary_errors", n=a["errors"]) if a.get("errors") else ""
        _enqueue(i18n.t("alert.summary", name=name, start=start, requests=a["requests"],
                        tokens=_fmt_tokens(a["tokens"]), switches=a["switches"], errors=errors),
                 "summary", agent=name)
    lines = balance_lines()
    if lines:
        _enqueue(i18n.t("alert.summary_balances") + "\n" + "\n".join(lines), "summary")
    _save_stats(force=True)
    return True


def balance_lines():
    """'• name: 12.40$ / 20.00$' for every key whose provider reported a balance (gateway keys and saved keys),
    each key once."""
    seen, out = set(), []
    items = []
    for p in gateway.store().providers():
        for k in p.get("keys", []):
            items.append((balance.key_id(k["key"]), f"{p['name']} · {gateway.mask_key(k['key'])}", k.get("balance")))
    try:
        items += list(PROFILE_BALANCES())
    except Exception as e:
        sys.stderr.write(f"[alerts] balances: {type(e).__name__}\n")
    for kid, name, b in items:
        txt = balance.text(b, i18n.t("bal.used"))
        if not txt or kid in seen:
            continue
        seen.add(kid)
        warn = " ⚠️" if balance.low(b, load()["balance_min"]) else ""
        out.append(f"• {name}: {txt}{warn} ({time.strftime('%d/%m %H:%M', time.localtime(b['at']))})")
    return out


def _ticker():
    while True:
        time.sleep(10)
        for job in (check_idle, check_summary, _save_stats):
            try:
                job()
            except Exception as e:
                sys.stderr.write(f"[alerts] {type(e).__name__}: {e}\n")


# ---------------------------------------------------------------------------
# Bot commands (long polling). Only the configured chat is answered.
# ---------------------------------------------------------------------------

def handle_command(text):
    """Reply text for a bot command."""
    parts = (text or "").strip().split(None, 1)
    cmd = parts[0].split("@", 1)[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd == "/status":
        lines = gateway.provider_status_lines()
        text = "📋 EskaGate\n" + ("\n".join(lines) if lines else i18n.t("bot.no_providers"))
        bal = balance_lines()
        return text + ("\n\n" + i18n.t("bot.balances") + "\n" + "\n".join(bal) if bal else "")
    if cmd == "/switch":
        if not arg:
            names = ", ".join(p["name"] for p in gateway.store().providers()) or "—"
            return i18n.t("bot.switch_usage", names=names)
        try:
            p, old, new = gateway.switch_active_key(arg)
        except ValueError as e:
            msg = {i18n.t("err.unknown_provider"): i18n.t("bot.unknown_provider", name=arg),
                   i18n.t("err.no_other_key"): i18n.t("bot.no_other_key", name=arg)}
            return "❌ " + msg.get(str(e), str(e))
        was = gateway.mask_key(old["key"]) if old else "—"
        return i18n.t("bot.switched", provider=p["name"], new=gateway.mask_key(new["key"]), old=was)
    return i18n.t("bot.help")


def _poll():
    offset, started = None, time.time()
    while True:
        c = load()
        if not _ready(c):
            _poller["running"] = False
            offset = None
            time.sleep(5)
            continue
        _poller["running"] = True
        try:
            res = _call(c["bot_token"], "getUpdates",
                        {"timeout": 25, "allowed_updates": ["message"], **({"offset": offset} if offset else {})},
                        timeout=40)
        except ValueError as e:
            sys.stderr.write(f"[alerts] poll: {e}\n")
            time.sleep(30 if "Conflict" in str(e) else 5)
            continue
        for upd in res.get("result") or []:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is None:
                continue
            _last_chat.update(chat_id=str(chat["id"]),
                              name=chat.get("title") or chat.get("first_name") or chat.get("username") or "")
            mine = str(chat["id"]) == c["chat_id"] or ("@" + str(chat.get("username") or "")) == c["chat_id"]
            if not mine or msg.get("date", 0) < started - 60 or not str(msg.get("text", "")).startswith("/"):
                continue                 # other chats are ignored; old commands from before start are skipped
            try:
                reply = handle_command(msg["text"])
            except Exception as e:
                reply = f"❌ {type(e).__name__}"
            try:
                _call(c["bot_token"], "sendMessage", {"chat_id": chat["id"], "text": reply})
            except ValueError as e:
                sys.stderr.write(f"[alerts] reply: {e}\n")


def start():
    gateway.ON_EVENT = on_event
    for target, name in ((_sender, "alerts-send"), (_ticker, "alerts-tick"), (_poll, "alerts-poll")):
        threading.Thread(target=target, daemon=True, name=name).start()
