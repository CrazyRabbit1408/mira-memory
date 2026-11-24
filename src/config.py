"""Загрузка настроек и путей прототипа."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any
import yaml

ROOT = Path(__file__).resolve().parents[1]
APP_VERSION = '0.14.0-orcarouter'


def _load_dotenv() -> None:
    """Загрузить локальный .env без дополнительной зависимости."""
    env_path = ROOT / '.env'
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip().lstrip('\ufeff'), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv()

def load_config() -> dict[str, Any]:
    """Загрузить настройки из файла проекта."""
    with (ROOT / 'config' / 'config.yaml').open(encoding='utf-8') as f:
        result = yaml.safe_load(f)
    if not isinstance(result, dict):
        raise ValueError('Проверьте config/config.yaml')
    return result

def data_dir() -> Path:
    """Вернуть абсолютный путь к данным; переопределение удобно для тестов."""
    location = Path(os.environ.get('KUMAR_DATA_DIR', load_config()['memory']['data_dir'])).expanduser()
    return location if location.is_absolute() else ROOT / location
