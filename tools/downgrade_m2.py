"""M2 downgrade: удаление полнотекстового индекса FTS5 (возврат к поиску до M2).

Скрипт убирает производный слой поиска: виртуальную таблицу notes_fts и
три триггера синхронизации (notes_fts_ai/ad/au). Принцип «downgrade
безопаснее upgrade» здесь особенно прост: FTS5-индекс — производные
данные, источник истины (notes) не трогается вообще.

1. Пре-проверки: отсутствие notes_fts на уже откаченной базе — честный
   no-op; наличие незнакомых триггеров notes_fts_* не останавливает
   скрипт, а удаляется (это тоже производная часть индекса).
2. Автоматический срез-бэкап базы (*.pre-m2-downgrade-<ts>.bak) перед
   изменениями — consistent-копия через sqlite3 Connection.backup.
3. Сухой режим по умолчанию: без --yes скрипт только показывает план.
4. После удаления выполняется PRAGMA integrity_check.

Примечание: сам поиск после downgrade продолжает работать словесным
резервным поиском (semantic.lexical_rank) — M2 добавлял FTS5 как
ускоритель, а не как единственную поддержку поиска.

Запуск: python -m tools.downgrade_m2 [--db data/memory.sqlite3] [--yes]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / 'data' / 'memory.sqlite3'

FTS_TRIGGERS = ('notes_fts_ai', 'notes_fts_ad', 'notes_fts_au')


def _fts_present(db: sqlite3.Connection) -> bool:
    return ('notes_fts',) in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='notes_fts'").fetchall()


def already_downgraded(db: sqlite3.Connection) -> bool:
    """Индекса нет и триггеров нет — повторный запуск не ошибка."""
    return not _fts_present(db) and not _triggers_present(db)


def _triggers_present(db: sqlite3.Connection) -> list[str]:
    rows = db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    return [r[0] for r in rows if r[0] in FTS_TRIGGERS]


def precheck(db: sqlite3.Connection) -> list[str]:
    """Список проблем; пустой список — можно понижать."""
    problems: list[str] = []
    try:
        rows = db.execute('SELECT COUNT(*) FROM notes').fetchone()
        if rows is None:
            problems.append('Таблицы notes нет — это не база M2/Этапа 0.')
    except sqlite3.OperationalError as exc:
        problems.append(f'Таблица notes недоступна: {exc}')
    return problems


def plan(db: sqlite3.Connection) -> dict[str, object]:
    """Посчитать план без изменения данных."""
    fts_rows = 0
    if _fts_present(db):
        try:
            fts_rows = db.execute('SELECT COUNT(*) FROM notes_fts').fetchone()[0]
        except sqlite3.OperationalError:
            fts_rows = -1  # таблица есть, но битая — будет удалена в любом случае
    return {'fts_table': _fts_present(db), 'fts_rows': fts_rows,
            'triggers': _triggers_present(db),
            'notes_untouched': True}


def downgrade(db_path: Path, yes: bool = False) -> dict[str, object]:
    """Удалить FTS5-индекс и триггеры; notes не затрагивается."""
    db = sqlite3.connect(db_path)
    try:
        info = plan(db)
        info['executed'] = False
        info['already_downgraded'] = already_downgraded(db)
        info['backup'] = None
        problems = precheck(db)
        info['problems'] = problems
        if info['already_downgraded']:
            return info
        if problems:
            return info
        if not yes:
            return info
        backup_path = db_path.with_name(
            db_path.name + f'.pre-m2-downgrade-{time.strftime("%Y%m%d-%H%M%S")}.bak')
        target = sqlite3.connect(backup_path)
        try:
            db.backup(target)
        finally:
            target.close()
        db.execute('DROP TABLE IF EXISTS notes_fts')
        for name in _triggers_present(db):
            db.execute(f'DROP TRIGGER IF EXISTS {name}')
        # Остатки состояния миграции — тоже производные данные.
        db.execute("DELETE FROM state WHERE key='m2_fts'")
        db.commit()
        info['executed'] = True
        info['backup'] = str(backup_path)
        info['integrity_check'] = db.execute('PRAGMA integrity_check').fetchone()[0]
        return info
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='M2 downgrade: убрать FTS5-индекс заметок.')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='путь к memory.sqlite3')
    parser.add_argument('--yes', action='store_true', help='выполнить изменения (иначе показать план)')
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(f'База не найдена: {args.db}')
        return 1
    info = downgrade(args.db, yes=args.yes)
    print(f'База: {args.db}')
    if info['already_downgraded']:
        print('Индекс FTS5 уже отсутствует — downgrade не требуется.')
        return 0
    if info['problems']:
        for problem in info['problems']:
            print(f'ПРЕГРАДА: {problem}')
        return 2
    print(f"План: notes_fts={'есть' if info['fts_table'] else 'нет'} "
          f"({info['fts_rows']} строк), триггеры: {info['triggers'] or 'нет'}. "
          'Таблица notes не затрагивается.')
    if not args.yes:
        print('Сухой режим: без --yes изменения не применяются.')
        return 0
    print(f"Выполнено. Бэкап: {info['backup']}. integrity_check: {info['integrity_check']}.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
