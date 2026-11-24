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
    host='0.0.0.0' if lan else '127.0.0.1'
    uvicorn.run('src.app:app',host=host,port=8000,log_level='info')

if __name__=='__main__': main()
