"""M4 downgrade: обратимая миграция жизненного цикла фактов (m4 → Этап 0).

Скрипт возвращает схему notes к Этапу 0: убирает status/valid_from/valid_to/
superseded_by_id и восстанавливает старый частичный уникальный индекс.
Принцип «downgrade безопаснее upgrade»:

1. Пре-проверки целостности до любых изменений. Битые ссылки
   superseded_by_id, нарушения FK или отсутствие note_versions останавливают
   скрипт со списком проблемных ID — мусор не схлопывается.
2. Перед схлопыванием тело каждой удаляемой строки и все её версии
   архивируются в note_versions актуальной строки. Ничего не теряется:
   тела остаются в базе (в истории версий), удаляются только сами
   дублирующие строки, без которых индекс Этапа 0 не пересоздать.
3. Автоматический срез-бэкап базы (*.pre-m4-downgrade-<ts>.bak) перед
   изменениями.
4. Пересоздание таблицы классической 12-шаговой процедурой SQLite — не
   полагаемся на ALTER TABLE DROP COLUMN (нет в SQLite < 3.35).
5. Сухой режим по умолчанию: без --yes скрипт только показывает план.

Запуск: python -m tools.downgrade_m4 [--db data/memory.sqlite3] [--yes]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / 'data' / 'memory.sqlite3'

STAGE0_COLUMNS = ('id,body,created,viewer_id,fact_key,revision,'
                  'memory_type,importance,source_kind,confidence')

STAGE0_TABLE_SQL = '''CREATE TABLE {name}(
  id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL, created REAL NOT NULL,
  viewer_id TEXT, fact_key TEXT, revision INTEGER NOT NULL DEFAULT 1,
  memory_type TEXT NOT NULL DEFAULT 'fact', importance REAL NOT NULL DEFAULT 0.5,
  source_kind TEXT NOT NULL DEFAULT 'owner', confidence REAL NOT NULL DEFAULT 1.0)'''

STAGE0_INDEX_SQL = ("CREATE UNIQUE INDEX note_scope_key ON notes("
                    "COALESCE(viewer_id,''),fact_key) WHERE fact_key IS NOT NULL")

M4_COLUMNS = ('status', 'valid_from', 'valid_to', 'superseded_by_id')


def _column_names(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute(f'PRAGMA table_info({table})')]


def already_stage0(db: sqlite3.Connection) -> bool:
    """База уже откачена (колонки M4 нет) — повторный запуск не ошибка."""
    return 'status' not in _column_names(db, 'notes')


def precheck(db: sqlite3.Connection) -> list[str]:
    """Список проблем целостности; пустой список — можно понижать."""
    problems: list[str] = []
    columns = _column_names(db, 'notes')
    missing = [c for c in M4_COLUMNS if c not in columns]
    if missing:
        problems.append(f'Миграция M4 incomplete: нет колонок {missing} — сначала запустите приложение.')
    if 'note_versions' not in {t[0] for t in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        problems.append('Таблицы note_versions нет — запускайте приложение один раз, чтобы она создалась.')
    if problems:
        return problems
    broken = db.execute('''SELECT id, superseded_by_id FROM notes
        WHERE superseded_by_id IS NOT NULL
          AND superseded_by_id NOT IN (SELECT id FROM notes)''').fetchall()
    for note_id, target in broken:
        problems.append(f'Битая ссылка: заметка #{note_id} указывает superseded_by_id на несуществующую #{target}')
    for row in db.execute('PRAGMA foreign_key_check'):
        problems.append(f'Нарушение внешнего ключа: таблица {row[0]}, строка {row[1]}, родитель {row[2]}')
    return problems


def plan(db: sqlite3.Connection) -> dict[str, Any]:
    """Посчитать план схлопывания без изменения данных."""
    by_status = {r[0] or 'active': r[1] for r in
                 db.execute('SELECT status, COUNT(*) FROM notes GROUP BY status')}
    rows = db.execute('''SELECT id,body,viewer_id,fact_key,revision,status,created
        FROM notes WHERE fact_key IS NOT NULL''').fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault((r['viewer_id'] or '', r['fact_key']), []).append(r)
    keep: list[int] = []
    drop: list[int] = []
    chains: list[dict[str, Any]] = []
    for (viewer, key), items in sorted(groups.items()):
        if len(items) < 2:
            continue
        ordered = sorted(items, key=lambda r: (
            0 if (r['status'] or 'active') == 'active' else
            1 if r['status'] == 'disputed' else 2,
            -r['created'], -r['id']))
        keeper, extra = ordered[0], ordered[1:]
        keep.append(keeper['id'])
        drop.extend(x['id'] for x in extra)
        chains.append({'fact_key': key, 'viewer_id': viewer or None,
                       'keeper': keeper['id'], 'removed': [x['id'] for x in extra]})
    versions = db.execute('SELECT COUNT(*) FROM note_versions').fetchone()[0]
    return {'by_status': by_status, 'chains': chains,
            'keep': keep, 'drop': drop, 'versions': versions,
            'total': db.execute('SELECT COUNT(*) FROM notes').fetchone()[0]}


def _archive_into(db: sqlite3.Connection, keeper_id: int, row_id: int) -> int:
    """Перенести тело строки и все её версии в note_versions актуальной строки.

    Возвращает количество присвоенных ревизий. Версии удаляемой строки
    перезаписываются на note_id=keeper со сдвигом после последней ревизии
    актуальной строки; тело самой строки добавляется как отдельная версия.
    """
    last = db.execute('SELECT COALESCE(MAX(revision),0) FROM note_versions WHERE note_id=?',
                      (keeper_id,)).fetchone()[0]
    note = db.execute('SELECT body,viewer_id,fact_key,created FROM notes WHERE id=?', (row_id,)).fetchone()
    moved = 0
    for v in db.execute('''SELECT revision,body,viewer_id,fact_key,memory_type,importance,
        source_kind,confidence,created FROM note_versions WHERE note_id=? ORDER BY revision''', (row_id,)):
        last += 1
        db.execute('''INSERT INTO note_versions(note_id,revision,body,viewer_id,fact_key,
            memory_type,importance,source_kind,confidence,created) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (keeper_id, last, v['body'], v['viewer_id'], v['fact_key'], v['memory_type'],
             v['importance'], v['source_kind'], v['confidence'], v['created']))
        moved += 1
    if note is not None and not moved:
        # Строка без собственной истории: тело сохраняем как версию.
        last += 1
        db.execute('''INSERT INTO note_versions(note_id,revision,body,viewer_id,fact_key,
            memory_type,importance,source_kind,confidence,created) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (keeper_id, last, note['body'], note['viewer_id'], note['fact_key'], 'fact',
             0.5, 'owner', 1.0, note['created']))
        moved += 1
    return moved


def downgrade(db_path: Path, yes: bool = False) -> dict[str, Any]:
    """Проверить, показать план и (при yes) выполнить откат m4 → Этап 0.

    Возвращает словарь с планом/результатом. Оставляет базу неизменённой
    при любом отказе пре-проверок. При yes создаёт срез-бэкап рядом с базой.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise SystemExit(f'База не найдена: {db_path}')
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    try:
        if already_stage0(db):
            print('База уже в формате Этапа 0 — downgrade не требуется, база не изменялась.')
            return {'executed': False, 'already_stage0': True}
        problems = precheck(db)
        if problems:
            print('ОТКАЗ: downgrade остановлен пре-проверками целостности:')
            for p in problems:
                print(f'  - {p}')
            raise SystemExit(2)
        info = plan(db)
        print(f"Заметок: {info['total']} ({info['by_status']}); версий: {info['versions']}")
        if not info['chains']:
            print('Дублирующихся ключей фактов нет — схлопывание не требуется.')
        for c in info['chains']:
            print(f"  ключ '{c['fact_key']}' (зритель: {c['viewer_id'] or 'общий'}): "
                  f"оставляем #{c['keeper']}, архивируем и убираем {c['removed']}")
        if not yes:
            print('Сухой режим: база не изменялась. Добавьте --yes для выполнения.')
            info['executed'] = False
            return info

        backup = db_path.with_name(f'{db_path.name}.pre-m4-downgrade-{int(time.time())}.bak')
        with sqlite3.connect(str(backup)) as b:
            db.backup(b)
        print(f'Срез-бэкап: {backup}')

        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        try:
            archived = 0
            for c in info['chains']:
                for row_id in c['removed']:
                    archived += _archive_into(db, c['keeper'], row_id)
                    db.execute('DELETE FROM note_versions WHERE note_id=?', (row_id,))
                    db.execute('DELETE FROM notes WHERE id=?', (row_id,))
            # 12-шаговое пересоздание таблицы без колонок M4.
            db.execute("DROP INDEX IF EXISTS note_scope_key")
            db.execute(STAGE0_TABLE_SQL.format(name='notes_stage0'))
            db.execute(f'INSERT INTO notes_stage0({STAGE0_COLUMNS}) SELECT {STAGE0_COLUMNS} FROM notes')
            db.execute('DROP TABLE notes')
            db.execute('ALTER TABLE notes_stage0 RENAME TO notes')
            db.execute(STAGE0_INDEX_SQL)
            db.execute("DELETE FROM state WHERE key IN ('schema_version','m4_migration_report')")
            checks = {
                'foreign_key_check': list(db.execute('PRAGMA foreign_key_check')),
                'integrity': db.execute('PRAGMA integrity_check').fetchone()[0],
            }
            if checks['foreign_key_check'] or checks['integrity'] != 'ok':
                raise RuntimeError(f'Пост-проверки не пройдены: {checks}')
            after_notes = db.execute('SELECT COUNT(*) FROM notes').fetchone()[0]
            after_versions = db.execute('SELECT COUNT(*) FROM note_versions').fetchone()[0]
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute('PRAGMA foreign_keys=ON')
        if len(info['keep']) and info['total'] - len(info['drop']) != after_notes:
            raise RuntimeError('Счётчик строк после отката не сходится с планом.')
        print(f"Готово: заметок {info['total']} → {after_notes}, "
              f"версий {info['versions']} → {after_versions} "
              f"(архивировано тел/версий: {archived}).")
        print('Перезапустите приложение: индекс перестроится сам при первом поиске.')
        info.update({'executed': True, 'backup': str(backup), 'archived': archived,
                     'after_notes': after_notes, 'after_versions': after_versions})
        return info
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Откат миграции M4 к схеме Этапа 0')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='путь к memory.sqlite3')
    parser.add_argument('--yes', action='store_true', help='выполнить (без флага — только план)')
    args = parser.parse_args(argv)
    try:
        downgrade(args.db, yes=args.yes)
    except SystemExit as exc:
        return 0 if exc.code is None else int(exc.code)
    return 0


if __name__ == '__main__':
    sys.exit(main())
