param(
    [string]$Distribution = "Ubuntu"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Distribution -notmatch '^[A-Za-z0-9._-]+$') {
    Write-Error "Error: scheduled dashboard update failed"
    exit 2
}

try {
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    $WslRunnerWindowsPath = Join-Path $PSScriptRoot "run-daily.sh"
    $PublisherWindowsPath = Join-Path $PSScriptRoot "publish-dashboard.py"
    $LogPath = Join-Path $ProjectRoot "logs\scheduler.log"
    if (
        -not (Test-Path -LiteralPath $WslRunnerWindowsPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $PublisherWindowsPath -PathType Leaf)
    ) {
        throw "required scheduled runner is unavailable"
    }

    $WslRunnerOutput = @(& wsl.exe -d $Distribution -e wslpath -a $WslRunnerWindowsPath)
    if ($LASTEXITCODE -ne 0) {
        throw "WSL runner path translation failed"
    }
    $WslRunnerPath = ($WslRunnerOutput | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($WslRunnerPath) -or $WslRunnerPath -match '[\x00-\x1F\x7F"]') {
        throw "WSL runner path is invalid"
    }

    & wsl.exe -d $Distribution -e bash $WslRunnerPath --no-publish
    $WslExitCode = $LASTEXITCODE
    if ($WslExitCode -ne 0) {
        exit $WslExitCode
    }

    $WindowsPython = (Get-Command python.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $PublisherOutput = @(& $WindowsPython -I -S $PublisherWindowsPath 2>&1)
    $PublisherExitCode = $LASTEXITCODE
    if ($PublisherOutput.Count -gt 0) {
        $PublisherOutput | ForEach-Object { $_.ToString() } | Add-Content -LiteralPath $LogPath -Encoding utf8
    }
    exit $PublisherExitCode
} catch {
    Write-Error "Error: scheduled dashboard update failed"
    exit 2
}
