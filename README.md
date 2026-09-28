# EskaGate

A local web app for testing API keys and running a local AI gateway. It uses only the Python 3 standard library, so there is nothing to install beyond Python.

- **Test keys:** finds a key's models (`/models`, `/v1/models`), sends a short test prompt to each one and shows the results live. If a provider hides its model list, type the model names in the manual models field.
- **Monitor saved keys:** re-checks them on a timer and flags added or removed models.
- **Local AI gateway:** one local key (`sk-local-...`) in front of many real provider keys, with failover and OpenAI ↔ Anthropic translation.
  - OpenAI clients: `http://127.0.0.1:8000/v1`
  - Anthropic clients: `http://127.0.0.1:8000`
- **Coding agents:** switches Claude Code, opencode, pi, Hermes or a custom agent onto the gateway and restores their original config afterwards.

The UI is available in Arabic (Moroccan Darija, the default), English and French. Pick the language from the menu at the top of the page or in ⚙️ Settings; the page switches between right-to-left and left-to-right, and the choice is remembered. Keys and settings are stored in `~/.api-test-console/` (on Windows `%USERPROFILE%\.api-test-console\`), which only your user can read.

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

Run the same command again to update (see [Update](#update)). To remove EskaGate, see [Uninstall](#uninstall).

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

## Update

Run the one-line install command again. It updates EskaGate to the latest version and keeps your keys and settings:

- **Ubuntu:** updates `~/EskaGate` with `git pull` and restarts EskaGate if it was running from there.
- **Windows:** replaces the files in `%LOCALAPPDATA%\EskaGate` and restarts EskaGate if it was running from there.

With a manual install, run `git pull` in the project folder, or download the ZIP again and replace the files. Then restart EskaGate.

## Uninstall

**Ubuntu:**

```bash
curl -fsSL https://raw.githubusercontent.com/Mohamedeskali/EskaGate/main/get.sh | bash -s -- --uninstall
```

**Windows** (in PowerShell):

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Mohamedeskali/EskaGate/main/get.ps1))) -Uninstall
```

Both commands stop EskaGate, remove its shortcuts (apps menu and desktop, or Start Menu and Desktop) and delete the app folder (`~/EskaGate` or `%LOCALAPPDATA%\EskaGate`).

Your keys and settings stay in `~/.api-test-console` (on Windows `%USERPROFILE%\.api-test-console`). They are only deleted if you delete that folder yourself. On Ubuntu:

```bash
rm -rf ~/.api-test-console
```

For a manual install on Linux, run `./install_eskali_api.sh --uninstall` in the project folder, then delete the folder.

## Troubleshooting

**Python is not found or not on PATH**

- **Ubuntu:** run `sudo apt install python3`, then check with `python3 --version`. It must be 3.8 or newer.
- **Windows:** `'python' is not recognized`, or the Microsoft Store opens when you type `python`, means Python is not installed or not on PATH.
  - Install it from [python.org](https://www.python.org/downloads/windows/) with **"Add python.exe to PATH"** ticked.
  - If Python is already installed without PATH: run its installer again, choose **Modify**, click **Next**, tick **"Add Python to environment variables"** and click **Install**.
  - To stop the Store from opening: **Settings → Apps → Advanced app settings → App execution aliases**, and turn off `python.exe` and `python3.exe`.
  - Open a new terminal afterwards. The one-line installer finds Python even when it is not on PATH.

**Port 8000 is already in use**

EskaGate says the port is used by another program, or you see `Address already in use` (Linux) or `WinError 10048` (Windows).

- Find out what uses it: `ss -ltnp | grep :8000` on Linux, `netstat -ano | findstr :8000` on Windows. Stop that program, or run EskaGate on another port:
  - **Ubuntu:** `ESKALI_API_PORT=8080 ~/EskaGate/eskali_api_launcher.sh start`, or `./run-api-dashboard.sh 8080` in a terminal. The apps-menu icon always uses port 8000.
  - **Windows:** run `$env:ESKALI_API_PORT = 8080` and then the one-line install command in the same PowerShell window. The shortcuts then use port 8080. You can also change `--port 8000` in `%LOCALAPPDATA%\EskaGate\EskaGate.cmd`, or `8000` in `Run API Dashboard.bat` for a manual install.
- The gateway addresses change with the port, for example `http://127.0.0.1:8080/v1`. In the Agents tab, press Enable again for each agent that uses the gateway.

**Windows blocks the script**

- The one-line `irm … | iex` command does not run a script file, so the execution policy does not block it.
- If you downloaded `get.ps1` and running `.\get.ps1` says *running scripts is disabled on this system*, run it once with:

  ```powershell
  powershell -ExecutionPolicy Bypass -File .\get.ps1
  ```

  Or allow local scripts for your user with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then run `Unblock-File .\get.ps1`.
- If Windows shows *Windows protected your PC* for `Run API Dashboard.bat` or `create_shortcut.vbs` from a downloaded ZIP, click **More info → Run anyway**. You can also avoid it: before extracting, right-click the ZIP, choose **Properties** and tick **Unblock**.

## Files

| File | Purpose |
|---|---|
| `api_web_dashboard_v2.py` | Server and the whole web page |
| `gateway.py` | Local AI gateway (keys, failover, format translation, logs) |
| `agents.py` | Coding-agent config switching |
| `i18n.py`, `i18n/` | Translations: `ar.json`, `en.json`, `fr.json` (same keys) and the helper that loads them |
| `tests/` | Checks: `python3 -m unittest discover tests` |
| `get.sh`, `get.ps1` | One-line installers for Ubuntu and Windows |
| `run-api-dashboard.sh` | Linux: run in a terminal |
| `eskali_api_launcher.sh`, `install_eskali_api.sh` | Linux: background launcher and app installer |
| `Run API Dashboard.bat`, `create_shortcut.vbs` | Windows: run and desktop shortcut |
| `assets/` | App icons (`.png` for Linux, `.ico` for Windows) |
| `docs/screenshots/` | Screenshots used in this README |
| `LICENSE` | MIT license |

## Translations

Every piece of text in the page, the server messages, the launcher and the Linux installer comes from `i18n/<lang>.json`. The three files must have the same keys; `python3 -m unittest discover tests` checks that, and also checks that every key the code uses exists and that the English and French pages have no Arabic left in them. Values can hold `{name}` placeholders. To add a string, add the key to all three files, then use `{{t:key}}` in the HTML, `T('key', {name})` in the page's JavaScript, `i18n.t("key", name=...)` in Python, or `t key name=...` in the shell scripts.

The one-line installers (`get.sh`, `get.ps1`) and the Windows `.bat`/`.vbs` files stay in English: they run before the translation files are on disk, or cannot read them.

## License

EskaGate is released under the [MIT License](LICENSE).
