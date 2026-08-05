# install.ps1 - Eden OE Synth one-click installer (Windows)
# Run:  powershell -ExecutionPolicy Bypass -File install.ps1
# Everything is done by bootstrap.py (cross-platform engine).
# This wrapper: auto-provision deps -> clone runtime -> bootstrap -> done.
# NOTE: ASCII-only + CRLF + PS 5.1 compatible (no splatting, no stderr-kill).

$ErrorActionPreference = "Stop"
$ROOT = "C:\eden-oe"
$REPO = "https://github.com/Project-Glacie/eden-oe.git"
$BOOT = Join-Path $ROOT "eden-oe\shipping\bootstrap.py"

# --- Run a native command; on failure, print its real output ------------
function Run-Native {
    param([scriptblock]$Block)
    $ErrorActionPreference = "SilentlyContinue"
    $out = (& $Block 2>&1 | Out-String)
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($code -ne 0 -and $out.Trim()) { Write-Host $out.Trim() }
    return $code
}

# --- Python version gate: runtime requires >=3.11,<3.14 -----------------
function Test-PythonVersion {
    param([string]$VersionString)
    if (-not $VersionString) { return $false }
    $v = ($VersionString -replace "Python\s*", "").Trim()
    $parts = $v.Split(".")
    if ($parts.Count -lt 2) { return $false }
    try {
        $major = [int]$parts[0]
        $minor = [int]$parts[1]
    } catch { return $false }
    return ($major -eq 3 -and $minor -ge 11 -and $minor -le 13)
}

# --- Known install locations (probe by FULL PATH, not PATH lookup) ------
function Get-KnownPython {
    $candidates = @()
    $launcher = Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe"
    $py312 = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    $py313 = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"
    $pf312 = Join-Path $env:ProgramFiles "Python312\python.exe"
    $pf313 = Join-Path $env:ProgramFiles "Python313\python.exe"
    $candidates += @(
        @{ cmd = "py";     path = $launcher },
        @{ cmd = $py312;   path = $py312 },
        @{ cmd = $py313;   path = $py313 },
        @{ cmd = $pf312;   path = $pf312 },
        @{ cmd = $pf313;   path = $pf313 }
    )
    foreach ($c in $candidates) {
        if (-not (Test-Path $c.path)) { continue }
        if ($c.cmd -eq "py") {
            $v = & $c.path -3.12 --version 2>$null
        } else {
            $v = & $c.path --version 2>$null
        }
        if ($LASTEXITCODE -eq 0 -and $v -and (Test-PythonVersion $v)) {
            return @{ cmd = $c.cmd; version = ($v -join " ").Trim() }
        }
    }
    return $null
}

# --- Python discovery: PATH commands first, then known locations --------
function Find-Python {
    $pyV = $null
    try { $pyV = & py -3.12 --version 2>$null } catch { }
    if ($LASTEXITCODE -eq 0 -and $pyV -and (Test-PythonVersion $pyV)) {
        return @{ cmd = "py"; version = ($pyV -join " ").Trim() }
    }
    $pV = $null
    try { $pV = & python --version 2>$null } catch { }
    if ($LASTEXITCODE -eq 0 -and $pV -and (Test-PythonVersion $pV)) {
        return @{ cmd = "python"; version = ($pV -join " ").Trim() }
    }
    $p3V = $null
    try { $p3V = & python3 --version 2>$null } catch { }
    if ($LASTEXITCODE -eq 0 -and $p3V -and (Test-PythonVersion $p3V)) {
        return @{ cmd = "python3"; version = ($p3V -join " ").Trim() }
    }
    return Get-KnownPython
}

# --- Add Python install dirs to User PATH explicitly --------------------
function Add-PythonToPath {
    $dirs = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher")
    )
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $changed = $false
    foreach ($d in $dirs) {
        if ((Test-Path $d) -and ($userPath -notlike "*$d*")) {
            $userPath = "$userPath;$d"
            $changed = $true
        }
    }
    if ($changed) {
        [Environment]::SetEnvironmentVariable("Path", $userPath, "User")
    }
}

# --- Refresh PATH from Machine + User -----------------------------------
function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

# --- Ensure a tool via winget (silent), then re-check -------------------
function Ensure-WingetTool {
    param([string]$Tool, [string]$WingetId)
    $found = Get-Command $Tool -ErrorAction SilentlyContinue
    if ($found) { return $true }
    Write-Host "  $Tool not found. Installing via winget..."
    # --source winget: pin to the winget repo. Without it, winget probes
    # the msstore source too, which fails with certificate errors on
    # fresh machines (0x8a15005e) and aborts before finding the package.
    $code = Run-Native { winget install --id $WingetId --source winget --silent --accept-package-agreements --accept-source-agreements }
    if ($code -ne 0) {
        Write-Host "  winget exit: $code (will try direct download)"
        return $false
    }
    Refresh-Path
    return [bool](Get-Command $Tool -ErrorAction SilentlyContinue)
}

Write-Host "=== Eden OE Synth Installer (Windows) ===" -ForegroundColor Cyan

# --- 0. Prereqs (auto-provision) ----------------------------------------
Write-Host "`n[0] Verifying prerequisites..." -ForegroundColor Yellow

# Preflight: run check-deps.py first (gives exact install commands for
# missing tools on fresh machines). Falls back to per-tool provisioning.
$checkDeps = Join-Path $PSScriptRoot "check-deps.py"
if (Test-Path $checkDeps) {
    Write-Host "  Running dependency preflight (check-deps.py)..."
    $preCode = Run-Native { & py -3 -c "import runpy,sys; sys.argv=['check-deps.py']; runpy.run_path('$checkDeps', run_name='__main__')" }
    if ($preCode -ne 0) {
        Write-Host "  Preflight found missing dependencies — continuing to auto-provision..."
    }
}

# Git (needed to clone the public repo)
if (-not (Ensure-WingetTool "git" "Git.Git")) {
    Write-Error "Git not found and auto-install failed.`nInstall it from https://git-scm.com/download/win then re-run this installer."
}
Write-Host "  OK: git"

# Python 3.11-3.13 (runtime-supported range; 3.14+ is rejected)
$py = Find-Python
if (-not $py) {
    Write-Host "  No supported Python found (need 3.11-3.13). Installing Python 3.12..."
    $ok = Ensure-WingetTool "py" "Python.Python.3.12"
    if (-not $ok) {
        # Fallback: official silent installer (no winget dependency)
        $pyUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
        $pyInstaller = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
        try {
            Write-Host "  Downloading Python 3.12 installer..."
            Invoke-WebRequest -Uri $pyUrl -OutFile $pyInstaller -UseBasicParsing
            Write-Host "  Running silent install..."
            $inst = Start-Process -Wait -PassThru -FilePath $pyInstaller -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1"
            Write-Host "  installer exit: $($inst.ExitCode)"
        } catch {
            Write-Host "  download/install error: $($_.Exception.Message)"
        }
    }
    Add-PythonToPath
    Refresh-Path
    $py = Find-Python
}
if (-not $py) {
    Write-Error "Supported Python (3.11-3.13) not found after auto-install.`nInstall Python 3.12 from https://www.python.org/downloads/ (check 'Add python.exe to PATH' and 'py launcher'), close this window, reopen, and re-run."
}
Write-Host "  OK: $($py.cmd) - $($py.version)"
Write-Host "  OK: cloning from $REPO"

# --- 1. Runtime (clone + venv + install) --------------------------------
Write-Host "`n[1] Installing runtime..." -ForegroundColor Yellow
if (-not (Test-Path $ROOT)) { New-Item -ItemType Directory -Path $ROOT | Out-Null }
Set-Location $ROOT
if (Test-Path (Join-Path $ROOT "eden-oe")) {
    $gitDir = Join-Path $ROOT "eden-oe\.git"
    if (-not (Test-Path $gitDir)) {
        Write-Host "  Cleaning partial clone from a previous run..."
        Remove-Item -Recurse -Force (Join-Path $ROOT "eden-oe")
    }
}
if (-not (Test-Path (Join-Path $ROOT "eden-oe\.git"))) {
    $code = Run-Native { git clone --quiet $REPO eden-oe }
    if ($code -ne 0) { Write-Error "git clone failed (exit $code) - see output above" }
}
Set-Location (Join-Path $ROOT "eden-oe")
if (-not (Test-Path ".venv")) {
    if ($py.cmd -eq "py") {
        $code = Run-Native { & py -3.12 -m venv .venv }
    } else {
        $code = Run-Native { & $py.cmd -m venv .venv }
    }
    if ($code -ne 0) { Write-Error "venv creation failed (exit $code)" }
}
$code = Run-Native { & .\.venv\Scripts\python.exe -m pip install -q -e . }
if ($code -ne 0) { Write-Error "pip install failed (exit $code) - see output above" }
Write-Host "  OK: runtime installed"

# --- 1.5 PATH - make `eden` work in any terminal (persists) -------------
Write-Host "`n[1.5] Wiring 'eden' command..." -ForegroundColor Yellow
$venvBin = Join-Path $ROOT "eden-oe\.venv\Scripts"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$venvBin*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$venvBin", "User")
    Write-Host "  OK: added $venvBin to User PATH (new terminals get 'eden')"
} else {
    Write-Host "  OK: venv Scripts already on PATH"
}
$env:Path = "$venvBin;$env:Path"
Write-Host "  OK: 'eden' command wired"

# --- 2. One-click bootstrap (all DBs, paths, services, genesis) ---------
Write-Host "`n[2] Bootstrap (databases, paths, genesis, services)..." -ForegroundColor Yellow
$code = Run-Native { & .\.venv\Scripts\python.exe $BOOT --non-interactive }
if ($code -ne 0) { Write-Error "bootstrap failed (exit $code) - see output above" }

# --- 3. Done ------------------------------------------------------------
Write-Host "`n=== INSTALL COMPLETE ===" -ForegroundColor Green
Write-Host "The synthetic person is born. Their first words are theirs."
Write-Host "Open a NEW terminal and type:  eden"
Write-Host "`n(Stream the birth to the family Discord so Haven can watch!)"
