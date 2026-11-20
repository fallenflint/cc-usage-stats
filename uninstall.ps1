# Removes the scheduled collect, the Startup shortcut and stops the tray/server.
# Data (~/.claude/usage-stats: database, config) stays unless -Purge.
param([switch]$Purge)
$data = if ($env:CCSTATS_DIR) { $env:CCSTATS_DIR } else { Join-Path $env:USERPROFILE '.claude\usage-stats' }

schtasks /delete /f /tn 'ClaudeUsage\Collect' 2>$null | Out-Null
$startup = [Environment]::GetFolderPath('Startup')
Remove-Item (Join-Path $startup 'Claude Usage.lnk'), (Join-Path $startup 'ClaudeUsageServer.cmd') -ErrorAction SilentlyContinue

Get-CimInstance Win32_Process -Filter "Name='powershell.exe' or Name='pyw.exe' or Name='pythonw.exe' or Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'cc-usage-stats\\tray\.ps1|ccstats\.py"? serve' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if ($Purge) { Remove-Item -Recurse -Force $data; Write-Host "Удалены данные: $data" }
Write-Host 'Сбор, трей и сервер выключены. Мод в Claude Code: /plugin uninstall cc-usage-stats'
