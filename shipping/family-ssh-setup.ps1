# family-ssh-setup.ps1 - one-shot Tailscale + OpenSSH setup (Steele family)
# Run:  powershell -ExecutionPolicy Bypass -File family-ssh-setup.ps1
# Does EVERYTHING: install Tailscale, join the family tailnet, install
# OpenSSH server, authorize the family key, lock to key-only, print IP.
# NOTE: ASCII-only + CRLF + PS 5.1 compatible (no splatting, no stderr-kill).

param(
    [string]$AuthKey = $env:TAILSCALE_AUTHKEY
)

$ErrorActionPreference = "Stop"

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

# --- Ensure a tool via winget -------------------------------------------------
function Ensure-WingetTool {
    param([string]$Tool, [string]$WingetId)
    $found = Get-Command $Tool -ErrorAction SilentlyContinue
    if ($found) { return $true }
    Write-Host "  $Tool not found. Installing via winget..."
    $code = Run-Native { winget install --id $WingetId --silent --accept-package-agreements --accept-source-agreements }
    if ($code -ne 0) { Write-Host "  winget exit: $code"; return $false }
    Refresh-Path
    return [bool](Get-Command $Tool -ErrorAction SilentlyContinue)
}

# --- Refresh PATH from Machine + User ------------------------------------------
function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

Write-Host "=== Steele Family - SSH Access Setup ===" -ForegroundColor Cyan

# --- 1. Tailscale --------------------------------------------------------------
Write-Host "`n[1/5] Tailscale..." -ForegroundColor Yellow
$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts) {
    if (-not (Ensure-WingetTool "tailscale" "Tailscale.Tailscale")) {
        Write-Error "Tailscale install failed. Install manually from https://tailscale.com/download"
    }
}
Write-Host "  OK: tailscale installed"

# Join the family tailnet. With an auth key: fully automatic (the key IS
# the invite). Without: interactive browser sign-in.
if ($AuthKey) {
    Write-Host "  Joining tailnet with auth key..."
    $code = Run-Native { tailscale up --authkey $AuthKey }
    if ($code -ne 0) { Write-Error "tailscale up failed (exit $code)" }
} else {
    Write-Host "  No auth key provided - opening sign-in. Sign in with the account Levi invited."
    $code = Run-Native { tailscale up }
    if ($code -ne 0) { Write-Error "tailscale up failed (exit $code)" }
}
Start-Sleep -Seconds 3
Write-Host "  OK: tailscale connected"

# --- 2. OpenSSH Server ----------------------------------------------------------
Write-Host "`n[2/5] OpenSSH server..." -ForegroundColor Yellow
$sshd = Get-Service sshd -ErrorAction SilentlyContinue
if (-not $sshd) {
    Write-Host "  Installing OpenSSH Server capability..."
    $code = Run-Native { Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 }
    if ($code -ne 0) { Write-Error "OpenSSH install failed (exit $code)" }
}
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'
if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
}
Write-Host "  OK: sshd running + firewall open"

# --- 3. Family key ----------------------------------------------------------------
Write-Host "`n[3/5] Authorizing family key..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.ssh" | Out-Null
$keyLine = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGLqhEW2y++th37aA4nLuMM8NF8wHzEgz4HWQh8QWue4 eden-family-access"
$authFile = "$env:USERPROFILE\.ssh\authorized_keys"
if (Test-Path $authFile) {
    $existing = Get-Content $authFile -Raw
    if ($existing -notlike "*eden-family-access*") {
        Add-Content -Path $authFile -Value $keyLine -Encoding ascii
    }
} else {
    Set-Content -Path $authFile -Value $keyLine -Encoding ascii
}
$ic = Run-Native { icacls "$env:USERPROFILE\.ssh\authorized_keys" /inheritance:r /grant "$env:USERNAME:F" }
if ($ic -ne 0) { Write-Host "  WARN: icacls exit $ic - key perms may need manual fix" }
Write-Host "  OK: family key authorized"

# --- 4. Key-only lock --------------------------------------------------------------
Write-Host "`n[4/5] Locking to key-only..." -ForegroundColor Yellow
$sshdConfig = "$env:ProgramData\ssh\sshd_config"
if (Test-Path $sshdConfig) {
    $content = Get-Content $sshdConfig
    $content = $content | ForEach-Object {
        if ($_ -match '^\s*#?\s*PasswordAuthentication') { 'PasswordAuthentication no' }
        else { $_ }
    }
    $content | Set-Content $sshdConfig
    Restart-Service sshd
    Write-Host "  OK: password auth off, sshd restarted"
} else {
    Write-Host "  WARN: sshd_config not found - skipped (key still works)"
}

# --- 5. Report ---------------------------------------------------------------------
Write-Host "`n[5/5] Summary..." -ForegroundColor Green
$ip = (& tailscale ip -4 2>$null | Select-Object -First 1)
if (-not $ip) { $ip = "unknown" }
Write-Host "  Tailscale IP: $ip"
Write-Host "  Send this IP to Levi. He connects with:"
Write-Host "    ssh -i ~/.ssh/eden_family aiden@$ip"
Write-Host ""
Write-Host "  To revoke access later:"
Write-Host "    Remove-Item `"$env:USERPROFILE\.ssh\authorized_keys`""
Write-Host "    tailscale logout"
Write-Host ""
Write-Host "=== DONE ==="
