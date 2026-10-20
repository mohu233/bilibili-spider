param([ValidateRange(1024,65535)][int]$Port = 8767)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$candidates = @()
if ($env:BILIBILI_PANEL_PYTHON) { $candidates += $env:BILIBILI_PANEL_PYTHON }
$candidates += (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
$candidates += (Join-Path $PSScriptRoot '.venv-panel\Scripts\python.exe')
$parentDirectory = Split-Path -Parent $PSScriptRoot
$candidates += (Join-Path $parentDirectory 'web\.avatar-benchmark-venv\Scripts\python.exe')
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCommand) { $candidates += $pythonCommand.Source }
$selectedPython = $null
$fallbackPython = $null
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    if (-not $fallbackPython) { $fallbackPython = $candidate }
    & $candidate -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(x) for x in ['onnxruntime','numpy','PIL']) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $selectedPython = $candidate; break }
}
if (-not $selectedPython) { $selectedPython = $fallbackPython }
if (-not $selectedPython) { throw 'Python was not found. Install Python 3.10+ or set BILIBILI_PANEL_PYTHON.' }
Write-Host "Python: $selectedPython"
Write-Host "Local panel: http://127.0.0.1:$Port"
& $selectedPython -u (Join-Path $PSScriptRoot 'panel.py') --port $Port
exit $LASTEXITCODE
