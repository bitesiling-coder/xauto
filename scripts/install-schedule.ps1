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

if ($Distribution -notmatch '^[A-Za-z0-9._-]+$') {
    throw "Distribution must contain only letters, digits, dots, underscores, or hyphens."
}

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$RunnerWindowsPath = Join-Path $ProjectRoot "scripts\run-daily.sh"
if (-not (Test-Path -LiteralPath $RunnerWindowsPath -PathType Leaf)) {
    throw "Daily runner does not exist: $RunnerWindowsPath"
}

$previousErrorActionPreference = $ErrorActionPreference
$previousConsoleOutputEncoding = [Console]::OutputEncoding
$stderrPath = [System.IO.Path]::GetTempFileName()
try {
    $ErrorActionPreference = "Continue"
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $wslRunnerOutput = @(& wsl.exe -d $Distribution -e wslpath -a $RunnerWindowsPath 2> $stderrPath)
    $lastExitCodeVariable = Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue
    $translationExitCode = if ($null -eq $lastExitCodeVariable) { 0 } else { $lastExitCodeVariable.Value }
    $wslStderr = if (Test-Path -LiteralPath $stderrPath) {
        [System.IO.File]::ReadAllText($stderrPath).Trim()
    } else {
        ""
    }
} finally {
    $ErrorActionPreference = $previousErrorActionPreference
    [Console]::OutputEncoding = $previousConsoleOutputEncoding
    try {
        [System.IO.File]::Delete($stderrPath)
    } catch {
        # The temporary diagnostic file contains no secrets and can be removed later by the OS.
    }
}

if ($translationExitCode -ne 0) {
    throw "Could not translate the runner path with WSL distribution '$Distribution' (exit code $translationExitCode)."
}

$WslRunnerPath = ($wslRunnerOutput | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($WslRunnerPath)) {
    throw "WSL returned an empty path for the daily runner."
}

if ($WslRunnerPath -match '[\x00-\x1F\x7F"]') {
    throw "The translated WSL runner path contains an unsupported quote or control character."
}

$ActionArguments = '-d {0} -e bash "{1}"' -f $Distribution, $WslRunnerPath

if ($DryRun) {
    Write-Output "Dry run: task='$TaskName' time='$ScheduleTime' action=wsl.exe $ActionArguments"
    return
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
