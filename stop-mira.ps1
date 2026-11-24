# =====================================================================
#  MIRA / KUMAR - корректная остановка панели (Windows).
#  Читает PID из .mira-app.pid и завершает процесс.
#  Запуск: двойной щелчок по STOP_MIRA.bat
# =====================================================================

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$root = $PSScriptRoot
Set-Location -LiteralPath $root
$pidFile = Join-Path $root '.mira-app.pid'

function Write-Info($m) { Write-Host "[..] $m" }
function Write-Ok($m)   { Write-Host "[ОК] $m" }
function Write-Err($m)  { Write-Host "[ОШИБКА] $m" }

Write-Host ''
Write-Host '====================================================='
Write-Host '  MIRA / KUMAR - остановка панели (Windows)'
Write-Host '====================================================='
Write-Host ''

if (-not (Test-Path $pidFile)) {
    Write-Info 'Файл .mira-app.pid не найден.'
    Write-Host '     Возможно, панель не запущена или была остановлена ранее.'
    Write-Host ''
    exit 0
}

$pidText = (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $pidText) {
    Write-Err 'Не удалось прочитать PID из .mira-app.pid.'
    Write-Host ''
    exit 1
}
$procId = 0
if (-not [int]::TryParse($pidText.Trim(), [ref]$procId)) {
    Write-Err 'Некорректный PID в .mira-app.pid.'
    Write-Host ''
    exit 1
}

Write-Info "Останавливаю процесс Mira (PID $procId)..."

$proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-Info "Процесс $procId уже не запущен."
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Write-Ok 'Панель остановлена.'
    Write-Host ''
    exit 0
}

try {
    Stop-Process -Id $procId -Force -ErrorAction Stop
} catch {
    Write-Err "Не удалось завершить процесс $procId."
    Write-Host 'Закройте окно MiraPanel вручную или перезагрузите компьютер.'
    Write-Host ''
    exit 1
}

Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
Write-Ok 'Панель остановлена.'
Write-Host ''
exit 0