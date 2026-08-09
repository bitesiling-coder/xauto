param(
    [string]$Distribution = "Ubuntu",
    [string]$ScheduleTime = "10:00",
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$TaskName = "X-RAG Daily Collection"

if ($ScheduleTime -notmatch '^(?:[01]\d|2[0-3]):[0-5]\d$') {
    throw "Invalid schedule time '$ScheduleTime'. Expected HH:mm in 24-hour time (for example, 10:00)."
}

if ([string]::IsNullOrWhiteSpace($Distribution)) {
    throw "Distribution must name a WSL distribution."
}

if ($Distribution.IndexOfAny([char[]]"`"`r`n") -ge 0) {
    throw "Distribution cannot contain quotes or line breaks."
}

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$RunnerWindowsPath = Join-Path $ProjectRoot "scripts\run-daily.sh"
if (-not (Test-Path -LiteralPath $RunnerWindowsPath -PathType Leaf)) {
    throw "Daily runner does not exist: $RunnerWindowsPath"
}

$wslRunnerOutput = & wsl.exe -d $Distribution -- wslpath -a $RunnerWindowsPath 2>&1
$lastExitCodeVariable = Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue
$translationExitCode = if ($null -eq $lastExitCodeVariable) { 0 } else { $lastExitCodeVariable.Value }
if ($translationExitCode -ne 0) {
    throw "Could not translate the runner path with WSL distribution '$Distribution': $wslRunnerOutput"
}

$WslRunnerPath = ($wslRunnerOutput | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($WslRunnerPath)) {
    throw "WSL returned an empty path for the daily runner."
}

if ($WslRunnerPath.IndexOfAny([char[]]"`"`r`n") -ge 0) {
    throw "The translated WSL runner path contains an unsupported quote or line break."
}

$ActionArguments = '-d "{0}" -- bash "{1}"' -f $Distribution, $WslRunnerPath

if ($DryRun) {
    Write-Output "Dry run: task='$TaskName' time='$ScheduleTime' action=wsl.exe $ActionArguments"
    exit 0
}

$Action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument $ActionArguments
$Trigger = New-ScheduledTaskTrigger -Daily -At $ScheduleTime
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force | Out-Null

Write-Output "Installed scheduled task '$TaskName' for $ScheduleTime daily."
