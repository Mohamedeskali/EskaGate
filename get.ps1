# One-line install / update of EskaGate on Windows (PowerShell):
#   irm https://raw.githubusercontent.com/Mohamedeskali/EskaGate/main/get.ps1 | iex
# Downloads the project ZIP (no git needed) to %LOCALAPPDATA%\EskaGate, adds Start Menu and
# Desktop shortcuts, then starts the app. Your keys and settings live in
# %USERPROFILE%\.api-test-console and are never touched.
# Runs inside "& { }" and never calls exit, so it cannot close the PowerShell window it runs in.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # the progress bar makes downloads very slow on PowerShell 5.1

    $ZipUrl = 'https://github.com/Mohamedeskali/EskaGate/archive/refs/heads/main.zip'
    $InstallDir = Join-Path $env:LOCALAPPDATA 'EskaGate'
    $Port = if ($env:ESKALI_API_PORT) { $env:ESKALI_API_PORT } else { '8000' }
    $Url = "http://127.0.0.1:$Port"
    $Script = Join-Path $InstallDir 'api_web_dashboard_v2.py'

    function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

    # Full path of a Python 3.8+ interpreter, or $null. Skips the Microsoft Store "python" alias,
    # which only opens the Store.
    function Find-Python {
        $ErrorActionPreference = 'Continue'
        # JSON keeps the path ASCII, so a user folder with non-English letters survives the console encoding.
        $code = 'import sys, json; sys.version_info >= (3, 8) and print(json.dumps(sys.executable))'
        $tries = @(
            @{ Exe = 'py'; Args = @('-3') },
            @{ Exe = 'python'; Args = @() },
            @{ Exe = 'python3'; Args = @() }
        )
        $known = @(Get-ChildItem -Path "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
            "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)
        foreach ($k in $known) { $tries += @{ Exe = $k.FullName; Args = @() } }
        foreach ($t in $tries) {
            if (-not (Get-Command $t.Exe -ErrorAction SilentlyContinue)) { continue }
            $a = $t.Args
            try {
                $out = & $t.Exe @a -c $code 2>$null | Select-Object -Last 1
                if ($out) {
                    $path = ConvertFrom-Json $out
                    if ($path -and (Test-Path -LiteralPath $path)) { return $path }
                }
            } catch { }   # not a usable Python; try the next one
        }
        return $null
    }

    function Test-EskaGate {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            return ($r.Content -match 'EskaGate')
        } catch { return $false }
    }

    function New-Shortcut($path, $target) {
        $shell = New-Object -ComObject WScript.Shell
        $lnk = $shell.CreateShortcut($path)
        $lnk.TargetPath = $target
        $lnk.WorkingDirectory = $InstallDir
        $lnk.IconLocation = (Join-Path $InstallDir 'assets\api_dashboard_icon.ico') + ',0'
        $lnk.Description = 'EskaGate: API key test console and local AI gateway'
        $lnk.Save()
    }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('eskagate-' + [Guid]::NewGuid().ToString('N'))
    try {
        if ($env:OS -ne 'Windows_NT') { throw 'This installer is for Windows. On Linux use get.sh.' }

        # --- Python -----------------------------------------------------------
        $python = Find-Python
        if (-not $python) {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                Say 'Python 3.8 or newer was not found. Installing Python 3.12 with winget (Windows may ask for permission)...'
                winget install --id Python.Python.3.12 --exact --source winget --accept-package-agreements --accept-source-agreements
                # Pick up the PATH the installer just changed, without opening a new window.
                $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                            [Environment]::GetEnvironmentVariable('Path', 'User')
                $python = Find-Python
                if (-not $python) {
                    throw 'Python was installed but cannot be found yet. Close this window, open a new PowerShell window and run the install command again.'
                }
            } else {
                Write-Host ''
                Write-Host 'Python 3.8 or newer is needed, and winget is not available to install it.' -ForegroundColor Yellow
                Write-Host '  1. Download Python from https://www.python.org/downloads/windows/'
                Write-Host '  2. In the installer, tick "Add python.exe to PATH", then click Install Now.'
                Write-Host '  3. Open a new PowerShell window and run the install command again.'
                return
            }
        }
        Say "Using Python: $python"

        # --- Download and extract (before touching the current install) --------
        Say 'Downloading EskaGate...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        New-Item -ItemType Directory -Path $tmp | Out-Null
        $zip = Join-Path $tmp 'eskagate.zip'
        Invoke-WebRequest -Uri $ZipUrl -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp 'x') -Force
        $src = Get-ChildItem -Path (Join-Path $tmp 'x') -Directory | Select-Object -First 1
        if (-not $src -or -not (Test-Path (Join-Path $src.FullName 'api_web_dashboard_v2.py'))) {
            throw 'The downloaded ZIP does not look like EskaGate.'
        }

        # --- Stop a copy running from the install folder, so the update is loaded
        $updating = Test-Path -LiteralPath $Script
        if ($updating) {
            Say "Updating $InstallDir"
            Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
                Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($Script, [StringComparison]::OrdinalIgnoreCase) -ge 0 } |
                ForEach-Object {
                    Say 'Stopping the running copy to load the update'
                    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($_.ParentProcessId)" -ErrorAction SilentlyContinue
                    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                    # Also close its EskaGate.cmd window (never some other terminal).
                    if ($parent -and $parent.Name -eq 'cmd.exe' -and $parent.CommandLine -like '*EskaGate.cmd*') {
                        Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue
                    }
                }
            Start-Sleep -Milliseconds 500
        } else {
            Say "Installing to $InstallDir"
        }

        # Mirror the new files into place (also removes files deleted upstream).
        robocopy $src.FullName $InstallDir /MIR /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "Copying the files to $InstallDir failed (robocopy code $LASTEXITCODE)." }

        # --- Launcher and shortcuts --------------------------------------------
        # Like "Run API Dashboard.bat", but with the exact Python found above, so it works
        # even when python is not on PATH. Closing its window stops the app.
        $launcher = Join-Path $InstallDir 'EskaGate.cmd'
        $cmd = "@echo off`r`nchcp 65001 >nul`r`ntitle EskaGate`r`ncd /d `"%~dp0`"`r`n" +
               "`"$python`" `"%~dp0api_web_dashboard_v2.py`" --port $Port --open`r`npause`r`n"
        [IO.File]::WriteAllText($launcher, $cmd, (New-Object Text.UTF8Encoding $false))

        $startMenu = [Environment]::GetFolderPath('Programs')
        $desktop = [Environment]::GetFolderPath('Desktop')
        New-Shortcut (Join-Path $startMenu 'EskaGate.lnk') $launcher
        if ($desktop) { New-Shortcut (Join-Path $desktop 'EskaGate.lnk') $launcher }
        Say 'Added EskaGate to the Start Menu and the Desktop'

        # --- Start --------------------------------------------------------------
        if (Test-EskaGate) {
            Say "EskaGate is already running at $Url. Opening it."
            Start-Process $Url
        } else {
            Say 'Starting EskaGate (keep its window open; closing it stops the app)'
            Start-Process -FilePath $launcher -WorkingDirectory $InstallDir
        }
        Write-Host ''
        Write-Host "Done. EskaGate runs at $Url" -ForegroundColor Green
        Write-Host '    Open it later from the Start Menu or the Desktop icon.'
        Write-Host '    Update it: run the same install command again.'
    } catch {
        Write-Host "Error: $($_.Exception.Message)" -ForegroundColor Red
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
