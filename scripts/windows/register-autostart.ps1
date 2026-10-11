<#
.SYNOPSIS
    Registers (or removes) a Task Scheduler task that runs start-uva.ps1 at logon.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\register-autostart.ps1
    powershell -ExecutionPolicy Bypass -File scripts\windows\register-autostart.ps1 -Unregister
#>
param(
    [string]$TaskName = "UVA Bot",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Tarefa '$TaskName' removida."
    exit 0
}

$script = Join-Path $PSScriptRoot "start-uva.ps1"
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Output "Tarefa '$TaskName' registrada: roda $script ao fazer logon de $user."
