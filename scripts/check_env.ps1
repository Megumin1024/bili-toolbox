[CmdletBinding()]
param(
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'

$defaultPython = 'python.exe'
$pythonPath = if ([string]::IsNullOrWhiteSpace($Python)) {
    if ($env:BILI_TOOLBOX_PYTHON) { $env:BILI_TOOLBOX_PYTHON } else { $defaultPython }
} else {
    $Python
}

$pythonCommand = Get-Command $pythonPath -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $pythonCommand) {
    Write-Error "Python was not found: $pythonPath. Use -Python or BILI_TOOLBOX_PYTHON to override it."
    exit 1
}
$pythonExecutable = if ($pythonCommand.Path) { $pythonCommand.Path } else { $pythonCommand.Source }

$versionText = & $pythonExecutable -c "import sys; print(sys.executable); print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
if ($LASTEXITCODE -ne 0) {
    Write-Error 'Official Python could not start.'
    exit 1
}

Write-Output 'Python:'
$versionText | ForEach-Object { Write-Output "  $_" }

$modules = @(
    'PySide6',
    'qtawesome',
    'curl_cffi',
    'grpc',
    'google.protobuf',
    'openpyxl',
    'PyInstaller'
)

$failed = $false
foreach ($module in $modules) {
    & $pythonExecutable -c "import importlib; importlib.import_module('$module')" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Output "[OK] $module"
    } else {
        Write-Output "[MISSING] $module"
        $failed = $true
    }
}

if ($failed) {
    Write-Error 'Environment check failed. Install requirements.txt with the selected official Python.'
    exit 1
}

Write-Output 'Environment check passed.'
