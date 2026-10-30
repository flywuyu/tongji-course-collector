[CmdletBinding(SupportsShouldProcess=$true)]
param()
$ErrorActionPreference='Stop'
Get-ScheduledTask -TaskName 'TjCourseCollector-*' -ErrorAction SilentlyContinue | ForEach-Object {
    if ($PSCmdlet.ShouldProcess($_.TaskName,'Remove this project schedule')) {
        Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
    }
}
