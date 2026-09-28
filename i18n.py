#!/usr/bin/env python3
"""
Translations for EskaGate: one flat JSON file per language in i18n/ (ar, en, fr),
all with the same keys. Values may hold {name} placeholders.

    t("srv.found", n=12)            -> text in the saved UI language
    t("srv.found", "fr", n=12)      -> text in French

The chosen language lives in <data dir>/ui-settings.json ({"lang": "en"}), so the page,
the server messages and the launcher/installer all use the same one. Default: Arabic.

Shell use (launcher / installer):  python3 i18n.py <key> [name=value ...]
                                   python3 i18n.py --lang
"""
import json
import os
import re
import sys
from pathlib import Path

LANGS = ("ar", "en", "fr")
DEFAULT_LANG = "ar"
RTL_LANGS = ("ar",)
I18N_DIR = Path(__file__).resolve().parent / "i18n"
# Same folder as gateway.DATA_DIR (not imported here, so the launcher stays light).
DATA_DIR = Path(os.environ.get("API_CONSOLE_HOME") or (Path.home() / ".api-test-console"))
SETTINGS_FILE = DATA_DIR / "ui-settings.json"

_cache = {}
_lang = None


def load(lang):
    """The dictionary for one language (cached)."""
    if lang not in _cache:
        try:
            _cache[lang] = json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache[lang] = {}
    return _cache[lang]


def get_lang():
    global _lang
    if _lang is None:
        try:
            lang = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("lang")
        except (OSError, ValueError, AttributeError):
            lang = None
        _lang = lang if lang in LANGS else DEFAULT_LANG
    return _lang


def set_lang(lang):
    global _lang
    if lang not in LANGS:
        raise ValueError(t("srv.bad_lang"))
    import gateway  # owner-only write, like every other file in the data folder
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data["lang"] = lang
    gateway.write_private(SETTINGS_FILE, json.dumps(data, ensure_ascii=False, indent=1))
    _lang = lang


def direction(lang=None):
    return "rtl" if (lang or get_lang()) in RTL_LANGS else "ltr"


def fill(text, params):
    """Replaces {name} with params[name]; unknown placeholders are left as they are."""
    return re.sub(r"\{(\w+)\}", lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), text)


def t(key, lang=None, **params):
    lang = lang if lang in LANGS else get_lang()
    text = load(lang).get(key)
    if text is None:
        text = load(DEFAULT_LANG).get(key, key)
    return fill(text, params)


def _cli(argv):
    if not argv:
        print("Usage: i18n.py <key> [name=value ...] | --lang", file=sys.stderr)
        return 2
    if argv[0] == "--lang":
        print(get_lang())
        return 0
    params = dict(a.split("=", 1) for a in argv[1:] if "=" in a)
    lang = params.pop("lang", None)
    print(t(argv[0], lang, **params))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(_cli(sys.argv[1:]))
