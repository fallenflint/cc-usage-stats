# Tray icon for cc-usage-stats: shows today's / month's spend in the tooltip, starts the dashboard
# server if it is not running, and stops it on exit. Launched hidden by tray.vbs at logon.
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

$code = $PSScriptRoot
$data = if ($env:CCSTATS_DIR) { $env:CCSTATS_DIR } else { Join-Path $env:USERPROFILE '.claude\usage-stats' }
$port = 8777
try { $cfg = Get-Content (Join-Path $data 'config.json') -Raw | ConvertFrom-Json; if ($cfg.port) { $port = [int]$cfg.port } } catch {}
$url = "http://127.0.0.1:$port/"

# one tray per user
$mutex = New-Object System.Threading.Mutex($false, 'Local\cc-usage-stats-tray')
if (-not $mutex.WaitOne(0)) { exit }

function Find-Pyw {
    foreach ($c in @('pyw.exe', 'pythonw.exe')) {
        $p = Get-Command $c -ErrorAction SilentlyContinue
        if ($p) { return @($p.Source, $(if ($c -eq 'pyw.exe') { '-3' } else { '' })) }
    }
    return $null
}
$py = Find-Pyw
$script = Join-Path $code 'ccstats.py'

function Test-Server {
    try { $null = Invoke-WebRequest "$($url)api/summary" -UseBasicParsing -TimeoutSec 2; return $true } catch { return $false }
}

$server = $null
if ($py -and -not (Test-Server)) {
    $server = Start-Process -FilePath $py[0] -ArgumentList (@($py[1], "`"$script`"", 'serve') | Where-Object { $_ }) -PassThru -WindowStyle Hidden
}

# icon: an orange disc with a white "$", drawn here so the repo needs no binary
$bmp = New-Object System.Drawing.Bitmap 32, 32
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = 'AntiAlias'; $g.TextRenderingHint = 'AntiAliasGridFit'
$g.FillEllipse((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(201, 100, 66))), 1, 1, 30, 30)
$font = New-Object System.Drawing.Font('Segoe UI', 17, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
$fmt = New-Object System.Drawing.StringFormat; $fmt.Alignment = 'Center'; $fmt.LineAlignment = 'Center'
$g.DrawString('$', $font, [System.Drawing.Brushes]::White, (New-Object System.Drawing.RectangleF(0, 1, 32, 32)), $fmt)
$icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())

$tray = New-Object System.Windows.Forms.NotifyIcon
$tray.Icon = $icon
$tray.Visible = $true

function Money($x) { if ($x -ge 100) { '$' + [math]::Round($x).ToString('N0', [Globalization.CultureInfo]::InvariantCulture) } else { '$' + $x.ToString('0.00', [Globalization.CultureInfo]::InvariantCulture) } }
function Update-Tip {
    try {
        $s = Get-Content (Join-Path $data 'summary.json') -Raw | ConvertFrom-Json
        $age = [int](([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $s.last_collect) / 3600)
        $t = "Claude: сегодня $(Money $s.today.cost) · месяц $(Money $s.period.cost)"
        if ($age -ge 1) { $t += " · $age ч назад" }
    } catch { $t = 'Claude usage: нет данных' }
    $tray.Text = $t.Substring(0, [math]::Min(63, $t.Length))
}

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$open = $menu.Items.Add('Открыть дашборд'); $open.Font = New-Object System.Drawing.Font($open.Font, [System.Drawing.FontStyle]::Bold)
$open.add_Click({ Start-Process $url })
$menu.Items.Add('Собрать сейчас').add_Click({
    $tray.Text = 'Claude usage: собираю…'
    if ($py) { Start-Process -FilePath $py[0] -ArgumentList (@($py[1], "`"$script`"", 'collect', '-q') | Where-Object { $_ }) -WindowStyle Hidden -Wait }
    Update-Tip
})
$menu.Items.Add('Папка с данными').add_Click({ Start-Process explorer.exe $data })
$null = $menu.Items.Add('-')
$menu.Items.Add('Выход').add_Click({
    $tray.Visible = $false
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
    [System.Windows.Forms.Application]::Exit()
})
$tray.ContextMenuStrip = $menu
$tray.add_MouseClick({ if ($_.Button -eq 'Left') { Start-Process $url } })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 60000
$timer.add_Tick({ Update-Tip })
$timer.Start()
Update-Tip

[System.Windows.Forms.Application]::Run()
$tray.Dispose()
