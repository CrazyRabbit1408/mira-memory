# =====================================================================
#  Внутренний запуск панели Mira для Windows.
#  Вызывается из START_MIRA.bat. Запускает uvicorn в фоне и записывает
#  PID процесса в .mira-app.pid, чтобы STOP_MIRA.bat мог его остановить.
# =====================================================================

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot          # папка проекта (родитель launcher)
$py   = Join-Path $root '.venv\Scripts\python.exe'
$log  = Join-Path $root 'launcher.log'
$err  = Join-Path $root 'launcher.err.log'
$pidFile = Join-Path $root '.mira-app.pid'

if (-not (Test-Path $py)) {
    Write-Host '[ОШИБКА] Не найден Python окружения: .venv\Scripts\python.exe'
    exit 1
}

# Запускаем uvicorn в фоне, скрыто, с перенаправлением журналов.
$p = Start-Process -FilePath $py `
    -ArgumentList '-m','uvicorn','src.app:app','--host','127.0.0.1','--port','8000' `
    -WorkingDirectory $root `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $log `
    -RedirectStandardError $err

Set-Content -LiteralPath $pidFile -Value $p.Id -Encoding ascii
Write-Host "[ОК] Процесс панели запущен, PID $($p.Id)"
exit 0