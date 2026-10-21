param(
    [Parameter(Mandatory=$true)][ValidateRange(1,2147483647)][int]$ExpectedProcessId,
    [Parameter(Mandatory=$true)][string]$ResultPath,
    [ValidateRange(0,2147483647)][int]$PreviousHelperProcessId=0,
    [ValidateRange(1024,65535)][int]$Port=8767
)
$ErrorActionPreference='Stop'
$projectDirectory=[IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
$resultFile=[IO.Path]::GetFullPath($ResultPath)
$resultDirectory=Join-Path $projectDirectory 'data\panel'
if ((Split-Path -Parent $resultFile) -ne $resultDirectory) {
    throw 'Restart result must be inside this project data/panel directory.'
}
function Save-RestartResult($status,$message) {
    [ordered]@{status=$status;message=$message;process_id=$ExpectedProcessId;time=[DateTime]::UtcNow.ToString('o')} |
        ConvertTo-Json -Compress | Set-Content -LiteralPath $resultFile -Encoding UTF8
}
try {
    $principal=[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Windows administrator consent is required to stop the old panel.'
    }
    Save-RestartResult 'checking' 'Administrator consent received; verifying the old panel.'
    if ($PreviousHelperProcessId -gt 0) {
        $previousHelper=Get-CimInstance Win32_Process -Filter "ProcessId = $PreviousHelperProcessId"
        $helperCommand=([string]$previousHelper.CommandLine).Replace('/','\')
        if ($previousHelper.Name -eq 'powershell.exe' -and
            $helperCommand.IndexOf($PSCommandPath,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $helperCommand -match "-ExpectedProcessId\s+$ExpectedProcessId(?:\s|$)") {
            Stop-Process -Id $PreviousHelperProcessId -Force -ErrorAction Stop
        } else {
            throw 'Previous helper identity changed; no process was stopped.'
        }
    }
    $listener=@(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
    if ($listener.Count -ne 1 -or $listener[0].LocalAddress -ne '127.0.0.1' -or $listener[0].OwningProcess -ne $ExpectedProcessId) {
        throw 'Panel listener changed; no process was stopped.'
    }
    $oldProcess=Get-CimInstance Win32_Process -Filter "ProcessId = $ExpectedProcessId"
    $expectedScript=Join-Path $projectDirectory 'panel.py'
    $command=([string]$oldProcess.CommandLine).Replace('/','\')
    if ($oldProcess.Name -notin @('python.exe','pythonw.exe') -or
        $command.IndexOf($expectedScript,[StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'Listener is not the expected Python panel command; no process was stopped.'
    }
    $created=$oldProcess.CreationDate
    $baseUrl="http://127.0.0.1:$Port"
    $bootstrap=Invoke-RestMethod -Uri "$baseUrl/api/bootstrap" -TimeoutSec 10
    if ([IO.Path]::GetFullPath([string]$bootstrap.project_root).TrimEnd('\') -ne $projectDirectory) {
        throw 'Panel serves a different project; no process was stopped.'
    }
    if ($bootstrap.collector_revision -ge 2) {
        throw 'Panel is already updated; no process was stopped.'
    }
    $state=Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 10
    if ($state.worker_active -ne $false -or $state.workers.active -ne 0 -or
        $state.job.state -notin @('paused','completed','blocked','failed','interrupted')) {
        throw 'Collector is not safely paused; no process was stopped.'
    }
    $latest=Get-CimInstance Win32_Process -Filter "ProcessId = $ExpectedProcessId"
    $listener=@(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
    if ($latest.CreationDate -ne $created -or $listener.Count -ne 1 -or $listener[0].OwningProcess -ne $ExpectedProcessId) {
        throw 'Panel process changed during verification; no process was stopped.'
    }
    Save-RestartResult 'stopping' 'Verified project and inactive collector; stopping the old panel.'
    Stop-Process -Id $ExpectedProcessId -Force -ErrorAction Stop
    for ($attempt=0;$attempt -lt 30;$attempt++) {
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 100
    }
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        throw 'Old panel was stopped but the port is still occupied; verify before restarting.'
    }
    Save-RestartResult 'stopped' 'Verified paused old panel stopped; start the replacement without elevation.'
} catch {
    Save-RestartResult 'error' $_.Exception.Message
    exit 1
}
