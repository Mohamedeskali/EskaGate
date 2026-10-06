"""
Remaining balance of a key, only as the provider reports it through the key itself.

Read-only GET calls that cost nothing. Unknown hosts, official APIs without a
balance endpoint, errors and odd replies all give {"ok": False}: the app never
estimates a balance.

Result: {"at", "ok": True, "remaining", "total" (or None), "currency", "unlimited", "source"}
"""

import datetime
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

UNLIMITED = 1e8          # One API / New API report "unlimited" keys as hard_limit_usd 100000000
CURRENCY_SIGN = {"USD": "$", "CNY": "¥", "EUR": "€"}
# Official APIs that have no balance endpoint for a key: nothing is sent to them.
NO_BALANCE = ("api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com", "api.groq.com",
              "api.mistral.ai", "api.x.ai", "api.cohere.com", "api.cohere.ai", "api.together.xyz",
              "api.fireworks.ai", "api.cerebras.ai", "integrate.api.nvidia.com", "models.inference.ai.azure.com")


def key_id(api_key):
    """Stable short id for a key (one low-balance alert per key per day, whichever tab checked it)."""
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:12]


def money(v, currency):
    sign = CURRENCY_SIGN.get(currency)
    n = f"{v:,.2f}" if abs(v) < 1e6 else f"{v / 1e6:,.1f}M"
    return f"{n}{sign}" if sign else f"{n} {currency}"


def text(b, used="{amount} used"):
    """'12.40$ / 20.00$' for messages; '' when the provider doesn't report it. `used` is the
    translated "{amount} used" for unlimited keys."""
    if not b or not b.get("ok"):
        return ""
    if b.get("unlimited"):
        return "∞" + (f" ({used.format(amount=money(b['used'], b['currency']))})" if b.get("used") is not None else "")
    left = money(b["remaining"], b["currency"])
    return f"{left} / {money(b['total'], b['currency'])}" if b.get("total") else left


def _get(url, api_key, timeout):
    headers = {"Accept": "application/json", "User-Agent": "EskaGate"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read(200_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        e.close()
        return None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _result(source, remaining, total=None, currency="USD"):
    remaining, total = _f(remaining), _f(total)
    if remaining is None:
        return None
    return {"ok": True, "source": source, "remaining": round(remaining, 4),
            "total": round(total, 4) if total and total > 0 else None, "currency": currency, "unlimited": False}


def _data(res):
    return res.get("data") if isinstance(res, dict) and isinstance(res.get("data"), dict) else None


def openrouter(root, api_key, timeout):
    d = _data(_get(root + "/api/v1/key", api_key, timeout))
    if d is None:
        return None
    if d.get("limit") is not None and d.get("limit_remaining") is not None:
        return _result("openrouter", d["limit_remaining"], d["limit"])
    c = _data(_get(root + "/api/v1/credits", api_key, timeout))      # account credits when the key has no limit
    if c and _f(c.get("total_credits")) is not None and _f(c.get("total_usage")) is not None:
        return _result("openrouter", _f(c["total_credits"]) - _f(c["total_usage"]), c["total_credits"])
    return None


def deepseek(root, api_key, timeout):
    res = _get(root + "/user/balance", api_key, timeout)
    infos = res.get("balance_infos") if isinstance(res, dict) else None
    if not isinstance(infos, list) or not infos:
        return None
    info = next((i for i in infos if i.get("currency") == "USD"), infos[0])
    return _result("deepseek", info.get("total_balance"), currency=str(info.get("currency") or "CNY"))


def moonshot(root, api_key, timeout):
    d = _data(_get(root + "/v1/users/me/balance", api_key, timeout))
    if d is None:
        return None
    return _result("moonshot", d.get("available_balance"), currency="CNY" if root.endswith(".cn") else "USD")


def siliconflow(root, api_key, timeout):
    d = _data(_get(root + "/v1/user/info", api_key, timeout))
    if d is None:
        return None
    return _result("siliconflow", d.get("totalBalance", d.get("balance")),
                   currency="CNY" if root.endswith(".cn") else "USD")


def _site(root, timeout):
    """New API / One API public /api/status: units per USD and the currency the site displays."""
    d = _data(_get(root + "/api/status", "", timeout))
    if d is None or _f(d.get("quota_per_unit")) in (None, 0):
        return None
    shown = str(d.get("quota_display_type") or ("USD" if d.get("display_in_currency", True) else "TOKENS")).upper()
    return {"per_usd": _f(d["quota_per_unit"]), "shown": shown}


def new_api(base, api_key, timeout):
    """One API / New API relays. New API's /api/usage/token gives the key's own numbers in quota
    units (quota_per_unit per USD, from /api/status). Older ones only have OpenAI's old billing
    routes, whose numbers are in the currency the site displays (USD or CNY; usage in cents)."""
    root = base[:-3] if base.endswith("/v1") else base
    site = _site(root, timeout)
    d = _data(_get(root + "/api/usage/token", api_key, timeout))
    if site and d and _f(d.get("total_used")) is not None:
        per = site["per_usd"]
        if d.get("unlimited_quota"):
            return {"ok": True, "source": "new-api", "remaining": None, "total": None, "currency": "USD",
                    "unlimited": True, "used": round(_f(d["total_used"]) / per, 4)}
        if _f(d.get("total_available")) is not None:
            return _result("new-api", _f(d["total_available"]) / per, (_f(d.get("total_granted")) or 0) / per)
    currency = site["shown"] if site else "USD"
    if currency not in ("USD", "CNY"):
        return None              # shown in tokens or a custom unit: no money figure to trust
    for prefix in dict.fromkeys((base, root)):
        sub = _get(prefix + "/dashboard/billing/subscription", api_key, timeout)
        hard = _f(sub.get("hard_limit_usd")) if isinstance(sub, dict) else None
        if hard is None:
            continue
        if hard >= UNLIMITED:
            return {"ok": True, "source": "new-api", "remaining": None, "total": None, "currency": currency,
                    "unlimited": True}
        today = datetime.date.today()
        q = urllib.parse.urlencode({"start_date": (today - datetime.timedelta(days=99)).isoformat(),
                                    "end_date": (today + datetime.timedelta(days=1)).isoformat()})
        use = _get(f"{prefix}/dashboard/billing/usage?{q}", api_key, timeout)
        used = _f(use.get("total_usage")) if isinstance(use, dict) else None
        if used is None:
            return None
        return _result("new-api", hard - used / 100, hard, currency)
    return None


def fetch(base_url, api_key, timeout=10):
    """Balance of one key, or {"ok": False} when the provider doesn't report it."""
    out = None
    try:
        u = urllib.parse.urlsplit(base_url)
        host = (u.hostname or "").lower()
        root = f"{u.scheme}://{u.netloc}"
        if host in NO_BALANCE or not api_key:
            out = None
        elif host.endswith("openrouter.ai"):
            out = openrouter(root, api_key, timeout)
        elif host.endswith("deepseek.com"):
            out = deepseek(root, api_key, timeout)
        elif host.startswith("api.moonshot."):
            out = moonshot(root, api_key, timeout)
        elif host.startswith("api.siliconflow."):
            out = siliconflow(root, api_key, timeout)
        else:
            out = new_api(base_url.rstrip("/"), api_key, timeout)
    except Exception:
        out = None
    return {**(out or {"ok": False}), "at": time.time()}


def low(b, minimum):
    """True when the reported balance is under `minimum` (in the key's own currency)."""
    return bool(b and b.get("ok") and not b.get("unlimited") and b.get("remaining") is not None
                and b["remaining"] < minimum)
