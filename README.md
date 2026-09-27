# EskaGate

A local web app for testing API keys and running a local AI gateway. It uses only the Python 3 standard library, so there is nothing to install beyond Python.

- **Test keys:** finds a key's models (`/models`, `/v1/models`), sends a short test prompt to each one and shows the results live. If a provider hides its model list, type the model names in the manual models field.
- **Monitor saved keys:** re-checks them on a timer and flags added or removed models.
- **Local AI gateway:** one local key (`sk-local-...`) in front of many real provider keys, with failover and OpenAI ↔ Anthropic translation.
  - OpenAI clients: `http://127.0.0.1:8000/v1`
  - Anthropic clients: `http://127.0.0.1:8000`
- **Coding agents:** switches Claude Code, opencode, pi, Hermes or a custom agent onto the gateway and restores their original config afterwards.

The UI is in Moroccan Darija. Keys and settings are stored in `~/.api-test-console/` (on Windows `%USERPROFILE%\.api-test-console\`), which only your user can read.

## Install in one line

> These commands download EskaGate from GitHub, so they work once this repository is public.

**Ubuntu** (in a terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/Mohamedeskali/EskaGate/main/get.sh | bash
```

It needs `python3` (3.8 or newer) and `git`. If one is missing, it prints the `sudo apt install` command to run and stops; it never runs `sudo` itself. It installs to `~/EskaGate`, adds EskaGate to the apps menu and the desktop, and starts it.

**Windows** (in PowerShell):

```powershell
irm https://raw.githubusercontent.com/Mohamedeskali/EskaGate/main/get.ps1 | iex
```

If Python 3.8 or newer is missing, it installs Python 3.12 with `winget`, or explains how to install it from python.org. It installs to `%LOCALAPPDATA%\EskaGate` (no git needed), adds Start Menu and Desktop shortcuts, and starts EskaGate in its own window. Closing that window stops the app.

Run the same command again to update. Your keys and settings are kept.

## Screenshots

The screenshots use made-up demo providers and keys.

**Test a key:** every model is tried live, with its status and response time.

![Test tab](docs/screenshots/test.png)

| My keys and monitoring | Providers |
|---|---|
| ![My keys tab](docs/screenshots/keys.png) | ![Providers tab](docs/screenshots/providers.png) |
| **Gateway:** local key, endpoints and request log | **Agents:** switch coding agents onto the gateway |
| ![Gateway tab](docs/screenshots/gateway.png) | ![Agents tab](docs/screenshots/agents.png) |

## Manual install

Use these steps if you prefer not to run the one-line installers.

### Requirements

- **Python 3.8 or newer.** Only the standard library is used, so there is no `pip install`.
- Linux (for example Ubuntu) or Windows 10/11, and a web browser.
- `git`, only if you want to get the project with `git clone`.

### Install Python

- **Ubuntu:** Python 3 is usually already installed. Check with `python3 --version`. If it is missing, run `sudo apt install python3`.
- **Windows:** download the installer from [python.org/downloads/windows](https://www.python.org/downloads/windows/) and run it.

  > ⚠️ On the first installer screen, tick **"Add python.exe to PATH"** before clicking **Install Now**. Without it, `python` is not found and the app does not start.

  Then open a new terminal and check with `python --version`.

### Get the project

With git:

```bash
git clone https://github.com/Mohamedeskali/EskaGate.git
cd EskaGate
```

Or without git: on the GitHub page click **Code → Download ZIP**, then extract the ZIP. On Linux, if the scripts don't run after extracting, make them executable with `chmod +x *.sh`.

### Run on Linux

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

### Run on Windows

1. Install Python as described above, with "Add python.exe to PATH" ticked.
2. Double-click `Run API Dashboard.bat`. The browser opens at `http://127.0.0.1:8000`. Keep the window open, because closing it stops the server.
3. Optional: double-click `create_shortcut.vbs` to add a desktop shortcut.

To use a different port, change `8000` in `Run API Dashboard.bat`.

## Uninstall

- **Ubuntu (one-line install):** run `~/EskaGate/install_eskali_api.sh --uninstall`, then delete `~/EskaGate`.
- **Windows (one-line install):** delete the EskaGate shortcuts from the Start Menu and the Desktop, then delete `%LOCALAPPDATA%\EskaGate`.

This leaves your keys and settings in `.api-test-console`. Delete that folder too if you want to remove them.

## Files

| File | Purpose |
|---|---|
| `api_web_dashboard_v2.py` | Server and the whole web page |
| `gateway.py` | Local AI gateway (keys, failover, format translation, logs) |
| `agents.py` | Coding-agent config switching |
| `get.sh`, `get.ps1` | One-line installers for Ubuntu and Windows |
| `run-api-dashboard.sh` | Linux: run in a terminal |
| `eskali_api_launcher.sh`, `install_eskali_api.sh` | Linux: background launcher and app installer |
| `Run API Dashboard.bat`, `create_shortcut.vbs` | Windows: run and desktop shortcut |
| `assets/` | App icons (`.png` for Linux, `.ico` for Windows) |
| `docs/screenshots/` | Screenshots used in this README |
