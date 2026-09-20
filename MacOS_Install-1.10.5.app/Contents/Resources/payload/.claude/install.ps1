param([switch]$Check, [switch]$Report, [switch]$Global, [switch]$BootstrapWinGet)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$helper = Join-Path $PSScriptRoot 'hooks/install_dependencies.py'

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
        [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
}

function Ensure-WinGet {
    if (Get-Command winget.exe -ErrorAction SilentlyContinue) { return }
    Write-Host 'Installing WinGet using Microsoft.WinGet.Client...'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Install-PackageProvider -Name NuGet -Force -Scope CurrentUser | Out-Null
    Install-Module -Name Microsoft.WinGet.Client -Repository PSGallery -Force -Scope CurrentUser
    Import-Module Microsoft.WinGet.Client
    Repair-WinGetPackageManager
    Refresh-Path
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw 'WinGet is unavailable. Windows 10 1809+ / Windows 11 is required.'
    }
}

function Install-PackageId([string]$Id) {
    Ensure-WinGet
    & winget.exe install --id $Id --exact --source winget --force --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "WinGet failed for $Id (exit $LASTEXITCODE)" }
    Refresh-Path
}

function Find-Python {
    $candidates = @()
    if ($env:CLAUDE_HOOK_PYTHON) {
        $candidates = @($env:CLAUDE_HOOK_PYTHON)
    } else {
        $saved = Join-Path $PSScriptRoot 'install-state/python-path.txt'
        if (Test-Path -LiteralPath $saved) { $candidates += (Get-Content -LiteralPath $saved -Encoding UTF8 -Raw).Trim() }
        foreach ($name in @('python3.exe', 'python.exe')) {
            $cmd = Get-Command $name -ErrorAction SilentlyContinue
            if ($cmd) { $candidates += $cmd.Source }
        }
        $candidates += @(Get-ChildItem "$env:LOCALAPPDATA/Programs/Python/Python*/python.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName)
        $candidates += @(Get-ChildItem "$env:ProgramFiles/Python*/python.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName)
        if (Get-Command py.exe -ErrorAction SilentlyContinue) {
            try {
                $fromLauncher = & py.exe -3 -c 'import sys; print(sys.executable)' 2>$null
                if ($LASTEXITCODE -eq 0) { $candidates += $fromLauncher }
            } catch { }
        }
    }
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        if ($candidate -like '*\Microsoft\WindowsApps\python*') { continue }
        try {
            if ($Report) {
                & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>$null
                if ($LASTEXITCODE -eq 0) { return $candidate }
                continue
            }
            $found = & $candidate -B $helper --probe-python 2>$null
            if ($LASTEXITCODE -eq 0) { return ($found | Select-Object -Last 1) }
        } catch { }
    }
    return $null
}

try {
    if ($Global -and ($Check -or $Report -or $BootstrapWinGet)) {
        throw '-Global cannot be combined with read-only checks or -BootstrapWinGet'
    }
    if ($BootstrapWinGet) {
        if ($Check -or $Report) { throw 'Read-only checks cannot be combined with -BootstrapWinGet' }
        Ensure-WinGet
        exit 0
    }
    $python = Find-Python
    if (-not $python) {
        if ($Report) {
            Write-Output '@claude-installer {"id":"python","state":"missing"}'
            foreach ($dependency in @('bash', 'node', 'audio', 'code', 'claude', 'codex')) {
                Write-Output ('@claude-installer {"id":"' + $dependency + '","state":"unknown"}')
            }
            exit 1
        }
        if ($Check) { throw 'Python 3.11+ is missing/incomplete. Run install.ps1 without -Check.' }
        if ($env:CLAUDE_HOOK_PYTHON) { throw 'Unset invalid CLAUDE_HOOK_PYTHON and rerun install.ps1.' }
        Install-PackageId 'Python.Python.3.13'
        $python = Find-Python
        if (-not $python) { throw 'Python installation could not be verified. Repair Python 3.13 and rerun.' }
    }
    $installerArgs = @()
    if ($Check) { $installerArgs = @('--check') }
    if ($Report) { $installerArgs = @('--report') }
    if ($Global) { $installerArgs = @('--global') }
    & $python -B $helper @installerArgs
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
