#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT/.venv"
PID_FILE="$PROJECT/.mira-app.pid"
LOG_FILE="$PROJECT/launcher.log"
HOST="${MIRA_HOST:-127.0.0.1}"
PORT="${MIRA_PORT:-8000}"

say(){ printf '[MIRA] %s\n' "$*"; }
warn(){ printf '[MIRA] ВНИМАНИЕ: %s\n' "$*" >&2; }

require_tools(){
  command -v python3 >/dev/null || { warn 'Не найден python3.'; exit 1; }
  command -v curl >/dev/null || { warn 'Не найден curl.'; exit 1; }
}

ensure_python_env(){
  if [[ ! -x "$VENV/bin/python" ]]; then
    say 'Создаю Python-окружение…'
    python3 -m venv "$VENV"
  fi
  if ! "$VENV/bin/python" - <<'PY'
import importlib.util, sys
missing=[x for x in ('yaml','fastapi','httpx','chromadb') if importlib.util.find_spec(x) is None]
if missing:
    sys.exit(2)
print('DEPENDENCIES_OK')
PY
  then
    say 'Устанавливаю Python-зависимости проекта…'
    if command -v uv >/dev/null 2>&1; then
      (cd "$PROJECT" && uv pip install --python "$VENV/bin/python" -e .)
    else
      "$VENV/bin/python" -m pip install -q -e "$PROJECT"
    fi
  fi
  "$VENV/bin/python" - <<'PY'
import importlib.util, sys
missing=[x for x in ('yaml','fastapi','httpx','chromadb') if importlib.util.find_spec(x) is None]
if missing:
    print('Не установились зависимости: '+', '.join(missing)); sys.exit(2)
print('DEPENDENCIES_OK')
PY
}

prepare(){
  require_tools
  ensure_python_env
  chmod +x "$PROJECT/launcher/run-mira.sh"
  if [[ ! -f "$PROJECT/.env" ]]; then
    warn 'Файл .env не найден. Скопируйте .env.example и добавьте ORCAROUTER_API_KEY.'
  elif ! grep -q '^ORCAROUTER_API_KEY=.[^ ]' "$PROJECT/.env"; then
    warn 'В .env пока не задан ORCAROUTER_API_KEY.'
  else
    say 'Ключ OrcaRouter найден локально (сам ключ не показываю).'
  fi
  say 'Ollama отключена. Смысловой поиск отключён; память работает по словам.'
}

status(){
  local running='нет'
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then running='да'; fi
  say "Панель запущена: $running"
  say "Адрес: http://$HOST:$PORT"
  say 'Движок: OrcaRouter API'
  say 'Ollama: отключена проектом'
  if [[ -f "$PROJECT/.env" ]] && grep -q '^ORCAROUTER_API_KEY=.[^ ]' "$PROJECT/.env"; then
    say 'ORCAROUTER_API_KEY: задан локально'
  else
    warn 'ORCAROUTER_API_KEY: не задан'
  fi
}

diagnostic(){
  say "Проект: $PROJECT"
  say "Python: $VENV/bin/python"
  say "Конфиг: config/config.yaml"
  status
  [[ -f "$PROJECT/.env" ]] || warn '.env отсутствует; это нормально до первого запуска, но чат не заработает.'
}

start_app(){
  require_tools
  ensure_python_env
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    say "Панель уже работает: http://$HOST:$PORT"; return 0
  fi
  mkdir -p "$PROJECT/data"
  say 'Запускаю панель через OrcaRouter API…'
  (
    cd "$PROJECT"
    nohup "$VENV/bin/python" -m uvicorn src.app:app --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
  )
  for _ in {1..30}; do
    if curl -fsS "http://$HOST:$PORT/api/status" >/dev/null 2>&1; then
      say "Панель готова: http://$HOST:$PORT"
      return 0
    fi
    sleep 1
  done
  warn 'Панель не ответила. Последние строки launcher.log:'
  tail -n 30 "$LOG_FILE" || true
  exit 1
}

stop_app(){
  if [[ -f "$PID_FILE" ]]; then
    local pid; pid=$(cat "$PID_FILE")
    if kill -0 "$pid" 2>/dev/null; then kill "$pid"; fi
    rm -f "$PID_FILE"
  fi
  say 'Панель остановлена. Локальная Ollama проектом не запускается.'
}

open_app(){
  if command -v cmd.exe >/dev/null 2>&1; then cmd.exe /c start "" "http://$HOST:$PORT" >/dev/null 2>&1 || true
  else say "Откройте в браузере: http://$HOST:$PORT"; fi
}

case "${1:-start}" in
  start|start-detached) start_app ;;
  start-attached) require_tools; ensure_python_env; cd "$PROJECT"; exec "$VENV/bin/python" -m uvicorn src.app:app --host "$HOST" --port "$PORT" ;;
  start-lan) MIRA_HOST=0.0.0.0 "$0" start ;;
  prepare) prepare ;;
  status) status ;;
  diagnostic) diagnostic ;;
  open) open_app ;;
  stop) stop_app ;;
  ollama) warn 'Команда Ollama отключена в версии 0.14.0. Используется OrcaRouter API.'; exit 2 ;;
  *) echo "Использование: $0 {start|start-attached|start-lan|prepare|status|diagnostic|open|stop}"; exit 2 ;;
esac
