param(
    [string]$Distribution = "Ubuntu",
    [string]$ScheduleTime = "10:00",
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$TaskName = "X-RAG Daily Collection"

function Get-CanonicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $fullPath = [System.IO.Path]::GetFullPath($Path)
        $root = [System.IO.Path]::GetPathRoot($fullPath)
        if ([string]::Equals($fullPath, $root, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $root
        }
        return $fullPath.TrimEnd('\', '/')
    } catch {
        throw "Unsafe Git pointer metadata: a path could not be resolved."
    }
}

function Test-SamePath {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )

    return [string]::Equals(
        (Get-CanonicalPath $Left),
        (Get-CanonicalPath $Right),
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-NoReparsePath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$Directory,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $fullPath = Get-CanonicalPath $Path
    $root = [System.IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($root)) {
        throw "Unsafe Git pointer metadata: $Label is not rooted."
    }

    $current = $root
    $parts = $fullPath.Substring($root.Length).Split(
        [char[]]@('\', '/'),
        [System.StringSplitOptions]::RemoveEmptyEntries
    )
    foreach ($part in $parts) {
        $current = Join-Path $current $part
        if (-not (Test-Path -LiteralPath $current)) {
            throw "Unsafe Git pointer metadata: $Label does not exist."
        }
        $item = Get-Item -Force -LiteralPath $current
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Unsafe Git pointer metadata: $Label contains a reparse point."
        }
    }

    $target = Get-Item -Force -LiteralPath $fullPath
    if ($Directory -and -not $target.PSIsContainer) {
        throw "Unsafe Git pointer metadata: $Label must be a directory."
    }
    if (-not $Directory -and $target.PSIsContainer) {
        throw "Unsafe Git pointer metadata: $Label must be a regular file."
    }
}

function Read-StrictUtf8File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    Assert-NoReparsePath -Path $Path -Directory $false -Label $Label
    try {
        $decoder = New-Object System.Text.UTF8Encoding($false, $true)
        return $decoder.GetString([System.IO.File]::ReadAllBytes($Path))
    } catch {
        throw "Unsafe Git pointer metadata: $Label is not strict UTF-8."
    }
}

function Resolve-GitPointerTarget {
    param(
        [Parameter(Mandatory = $true)][string]$BaseDirectory,
        [Parameter(Mandatory = $true)][string]$Pointer,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($Pointer -ne $Pointer.Trim() -or $Pointer -match '[\x00-\x1F\x7F"]') {
        throw "Unsafe Git pointer metadata: $Label contains unsupported characters."
    }
    if ($Pointer -match '^[\\/]{2}' -or $Pointer -match '^/' -or $Pointer -match '^[A-Za-z]:[^\\/]') {
        throw "Unsafe Git pointer metadata: $Label uses an unsupported path form."
    }

    $candidate = if ($Pointer -match '^[A-Za-z]:[\\/]') {
        $Pointer
    } else {
        Join-Path $BaseDirectory $Pointer
    }
    return Get-CanonicalPath $candidate
}

function Read-GitMarkerTarget {
    param(
        [Parameter(Mandatory = $true)][string]$MarkerPath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $text = Read-StrictUtf8File -Path $MarkerPath -Label $Label
    $match = [System.Text.RegularExpressions.Regex]::Match(
        $text,
        '\Agitdir: (?<pointer>[^\r\n\x00]+)(?:\r?\n)?\z'
    )
    if (-not $match.Success) {
        throw "Unsafe Git pointer metadata: $Label must contain exactly one gitdir line."
    }
    return Resolve-GitPointerTarget `
        -BaseDirectory ([System.IO.Path]::GetDirectoryName($MarkerPath)) `
        -Pointer $match.Groups['pointer'].Value `
        -Label $Label
}

function Read-GitBackpointerTarget {
    param(
        [Parameter(Mandatory = $true)][string]$BackpointerPath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $text = Read-StrictUtf8File -Path $BackpointerPath -Label $Label
    $match = [System.Text.RegularExpressions.Regex]::Match(
        $text,
        '\A(?<pointer>[^\r\n\x00]+)(?:\r?\n)?\z'
    )
    if (-not $match.Success) {
        throw "Unsafe Git pointer metadata: $Label must contain exactly one path line."
    }
    return Resolve-GitPointerTarget `
        -BaseDirectory ([System.IO.Path]::GetDirectoryName($BackpointerPath)) `
        -Pointer $match.Groups['pointer'].Value `
        -Label $Label
}

function Get-PortableRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$BaseDirectory,
        [Parameter(Mandatory = $true)][string]$TargetPath
    )

    $base = (Get-CanonicalPath $BaseDirectory) + [System.IO.Path]::DirectorySeparatorChar
    $target = Get-CanonicalPath $TargetPath
    if (-not [string]::Equals(
        [System.IO.Path]::GetPathRoot($base),
        [System.IO.Path]::GetPathRoot($target),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Unsafe Git pointer metadata: linked worktree pointers cross volumes."
    }

    $baseUri = New-Object System.Uri($base)
    $targetUri = New-Object System.Uri($target)
    $relative = [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString()).Replace('\', '/')
    if ([string]::IsNullOrWhiteSpace($relative) -or $relative -match '^[A-Za-z]+:' -or $relative -match '[\x00-\x1F\x7F\\]') {
        throw "Unsafe Git pointer metadata: a portable relative path could not be created."
    }
    return $relative
}

function Test-ByteArraysEqual {
    param([byte[]]$Left, [byte[]]$Right)

    if ($Left.Length -ne $Right.Length) {
        return $false
    }
    for ($index = 0; $index -lt $Left.Length; $index++) {
        if ($Left[$index] -ne $Right[$index]) {
            return $false
        }
    }
    return $true
}

function New-PointerUpdate {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $encoder = New-Object System.Text.UTF8Encoding($false)
    $original = [System.IO.File]::ReadAllBytes($Path)
    $desired = $encoder.GetBytes($Content)
    return [pscustomobject]@{
        Path = Get-CanonicalPath $Path
        Label = $Label
        Original = $original
        Desired = $desired
        NeedsUpdate = -not (Test-ByteArraysEqual -Left $original -Right $desired)
    }
}

function Get-ValidatedWorktreeEntry {
    param(
        [Parameter(Mandatory = $true)][string]$MarkerPath,
        [Parameter(Mandatory = $true)][string]$Label,
        [string]$ExpectedCommonDirectory
    )

    $admin = Read-GitMarkerTarget -MarkerPath $MarkerPath -Label "$Label marker"
    Assert-NoReparsePath -Path $admin -Directory $true -Label "$Label administrative directory"
    $worktrees = [System.IO.Path]::GetDirectoryName($admin)
    $common = [System.IO.Path]::GetDirectoryName($worktrees)
    if (
        -not [string]::Equals(
            [System.IO.Path]::GetFileName($worktrees),
            'worktrees',
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or
        -not [string]::Equals(
            [System.IO.Path]::GetFileName($common),
            '.git',
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Unsafe Git pointer metadata: $Label is outside a common .git/worktrees directory."
    }
    Assert-NoReparsePath -Path $worktrees -Directory $true -Label "$Label worktrees directory"
    Assert-NoReparsePath -Path $common -Directory $true -Label "$Label common Git directory"
    if ($ExpectedCommonDirectory -and -not (Test-SamePath $common $ExpectedCommonDirectory)) {
        throw "Unsafe Git pointer metadata: $Label belongs to a different Git repository."
    }

    $backpointer = Join-Path $admin 'gitdir'
    $backTarget = Read-GitBackpointerTarget -BackpointerPath $backpointer -Label "$Label backpointer"
    if (-not (Test-SamePath $backTarget $MarkerPath)) {
        throw "Unsafe Git pointer metadata: $Label backpointer does not reference the exact marker."
    }

    return [pscustomobject]@{
        Marker = Get-CanonicalPath $MarkerPath
        Admin = Get-CanonicalPath $admin
        Backpointer = Get-CanonicalPath $backpointer
        Common = Get-CanonicalPath $common
    }
}

function Get-LinkedWorktreePointerPlan {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $updates = @()
    $projectMarker = Join-Path $ProjectRoot '.git'
    if (Test-Path -LiteralPath $projectMarker -PathType Container) {
        Assert-NoReparsePath -Path $projectMarker -Directory $true -Label 'project Git directory'
        $common = Get-CanonicalPath $projectMarker
    } elseif (Test-Path -LiteralPath $projectMarker -PathType Leaf) {
        $projectEntry = Get-ValidatedWorktreeEntry -MarkerPath $projectMarker -Label 'project worktree'
        $common = $projectEntry.Common
        $markerRelative = Get-PortableRelativePath `
            -BaseDirectory ([System.IO.Path]::GetDirectoryName($projectEntry.Marker)) `
            -TargetPath $projectEntry.Admin
        $backRelative = Get-PortableRelativePath `
            -BaseDirectory $projectEntry.Admin `
            -TargetPath $projectEntry.Marker
        $updates += New-PointerUpdate `
            -Path $projectEntry.Marker `
            -Content "gitdir: $markerRelative`n" `
            -Label 'project worktree marker'
        $updates += New-PointerUpdate `
            -Path $projectEntry.Backpointer `
            -Content "$backRelative`n" `
            -Label 'project worktree backpointer'
    } else {
        throw "Unsafe Git pointer metadata: the project has no regular .git directory or marker."
    }

    $pagesRoot = Join-Path $ProjectRoot '.worktrees\x-rag-pages'
    $pagesMarker = Join-Path $pagesRoot '.git'
    if (Test-Path -LiteralPath $pagesRoot) {
        Assert-NoReparsePath -Path $pagesRoot -Directory $true -Label 'dedicated pages worktree root'
        if (-not (Test-Path -LiteralPath $pagesMarker -PathType Leaf)) {
            throw "Unsafe Git pointer metadata: the existing pages .git marker is missing or not a regular file."
        }
        $pagesEntry = Get-ValidatedWorktreeEntry `
            -MarkerPath $pagesMarker `
            -Label 'pages worktree' `
            -ExpectedCommonDirectory $common
        $markerRelative = Get-PortableRelativePath `
            -BaseDirectory ([System.IO.Path]::GetDirectoryName($pagesEntry.Marker)) `
            -TargetPath $pagesEntry.Admin
        $backRelative = Get-PortableRelativePath `
            -BaseDirectory $pagesEntry.Admin `
            -TargetPath $pagesEntry.Marker
        $updates += New-PointerUpdate `
            -Path $pagesEntry.Marker `
            -Content "gitdir: $markerRelative`n" `
            -Label 'pages worktree marker'
        $updates += New-PointerUpdate `
            -Path $pagesEntry.Backpointer `
            -Content "$backRelative`n" `
            -Label 'pages worktree backpointer'
    }

    return $updates
}

function Remove-OwnedPointerTemporaryFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $expectedPrefix = (Get-CanonicalPath $Destination) + '.xrag-scheduler-'
    $candidate = Get-CanonicalPath $Path
    if (-not $candidate.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove an unowned Git pointer temporary file."
    }
    Assert-NoReparsePath -Path $candidate -Directory $false -Label 'scheduler-owned pointer temporary file'
    [System.IO.File]::SetAttributes($candidate, [System.IO.FileAttributes]::Normal)
    [System.IO.File]::Delete($candidate)
}

function Invoke-LinkedWorktreePointerPlan {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Plan)

    $pending = @($Plan | Where-Object { $_.NeedsUpdate })
    if ($pending.Count -eq 0) {
        return
    }

    $operations = @()
    try {
        foreach ($update in $pending) {
            Assert-NoReparsePath -Path $update.Path -Directory $false -Label $update.Label
            $currentBytes = [System.IO.File]::ReadAllBytes($update.Path)
            if (-not (Test-ByteArraysEqual -Left $currentBytes -Right $update.Original)) {
                throw "Git pointer metadata changed after validation; refusing to overwrite it."
            }
            $token = "$PID-$([System.Guid]::NewGuid().ToString('N'))"
            $temporary = $update.Path + ".xrag-scheduler-$token.tmp"
            $backup = $update.Path + ".xrag-scheduler-$token.bak"
            $stream = $null
            try {
                $stream = New-Object System.IO.FileStream(
                    $temporary,
                    [System.IO.FileMode]::CreateNew,
                    [System.IO.FileAccess]::Write,
                    [System.IO.FileShare]::None
                )
                $stream.Write($update.Desired, 0, $update.Desired.Length)
                $stream.Flush($true)
            } finally {
                if ($null -ne $stream) {
                    $stream.Dispose()
                }
            }
            $operations += [pscustomobject]@{
                Update = $update
                Temporary = $temporary
                Backup = $backup
                Replaced = $false
            }
        }

        foreach ($operation in $operations) {
            Assert-NoReparsePath -Path $operation.Update.Path -Directory $false -Label $operation.Update.Label
            $currentBytes = [System.IO.File]::ReadAllBytes($operation.Update.Path)
            if (-not (Test-ByteArraysEqual -Left $currentBytes -Right $operation.Update.Original)) {
                throw "Git pointer metadata changed during preparation; refusing to overwrite it."
            }
            [System.IO.File]::Replace(
                $operation.Temporary,
                $operation.Update.Path,
                $operation.Backup,
                $true
            )
            $operation.Replaced = $true
        }
        foreach ($operation in $operations) {
            $writtenBytes = [System.IO.File]::ReadAllBytes($operation.Update.Path)
            if (-not (Test-ByteArraysEqual -Left $writtenBytes -Right $operation.Update.Desired)) {
                throw "Git pointer metadata did not match the prepared bytes after atomic replacement."
            }
        }
    } catch {
        $originalFailure = $_
        for ($index = $operations.Count - 1; $index -ge 0; $index--) {
            $operation = $operations[$index]
            if ($operation.Replaced -and (Test-Path -LiteralPath $operation.Backup -PathType Leaf)) {
                $discard = $operation.Update.Path + ".xrag-scheduler-$PID-$([System.Guid]::NewGuid().ToString('N')).discard"
                try {
                    [System.IO.File]::Replace(
                        $operation.Backup,
                        $operation.Update.Path,
                        $discard,
                        $true
                    )
                    Remove-OwnedPointerTemporaryFile -Path $discard -Destination $operation.Update.Path
                } catch {
                    throw "Git pointer preparation failed and its atomic rollback could not be completed."
                }
            }
        }
        foreach ($operation in $operations) {
            Remove-OwnedPointerTemporaryFile -Path $operation.Temporary -Destination $operation.Update.Path
            Remove-OwnedPointerTemporaryFile -Path $operation.Backup -Destination $operation.Update.Path
        }
        throw $originalFailure
    }

    foreach ($operation in $operations) {
        Remove-OwnedPointerTemporaryFile -Path $operation.Temporary -Destination $operation.Update.Path
        Remove-OwnedPointerTemporaryFile -Path $operation.Backup -Destination $operation.Update.Path
    }
}

function Restore-LinkedWorktreePointerPlan {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Plan)

    $reversePlan = @()
    foreach ($update in @($Plan | Where-Object { $_.NeedsUpdate })) {
        Assert-NoReparsePath -Path $update.Path -Directory $false -Label $update.Label
        $currentBytes = [System.IO.File]::ReadAllBytes($update.Path)
        if (-not (Test-ByteArraysEqual -Left $currentBytes -Right $update.Desired)) {
            throw "Git pointer metadata changed before scheduler rollback."
        }
        $reversePlan += [pscustomobject]@{
            Path = $update.Path
            Label = $update.Label
            Original = $update.Desired
            Desired = $update.Original
            NeedsUpdate = $true
        }
    }

    Invoke-LinkedWorktreePointerPlan -Plan $reversePlan
    foreach ($update in @($Plan | Where-Object { $_.NeedsUpdate })) {
        $restoredBytes = [System.IO.File]::ReadAllBytes($update.Path)
        if (-not (Test-ByteArraysEqual -Left $restoredBytes -Right $update.Original)) {
            throw "Git pointer metadata did not match its original bytes after scheduler rollback."
        }
    }
}

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
$WindowsRunnerPath = Join-Path $ProjectRoot "scripts\run-daily.ps1"
if (-not (Test-Path -LiteralPath $WindowsRunnerPath -PathType Leaf)) {
    throw "Windows daily launcher does not exist: $WindowsRunnerPath"
}
if ($WindowsRunnerPath -match '[\x00-\x1F\x7F"]') {
    throw "The Windows daily launcher path contains an unsupported quote or control character."
}
$PowerShellExecutable = Join-Path $PSHOME "powershell.exe"
if (-not (Test-Path -LiteralPath $PowerShellExecutable -PathType Leaf)) {
    throw "Windows PowerShell is unavailable."
}

$PointerPlan = @(Get-LinkedWorktreePointerPlan -ProjectRoot $ProjectRoot)
$PendingPointerCount = @($PointerPlan | Where-Object { $_.NeedsUpdate }).Count

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

$ActionArguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Distribution "{1}"' -f $WindowsRunnerPath, $Distribution

if ($DryRun) {
    Write-Output "Dry run: would normalize $PendingPointerCount linked-worktree Git pointer file(s); no Git metadata was changed."
    Write-Output "Dry run: task='$TaskName' time='$ScheduleTime' action=$PowerShellExecutable $ActionArguments"
    return
}

$PointersApplied = $false
try {
    Invoke-LinkedWorktreePointerPlan -Plan $PointerPlan
    $PointersApplied = $true
    $RemainingPointerUpdates = @(
        Get-LinkedWorktreePointerPlan -ProjectRoot $ProjectRoot |
            Where-Object { $_.NeedsUpdate }
    )
    if ($RemainingPointerUpdates.Count -ne 0) {
        throw "Git pointer preparation did not produce stable portable metadata."
    }

    $Action = New-ScheduledTaskAction -Execute $PowerShellExecutable -Argument $ActionArguments
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
} catch {
    $schedulerFailure = $_
    if ($PointersApplied -and $PendingPointerCount -gt 0) {
        try {
            Restore-LinkedWorktreePointerPlan -Plan $PointerPlan
        } catch {
            throw "Scheduled task installation failed and Git pointer rollback could not be completed safely."
        }
    }
    throw $schedulerFailure
}

Write-Output "Prepared linked-worktree Git metadata for Windows and WSL ($PendingPointerCount file(s) normalized)."
Write-Output "Installed scheduled task '$TaskName' for $ScheduleTime daily."
