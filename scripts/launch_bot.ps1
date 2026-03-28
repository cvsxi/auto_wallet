$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$systemPython = "C:\Users\mrhap\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$pythonExe = if (Test-Path $venvPython) { $venvPython } else { $systemPython }
$botScript = Join-Path $projectRoot "bot.py"
$stdoutLog = Join-Path $projectRoot "bot_stdout.log"
$stderrLog = Join-Path $projectRoot "bot_stderr.log"

$escapedBotPath = [regex]::Escape((Resolve-Path $botScript).Path)
$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @("python.exe", "pythonw.exe") -and
    $_.CommandLine -and
    (
        $_.CommandLine -match $escapedBotPath -or
        $_.CommandLine -match '(^|["''\s\\])bot\.py($|["''\s])'
    )
}

if ($existing) {
    exit 0
}

Start-Process `
    -FilePath $pythonExe `
    -ArgumentList @("-u", $botScript) `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WindowStyle Hidden
