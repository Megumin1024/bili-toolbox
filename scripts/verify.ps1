[CmdletBinding()]
param(
    [switch]$Screenshots,
    [switch]$Build,
    [Alias('SmokeTest')]
    [switch]$ExeSmokeTest,
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$defaultPython = 'python.exe'
$pythonPath = if ([string]::IsNullOrWhiteSpace($Python)) {
    if ($env:BILI_TOOLBOX_PYTHON) { $env:BILI_TOOLBOX_PYTHON } else { $defaultPython }
} else {
    $Python
}
$pythonCommand = Get-Command $pythonPath -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $pythonCommand) {
    throw "Python was not found: $pythonPath. Use -Python or BILI_TOOLBOX_PYTHON to override it."
}
$pythonExecutable = if ($pythonCommand.Path) { $pythonCommand.Path } else { $pythonCommand.Source }

function Invoke-Checked {
    param(
        [string]$Label,
        [scriptblock]$Command
    )

    Write-Output "--- $Label ---"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed. Exit code: $LASTEXITCODE"
    }
}

Write-Output "Project: $projectRoot"
& "$PSScriptRoot\check_env.ps1" -Python $pythonExecutable
if ($LASTEXITCODE -ne 0) {
    throw 'Environment check failed.'
}

Invoke-Checked 'Python compile check' {
    & $pythonExecutable -m compileall -q main.py app core tools scripts tests
}

Invoke-Checked 'Unit tests' {
    & $pythonExecutable -m unittest discover -s tests -v
}

Invoke-Checked 'Module boundary check' {
    & $pythonExecutable scripts\check_boundaries.py
}

Invoke-Checked 'Git whitespace check' {
    git diff --check
}

if ($Screenshots) {
    Invoke-Checked 'UI screenshot check' {
        & $pythonExecutable scripts\take_screenshots.py
    }
}

if ($Build) {
    $targetRoot = Join-Path $projectRoot 'dist'
    $running = Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and
        $_.ExecutablePath.StartsWith($targetRoot, [System.StringComparison]::OrdinalIgnoreCase)
    }
    if ($running) {
        throw "A packaged app is still running: $($running.ExecutablePath -join ', '). Close it before building."
    }

    $pythonDir = Split-Path -Parent $pythonExecutable
    $scriptsDir = Join-Path $pythonDir 'Scripts'
    $env:PATH = "$pythonDir;$scriptsDir;C:\Windows\System32;C:\Windows;C:\Windows\System32\Wbem;C:\Windows\System32\WindowsPowerShell\v1.0"

    Invoke-Checked 'PyInstaller build' {
        & $pythonExecutable -m PyInstaller --clean build_toolbox.spec --noconfirm
    }

    $icuFiles = Get-ChildItem -LiteralPath $targetRoot -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @('icudt78.dll', 'icuuc.dll') }
    if ($icuFiles) {
        throw "Build contains unexpected ICU DLLs: $($icuFiles.FullName -join ', ')"
    }
    Write-Output 'Build artifact check passed: no unexpected ICU DLLs found.'
}

if ($ExeSmokeTest) {
    $targetRoot = Join-Path $projectRoot 'dist'
    # 从 dist 中定位唯一 EXE，避免 PowerShell 5.1 对中文路径脚本编码的影响。
    $exeCandidates = @()
    foreach ($candidate in (Get-ChildItem -LiteralPath $targetRoot -Recurse -Force -ErrorAction Stop)) {
        if (-not $candidate.PSIsContainer -and $candidate.Extension -ieq '.exe') {
            $exeCandidates += $candidate
        }
    }
    if ($exeCandidates.Count -ne 1) {
        throw "EXE smoke check expected exactly one packaged EXE under $targetRoot, found $($exeCandidates.Count)."
    }
    $exeItem = $exeCandidates[0]
    $exePath = $exeItem.FullName
    $exeDir = $exeItem.DirectoryName

    $internalRoot = Join-Path $exeDir '_internal'
    $requiredResources = @(
        (Join-Path $internalRoot 'PySide6\Qt6Core.dll'),
        (Join-Path $internalRoot 'PySide6\QtCore.pyd'),
        (Join-Path $internalRoot 'shiboken6\shiboken6.abi3.dll'),
        (Join-Path $internalRoot 'shiboken6\Shiboken.pyd')
    )
    $missingResources = @($requiredResources | Where-Object {
        -not (Test-Path -LiteralPath $_ -PathType Leaf)
    })
    if ($missingResources) {
        throw "EXE smoke check is missing PySide6/shiboken6 resources: $($missingResources -join ', ')"
    }

    $icuFiles = @(Get-ChildItem -LiteralPath (Join-Path $projectRoot 'dist') -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @('icudt78.dll', 'icuuc.dll') })
    if ($icuFiles) {
        throw "EXE smoke check found unexpected ICU DLLs: $($icuFiles.FullName -join ', ')"
    }

    $existing = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and
        $_.ExecutablePath.Equals($exePath, [System.StringComparison]::OrdinalIgnoreCase)
    })
    if ($existing) {
        throw "EXE smoke check cannot start because the packaged app is already running: $exePath"
    }

    $errorLogPath = Join-Path $exeDir 'error.log'
    $beforeErrorLog = if (Test-Path -LiteralPath $errorLogPath -PathType Leaf) {
        Get-Item -LiteralPath $errorLogPath
    } else {
        $null
    }
    $process = $null
    $failure = $null
    try {
        $process = Start-Process -FilePath $exePath -WorkingDirectory $exeDir -PassThru -WindowStyle Hidden
        Start-Sleep -Seconds 8
        $process.Refresh()
        if ($process.HasExited) {
            $failure = "进程在 8 秒检查前退出，退出码：$($process.ExitCode)"
        }

        $afterErrorLog = if (Test-Path -LiteralPath $errorLogPath -PathType Leaf) {
            Get-Item -LiteralPath $errorLogPath
        } else {
            $null
        }
        $newStartupError = $false
        if ($afterErrorLog) {
            $newStartupError = if ($beforeErrorLog) {
                $afterErrorLog.LastWriteTimeUtc -gt $beforeErrorLog.LastWriteTimeUtc
            } else {
                $afterErrorLog.LastWriteTimeUtc -ge [DateTime]::UtcNow.AddSeconds(-10)
            }
        }
        if ($newStartupError -and -not $failure) {
            $failure = "启动检查生成了新的错误日志：$errorLogPath"
        }
    } catch {
        $failure = $_.Exception.Message
    } finally {
        if ($process -and -not $process.HasExited) {
            try {
                Stop-Process -Id $process.Id -Force -ErrorAction Stop
                $process.WaitForExit()
            } catch {
                if (-not $failure) {
                    $failure = "无法清理启动检查进程：$($_.Exception.Message)"
                }
            }
        }
        $remaining = @(Get-CimInstance Win32_Process | Where-Object {
            $_.ExecutablePath -and
            $_.ExecutablePath.Equals($exePath, [System.StringComparison]::OrdinalIgnoreCase)
        })
        if ($remaining -and -not $failure) {
            $failure = "启动检查结束后仍有 B站工具箱进程：$exePath"
        }
    }

    if ($failure) {
        throw "EXE smoke check failed: $failure"
    }
    Write-Output 'EXE smoke check passed: process stayed alive for 8 seconds, no new startup error, PySide6/shiboken6 resources located, no unexpected ICU DLLs, and no packaged process remained.'
}

Write-Output 'All verification checks passed.'
