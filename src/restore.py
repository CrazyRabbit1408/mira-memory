"""Восстановление в отдельный каталог без изменения проекта."""
from __future__ import annotations
import json, shutil, sqlite3, stat, sys, tempfile, zipfile
from pathlib import Path, PurePosixPath
MAX_BYTES=512*1024*1024

def restore(archive: Path, destination: Path) -> None:
    """Проверить архив и базу во временной папке; назначение должно отсутствовать."""
    if destination.exists(): raise ValueError('Папка назначения уже существует; выберите новое имя.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive) as z, tempfile.TemporaryDirectory(prefix='.memory-restore-',dir=destination.parent) as tmp:
        files=z.infolist(); names=z.namelist()
        if len(files)>500 or sum(f.file_size for f in files)>MAX_BYTES: raise ValueError('Архив слишком большой')
        if len(names)!=len(set(names)) or not {'backup-info.json','data/memory.sqlite3','config/character.md'}.issubset(names): raise ValueError('Неполная или повреждённая копия')
        if json.loads(z.read('backup-info.json')).get('format_version')!=1: raise ValueError('Версия копии не поддерживается')
        allowed={'backup-info.json','data/memory.sqlite3','config/config.yaml','config/character.md','config/name.txt','config/graph.json'}
        for entry in files:
            p=PurePosixPath(entry.filename)
            safe=entry.filename in allowed or (len(p.parts)==4 and p.parts[:3]==('config','lore','character') and p.suffix=='.md') or (len(p.parts)==2 and p.parts[0]=='reference' and p.suffix=='.md')
            if not safe or '..' in p.parts or p.is_absolute() or entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16): raise ValueError('Недопустимый файл в архиве: '+entry.filename)
            target=Path(tmp).joinpath(*p.parts); target.parent.mkdir(parents=True,exist_ok=True)
            with z.open(entry) as src, target.open('wb') as out: shutil.copyfileobj(src,out)
        db=sqlite3.connect(Path(tmp)/'data/memory.sqlite3')
        try:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('База данных повреждена')
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'messages','notes','state'}.issubset(tables): raise ValueError('Неизвестная структура базы')
        finally: db.close()
        # Переименовываем только после полной проверки; копию не запускаем как программу.
        Path(tmp).rename(destination)

def main() -> None:
    """Проверить и распаковать архив памяти в новый каталог."""
    if len(sys.argv)!=3:
        print('Использование: uv run python -m src.restore КОПИЯ.zip НОВАЯ_ПАПКА'); raise SystemExit(2)
    try: restore(Path(sys.argv[1]),Path(sys.argv[2]))
    except (ValueError, OSError, zipfile.BadZipFile, sqlite3.Error) as e:
        print(f'Ошибка восстановления: {e}'); raise SystemExit(1) from e
    print('Копия восстановлена. См. README: файлы нужно перенести в установленный проект.')
if __name__=='__main__': main()
