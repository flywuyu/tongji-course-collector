[CmdletBinding(SupportsShouldProcess=$true)]
param([Parameter(Mandatory=$true)][string]$PythonPath)
$ErrorActionPreference='Stop'
$projectDir=$PSScriptRoot
if (-not $WhatIfPreference -and (Get-TimeZone).BaseUtcOffset.TotalHours -ne 8) {
    throw 'Set Windows timezone to UTC+08:00 before installing the midnight schedule.'
}
if (-not (Test-Path -LiteralPath $PythonPath)) { throw 'Python runtime not found' }
$pythonw=Join-Path (Split-Path -Parent $PythonPath) 'pythonw.exe'
if (Test-Path -LiteralPath $pythonw) { $PythonPath=$pythonw }
$principal=New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$runSettings=New-ScheduledTaskSettingsSet -WakeToRun -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8 -Minutes 5)
$shortSettings=New-ScheduledTaskSettingsSet -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$stopSettings=New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$ownerCmdlet=$PSCmdlet
function Install-CollectorTask($suffix,$days,$at,$mode,$settings) {
    $name='TjCourseCollector-'+$suffix
    $action=New-ScheduledTaskAction -Execute $PythonPath -Argument ('-X utf8 "'+(Join-Path $projectDir 'scheduled_entry.py')+'" '+$mode) -WorkingDirectory $projectDir
    $trigger=New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $days -At $at
    $task=New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings
    if ($ownerCmdlet.ShouldProcess($name,'Register local course collector schedule')) {
        Register-ScheduledTask -TaskName $name -InputObject $task -Force | Out-Null
    }
}
Install-CollectorTask 'LoginCheck' 'Thursday' '21:00' '--preflight' $shortSettings
Install-CollectorTask 'Weekly' 'Friday' '00:00' '--run' $runSettings
Install-CollectorTask 'MorningStop' 'Friday' '08:00' '--stop' $stopSettings
Install-CollectorTask 'SaturdayCatchup' 'Saturday' '00:00' '--run' $runSettings
Install-CollectorTask 'SaturdayStop' 'Saturday' '08:00' '--stop' $stopSettings
$recovery=New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Friday,Saturday -At '00:05'
$repeat=New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 7 -Minutes 55)
$recovery.Repetition=$repeat.Repetition
$action=New-ScheduledTaskAction -Execute $PythonPath -Argument ('-X utf8 "'+(Join-Path $projectDir 'scheduled_entry.py')+'" --recover') -WorkingDirectory $projectDir
$task=New-ScheduledTask -Action $action -Trigger $recovery -Principal $principal -Settings $runSettings
if ($PSCmdlet.ShouldProcess('TjCourseCollector-WindowRecovery','Register bounded recovery checks')) {
    Register-ScheduledTask -TaskName 'TjCourseCollector-WindowRecovery' -InputObject $task -Force | Out-Null
}
Write-Output 'Schedule: Thu 21:00 preflight; Fri/Sat 00:00-08:00; no daytime catch-up.'
