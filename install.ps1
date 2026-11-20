# Installs cc-usage-stats for the current Windows user (no admin needed):
#   - data folder ~/.claude/usage-stats with config.json (from config.example.json, kept if present)
#   - Task Scheduler job "ClaudeUsage\Collect": incremental collect every N hours (default 6)
#   - Startup shortcut "Claude Usage": tray icon + dashboard server at logon
#   - first collect (reads all transcripts once; a few minutes for many GB), then starts the tray
# Run:  powershell -ExecutionPolicy Bypass -File install.ps1 [-Hours 6] [-NoCollect]
param([int]$Hours = 6, [switch]$NoCollect)
$ErrorActionPreference = 'Stop'

$code = $PSScriptRoot
$data = if ($env:CCSTATS_DIR) { $env:CCSTATS_DIR } else { Join-Path $env:USERPROFILE '.claude\usage-stats' }
New-Item -ItemType Directory -Force $data | Out-Null
$cfg = Join-Path $data 'config.json'
if (-not (Test-Path $cfg)) {
    Copy-Item (Join-Path $code 'config.example.json') $cfg
    Write-Host "Создан $cfg — впиши туда день продления подписки (billing_day) и правила проектов."
}

$pyw = Get-Command pyw.exe -ErrorAction SilentlyContinue
if ($pyw) { $exe = $pyw.Source; $pre = '-3 ' } else {
    $pyw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $pyw) { throw 'Не найден Python (pyw.exe или pythonw.exe). Поставь Python 3.10+ с python.org.' }
    $exe = $pyw.Source; $pre = ''
}
$script = Join-Path $code 'ccstats.py'

# scheduled collect
$tr = "`"$exe`" $pre`"$script`" collect -q"
schtasks /create /f /sc hourly /mo $Hours /tn 'ClaudeUsage\Collect' /tr $tr | Out-Null
Write-Host "Сбор по расписанию: каждые $Hours ч (Планировщик задач → ClaudeUsage\Collect)."

# tray + server at logon
$startup = [Environment]::GetFolderPath('Startup')
Remove-Item (Join-Path $startup 'ClaudeUsageServer.cmd') -ErrorAction SilentlyContinue   # older setup
$sh = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut((Join-Path $startup 'Claude Usage.lnk'))
$lnk.TargetPath = "$env:WINDIR\System32\wscript.exe"
$lnk.Arguments = "`"$(Join-Path $code 'tray.vbs')`""
$lnk.WorkingDirectory = $code
$lnk.Description = 'cc-usage-stats: tray icon and dashboard'
$lnk.Save()
Write-Host 'Иконка в трее и дашборд запускаются при входе в Windows.'

if (-not $NoCollect) {
    Write-Host 'Первый сбор (может занять несколько минут)…'
    Start-Process $exe -ArgumentList "$pre`"$script`" collect -q" -Wait -WindowStyle Hidden
}
Start-Process "$env:WINDIR\System32\wscript.exe" "`"$(Join-Path $code 'tray.vbs')`""
Write-Host ''
Write-Host 'Готово. Статус-строка в Claude Code — мод из этого же репозитория:'
Write-Host "  /plugin marketplace add $code"
Write-Host '  /plugin install cc-usage-stats@cc-usage-stats'
