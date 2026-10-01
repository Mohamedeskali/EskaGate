#!/usr/bin/env python3
"""Checks the translations: python3 -m unittest discover tests  (standard library only)."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("API_CONSOLE_HOME", tempfile.mkdtemp(prefix="eskagate-test-"))

import i18n  # noqa: E402
import api_web_dashboard_v2 as app  # noqa: E402

ARABIC = re.compile("[؀-ۿ]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def load_all():
    return {lang: json.loads((ROOT / "i18n" / f"{lang}.json").read_text(encoding="utf-8")) for lang in i18n.LANGS}


CODE_FILES = ("api_web_dashboard_v2.py", "gateway.py", "agents.py", "alerts.py", "phone.py", "i18n.py", "detect.py")


def mentioned_keys(known):
    """Keys built at run time also count as used: a quoted key ('keys.deep_progress' in a ternary)
    or a prefix joined with a value (T('ks.' + status), i18n.t("srv.key." + status))."""
    found = set()
    for name in CODE_FILES:
        src = (ROOT / name).read_text(encoding="utf-8")
        found |= set(re.findall(r"""["']([a-z_]+\.[\w.]+)["']""", src)) & known
        for prefix in re.findall(r"""\b(?:T|i18n\.t)\(["']([\w.]+\.)["'] \+""", src):
            found |= {k for k in known if k.startswith(prefix)}
    return found


def used_keys():
    """Every key the code asks for: page ({{t:..}}, {{h:..}}, T('..')), Python (i18n.t("..")), shell (t key)."""
    keys = set()
    for name in CODE_FILES:
        src = (ROOT / name).read_text(encoding="utf-8")
        keys |= set(re.findall(r"\{\{[th]:(\w+\.[\w.]+)\}\}", src))
        keys |= set(re.findall(r"\bT\('(\w+\.[\w.]+)'", src))
        keys |= set(re.findall(r"""i18n\.t\(["']([\w.]+)["']""", src))
        keys |= set(re.findall(r"""\bt\(["']([\w.]+)["']""", src))
        # T(cond ? 'a' : 'b') and i18n.t("a" if cond else "b")
        for pair in re.findall(r"T\([\w.]+ \? '([\w.]+)' : '([\w.]+)'\)", src) + \
                re.findall(r'i18n\.t\("([\w.]+)" if .*? else "([\w.]+)"', src):
            keys |= set(pair)
    for name in ("eskali_api_launcher.sh", "install_eskali_api.sh"):
        src = (ROOT / name).read_text(encoding="utf-8")
        keys |= set(re.findall(r"\bt ((?:launcher|install)\.\w+)", src))
    return {k for k in keys if not k.endswith(".")}   # a prefix like T('ks.' + status): see mentioned_keys()


class TranslationFiles(unittest.TestCase):
    def test_same_keys(self):
        files = load_all()
        ar = set(files["ar"])
        for lang, words in files.items():
            self.assertEqual(set(words), ar, f"{lang}.json keys differ from ar.json: "
                             f"missing {sorted(ar - set(words))}, extra {sorted(set(words) - ar)}")

    def test_no_empty_values(self):
        for lang, words in load_all().items():
            empty = [k for k, v in words.items() if not isinstance(v, str) or not v.strip()]
            self.assertFalse(empty, f"{lang}.json has empty values: {empty}")

    def test_same_placeholders(self):
        files = load_all()
        for key, text in files["ar"].items():
            want = sorted(PLACEHOLDER.findall(text))
            for lang in ("en", "fr"):
                self.assertEqual(sorted(PLACEHOLDER.findall(files[lang][key])), want, f"{lang}:{key} placeholders")

    def test_every_used_key_exists(self):
        missing = sorted(used_keys() - set(load_all()["ar"]))
        self.assertFalse(missing, f"keys used in code but missing from the translation files: {missing}")

    def test_no_unused_keys(self):
        known = set(load_all()["ar"])
        unused = sorted(k for k in known - used_keys() - mentioned_keys(known) if not k.startswith("lang."))
        self.assertFalse(unused, f"keys in the translation files that no code uses: {unused}")

    def test_shell_keys_readable_without_python(self):
        # The launcher falls back to sed on the JSON file: one key per line, no quotes/backslashes inside.
        for lang in i18n.LANGS:
            raw = (ROOT / "i18n" / f"{lang}.json").read_text(encoding="utf-8")
            for key in [k for k in load_all()[lang] if k.startswith(("launcher.", "install."))]:
                m = re.search(r'^ *"%s": "(.*)",?$' % re.escape(key), raw, re.M)
                self.assertTrue(m, f"{lang}:{key} is not on its own line")
                self.assertEqual(m.group(1), load_all()[lang][key], f"{lang}:{key} has escapes the sed fallback can't read")


class RenderedPage(unittest.TestCase):
    def test_render_each_language(self):
        for lang in i18n.LANGS:
            page = app.render_index(lang).decode("utf-8")
            self.assertNotRegex(page, r"\{\{(t|h|i18n):", f"{lang}: placeholder left in the page")
            want_dir = "rtl" if lang == "ar" else "ltr"
            self.assertIn(f'<html lang="{lang}" dir="{want_dir}">', page)
            if lang != "ar":
                # Only the language picker may show Arabic ("العربية") in the English/French page.
                visible = page.replace(i18n.t("lang.ar", lang), "")
                leftover = sorted(set(ARABIC.findall(visible)))
                self.assertFalse(leftover, f"{lang}: Arabic text left in the page")

    def test_scripts_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        for lang in i18n.LANGS:
            page = app.render_index(lang).decode("utf-8")
            for i, js in enumerate(re.findall(r"<script>(.*?)</script>", page, re.S)):
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                    f.write(js)
                res = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
                os.unlink(f.name)
                self.assertEqual(res.returncode, 0, f"{lang} script {i}: {res.stderr}")


class Helper(unittest.TestCase):
    def test_fill_and_fallback(self):
        self.assertEqual(i18n.t("srv.found", "en", n=3), "Found 3 model(s).")
        self.assertEqual(i18n.t("srv.found", "fr", n=3), "3 modèle(s) trouvé(s).")
        self.assertEqual(i18n.t("no.such.key", "en"), "no.such.key")
        self.assertEqual(i18n.fill("{a} {b}", {"a": 1}), "1 {b}")

    def test_saved_language(self):
        i18n.set_lang("fr")
        i18n._lang = None                       # read it back from the file
        self.assertEqual(i18n.get_lang(), "fr")
        self.assertEqual(oct(os.stat(i18n.SETTINGS_FILE).st_mode & 0o777), "0o600")
        with self.assertRaises(ValueError):
            i18n.set_lang("de")
        i18n.set_lang("ar")


if __name__ == "__main__":
    unittest.main()
