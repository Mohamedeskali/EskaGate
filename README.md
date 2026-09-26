# EskaGate

A local web app for testing API keys and running a local AI gateway. It uses only the Python 3 standard library, so there is nothing to install beyond Python.

- **Test keys:** finds a key's models (`/models`, `/v1/models`), sends a short test prompt to each one and shows the results live. If a provider hides its model list, type the model names in the manual models field.
- **Monitor saved keys:** re-checks them on a timer and flags added or removed models.
- **Local AI gateway:** one local key (`sk-local-...`) in front of many real provider keys, with failover and OpenAI ↔ Anthropic translation.
  - OpenAI clients: `http://127.0.0.1:8000/v1`
  - Anthropic clients: `http://127.0.0.1:8000`
- **Coding agents:** switches Claude Code, opencode, pi, Hermes or a custom agent onto the gateway and restores their original config afterwards.

The UI is in Moroccan Darija. Keys and settings are stored in `~/.api-test-console/`, which only your user can read.

## Run on Linux

Run it in a terminal (stop with Ctrl+C):

```bash
./run-api-dashboard.sh          # port 8000
./run-api-dashboard.sh 8080     # another port
```

Or install it as an app, with an apps-menu entry and a desktop icon:

```bash
./install_eskali_api.sh               # run again if you move this folder
./eskali_api_launcher.sh stop         # or right-click the icon → stop
./install_eskali_api.sh --uninstall
```

The app runs in the background and logs to `~/.api-test-console/eskali_api.log`.

## Run on Windows

1. Install Python 3 and make sure `python` is on your PATH.
2. Double-click `Run API Dashboard.bat`. The browser opens at `http://127.0.0.1:8000`. Keep the window open, because closing it stops the server.
3. Optional: double-click `create_shortcut.vbs` to add a desktop shortcut.

To use a different port, change `8000` in `Run API Dashboard.bat`.

## Files

| File | Purpose |
|---|---|
| `api_web_dashboard_v2.py` | Server and the whole web page |
| `gateway.py` | Local AI gateway (keys, failover, format translation, logs) |
| `agents.py` | Coding-agent config switching |
| `run-api-dashboard.sh` | Linux: run in a terminal |
| `eskali_api_launcher.sh`, `install_eskali_api.sh` | Linux: background launcher and app installer |
| `Run API Dashboard.bat`, `create_shortcut.vbs` | Windows: run and desktop shortcut |
| `assets/` | App icons (`.png` for Linux, `.ico` for Windows) |
