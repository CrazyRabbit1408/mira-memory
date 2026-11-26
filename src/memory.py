"""Постоянные сообщения, заметки и отдельный тестовый поисковый индекс.

SQLite — источник истины; Chroma — восстанавливаемый индекс. Лор персонажа
индексируется только из отдельной папки. Полный авторский лор не индексируется.

M4: заметки имеют явный жизненный цикл факта (status/valid_from/valid_to/
superseded_by_id). Правка содержания создаёт новую строку и помечает старую
(status='superseded'), ничего не удаляя: история изменений сохраняется
полностью и остаётся видимой через цепочки суперсессий.

M2: заметки дополнительно индексируются полнотекстовым индексом FTS5
(notes_fts: body, fact_key, viewer_id; tokenizer=unicode61). Индекс —
производные данные: он всегда перестраивается из notes (M1, честный
rebuild_index) и синхронизируется триггерами на каждой правке. Поиск
идёт по BM25-рангу с префиксными запросами (морфология русского языка)
и фильтром жизненного цикла M4: по умолчанию — только текущая правда
(active/disputed), история — по явному include_history=True.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
import chromadb
from chromadb.config import Settings
from .embeddings import stub_embedding, tokens
from .scoring import safe_float, message_relates_to_fact
from . import knowledge

# M2: маркеры подсветки для snippet(). Символы Private Use Area не встречаются
# в обычном тексте: фронтенд экранирует HTML сниппета и только потом превращает
# их в <mark>, поэтому подсветка не может протащить разметку из тела заметки.
SNIPPET_OPEN = '\ue000'
SNIPPET_CLOSE = '\ue001'

# Схема и триггеры FTS5-индекса заметок. rowid = notes.id; колонки — копии
# (NOT body NULL в notes, NULL-ключи и зрители заменяются пустой строкой).
FTS_TABLE_SQL = ("CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
                 "body, fact_key, viewer_id, tokenize='unicode61')")
FTS_TRIGGER_SQLS = (
    '''CREATE TRIGGER IF NOT EXISTS notes_fts_ai AFTER INSERT ON notes BEGIN
        INSERT INTO notes_fts(rowid,body,fact_key,viewer_id)
        VALUES(new.id,new.body,COALESCE(new.fact_key,''),COALESCE(new.viewer_id,''));
    END''',
    '''CREATE TRIGGER IF NOT EXISTS notes_fts_ad AFTER DELETE ON notes BEGIN
        DELETE FROM notes_fts WHERE rowid=old.id;
    END''',
    '''CREATE TRIGGER IF NOT EXISTS notes_fts_au AFTER UPDATE ON notes BEGIN
        DELETE FROM notes_fts WHERE rowid=old.id;
        INSERT INTO notes_fts(rowid,body,fact_key,viewer_id)
        VALUES(new.id,new.body,COALESCE(new.fact_key,''),COALESCE(new.viewer_id,''));
    END''',
)


MEMORY_TYPES = frozenset({
    'fact', 'preference', 'relationship', 'event',
    'promise', 'open_thread', 'belief', 'canon',
})

def normalize_memory_type(value: str | None) -> str:
    """Return a bounded type for structured memories."""
    value = (value or 'fact').strip().casefold()
    return value if value in MEMORY_TYPES else 'fact'

NOTE_STATUSES = frozenset({'active', 'superseded', 'disputed', 'retracted'})

# Мягкое отзывание допустимо для любой ещё не отозванной строки: отозвать
# историческую (уже superseded) версию — законное кураторское действие.
RETRACTABLE = frozenset({'active', 'disputed', 'superseded'})

def normalize_note_status(value: str | None) -> str:
    """Ограничить статус заметки допустимым перечнем M4."""
    value = (value or 'active').strip().casefold()
    return value if value in NOTE_STATUSES else 'active'

def clamp_score(value: float | int | None, default: float = 0.5) -> float:
    """Keep user-editable confidence/importance values in a stable range."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))

class MemoryManager:
    """Хранить и искать записи одного тестового персонажа."""
    def __init__(self, data: Path, lore: Path, history_days: int = 7) -> None:
        self.data, self.lore, self.history_days = data, lore, history_days
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.data / 'memory.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
          viewer_id TEXT NOT NULL, speaker TEXT NOT NULL CHECK(speaker IN ('viewer','kumar')),
          body TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT,
          body TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS viewers(viewer_id TEXT PRIMARY KEY, first_seen REAL NOT NULL,
          last_seen REAL NOT NULL, count INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS graph_nodes(id TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS graph_edges(source TEXT NOT NULL REFERENCES graph_nodes(id),
          target TEXT NOT NULL REFERENCES graph_nodes(id), relation TEXT NOT NULL,
          PRIMARY KEY(source,target,relation));
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stream_memories(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          body TEXT NOT NULL,
          memory_type TEXT NOT NULL DEFAULT 'fact',
          fragment_kind TEXT NOT NULL DEFAULT 'memory',
          importance REAL NOT NULL DEFAULT 0.8,
          created REAL NOT NULL,
          active INTEGER NOT NULL DEFAULT 1
        );
        ''')
        columns={r['name'] for r in self.db.execute('PRAGMA table_info(notes)')}
        for name,decl in [('viewer_id','TEXT'),('fact_key','TEXT'),('revision','INTEGER NOT NULL DEFAULT 1')]:
            if name not in columns: self.db.execute(f'ALTER TABLE notes ADD COLUMN {name} {decl}')
        for name,decl in [
            ('memory_type',"TEXT NOT NULL DEFAULT 'fact'"),
            ('importance','REAL NOT NULL DEFAULT 0.5'),
            ('source_kind',"TEXT NOT NULL DEFAULT 'owner'"),
            ('confidence','REAL NOT NULL DEFAULT 1.0'),
        ]:
            if name not in columns: self.db.execute(f'ALTER TABLE notes ADD COLUMN {name} {decl}')
        self.db.executescript(knowledge.SCHEMA)
        version_columns={r['name'] for r in self.db.execute('PRAGMA table_info(note_versions)')}
        for name,decl in [
            ('memory_type',"TEXT NOT NULL DEFAULT 'fact'"),
            ('importance','REAL NOT NULL DEFAULT 0.5'),
            ('source_kind',"TEXT NOT NULL DEFAULT 'owner'"),
            ('confidence','REAL NOT NULL DEFAULT 1.0'),
        ]:
            if version_columns and name not in version_columns:
                self.db.execute(f'ALTER TABLE note_versions ADD COLUMN {name} {decl}')
        self._migrate_note_lifecycle()
        self._migrate_fts_index()
        # Эфирные фрагменты не должны неожиданно переживать полный перезапуск
        # панели: постоянный канон хранится в notes, а этот слой относится к
        # текущему эфиру.
        self.db.execute("UPDATE stream_memories SET active=0 WHERE active=1")
        with self.db: knowledge.sync_graph(self.db,self.lore)
        self.db.commit()
        self.client = chromadb.PersistentClient(path=str(self.data / 'chroma'),
            settings=Settings(anonymized_telemetry=False))


    def _migrate_note_lifecycle(self) -> None:
        """M4: идемпотентная миграция Этапа 0 → жизненный цикл фактов.

        Уточнение заказчика: вся миграция выполняется в одной транзакции
        BEGIN IMMEDIATE — между DROP старого уникального индекса и CREATE
        нового ни один параллельный запрос не успеет вставить две active-
        заметки с одним ключом. Пересоздание индекса происходит после
        бэкфилла valid_from и после схлопывания дублей ключей, но до любых
        пользовательских INSERT.

        Существующие строки безопасно переносятся: DEFAULT 'active'
        мгновенно помечает их актуальными, valid_from честно берётся из
        created. Дубли ключей (возможны в базах, переживших версии до
        индекса) не удаляются молча: новейшая строка остаётся active,
        старые помечаются superseded со ссылкой на неё, отчёт пишется в
        state. Обратимая сторона — tools/downgrade_m4.py.
        """
        with self.lock:
            before = self.db.execute('SELECT COUNT(*) FROM notes').fetchone()[0]
            self.db.execute('BEGIN IMMEDIATE')
            try:
                columns = {r['name'] for r in self.db.execute('PRAGMA table_info(notes)')}
                for name, decl in [
                    ('status', "TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded','disputed','retracted'))"),
                    ('valid_from', 'REAL'),
                    ('valid_to', 'REAL'),
                    ('superseded_by_id', 'INTEGER REFERENCES notes(id)'),
                ]:
                    if name not in columns:
                        self.db.execute(f'ALTER TABLE notes ADD COLUMN {name} {decl}')
                self.db.execute('UPDATE notes SET valid_from=created WHERE valid_from IS NULL')
                report = self._collapse_duplicate_scope_keys()
                # Индекс пересоздаётся, только если его определение не содержит
                # условия статуса: на уже мигрированной базе ничего не трогаем.
                idx = self.db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='note_scope_key'").fetchone()
                if not idx or "status='active'" not in (idx['sql'] or ''):
                    self.db.execute('DROP INDEX IF EXISTS note_scope_key')
                    self.db.execute("CREATE UNIQUE INDEX note_scope_key ON notes(COALESCE(viewer_id,''),fact_key) WHERE fact_key IS NOT NULL AND status='active'")
                self.db.execute("INSERT INTO state(key,value) VALUES('schema_version','m4') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
                if report:
                    self.db.execute("INSERT INTO state(key,value) VALUES('m4_migration_report',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                    (json.dumps(report, ensure_ascii=False),))
                after = self.db.execute('SELECT COUNT(*) FROM notes').fetchone()[0]
                if after != before:
                    raise RuntimeError('Миграция M4 изменила количество заметок — откат.')
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def _collapse_duplicate_scope_keys(self) -> list[dict[str, Any]]:
        """Схлопнуть дубли одного ключа факта: новейшая строка остаётся active.

        Ничего не удаляется: старые дубли помечаются status='superseded'
        со ссылкой на новейшую строку и valid_to, равным created новейшей
        (момент, когда появился более свежий факт). Возвращённый отчёт
        попадает в state('m4_migration_report').
        """
        rows = self.db.execute('''SELECT id,body,viewer_id,fact_key,created FROM notes
            WHERE fact_key IS NOT NULL ORDER BY COALESCE(viewer_id,''),fact_key,created DESC,id DESC''').fetchall()
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for r in rows:
            groups.setdefault((r['viewer_id'] or '', r['fact_key']), []).append(r)
        report: list[dict[str, Any]] = []
        for (viewer, key), items in groups.items():
            if len(items) < 2:
                continue
            keeper = items[0]
            for stale in items[1:]:
                self.db.execute("UPDATE notes SET status='superseded',valid_to=?,superseded_by_id=? WHERE id=?",
                                (keeper['created'], keeper['id'], stale['id']))
            report.append({'fact_key': key, 'viewer_id': viewer or None,
                           'kept_id': keeper['id'],
                           'superseded_ids': sorted(s['id'] for s in items[1:])})
        return report

    def _ensure_fts_schema(self) -> None:
        """Создать (или пересоздать) схему FTS5-индекса.

        Триггеры всегда пересоздаются: если notes_fts была удалена вручную
        (M1-тест «принудительное удаление индекса»), оставшиеся триггеры
        ссылались бы на несуществующую таблицу и роняли бы каждую правку
        заметок. CREATE TRIGGER IF NOT EXISTS этого не заметил бы.
        """
        self.db.execute(FTS_TABLE_SQL)
        for name in ('notes_fts_ai', 'notes_fts_ad', 'notes_fts_au'):
            self.db.execute(f'DROP TRIGGER IF EXISTS {name}')
        for sql in FTS_TRIGGER_SQLS:
            self.db.execute(sql)

    def _rebuild_fts(self) -> int:
        """M2: полная пересборка notes_fts из notes (источник истины).

        Индекс — производные данные, потеря или рассинхронизация не
        считается повреждением памяти: метод чистит таблицу и заново
        копирует все строки notes. Возвращает число проиндексированных
        заметок.
        """
        with self.lock, self.db:
            self._ensure_fts_schema()
            self.db.execute('DELETE FROM notes_fts')
            self.db.execute('''INSERT INTO notes_fts(rowid,body,fact_key,viewer_id)
                SELECT id,body,COALESCE(fact_key,''),COALESCE(viewer_id,'') FROM notes''')
            count = self.db.execute('SELECT COUNT(*) FROM notes_fts').fetchone()[0]
        return int(count)

    def _migrate_fts_index(self) -> None:
        """M2: идемпотентная миграция — создать индекс и сверить его с notes.

        На первой базе Этапа 0/M4 таблицы notes_fts нет — она создаётся и
        заполняется всеми заметками. При последующих запусках сравнивается
        число строк: расхождение (падение питания, внешние инструменты,
        ручное удаление) означает рассинхронизацию — индекс перестраивается
        из notes, молчаливо оставить битый индекс нельзя.
        """
        with self.lock, self.db:
            self._ensure_fts_schema()
            fts_count = self.db.execute('SELECT COUNT(*) FROM notes_fts').fetchone()[0]
            notes_count = self.db.execute('SELECT COUNT(*) FROM notes').fetchone()[0]
            if fts_count != notes_count:
                self.db.execute('DELETE FROM notes_fts')
                self.db.execute('''INSERT INTO notes_fts(rowid,body,fact_key,viewer_id)
                    SELECT id,body,COALESCE(fact_key,''),COALESCE(viewer_id,'') FROM notes''')
                self.db.execute("INSERT INTO state(key,value) VALUES('m2_fts',?) "
                                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (json.dumps({'version': 1, 'notes': notes_count,
                                             'rebuilt_at_startup': True},
                                            ensure_ascii=False),))

    @staticmethod
    def fts_match_expression(query: str) -> str:
        """M2: превратить текст запроса в безопасное MATCH-выражение FTS5.

        Из запроса берутся только \\w-токены (кавычки, двоеточия и операторы
        FTS5 не могут проникнуть в выражение), каждый токен становится
        префиксным фраз-запросом — так unicode61 даёт морфологию русского:
        «кумара» находит «Кумар», «корабли» — «кораблей». Для длинных слов
        добавляется префикс первых трёх символов (основы): «люблю» находит
        «любимый», «читал» — «читает». Токены объединяются через AND.
        """
        found = re.findall(r'\w+', query, flags=re.UNICODE)[:8]
        clauses = []
        for tok in found:
            if len(tok) >= 4:
                clauses.append(f'("{tok}"* OR "{tok[:3]}"*)')
            else:
                clauses.append(f'"{tok}"*')
        return ' AND '.join(clauses)

    def search_notes(self, query: str, limit: int = 20, viewer_id: str | None = None,
                     include_history: bool = False) -> list[dict[str, Any]]:
        """M2: полнотекстовый поиск по заметкам (FTS5 + BM25-ранжирование).

        Индексируются body, fact_key и viewer_id заметок; ранжирование —
        bm25(notes_fts) (меньше — лучше), сниппет — snippet() с маркерами
        SNIPPET_OPEN/SNIPPET_CLOSE для безопасной подсветки на фронтенде.

        Интеграция с M4: по умолчанию ищется только «текущая правда» —
        active и disputed (disputed остаётся текущим фактом, спор не
        инвалидирует его, см. модель конфликтов). include_history=True
        добавляет superseded и retracted версии (поиск по истории).

        Заметки зрителя и общие заметки доступны при заданном viewer_id;
        чужие заметки не выдаются никогда.
        """
        match = self.fts_match_expression(query)
        if not match:
            return []
        cap = max(1, min(int(limit), 100))
        statuses = ('active', 'superseded', 'disputed', 'retracted') if include_history \
            else ('active', 'disputed')
        # Дополнение до четырёх плейсхолдеров: пустая строка не совпадает
        # ни с одним статусом, поэтому IN-фильтр остаётся честным.
        padded = tuple(statuses) + ('',) * (4 - len(statuses))
        with self.lock:
            rows = self.db.execute('''
                SELECT n.id, n.body, n.created, n.viewer_id, n.fact_key, n.revision,
                    n.memory_type, n.importance, n.source_kind, n.confidence,
                    n.status, n.valid_from, n.valid_to, n.superseded_by_id,
                    f.fts_rank, f.snippet
                FROM (SELECT rowid AS note_rowid,
                             bm25(notes_fts) AS fts_rank,
                             snippet(notes_fts, 0, :snippet_open, :snippet_close, '…', 12) AS snippet
                      FROM notes_fts WHERE notes_fts MATCH :match) f
                JOIN notes n ON n.id = f.note_rowid
                WHERE n.status IN (:s0,:s1,:s2,:s3)
                  AND (:viewer_id IS NULL OR COALESCE(n.viewer_id,'') IN ('', :viewer_id))
                ORDER BY f.fts_rank, n.id DESC
                LIMIT :limit
            ''', {'snippet_open': SNIPPET_OPEN, 'snippet_close': SNIPPET_CLOSE,
                  'match': match, 's0': padded[0], 's1': padded[1],
                  's2': padded[2], 's3': padded[3],
                  'viewer_id': (viewer_id or '').strip() or None,
                  'limit': cap}).fetchall()
            return [{'id': r['id'], 'body': r['body'], 'created': r['created'],
                     'viewer_id': r['viewer_id'], 'fact_key': r['fact_key'],
                     'revision': r['revision'], 'memory_type': r['memory_type'] or 'fact',
                     'importance': safe_float(r['importance'], 0.5),
                     'source_kind': r['source_kind'] or 'owner',
                     'confidence': safe_float(r['confidence'], 1.0),
                     'status': r['status'] or 'active', 'valid_to': r['valid_to'],
                     'superseded_by_id': r['superseded_by_id'],
                     'fts_rank': round(r['fts_rank'], 6), 'snippet': r['snippet'],
                     'source': f'Заметка #{r["id"]}'}
                    for r in rows]

    def fts_count(self) -> int:
        """M2: число строк в полнотекстовом индексе (диагностика честного rebuild)."""
        with self.lock:
            try:
                return int(self.db.execute('SELECT COUNT(*) FROM notes_fts').fetchone()[0])
            except sqlite3.OperationalError:
                return 0  # индекс отсутствует — честно сообщаем нулём

    def close(self) -> None:
        """Закрыть базу данных."""
        with self.lock:
            self.db.close()

    def lore_chunks(self) -> list[dict[str, Any]]:
        """Взять только файлы из выделенной папки персонажа."""
        result = []
        self.lore.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.lore.glob('*.md')):
            # Не следовать по ссылкам наружу и не индексировать приватный архив.
            if path.is_symlink() or not path.is_file():
                continue
            text = path.read_text(encoding='utf-8')
            chunks = [x.strip() for x in re.split(r'\n\s*\n', text) if x.strip() and not x.lstrip().startswith('#')]
            for position, chunk in enumerate(chunks):
                for offset in range(0, len(chunk), 1000):
                    part = chunk[offset:offset+1000]
                    result.append({'id':f'lore:{path.name}:{position}:{offset}', 'kind':'lore',
                        'text':part, 'source':f'config/lore/character/{path.name}'})
        return result

    def _records(self) -> list[dict[str, Any]]:
        cutoff = time.time() - self.history_days * 86400
        messages = self.db.execute('SELECT id,body,viewer_id FROM messages WHERE speaker=? AND created>=?', ('viewer',cutoff)).fetchall()
        # В поисковый индекс попадают только актуальные и спорные заметки:
        # выдача поиска — это «текущая правда». Устаревшие (superseded) и
        # отозванные (retracted) факты не ищутся как факты, но участвуют в
        # конфликте через MemoryManager.stale_facts().
        notes = self.db.execute('''SELECT id,body,viewer_id,fact_key,revision,memory_type,
            importance,source_kind,confidence,created,status,valid_from,valid_to,superseded_by_id
            FROM notes WHERE status IN ('active','disputed')''').fetchall()
        return self.lore_chunks() + knowledge.records(self.db) + [
            {'id':f'message:{r["id"]}', 'kind':'message', 'text':r['body'], 'source':f'Сообщение #{r["id"]}', 'viewer_id':r['viewer_id']} for r in messages
        ] + [{'id':f'note:{r["id"]}', 'kind':'note', 'text':r['body'], 'source':f'Заметка #{r["id"]}',
              'viewer_id':r['viewer_id'] or '', 'fact_key':r['fact_key'] or '', 'revision':r['revision'],
              'memory_type':r['memory_type'] or 'fact', 'importance':safe_float(r['importance'], 0.5),
              'source_kind':r['source_kind'] or 'owner', 'confidence':safe_float(r['confidence'], 1.0),
              'created':r['created'], 'status':r['status'] or 'active', 'valid_to':r['valid_to'],
              'superseded_by_id':r['superseded_by_id']} for r in notes] + [
            {'id':f'stream:{r["id"]}', 'kind':'stream_memory', 'text':r['body'],
             'source':f'Эфирный фрагмент #{r["id"]}',
             'memory_type':r['memory_type'] or 'fact',
             'fragment_kind':r['fragment_kind'] or 'memory',
             'importance':safe_float(r['importance'], 0.8),
             'source_kind':'backstage', 'confidence':1.0,
             'created':r['created']} for r in self.db.execute(
                 'SELECT id,body,memory_type,fragment_kind,importance,created '
                 'FROM stream_memories WHERE active=1 ORDER BY id DESC').fetchall()
        ]

    def rebuild_index(self) -> int:
        """Проверить исходные записи; смысловой индекс ведётся отдельно."""
        with self.lock: return len(self._records())

    def save_exchange(self, viewer_id: str, text: str, response: str) -> None:
        """Сохранить вход и ответ одной транзакцией и обновить профиль."""
        now=time.time()
        with self.lock, self.db:
            cur=self.db.execute('INSERT INTO messages(viewer_id,speaker,body,created) VALUES(?,?,?,?)',(viewer_id,'viewer',text,now))
            knowledge.capture(self.db,int(cur.lastrowid),viewer_id,text)
            self.db.execute('INSERT INTO messages(viewer_id,speaker,body,created) VALUES(?,?,?,?)',(viewer_id,'kumar',response,now))
            self.db.execute('''INSERT INTO viewers(viewer_id,first_seen,last_seen,count) VALUES(?,?,?,1)
                ON CONFLICT(viewer_id) DO UPDATE SET last_seen=excluded.last_seen,count=count+1''',(viewer_id,now,now))
        self.rebuild_index()

    def recent(self, limit: int = 20, viewer_id: str | None = None) -> list[dict[str, Any]]:
        """Вернуть историю в хронологическом порядке, при необходимости для одного зрителя."""
        with self.lock:
            if viewer_id is None:
                rows=self.db.execute('SELECT id,viewer_id,speaker,body,created FROM messages ORDER BY id DESC LIMIT ?', (max(1,min(limit,100)),)).fetchall()
            else:
                rows=self.db.execute('SELECT id,viewer_id,speaker,body,created FROM messages WHERE viewer_id=? ORDER BY id DESC LIMIT ?', (viewer_id,max(1,min(limit,100)))).fetchall()
            return [dict(x) for x in reversed(rows)]

    def add_note(self, text: str, viewer_id: str | None = None, fact_key: str | None = None,
                 memory_type: str = 'fact', importance: float = 0.5,
                 source_kind: str = 'owner', confidence: float = 1.0) -> int:
        """Добавить запись. Ключ защищает от повторного сохранения одного слота."""
        viewer_id=(viewer_id or '').strip() or None
        fact_key=(fact_key or '').strip().casefold() or None
        memory_type=normalize_memory_type(memory_type)
        importance=clamp_score(importance)
        source_kind=(source_kind or 'owner').strip()[:40] or 'owner'
        confidence=clamp_score(confidence, 1.0)
        with self.lock, self.db:
            now=time.time()
            cur=self.db.execute('''INSERT INTO notes(body,created,viewer_id,fact_key,memory_type,
                importance,source_kind,confidence,status,valid_from,valid_to,superseded_by_id)
                VALUES(?,?,?,?,?,?,?,?, 'active',?,NULL,NULL)''',
                (text,now,viewer_id,fact_key,memory_type,importance,source_kind,confidence,now))
            note_id=int(cur.lastrowid or 0)
            self.db.execute('''INSERT INTO note_versions(note_id,revision,body,viewer_id,fact_key,
                memory_type,importance,source_kind,confidence,created) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (note_id,1,text,viewer_id,fact_key,memory_type,importance,source_kind,confidence,now))
        self.rebuild_index()
        return note_id

    def update_note(self, note_id: int, text: str, viewer_id: str | None, fact_key: str | None,
                    revision: int, memory_type: str = 'fact', importance: float = 0.5,
                    source_kind: str = 'owner', confidence: float = 1.0) -> dict[str, Any] | None:
        """M4: изменить заметку с учётом жизненного цикла факта.

        Изменение текста или ключа факта — суперсессия: старая строка
        помечается status='superseded' (valid_to=now), новая создаётся со
        status='active' и сквозным номером ревизии; старая строка не
        удаляется, история сохраняется полностью. Правка только
        метаданных (важность/уверенность/тип/источник при том же тексте и
        ключе) остаётся правкой на месте без новой версии.

        Возвращает {'mode': 'superseded'|'edited', 'old_id', 'new_id'} или
        None, если заметки нет. Несовпадение ревизии — ValueError (409);
        попытка править устаревшую/отозванную строку — ValueError.
        """
        viewer_id=(viewer_id or '').strip() or None
        fact_key=(fact_key or '').strip().casefold() or None
        memory_type=normalize_memory_type(memory_type)
        importance=clamp_score(importance)
        source_kind=(source_kind or 'owner').strip()[:40] or 'owner'
        confidence=clamp_score(confidence, 1.0)
        result: dict[str, Any] | None = None
        with self.lock, self.db:
            row=self.db.execute('SELECT * FROM notes WHERE id=?',(note_id,)).fetchone()
            if not row: return None
            if row['revision']!=revision: raise ValueError('Заметка уже изменена. Обновите страницу и повторите.')
            current_status=row['status'] or 'active'
            if current_status in ('superseded','retracted'):
                pointer=row['superseded_by_id']
                raise ValueError('Эта версия устарела и не редактируется.'
                                 + (f' Актуальная версия — заметка #{pointer}.' if pointer else ''))
            now=time.time()
            content_changed = text!=row['body'] or fact_key!=(row['fact_key'] or None)
            if not content_changed:
                # Правка метаданных на месте: содержание и ключ не трогаем.
                self.db.execute('''INSERT OR IGNORE INTO note_versions(note_id,revision,body,viewer_id,fact_key,
                    memory_type,importance,source_kind,confidence,created) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (note_id,row['revision'],row['body'],row['viewer_id'],row['fact_key'],
                     row['memory_type'] or 'fact',safe_float(row['importance'], 0.5),row['source_kind'] or 'owner',
                     safe_float(row['confidence'], 1.0),row['created']))
                self.db.execute('''UPDATE notes SET viewer_id=?,memory_type=?,
                    importance=?,source_kind=?,confidence=?,revision=revision+1 WHERE id=?''',
                    (viewer_id,memory_type,importance,source_kind,confidence,note_id))
                self.db.execute('''INSERT OR REPLACE INTO note_versions(note_id,revision,body,viewer_id,fact_key,
                    memory_type,importance,source_kind,confidence,created) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (note_id,revision+1,row['body'],viewer_id,row['fact_key'],memory_type,importance,source_kind,confidence,now))
                result={'mode':'edited','old_id':note_id,'new_id':note_id}
            else:
                # Суперсессия. Порядок внутри транзакции важен: сначала старая
                # строка покидает статус active (иначе уникальный индекс
                # не пустит новую строку с тем же ключом факта).
                self.db.execute("UPDATE notes SET status='superseded',valid_to=? WHERE id=?",(now,note_id))
                cur=self.db.execute('''INSERT INTO notes(body,created,viewer_id,fact_key,memory_type,
                    importance,source_kind,confidence,revision,status,valid_from,valid_to,superseded_by_id)
                    VALUES(?,?,?,?,?,?,?,?,?,'active',?,NULL,NULL)''',
                    (text,now,viewer_id,fact_key,memory_type,importance,source_kind,confidence,row['revision']+1,now))
                new_id=int(cur.lastrowid or 0)
                # Прямая ссылка «старая → актуальная» (решение заказчика):
                # вопрос «что сейчас верно?» отвечает одним запросом.
                self.db.execute('UPDATE notes SET superseded_by_id=? WHERE id=?',(new_id,note_id))
                self.db.execute('''INSERT OR IGNORE INTO note_versions(note_id,revision,body,viewer_id,fact_key,
                    memory_type,importance,source_kind,confidence,created) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (note_id,row['revision'],row['body'],row['viewer_id'],row['fact_key'],
                     row['memory_type'] or 'fact',safe_float(row['importance'], 0.5),row['source_kind'] or 'owner',
                     safe_float(row['confidence'], 1.0),row['created']))
                self.db.execute('''INSERT OR REPLACE INTO note_versions(note_id,revision,body,viewer_id,fact_key,
                    memory_type,importance,source_kind,confidence,created) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (new_id,revision+1,text,viewer_id,fact_key,memory_type,importance,source_kind,confidence,now))
                result={'mode':'superseded','old_id':note_id,'new_id':new_id}
        self.rebuild_index()
        return result

    def notes(self, include_history: bool=False, limit: int=100) -> list[dict[str, Any]]:
        """M4: вернуть заметки; по умолчанию — только актуальные.

        include_history=True добавляет superseded и retracted строки
        (просмотр истории оператором). Сортировка — от новых к старым.
        """
        cap=max(1,min(int(limit),500 if include_history else 100))
        with self.lock:
            if include_history:
                rows=self.db.execute('''SELECT id,body,created,viewer_id,fact_key,revision,
                    memory_type,importance,source_kind,confidence,status,valid_from,valid_to,superseded_by_id
                    FROM notes ORDER BY id DESC LIMIT ?''',(cap,)).fetchall()
            else:
                rows=self.db.execute('''SELECT id,body,created,viewer_id,fact_key,revision,
                    memory_type,importance,source_kind,confidence,status,valid_from,valid_to,superseded_by_id
                    FROM notes WHERE status IN ('active','disputed') ORDER BY id DESC LIMIT ?''',(cap,)).fetchall()
            return [dict(r) for r in rows]

    def set_note_status(self, note_id: int, status: str) -> dict[str, Any] | None:
        """M4: ручная кураторская пометка оператора (решение заказчика).

        Разрешённые переходы: active→disputed (спорная пометка),
        active/disputed→retracted (мягкое отзывание), disputed/retracted→
        active (оператор передумал; valid_to сбрасывается). superseded не
        переводится в active вручную — у неё есть актуальная замена.
        """
        status=normalize_note_status(status)
        if status=='superseded':
            # Суперсессия — внутреннее состояние, которое устанавливает только
            # правка содержания; вручную переводить строки в него нельзя.
            raise ValueError('Статус superseded устанавливается автоматически при правке факта.')
        result: dict[str, Any] | None = None
        with self.lock, self.db:
            row=self.db.execute('SELECT status FROM notes WHERE id=?',(note_id,)).fetchone()
            if not row: return None
            current=row['status'] or 'active'
            if status==current:
                return {'mode':current,'changed':False}
            if status=='active':
                if current not in ('disputed','retracted'):
                    raise ValueError('Вернуть в актуальные можно только спорную или отозванную заметку.')
                self.db.execute('UPDATE notes SET status=?,valid_to=NULL WHERE id=?',(status,note_id))
            elif status=='disputed':
                if current!='active':
                    raise ValueError('Пометить спорной можно только актуальную заметку.')
                self.db.execute('UPDATE notes SET status=? WHERE id=?',(status,note_id))
            else:  # retracted
                if current not in RETRACTABLE:
                    raise ValueError('Эта заметка уже отозвана.')
                self.db.execute("UPDATE notes SET status='retracted',valid_to=? WHERE id=?",(time.time(),note_id))
            result={'mode':status,'changed':True,'note_id':note_id}
        self.rebuild_index()
        return result

    def note_chain(self, note_id: int, max_depth: int=50) -> list[dict[str, Any]]:
        """M4: версии цепочки суперсессий от любой её точки.

        Обход двусторонний: в прошлое (строки, для которых текущая заметка
        является заменой) и в будущее (по links superseded_by_id).
        Связь старая→новая делает прыжок к актуальной версии O(1); обход
        в прошлое покрывает кнопку «Просмотреть историю версий». Защита
        от циклов и ограничение глубины — обязательны.
        """
        with self.lock:
            nodes: dict[int, sqlite3.Row] = {}
            pending=[note_id]
            depth=0
            while pending and depth<max_depth:
                current=pending.pop(0); depth+=1
                if current in nodes: continue
                row=self.db.execute('SELECT * FROM notes WHERE id=?',(current,)).fetchone()
                if not row: continue
                nodes[current]=row
                pending.append(row['superseded_by_id'])  # в будущее
                for parent in self.db.execute('SELECT id FROM notes WHERE superseded_by_id=? ORDER BY id',(current,)).fetchall():
                    pending.append(parent['id'])  # в прошлое
            versions: list[dict[str, Any]] = []
            for node in nodes.values():
                status=node['status'] or 'active'
                found=False
                for v in self.db.execute('''SELECT revision,body,viewer_id,fact_key,memory_type,importance,
                    source_kind,confidence,created FROM note_versions WHERE note_id=? ORDER BY revision DESC''',(node['id'],)):
                    found=True
                    versions.append({'note_id':node['id'],'status':status,'superseded_by_id':node['superseded_by_id'],
                                     **dict(v)})
                if not found:
                    # Предмиграционная строка без истории: сама строка — версия.
                    versions.append({'note_id':node['id'],'status':status,'superseded_by_id':node['superseded_by_id'],
                                     'revision':node['revision'],'body':node['body'],'viewer_id':node['viewer_id'],
                                     'fact_key':node['fact_key'],'memory_type':node['memory_type'] or 'fact',
                                     'importance':safe_float(node['importance'],0.5),'source_kind':node['source_kind'] or 'owner',
                                     'confidence':safe_float(node['confidence'],1.0),'created':node['created']})
            versions.sort(key=lambda x: (-x.get('revision',0), x['note_id']))
            return versions

    def add_stream_memory(self, text: str, memory_type: str = 'fact',
                          fragment_kind: str = 'memory', importance: float = 0.8) -> int:
        """Добавить временный backstage-фрагмент в текущий эфир."""
        text=text.strip()
        fragment_kind=(fragment_kind or 'memory').strip().casefold()
        if fragment_kind not in {'memory','signal'}:
            fragment_kind='memory'
        with self.lock, self.db:
            cur=self.db.execute(
                '''INSERT INTO stream_memories(body,memory_type,fragment_kind,importance,created,active)
                   VALUES(?,?,?,?,?,1)''',
                (text,normalize_memory_type(memory_type),fragment_kind,clamp_score(importance,0.8),time.time())
            )
            return int(cur.lastrowid or 0)

    def stream_memories(self) -> list[dict[str, Any]]:
        """Вернуть активные фрагменты текущего эфира, новые первыми."""
        with self.lock:
            return [dict(r) for r in self.db.execute(
                '''SELECT id,body,memory_type,fragment_kind,importance,created
                   FROM stream_memories WHERE active=1 ORDER BY id DESC LIMIT 50'''
            )]

    def clear_stream_memories(self) -> int:
        """Завершить текущий эфир, не трогая постоянную память."""
        with self.lock, self.db:
            cur=self.db.execute('UPDATE stream_memories SET active=0 WHERE active=1')
            return int(cur.rowcount or 0)

    def delete_note(self, note_id: int, hard: bool=False) -> dict[str, Any] | None:
        """M4: по умолчанию — мягкое отзывание (решение заказчика).

        «Ничто не исчезает бесследно»: отзыв (status='retracted',
        valid_to=now) сохраняет строку и её историю; отозванная заметка
        исключается из поискового индекса. hard=True — физическое удаление
        строки и её note_versions (CASCADE) только для PII и явных
        нарушений правил платформы.
        """
        result: dict[str, Any] | None = None
        with self.lock, self.db:
            row=self.db.execute('SELECT status FROM notes WHERE id=?',(note_id,)).fetchone()
            if not row: return None
            if hard:
                # M4/M2: физическое удаление обязано работать и тогда, когда
                # другие строки цепочки ссылаются на удаляемую заметку через
                # superseded_by_id — иначе FK не пускает удаление PII.
                self.db.execute('UPDATE notes SET superseded_by_id=NULL WHERE superseded_by_id=?',(note_id,))
                if self.db.execute('DELETE FROM notes WHERE id=?',(note_id,)).rowcount>0:
                    result={'mode':'hard','note_id':note_id}
            else:
                current=row['status'] or 'active'
                if current=='retracted':
                    result={'mode':'already-retracted','note_id':note_id}
                else:
                    self.db.execute("UPDATE notes SET status='retracted',valid_to=? WHERE id=?",(time.time(),note_id))
                    result={'mode':'retracted','note_id':note_id}
        if result:
            self.rebuild_index()
        return result

    def stale_facts(self, query: str, matches: list[dict[str, Any]] | None=None,
                    viewer_id: str | None=None, limit: int=6) -> list[dict[str, Any]]:
        """M4: устаревшие факты, относящиеся к запросу (без поискового индекса).

        Два источника:
        1) предки в цепочках суперсессий среди найденных active-заметок —
           их тела образуют часть «Ранее было сказано…»;
        2) прямой лексический отбор по retracted-заметкам, которые
           исключены из обычного индекса (приватность отзыва), и по
           superseded, до которых цепочка не доходит.
        Отбор детерминированный (scoring.message_relates_to_fact), без
        эмбеддингов: смысловой поиск (M3) не затрагивается.
        """
        stale: dict[int, dict[str, Any]] = {}
        with self.lock:
            # 1) Цепочки найденных актуальных заметок.
            for item in matches or []:
                if item.get('kind')!='note': continue
                raw=str(item.get('id',''))
                if not raw.startswith('note:'): continue
                try: root=int(raw.split(':',1)[1])
                except ValueError: continue
                row=self.db.execute('SELECT * FROM notes WHERE id=?',(root,)).fetchone()
                seen: set[int]=set()
                while row is not None and row['id'] not in seen:
                    seen.add(row['id'])
                    parent=self.db.execute('SELECT * FROM notes WHERE superseded_by_id=? ORDER BY id LIMIT 1',(row['id'],)).fetchone()
                    if parent is not None and parent['status']=='superseded':
                        stale.setdefault(parent['id'], self._stale_record(parent))
                    row=parent
            # 2) Лексический отбор по всем устаревшим строкам.
            rows=self.db.execute('''SELECT * FROM notes WHERE status IN ('superseded','retracted')''').fetchall()
            for r in rows:
                if viewer_id and (r['viewer_id'] or '') not in ('', viewer_id):
                    continue
                if message_relates_to_fact(query, r['body'], r['fact_key']):
                    stale.setdefault(r['id'], self._stale_record(r))
                    if len(stale)>=limit*3: break
        ranked=sorted(stale.values(), key=lambda x: (-safe_float(x.get('importance'),0.5), -x['created']))[:limit]
        return ranked

    def _stale_record(self, row: sqlite3.Row) -> dict[str, Any]:
        """Собрать запись устаревшего факта с данными актуальной замены."""
        replacement=None
        if row['superseded_by_id']:
            nr=self.db.execute('SELECT id,body FROM notes WHERE id=?',(row['superseded_by_id'],)).fetchone()
            if nr is not None: replacement={'id':nr['id'],'body':nr['body']}
        return {'id':f'note:{row["id"]}','kind':'stale_fact','note_id':row['id'],'text':row['body'],
                'fact_key':row['fact_key'],'status':row['status'] or 'active',
                'superseded_by_id':row['superseded_by_id'],'valid_to':row['valid_to'],
                'created':row['created'],'viewer_id':row['viewer_id'] or '',
                'importance':safe_float(row['importance'],0.5),'replacement':replacement,
                'source':f'Заметка #{row["id"]} ({"заменена" if row["status"]=="superseded" else "отозвана"})'}

    def search(self, query: str, limit: int=5, viewer_id: str | None=None) -> list[dict[str, Any]]:
        """Словесный резервный поиск по разрешённым текущим записям."""
        from .semantic import lexical_rank
        with self.lock:
            records=[r for r in self._records() if viewer_id is None or not r.get('viewer_id') or r['viewer_id']==viewer_id]
            return lexical_rank(query,records,limit)

    def counts(self) -> dict[str, int]:
        """Вернуть количество записей по типам и статусам жизненного цикла."""
        with self.lock:
            type_rows=self.db.execute('SELECT memory_type,COUNT(*) AS count FROM notes GROUP BY memory_type').fetchall()
            status_rows=self.db.execute('SELECT status,COUNT(*) AS count FROM notes GROUP BY status').fetchall()
            return {'messages':self.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],
                    'viewers':self.db.execute('SELECT COUNT(*) FROM viewers').fetchone()[0],
                    'notes':self.db.execute('SELECT COUNT(*) FROM notes').fetchone()[0],
                    'active_notes':self.db.execute("SELECT COUNT(*) FROM notes WHERE status='active'").fetchone()[0],
                    'fts_notes':self.fts_count(),
                    'note_statuses':{r['status'] or 'active':r['count'] for r in status_rows},
                    'stream_memories':self.db.execute(
                        'SELECT COUNT(*) FROM stream_memories WHERE active=1').fetchone()[0],
                    'lore':len(self.lore_chunks()),
                    'memory_types':{r['memory_type'] or 'fact':r['count'] for r in type_rows}}

    def copy_database(self, destination: Path) -> None:
        """Создать согласованную копию SQLite, включая данные WAL."""
        with self.lock:
            target=sqlite3.connect(destination)
            try: self.db.backup(target)
            finally: target.close()
