# =====================================================================
#  MIRA / KUMAR - запуск панели в Windows двойным щелчком.
#  При первом запуске автоматически подготавливает окружение.
#  Панель: http://127.0.0.1:8000   Остановка: STOP_MIRA.bat
# =====================================================================

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$root = $PSScriptRoot
Set-Location -LiteralPath $root

$hostAddr = '127.0.0.1'
$port     = 8000
$url      = "http://${hostAddr}:$port"
$vpy      = Join-Path $root '.venv\Scripts\python.exe'
$pidFile  = Join-Path $root '.mira-app.pid'
$logFile  = Join-Path $root 'launcher.log'

function Write-Info($m) { Write-Host "[..] $m" }
function Write-Ok($m)   { Write-Host "[ОК] $m" }
function Write-Warn($m) { Write-Host "[ВНИМАНИЕ] $m" }
function Write-Err($m)  { Write-Host "[ОШИБКА] $m" }

Write-Host ''
Write-Host '====================================================='
Write-Host '  MIRA / KUMAR - запуск панели (Windows)'
Write-Host '====================================================='
Write-Host ''

# --- 1. Проверка Python -------------------------------------------
$pyExe = $null
foreach ($cand in @('py', 'python')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $pyExe = $cand; break }
}
if (-not $pyExe) {
    Write-Err 'Python не найден.'
    Write-Host 'Установите Python 3.10-3.13 с https://www.python.org/downloads/'
    Write-Host 'и включите "Add python.exe to PATH", затем запустите SETUP_MIRA.bat.'
    Write-Host ''
    exit 1
}

# --- 2. Автоподготовка окружения при первом запуске ----------------
if (-not (Test-Path $vpy)) {
    Write-Info 'Окружение не найдено. Запускаю первоначальную подготовку...'
    Write-Host '     (Это может занять несколько минут)'
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'setup-mira.ps1')
    if (-not (Test-Path $vpy)) {
        Write-Err 'Подготовка не удалась. См. сообщения выше.'
        Write-Host ''
        exit 1
    }
} else {
    Write-Info 'Проверяю зависимости...'
    $check = & $vpy -c "import importlib.util; missing=[m for m in ('yaml','fastapi','uvicorn','httpx','chromadb','networkx') if importlib.util.find_spec(m) is None]; print(','.join(missing))" 2>&1
    $missing = ($check | Out-String).Trim()
    if (-not [string]::IsNullOrWhiteSpace($missing)) {
        Write-Info "Не хватает зависимостей: $missing. Устанавливаю..."
        & $vpy -m pip install --disable-pip-version-check -e "$root\." *>> $logFile
        if ($LASTEXITCODE -ne 0) {
            Write-Err "Не удалось установить зависимости. См. $logFile"
            Write-Host ''
            exit 1
        }
    }
}

# --- 3. Проверка, не запущена ли панель уже ------------------------
if (Test-Path $pidFile) {
    $oldPid = (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($oldPid -and (Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue)) {
        Write-Ok "Панель уже запущена: $url"
        Write-Host 'Повторно открываю браузер.'
        Start-Process $url
        exit 0
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

# --- 4. Проверка .env и ключа -------------------------------------
$envPath = Join-Path $root '.env'
if (-not (Test-Path $envPath)) {
    Write-Warn 'Файл .env не найден. Создаю из .env.example...'
    Copy-Item -LiteralPath (Join-Path $root '.env.example') -Destination $envPath
}
$keyFound = $false
foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8 -ErrorAction SilentlyContinue) {
    if ($line -match '^\s*ORCAROUTER_API_KEY\s*=\s*(.+)$') {
        if ($matches[1].Trim().Trim('"').Trim("'") -ne '') { $keyFound = $true }
    }
}
if (-not $keyFound) {
    Write-Warn 'ORCAROUTER_API_KEY не задан в .env.'
    Write-Host 'Панель запустится, но чат будет показывать ошибку, пока вы не'
    Write-Host 'укажете ключ OrcaRouter в файле .env (откройте его блокнотом).'
}

# --- 5. Запуск панели ---------------------------------------------
Write-Info "Запускаю панель: $url"
$null = New-Item -ItemType Directory -Force -Path (Join-Path $root 'data')
if (Test-Path $pidFile) { Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue }

# Запуск в отдельном процессе, чтобы панель жила после закрытия окна.
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'launcher\run-mira-win.ps1')
if ($LASTEXITCODE -ne 0) {
    Write-Err 'Не удалось запустить процесс панели.'
    Write-Host ''
    exit 1
}

# --- 6. Ожидание готовности панели --------------------------------
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "$url/api/status" -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}

if (-not $ready) {
    Write-Err 'Панель не ответила за 30 секунд.'
    Write-Host "Последние строки журнала ${logFile}:"
    if (Test-Path $logFile) { Get-Content -LiteralPath $logFile -Tail 30 }
    Write-Host ''
    exit 1
}

Write-Ok "Панель готова: $url"
Write-Host 'Открываю браузер...'
Start-Process $url
Write-Host ''
Write-Host "Панель работает в фоне. Журнал: $logFile"
Write-Host 'Для остановки запустите STOP_MIRA.bat.'
Write-Host ''
Start-Sleep -Seconds 5
exit 0