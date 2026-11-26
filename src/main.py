"""Запуск панели локально или в режиме доступа из домашней сети."""
import os
from .config import load_config
import uvicorn

def main() -> None:
    """Обычный режим — только этот компьютер; MIRA_LAN=1 — домашняя сеть."""
    cfg=load_config()
    if int(cfg['app']['port']) != 8000:
        raise ValueError('Для панели пока разрешён только порт 8000')
    lan=os.getenv('MIRA_LAN')=='1'
    if lan and not os.getenv('KUMAR_ADMIN_TOKEN','').strip():
        # C4: в LAN-режиме admin-токен обязателен, иначе destructive-эндпоинты
        # (заметки, эмоция, backstage, backup, настройки движка) доступны всем.
        print('[MIRA] ПРЕДУПРЕЖДЕНИЕ: включён LAN-режим без KUMAR_ADMIN_TOKEN — '
              'изменяющие эндпоинты панели доступны всем в домашней сети. '
              'Задайте KUMAR_ADMIN_TOKEN в .env и перезапустите панель.')
    host='0.0.0.0' if lan else '127.0.0.1'
    uvicorn.run('src.app:app',host=host,port=8000,log_level='info')

if __name__=='__main__': main()
