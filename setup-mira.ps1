# =====================================================================
#  MIRA / KUMAR - первоначальная подготовка окружения (Windows).
#  Создаёт виртуальное окружение, устанавливает только отсутствующие
#  зависимости и создаёт .env из .env.example, если его ещё нет.
#  Запуск: двойной щелчок по SETUP_MIRA.bat
# =====================================================================

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$root = $PSScriptRoot
Set-Location -LiteralPath $root

function Write-Info($m) { Write-Host "[..] $m" }
function Write-Ok($m)   { Write-Host "[ОК] $m" }
function Write-Warn($m) { Write-Host "[ВНИМАНИЕ] $m" }
function Write-Err($m)  { Write-Host "[ОШИБКА] $m" }

Write-Host ''
Write-Host '====================================================='
Write-Host '  MIRA / KUMAR - подготовка окружения (Windows)'
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
    Write-Host 'и при установке включите "Add python.exe to PATH".'
    Write-Host ''
    exit 1
}
Write-Ok "Python найден: $pyExe"
$ver = & $pyExe --version 2>&1
Write-Host "    $ver"

# --- 2. Создание виртуального окружения ---------------------------
$vpy = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path $vpy) {
    Write-Ok 'Виртуальное окружение уже существует: .venv'
} else {
    Write-Info 'Создаю виртуальное окружение .venv ...'
    & $pyExe -m venv (Join-Path $root '.venv')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $vpy)) {
        Write-Err 'Не удалось создать виртуальное окружение.'
        Write-Host 'Проверьте установку Python и наличие модуля venv.'
        Write-Host ''
        exit 1
    }
    Write-Ok 'Виртуальное окружение создано.'
}

# --- 3. Проверка и установка зависимостей -------------------------
Write-Info 'Проверяю зависимости проекта...'
$check = & $vpy -c "import importlib.util; missing=[m for m in ('yaml','fastapi','uvicorn','httpx','chromadb','networkx') if importlib.util.find_spec(m) is None]; print(','.join(missing))" 2>&1
$missing = ($check | Out-String).Trim()

if ([string]::IsNullOrWhiteSpace($missing)) {
    Write-Ok 'Все зависимости уже установлены.'
} else {
    Write-Info "Не хватает зависимостей: $missing"
    Write-Info 'Устанавливаю проект и зависимости (может занять несколько минут)...'
    & $vpy -m pip install --disable-pip-version-check -e "$root\." 2>&1 |
        Tee-Object -FilePath (Join-Path $root 'launcher-setup.log')
    if ($LASTEXITCODE -ne 0) {
        Write-Err 'Не удалось установить зависимости.'
        Write-Host 'Проверьте подключение к интернету и повторите запуск SETUP_MIRA.bat.'
        Write-Host 'Подробности: launcher-setup.log'
        Write-Host ''
        exit 1
    }
    Write-Ok 'Зависимости установлены.'
}

# --- 4. Проверка импорта основных модулей -----------------------
Write-Info 'Проверяю импорт основных модулей...'
& $vpy -c "import yaml, fastapi, uvicorn, httpx, chromadb, networkx; import src.config; print('IMPORT_OK')" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Err 'Импорт основных модулей не удался.'
    Write-Host 'Подробности: launcher-setup.log'
    Write-Host ''
    exit 1
}
Write-Ok 'Основные модули импортируются успешно.'

# --- 5. Создание .env из .env.example ----------------------------
$envPath = Join-Path $root '.env'
if (Test-Path $envPath) {
    Write-Ok 'Файл .env уже существует (не изменяю его).'
} else {
    Write-Info 'Создаю .env из .env.example ...'
    Copy-Item -LiteralPath (Join-Path $root '.env.example') -Destination $envPath
    Write-Ok 'Файл .env создан.'
}

# --- 6. Проверка наличия API-ключа ------------------------------
$keyFound = $false
foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
    if ($line -match '^\s*ORCAROUTER_API_KEY\s*=\s*(.+)$') {
        if ($matches[1].Trim().Trim('"').Trim("'") -ne '') { $keyFound = $true }
    }
}
if ($keyFound) {
    Write-Ok 'ORCAROUTER_API_KEY задан в .env (значение не показываю).'
} else {
    Write-Warn 'ORCAROUTER_API_KEY не задан в .env.'
    Write-Host 'Панель запустится, но чат будет показывать ошибку, пока вы не'
    Write-Host 'откроете файл .env блокнотом и не вставите свой ключ OrcaRouter.'
}

Write-Host ''
Write-Host '====================================================='
Write-Host '  Подготовка завершена.'
Write-Host '  Теперь запускайте START_MIRA.bat'
Write-Host '====================================================='
Write-Host ''
exit 0