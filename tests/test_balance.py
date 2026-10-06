#!/usr/bin/env python3
"""Key balance against fake providers: python3 -m unittest discover tests  (standard library only)."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("API_CONSOLE_HOME", tempfile.mkdtemp(prefix="eskagate-test-"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(
    filter(None, ["127.0.0.1", "localhost", os.environ.get("NO_PROXY", "")]))

import i18n  # noqa: E402
import balance  # noqa: E402
import gateway  # noqa: E402
import alerts  # noqa: E402
import api_web_dashboard_v2 as app  # noqa: E402

i18n._lang = "en"
KEY = "sk-test-Balance0123456789abcdef"
SEEN = []           # (method, path) of every request the fake got

# New API: 500000 units per USD. Limited key: 6.2$ left of 20$; unlimited key: 3.19$ used.
REPLIES = {
    "/newapi/api/status": {"data": {"quota_per_unit": 500000, "quota_display_type": "USD", "display_in_currency": True}},
    "/newapi/api/usage/token": {"code": True, "data": {"total_available": 3100000, "total_granted": 10000000,
                                                       "total_used": 6900000, "unlimited_quota": False}},
    "/unlim/api/status": {"data": {"quota_per_unit": 500000, "quota_display_type": "CNY"}},
    "/unlim/api/usage/token": {"code": True, "data": {"total_available": -1592633, "total_granted": 0,
                                                      "total_used": 1592633, "unlimited_quota": True}},
    # One API style: only the old billing routes, shown in CNY by the site.
    "/oneapi/api/status": {"data": {"quota_per_unit": 500000, "quota_display_type": "CNY"}},
    "/oneapi/v1/dashboard/billing/subscription": {"object": "billing_subscription", "hard_limit_usd": 146},
    "/oneapi/v1/dashboard/billing/usage": {"object": "list", "total_usage": 4600},
    "/tokens/api/status": {"data": {"quota_per_unit": 500000, "display_in_currency": False}},
    "/tokens/v1/dashboard/billing/subscription": {"hard_limit_usd": 9000000},
    "/tokens/v1/dashboard/billing/usage": {"total_usage": 100},
    "/openrouter/api/v1/key": {"data": {"label": "x", "limit": 10, "usage": 2.5, "limit_remaining": 7.5}},
    "/orfree/api/v1/key": {"data": {"label": "x", "limit": None, "usage": 1, "limit_remaining": None}},
    "/orfree/api/v1/credits": {"data": {"total_credits": 5, "total_usage": 1.25}},
    "/deepseek/user/balance": {"is_available": True, "balance_infos": [
        {"currency": "CNY", "total_balance": "110.00", "granted_balance": "10.00", "topped_up_balance": "100.00"}]},
    "/moonshot.cn/v1/users/me/balance": {"code": 0, "data": {"available_balance": 49.58, "voucher_balance": 46.58,
                                                             "cash_balance": 3.0}, "status": True},
    "/silicon.cn/v1/user/info": {"code": 20000, "status": True, "data": {"balance": "0.88", "totalBalance": "88.88"}},
}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        SEEN.append(("GET", path))
        if path.startswith("/html/"):
            body, ctype, code = b"<!doctype html><html>hi</html>", "text/html", 200
        elif path in REPLIES and (path.endswith("/api/status") or self.headers.get("Authorization") == f"Bearer {KEY}"):
            body, ctype, code = json.dumps(REPLIES[path]).encode(), "application/json", 200
        else:
            body, ctype, code = b"404 page not found", "text/plain", 404
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        SEEN.append(("POST", self.path))
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class Balance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        SEEN.clear()

    def get(self, scen, key=KEY):
        b = balance.fetch(f"{self.base}/{scen}/v1", key, 5)
        self.assertIn("at", b)
        return b

    def test_new_api_limited_key_in_usd(self):
        b = self.get("newapi")
        self.assertEqual((b["ok"], b["remaining"], b["total"], b["currency"]), (True, 6.2, 20, "USD"))
        self.assertEqual(balance.text(b), "6.20$ / 20.00$")

    def test_new_api_unlimited_key_shows_what_was_used(self):
        b = self.get("unlim")
        self.assertTrue(b["unlimited"])
        self.assertEqual(b["used"], 3.1853)
        self.assertEqual(balance.text(b), "∞ (3.19$ used)")
        self.assertFalse(balance.low(b, 1))

    def test_old_billing_routes_follow_the_site_currency(self):
        b = self.get("oneapi")
        self.assertEqual((b["remaining"], b["total"], b["currency"]), (100, 146, "CNY"))

    def test_token_display_is_not_turned_into_money(self):
        self.assertFalse(self.get("tokens")["ok"])

    def test_wrong_key_html_and_unknown_hosts_are_not_available(self):
        self.assertFalse(self.get("newapi", "sk-wrong-000000000000000000")["ok"])
        self.assertFalse(self.get("html")["ok"])
        self.assertFalse(self.get("nothing")["ok"])

    def test_official_apis_get_no_request(self):
        self.assertFalse(balance.fetch("https://api.openai.com/v1", KEY, 5)["ok"])
        self.assertFalse(balance.fetch("https://api.anthropic.com", KEY, 5)["ok"])

    def test_only_get_requests(self):
        for scen in ("newapi", "oneapi", "html"):
            self.get(scen)
        self.assertTrue(SEEN and all(m == "GET" for m, _ in SEEN))

    def test_provider_specific_endpoints(self):
        b = balance.openrouter(f"{self.base}/openrouter", KEY, 5)
        self.assertEqual((b["remaining"], b["total"], b["currency"]), (7.5, 10, "USD"))
        b = balance.openrouter(f"{self.base}/orfree", KEY, 5)
        self.assertEqual((b["remaining"], b["total"]), (3.75, 5))
        b = balance.deepseek(f"{self.base}/deepseek", KEY, 5)
        self.assertEqual((b["remaining"], b["currency"]), (110, "CNY"))
        b = balance.moonshot(f"{self.base}/moonshot.cn", KEY, 5)
        self.assertEqual((b["remaining"], b["currency"]), (49.58, "CNY"))
        b = balance.siliconflow(f"{self.base}/silicon.cn", KEY, 5)
        self.assertEqual((b["remaining"], b["currency"]), (88.88, "CNY"))

    def test_pipeline_emits_the_balance(self):
        events = []
        app.run_pipeline({"base_url": f"{self.base}/newapi/v1", "api_key": KEY, "mode": "quick", "timeout": 5,
                          "retries": 0}, lambda kind, data: events.append((kind, data)))
        kinds = [k for k, _ in events]
        self.assertIn("balance", kinds)
        self.assertEqual(dict(events)["balance"]["remaining"], 6.2)
        self.assertLess(kinds.index("balance"), kinds.index("error"))      # sent before the run ends

    def test_page_open_refresh_fills_saved_and_gateway_keys(self):
        old_store, old_read = gateway.STORE, app.ui_store_read
        gateway.STORE = None
        app.ui_store_read = lambda name: [{"name": "relay", "base_url": f"{self.base}/newapi/v1", "api_key": KEY},
                                          {"name": "web", "base_url": f"{self.base}/html/v1", "api_key": "sk-x-1"}]
        try:
            pid = gateway.save_provider({"name": "gw", "base_url": f"{self.base}/unlim/v1", "format": "openai"})
            gateway.add_keys(pid, KEY.replace("Balance", "Unlimit0"))
            r = app.refresh_balances(force=True)
            self.assertEqual(r["profiles"]["relay"]["remaining"], 6.2)
            self.assertFalse(r["profiles"]["web"]["ok"])
            self.assertFalse(r["providers"][0]["keys"][0]["balance"]["ok"])     # wrong key for /unlim: not available
            SEEN.clear()
            app.refresh_balances()                                               # fresh enough: no new calls
            self.assertEqual(SEEN, [])
        finally:
            gateway.STORE, app.ui_store_read = old_store, old_read


class Alerts(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._enqueue, self._ready = alerts._enqueue, alerts._ready
        alerts._enqueue = lambda text, kind, provider="", agent="": self.sent.append((kind, text))
        alerts._ready = lambda c=None: True
        alerts._load_stats()["balance_warned"].clear()

    def tearDown(self):
        alerts._enqueue, alerts._ready = self._enqueue, self._ready

    def event(self, remaining, key_id="k1"):
        alerts.on_event("balance", key_id=key_id, key="sk-…cdef", label="relay",
                        balance={"ok": True, "remaining": remaining, "total": 20, "currency": "USD",
                                 "unlimited": False, "at": 0})

    def test_low_balance_alert_once_a_day_under_the_threshold(self):
        self.event(5)
        self.assertEqual(self.sent, [])
        self.event(0.4)
        self.event(0.3)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "balance_low")
        self.assertIn("0.40$", self.sent[0][1])
        self.assertIn("1.00$", self.sent[0][1])
        self.event(0.2, key_id="k2")
        self.assertEqual(len(self.sent), 2)

    def test_status_lists_saved_key_balances(self):
        old = alerts.PROFILE_BALANCES
        alerts.PROFILE_BALANCES = lambda: [("a", "my relay", {"ok": True, "remaining": 2, "total": None,
                                                              "currency": "USD", "unlimited": False, "at": 0}),
                                           ("b", "other", {"ok": False, "at": 0})]
        try:
            text = alerts.handle_command("/status")
        finally:
            alerts.PROFILE_BALANCES = old
        self.assertIn("my relay: 2.00$", text)
        self.assertNotIn("other", text)

    def test_threshold_setting(self):
        self.assertEqual(alerts._merged({"balance_min": "2.5"})["balance_min"], 2.5)
        with self.assertRaises(ValueError):
            alerts._merged({"balance_min": "abc"})


if __name__ == "__main__":
    unittest.main()
