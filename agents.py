"""
Connect coding agents to the local gateway.

Enable  = back up the agent's config, then write the local URL + local key.
Disable = put the original values back. If nothing else touched the file since
          we wrote it, the backup is restored byte for byte; otherwise only the
          values we changed are restored, so edits made in between are kept.

Config formats were checked against the installed tools, not guessed:
  Claude Code  ~/.claude/settings.json   "env": ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / model vars
  opencode     ~/.config/opencode/opencode.json   "provider": {id: {npm, options.baseURL/apiKey, models}}, "model"
  pi           ~/.pi/agent/models.json   "providers": {id: {baseUrl, api, apiKey, models}}
               ~/.pi/agent/settings.json "defaultProvider" / "defaultModel"
  Hermes       ~/.hermes/config.yaml   model.provider/base_url/api_key/default, written with `hermes config set`
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import gateway
import i18n

STATE_FILE = gateway.DATA_DIR / "agents.json"
MODEL_FILTER_FILE = gateway.DATA_DIR / "agent-models.json"   # {agent_id: [models shown to that agent]}
BACKUP_DIR = gateway.DATA_DIR / "agent-backups"
MISSING = {"__missing__": True}
PROVIDER_ID = "local-gateway"


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state):
    gateway.write_private(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))


def _sha(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _backup(agent, path):
    """Copy the config aside (owner-only: these files contain API keys)."""
    gateway.ensure_data_dir()
    BACKUP_DIR.mkdir(exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    if not Path(path).exists():
        return None
    dest = BACKUP_DIR / f"{agent}-{time.strftime('%Y%m%d-%H%M%S')}{Path(path).suffix}.bak"
    gateway.write_private(dest, Path(path).read_text(encoding="utf-8"))
    return str(dest)


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _strip_jsonc(text):
    """Remove // and /* */ comments and trailing commas outside of strings."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    return json.loads(_strip_jsonc(text) if path.suffix == ".jsonc" else text)


def _get(d, keys):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return MISSING
        d = d[k]
    return d


def _set(d, keys, value):
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    if value == MISSING:          # also matches the copy read back from agents.json
        d.pop(keys[-1], None)
    else:
        d[keys[-1]] = value


_versions = {}


def _version(binary):
    """`--version` of the installed tool, cached (some CLIs take a second to start)."""
    if binary not in _versions:
        try:
            out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
            _versions[binary] = (out.stdout or out.stderr).strip().splitlines()[0].split(" · ")[0][:48]
        except Exception:
            _versions[binary] = ""
    return _versions[binary]


# ---------------------------------------------------------------------------
# JSON-file agents (Claude Code, opencode)
# ---------------------------------------------------------------------------
class JsonAgent:
    id = name = binary = ""

    def config_path(self):
        raise NotImplementedError

    def changes(self, base_url, local_key, model, models):
        """{path_tuple: new_value or MISSING}"""
        raise NotImplementedError

    def is_ours(self, data, base_url):
        raise NotImplementedError

    def enable(self, base_url, local_key, model, models):
        path = self.config_path()
        state = _load_state()
        previous = state.get(self.id) if state.get(self.id, {}).get("enabled") else None
        data = _read_json(path)
        changes = self.changes(base_url, local_key, model, models)
        if previous:
            # Already enabled (e.g. new model or new local key): keep the ORIGINAL values.
            originals = previous["originals"]
            backup = previous["backup"]
            existed = previous["existed"]
        else:
            originals = {json.dumps(k): _get(data, k) for k in changes}
            backup = _backup(self.id, path)
            existed = Path(path).exists()
        for k, v in changes.items():
            _set(data, k, v)
        _write_json(path, data)
        state[self.id] = {"enabled": True, "path": str(path), "backup": backup, "existed": existed,
                          "originals": originals, "written_hash": _sha(path), "model": model,
                          "base_url": base_url, "since": time.time()}
        _save_state(state)

    def disable(self):
        state = _load_state()
        info = state.get(self.id)
        if not info or not info.get("enabled"):
            raise ValueError(i18n.t("err.not_enabled_by_gw"))
        path = Path(info["path"])
        if _sha(path) == info.get("written_hash"):
            # Untouched since we wrote it: put the original file back exactly.
            if info.get("backup") and Path(info["backup"]).exists():
                path.write_text(Path(info["backup"]).read_text(encoding="utf-8"), encoding="utf-8")
            elif not info.get("existed"):
                path.unlink(missing_ok=True)
            restored = "exact"
        else:
            data = _read_json(path)
            for k, v in info["originals"].items():
                _set(data, json.loads(k), v)
            self.cleanup(data)
            _write_json(path, data)
            restored = "values"
        info.update(enabled=False, restored=restored, disabled_at=time.time())
        _save_state(state)
        return restored

    def cleanup(self, data):
        pass

    def status(self, base_url):
        path = self.config_path()
        info = _load_state().get(self.id, {})
        binary = shutil.which(self.binary)
        out = {"id": self.id, "name": self.name, "installed": bool(binary) or Path(path).exists(),
               "binary": binary or "", "version": _version(binary) if binary else "",
               "config_path": str(path), "config_exists": Path(path).exists(),
               "enabled": bool(info.get("enabled")), "model": info.get("model", ""),
               "backup": info.get("backup") or "", "note": ""}
        if out["enabled"]:
            try:
                ours = self.is_ours(_read_json(path), base_url)
            except Exception:
                ours = False
            if not ours:
                out["note"] = i18n.t("note.changed_outside")
            elif info.get("base_url") != base_url:
                out["note"] = i18n.t("note.other_port")
        return out


class ClaudeCode(JsonAgent):
    id, name, binary = "claude", "Claude Code", "claude"

    def config_path(self):
        root = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
        return Path(root) / "settings.json"

    def changes(self, base_url, local_key, model, models):
        return {
            ("env", "ANTHROPIC_BASE_URL"): base_url,            # Claude Code appends /v1/messages
            ("env", "ANTHROPIC_AUTH_TOKEN"): local_key,         # sent as Authorization: Bearer
            ("env", "ANTHROPIC_API_KEY"): MISSING,              # would take precedence over the token
            ("env", "ANTHROPIC_MODEL"): model,
            ("env", "ANTHROPIC_DEFAULT_OPUS_MODEL"): model,     # the "opus"/"sonnet"/"haiku" aliases
            ("env", "ANTHROPIC_DEFAULT_SONNET_MODEL"): model,
            ("env", "ANTHROPIC_DEFAULT_HAIKU_MODEL"): model,
            ("env", "CLAUDE_CODE_SUBAGENT_MODEL"): model,
        }

    def is_ours(self, data, base_url):
        return _get(data, ("env", "ANTHROPIC_BASE_URL")) == base_url

    def cleanup(self, data):
        if data.get("env") == {}:
            data.pop("env")


class OpenCode(JsonAgent):
    id, name, binary = "opencode", "opencode", "opencode"

    def config_path(self):
        if os.environ.get("OPENCODE_CONFIG"):
            return Path(os.environ["OPENCODE_CONFIG"])
        root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "opencode"
        for name in ("opencode.json", "opencode.jsonc"):
            if (root / name).exists():
                return root / name
        return root / "opencode.json"

    def changes(self, base_url, local_key, model, models):
        return {
            ("provider", PROVIDER_ID): {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Local Gateway",
                "options": {"baseURL": base_url + "/v1", "apiKey": local_key},
                "models": {m: {"name": m} for m in (models or [model])},
            },
            ("model",): f"{PROVIDER_ID}/{model}",
        }

    def is_ours(self, data, base_url):
        return _get(data, ("provider", PROVIDER_ID, "options", "baseURL")) == base_url + "/v1"

    def cleanup(self, data):
        if data.get("provider") == {}:
            data.pop("provider")


def _pi_dir():
    return Path.home() / ".pi" / "agent"


class _PiSettings(JsonAgent):
    """pi's startup model lives in settings.json, next to models.json; switched together with Pi."""
    id, name, binary = "pi-settings", "pi settings", "pi"

    def config_path(self):
        return _pi_dir() / "settings.json"

    def changes(self, base_url, local_key, model, models):
        return {("defaultProvider",): PROVIDER_ID, ("defaultModel",): model}

    def is_ours(self, data, base_url):
        return _get(data, ("defaultProvider",)) == PROVIDER_ID


class Pi(JsonAgent):
    id, name, binary = "pi", "pi", "pi"

    def config_path(self):
        return _pi_dir() / "models.json"

    def changes(self, base_url, local_key, model, models):
        return {
            ("providers", PROVIDER_ID): {
                "name": "Local Gateway",
                "baseUrl": base_url + "/v1",
                "api": "openai-completions",
                "apiKey": local_key,
                "models": [{"id": m, "name": m} for m in (models or [model])],
            },
        }

    def is_ours(self, data, base_url):
        return _get(data, ("providers", PROVIDER_ID, "baseUrl")) == base_url + "/v1"

    def cleanup(self, data):
        if data.get("providers") == {}:
            data.pop("providers")

    def enable(self, base_url, local_key, model, models):
        super().enable(base_url, local_key, model, models)
        _PiSettings().enable(base_url, local_key, model, models)

    def disable(self):
        restored = super().disable()
        try:
            _PiSettings().disable()
        except ValueError:
            pass            # enabled before settings.json was handled
        return restored


# ---------------------------------------------------------------------------
# Hermes (YAML): written through Hermes' own CLI so the format is always right
# ---------------------------------------------------------------------------
class Hermes:
    id, name, binary = "hermes", "Hermes", "hermes"
    KEYS = ("provider", "base_url", "api_key", "default")

    def config_path(self):
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")) / "config.yaml"

    @staticmethod
    def read_model_block(text):
        """Top-level `model:` mapping of config.yaml (simple `key: value` lines)."""
        values, inside = {}, False
        for line in text.splitlines():
            if re.match(r"^model:\s*$", line):
                inside = True
                continue
            if inside:
                if line.strip() and not line.startswith((" ", "\t")):
                    break
                m = re.match(r"^\s{2}([A-Za-z_]+):\s*(.*?)\s*$", line)
                if m:
                    v = m.group(2)
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                        v = v[1:-1]
                    values[m.group(1)] = v
        return values

    def _cli(self, *args):
        binary = shutil.which(self.binary)
        if not binary:
            raise ValueError(i18n.t("err.hermes_missing"))
        res = subprocess.run([binary, "config", *args], capture_output=True, text=True, timeout=60,
                             env=os.environ.copy())
        if res.returncode != 0:
            raise ValueError(i18n.t("err.hermes_failed", cmd=args[0], error=(res.stderr or res.stdout).strip()[:200]))

    def enable(self, base_url, local_key, model, models):
        path = self.config_path()
        state = _load_state()
        previous = state.get(self.id) if state.get(self.id, {}).get("enabled") else None
        if previous:
            originals, backup = previous["originals"], previous["backup"]
        else:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            block = self.read_model_block(text)
            originals = {k: block.get(k, None) for k in self.KEYS}
            backup = _backup(self.id, path)
        for k, v in (("provider", "custom"), ("base_url", base_url + "/v1"),
                     ("api_key", local_key), ("default", model)):
            self._cli("set", f"model.{k}", v)
        state[self.id] = {"enabled": True, "path": str(path), "backup": backup, "originals": originals,
                          "written_hash": _sha(path), "model": model, "base_url": base_url, "since": time.time()}
        _save_state(state)

    def disable(self):
        state = _load_state()
        info = state.get(self.id)
        if not info or not info.get("enabled"):
            raise ValueError(i18n.t("err.not_enabled_by_gw"))
        path = Path(info["path"])
        if _sha(path) == info.get("written_hash") and info.get("backup") and Path(info["backup"]).exists():
            text = Path(info["backup"]).read_text(encoding="utf-8")
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
            path.write_text(text, encoding="utf-8")
            os.chmod(path, mode)
            restored = "exact"
        else:
            for k, v in info["originals"].items():
                if v is None:
                    self._cli("unset", f"model.{k}")
                else:
                    self._cli("set", f"model.{k}", v)
            restored = "values"
        info.update(enabled=False, restored=restored, disabled_at=time.time())
        _save_state(state)
        return restored

    def status(self, base_url):
        path = self.config_path()
        info = _load_state().get(self.id, {})
        binary = shutil.which(self.binary)
        out = {"id": self.id, "name": self.name, "installed": bool(binary), "binary": binary or "",
               "version": _version(binary) if binary else "", "config_path": str(path),
               "config_exists": path.exists(), "enabled": bool(info.get("enabled")),
               "model": info.get("model", ""), "backup": info.get("backup") or "", "note": ""}
        if out["enabled"]:
            block = self.read_model_block(path.read_text(encoding="utf-8")) if path.exists() else {}
            if block.get("base_url") != base_url + "/v1":
                out["note"] = i18n.t("note.changed_outside")
            elif info.get("base_url") != base_url:
                out["note"] = i18n.t("note.other_port")
        return out


# ---------------------------------------------------------------------------
# Custom agents added from the page: any tool whose config is a JSON file or
# a KEY=VALUE .env file. The user says which fields hold the URL/key/model.
# ---------------------------------------------------------------------------
CUSTOM_FILE = gateway.DATA_DIR / "agents-custom.json"
ICON_MAX = 200_000   # bytes of data: URL (the page resizes images to 96x96 first)


def _load_custom():
    try:
        data = json.loads(CUSTOM_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.setdefault("agents", [])
    data.setdefault("icons", {})
    return data


def _save_custom(data):
    gateway.write_private(CUSTOM_FILE, json.dumps(data, indent=2, ensure_ascii=False))


def _gateway_url(base_url, fmt):
    # OpenAI clients append /chat/completions to a .../v1 base;
    # Anthropic clients append /v1/messages to the bare root.
    return base_url + "/v1" if fmt == "openai" else base_url


class CustomJsonAgent(JsonAgent):
    def __init__(self, spec):
        self.spec = spec
        self.id, self.name, self.binary = spec["id"], spec["name"], spec.get("binary", "")

    def config_path(self):
        return Path(os.path.expanduser(self.spec["path"]))

    def _fields(self):
        f = self.spec["fields"]
        return [(k, tuple(f[k].split("."))) for k in ("base_url", "api_key", "model") if f.get(k)]

    def changes(self, base_url, local_key, model, models):
        values = {"base_url": _gateway_url(base_url, self.spec["format"]), "api_key": local_key, "model": model}
        return {path: values[k] for k, path in self._fields()}

    def is_ours(self, data, base_url):
        path = dict(self._fields())["base_url"]
        return _get(data, path) == _gateway_url(base_url, self.spec["format"])

    def current(self):
        try:
            data = _read_json(self.config_path())
        except Exception:
            return {}
        out = {}
        for k, path in self._fields():
            v = _get(data, path)
            out[k] = None if v == MISSING else (gateway.mask_key(str(v)) if k == "api_key" else str(v))
        return out


class CustomEnvAgent:
    """KEY=VALUE files (.env style). Only the chosen variables are touched."""

    def __init__(self, spec):
        self.spec = spec
        self.id, self.name, self.binary = spec["id"], spec["name"], spec.get("binary", "")

    def config_path(self):
        return Path(os.path.expanduser(self.spec["path"]))

    def _vars(self, base_url, local_key, model):
        values = {"base_url": _gateway_url(base_url, self.spec["format"]), "api_key": local_key, "model": model}
        f = self.spec["fields"]
        return {f[k]: values[k] for k in ("base_url", "api_key", "model") if f.get(k)}

    @staticmethod
    def _parse(text, raw=False):
        vals = {}
        for line in text.splitlines():
            m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
            if m:
                v = m.group(2).strip()
                if not raw and len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                vals[m.group(1)] = v
        return vals

    @staticmethod
    def _apply(text, updates):
        """Replace existing KEY= lines in place, append new ones, drop keys set to None."""
        lines, seen = [], set()
        for line in text.splitlines():
            m = re.match(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if m and m.group(2) in updates:
                seen.add(m.group(2))
                if updates[m.group(2)] is not None:
                    lines.append(f"{m.group(1)}{m.group(2)}={updates[m.group(2)]}")
                continue
            lines.append(line)
        for k, v in updates.items():
            if k not in seen and v is not None:
                lines.append(f"{k}={v}")
        return "\n".join(lines) + "\n"

    def _write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)

    def enable(self, base_url, local_key, model, models):
        path = self.config_path()
        state = _load_state()
        previous = state.get(self.id) if state.get(self.id, {}).get("enabled") else None
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        updates = self._vars(base_url, local_key, model)
        if previous:
            originals, backup, existed = previous["originals"], previous["backup"], previous["existed"]
        else:
            current = self._parse(text, raw=True)      # keep original quoting for the restore
            originals = {k: current.get(k) for k in updates}
            backup, existed = _backup(self.id, path), path.exists()
        self._write(path, self._apply(text, updates))
        state[self.id] = {"enabled": True, "path": str(path), "backup": backup, "existed": existed,
                          "originals": originals, "written_hash": _sha(path), "model": model,
                          "base_url": base_url, "since": time.time()}
        _save_state(state)

    def disable(self):
        state = _load_state()
        info = state.get(self.id)
        if not info or not info.get("enabled"):
            raise ValueError(i18n.t("err.not_enabled_by_gw"))
        path = Path(info["path"])
        if _sha(path) == info.get("written_hash"):
            if info.get("backup") and Path(info["backup"]).exists():
                self._write(path, Path(info["backup"]).read_text(encoding="utf-8"))
            elif not info.get("existed"):
                path.unlink(missing_ok=True)
            restored = "exact"
        else:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            self._write(path, self._apply(text, info["originals"]))
            restored = "values"
        info.update(enabled=False, restored=restored, disabled_at=time.time())
        _save_state(state)
        return restored

    def current(self):
        path = self.config_path()
        vals = self._parse(path.read_text(encoding="utf-8")) if path.exists() else {}
        f = self.spec["fields"]
        out = {}
        for k in ("base_url", "api_key", "model"):
            if f.get(k):
                v = vals.get(f[k])
                out[k] = gateway.mask_key(v) if (k == "api_key" and v) else v
        return out

    def status(self, base_url):
        path = self.config_path()
        info = _load_state().get(self.id, {})
        out = {"id": self.id, "name": self.name, "installed": True, "binary": "", "version": "",
               "config_path": str(path), "config_exists": path.exists(),
               "enabled": bool(info.get("enabled")), "model": info.get("model", ""),
               "backup": info.get("backup") or "", "note": ""}
        if out["enabled"]:
            cur = self._parse(path.read_text(encoding="utf-8")) if path.exists() else {}
            if cur.get(self.spec["fields"]["base_url"]) != _gateway_url(base_url, self.spec["format"]):
                out["note"] = i18n.t("note.changed_outside")
        return out


BUILTIN = {a.id: a for a in (ClaudeCode(), OpenCode(), Pi(), Hermes())}


def all_agents():
    agents = dict(BUILTIN)
    for spec in _load_custom()["agents"]:
        agents[spec["id"]] = (CustomEnvAgent if spec["kind"] == "env" else CustomJsonAgent)(spec)
    return agents


def _load_model_filters():
    try:
        data = json.loads(MODEL_FILTER_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def visible_models(agent_id, all_models=None):
    """Models this agent should see: the ones picked for it, or all when none are picked
    (or none of the picked ones exist anymore)."""
    all_models = gateway.store().all_models() if all_models is None else all_models
    chosen = set(_load_model_filters().get(agent_id) or [])
    return [m for m in all_models if m in chosen] or all_models


def set_model_filter(agent_id, models, base_url, model=None, effort=None):
    """Save which models show up for an agent; an empty list means all of them.
    An enabled agent is rewritten right away (with `model` as its model, when given)
    so its config lists only those models."""
    if agent_id not in all_agents():
        raise ValueError(i18n.t("err.unknown_agent"))
    clean = list(dict.fromkeys(m.strip() for m in (models or []) if isinstance(m, str) and m.strip()))
    filters = _load_model_filters()
    if clean:
        filters[agent_id] = clean
    else:
        filters.pop(agent_id, None)
    gateway.ensure_data_dir()
    gateway.write_private(MODEL_FILTER_FILE, json.dumps(filters, indent=2, ensure_ascii=False))
    info = _load_state().get(agent_id, {})
    if info.get("enabled"):
        shown = visible_models(agent_id)
        old_model, old_effort = gateway.split_effort(info.get("model"))
        model = next((m for m in (model, old_model) if m and m in shown), shown[0] if shown else old_model)
        enable(agent_id, base_url, model, old_effort if effort is None else effort)


def list_agents(base_url):
    custom = _load_custom()
    specs = {s["id"]: s for s in custom["agents"]}
    filters = _load_model_filters()
    out = []
    for agent in all_agents().values():
        st = agent.status(base_url)
        st["model"], st["effort"] = gateway.split_effort(st.get("model"))
        st["model_filter"] = filters.get(agent.id, [])
        st["builtin"] = agent.id in BUILTIN
        st["icon"] = custom["icons"].get(agent.id, "")
        if agent.id in specs:
            st["spec"] = specs[agent.id]
            st["installed"] = True
            try:
                st["current"] = agent.current()
            except Exception:
                st["current"] = {}
        out.append(st)
    return out


def enable(agent_id, base_url, model, effort=None):
    """`effort` is a thinking level from gateway.EFFORTS ("" = the agent's own default).
    None keeps the level already in `model` ("gpt-5@high"), as saved by an earlier enable."""
    agent = all_agents().get(agent_id)
    if not agent:
        raise ValueError(i18n.t("err.unknown_agent"))
    model, saved = gateway.split_effort(model)
    effort = saved if effort is None else (effort or "")
    if effort and effort not in gateway.EFFORTS:
        raise ValueError("Unknown thinking level.")
    models = visible_models(agent_id)
    if not model:
        raise ValueError(i18n.t("err.pick_model"))
    if model not in models:
        if _load_model_filters().get(agent_id) and models:
            model = models[0]          # only the marked models, even if that's just one
        else:
            models = [model] + models
    # The chosen model goes out as "model@level"; the gateway turns the suffix into the
    # provider's own thinking parameter, so this works the same for every agent.
    wire = gateway.with_effort(model, effort)
    agent.enable(base_url, gateway.store().local_key, wire, [wire if m == model else m for m in models])


def _effort_models():
    """The "model@level" names enabled agents were given, so /v1/models lists them too."""
    return [info["model"] for info in _load_state().values()
            if info.get("enabled") and gateway.split_effort(info.get("model"))[1]]


gateway.EXTRA_MODELS = _effort_models


def disable(agent_id):
    agent = all_agents().get(agent_id)
    if not agent:
        raise ValueError(i18n.t("err.unknown_agent"))
    return agent.disable()


def refresh_enabled(base_url):
    """After the local key is regenerated, rewrite it into every enabled agent."""
    done = []
    known = all_agents()
    for agent_id, info in _load_state().items():
        if info.get("enabled") and agent_id in known:
            enable(agent_id, base_url, info.get("model"))
            done.append(agent_id)
    return done


FIELD_RE = re.compile(r"^[A-Za-z0-9_\-$]+(\.[A-Za-z0-9_\-$]+)*$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def save_custom(spec_in):
    name = (spec_in.get("name") or "").strip()[:40]
    kind = spec_in.get("kind") if spec_in.get("kind") in ("json", "env") else "json"
    fmt = spec_in.get("format") if spec_in.get("format") in ("openai", "anthropic") else "openai"
    path = (spec_in.get("path") or "").strip()
    fields = {k: (spec_in.get("fields", {}).get(k) or "").strip() for k in ("base_url", "api_key", "model")}
    if not name:
        raise ValueError(i18n.t("err.agent_name"))
    if not path or not os.path.isabs(os.path.expanduser(path)):
        raise ValueError(i18n.t("err.agent_path"))
    if Path(os.path.expanduser(path)).is_dir():
        raise ValueError(i18n.t("err.agent_path_dir"))
    if not fields["base_url"] or not fields["api_key"]:
        raise ValueError(i18n.t("err.agent_fields"))
    check = ENV_RE if kind == "env" else FIELD_RE
    for k, v in fields.items():
        if v and not check.match(v):
            raise ValueError(i18n.t("err.agent_field_env" if kind == "env" else "err.agent_field_json", field=v))
    data = _load_custom()
    state = _load_state()
    existing = next((a for a in data["agents"] if a["id"] == spec_in.get("id")), None)
    if existing and state.get(existing["id"], {}).get("enabled"):
        raise ValueError(i18n.t("err.agent_disable_edit"))
    if any(a["name"].lower() == name.lower() and a["id"] != spec_in.get("id") for a in data["agents"]) \
            or name.lower() in (a.name.lower() for a in BUILTIN.values()):
        raise ValueError(i18n.t("err.agent_exists", name=name))
    spec = {"id": existing["id"] if existing else gateway.new_id("custom_"), "name": name, "kind": kind,
            "format": fmt, "path": path, "fields": fields}
    if existing:
        data["agents"][data["agents"].index(existing)] = spec
    else:
        data["agents"].append(spec)
    _save_custom(data)
    return spec["id"]


def delete_custom(agent_id):
    if _load_state().get(agent_id, {}).get("enabled"):
        raise ValueError(i18n.t("err.agent_disable_delete"))
    data = _load_custom()
    data["agents"] = [a for a in data["agents"] if a["id"] != agent_id]
    data["icons"].pop(agent_id, None)
    _save_custom(data)


def set_icon(agent_id, image):
    """image: a data:image/... URL, or empty to go back to the default icon."""
    if agent_id not in all_agents():
        raise ValueError(i18n.t("err.unknown_agent"))
    data = _load_custom()
    if not image:
        data["icons"].pop(agent_id, None)
    else:
        if not re.match(r"^data:image/(png|jpeg|webp|gif|svg\+xml);base64,[A-Za-z0-9+/=]+$", image):
            raise ValueError(i18n.t("err.image_type"))
        if len(image) > ICON_MAX:
            raise ValueError(i18n.t("err.image_size"))
        data["icons"][agent_id] = image
    _save_custom(data)
