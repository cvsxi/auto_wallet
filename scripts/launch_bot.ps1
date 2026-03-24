$ErrorActionPreference = "Stop"

$projectRoot = "C:\agent1"
$pythonExe = "C:\Users\mrhap\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$botScript = Join-Path $projectRoot "bot.py"
$stdoutLog = Join-Path $projectRoot "bot_stdout.log"
$stderrLog = Join-Path $projectRoot "bot_stderr.log"

$escapedBotPath = [regex]::Escape($botScript)
$existing = Get-CimInstance Win32_Process -Filter "name = 'python.exe'" | Where-Object {
    $_.CommandLine -match $escapedBotPath
}

if ($existing) {
    exit 0
}

Start-Process `
    -FilePath $pythonExe `
    -ArgumentList $botScript `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WindowStyle Hidden
