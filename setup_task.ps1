# Run this once to register the daily shop check task in Windows Task Scheduler.
# It will wake your PC from sleep at the time you set and run the bot.

param(
    [string]$RunAt = "17:05",  # Time to run daily (24h format) — NA shop resets at 5 PM PDT
    [string]$PythonPath = "py",
    [string]$BotScript = "$PSScriptRoot\bot.py"
)

$taskName = "ValorantShopBot"
$action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$BotScript`"" `
    -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $RunAt

$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 24) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType S4U `
    -RunLevel Highest

# Pass env vars into the task
$envVars = @(
    "DISCORD_BOT_TOKEN=$env:DISCORD_BOT_TOKEN",
    "DISCORD_CHANNEL_ID=$env:DISCORD_CHANNEL_ID",
    "RIOT_REGION=na"
)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "Task '$taskName' registered. Runs daily at $RunAt and will wake your PC from sleep." -ForegroundColor Green
Write-Host "To change the time: Task Scheduler > Task Scheduler Library > $taskName > Properties > Triggers" -ForegroundColor Cyan
