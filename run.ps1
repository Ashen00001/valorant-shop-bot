# Load credentials from config.env (never hardcode them here)
$configPath = "$PSScriptRoot\config.env"
if (-not (Test-Path $configPath)) {
    Write-Error "config.env not found. Copy config.env.example to config.env and fill it in."
    exit 1
}
foreach ($line in (Get-Content $configPath)) {
    if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
    $key, $val = $line -split '=', 2
    Set-Item -Path "Env:$($key.Trim())" -Value $val.Trim()
}

# Give the PC time to fully wake and reconnect to the network before touching Valorant
Start-Sleep -Seconds 180

py -u "$PSScriptRoot\bot.py"
