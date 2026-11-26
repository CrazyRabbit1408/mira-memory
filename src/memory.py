"""Постоянные сообщения, заметки и отдельный тестовый поисковый индекс.

SQLite — источник истины; Chroma — восстанавливаемый индекс. Лор персонажа
индексируется только из отдельной папки. Полный авторский лор не индексируется.
"""
from __future__ import annotations
import hashlib
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
import chromadb
from chromadb.config import Settings
from .embeddings import stub_embedding, tokens
from .scoring import safe_float
from . import knowledge

MEMORY_TYPES = frozenset({
    'fact', 'preference', 'relationship', 'event',
    'promise', 'open_thread', 'belief', 'canon',
})

def normalize_memory_type(value: str | None) -> str:
    """Return a bounded type for structured memories."""
    value = (value or 'fact').strip().casefold()
    return value if value in MEMORY_TYPES else 'fact'

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
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS note_scope_key ON notes(COALESCE(viewer_id,''),fact_key) WHERE fact_key IS NOT NULL")
        # Эфирные фрагменты не должны неожиданно переживать полный перезапуск
        # панели: постоянный канон хранится в notes, а этот слой относится к
        # текущему эфиру.
        self.db.execute("UPDATE stream_memories SET active=0 WHERE active=1")
        with self.db: knowledge.sync_graph(self.db,self.lore)
        self.db.commit()
        self.client = chromadb.PersistentClient(path=str(self.data / 'chroma'),
            settings=Settings(anonymized_telemetry=False))


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
        notes = self.db.execute('''SELECT id,body,viewer_id,fact_key,revision,memory_type,
            importance,source_kind,confidence,created FROM notes''').fetchall()
        return self.lore_chunks() + knowledge.records(self.db) + [
            {'id':f'message:{r["id"]}', 'kind':'message', 'text':r['body'], 'source':f'Сообщение #{r["id"]}', 'viewer_id':r['viewer_id']} for r in messages
        ] + [{'id':f'note:{r["id"]}', 'kind':'note', 'text':r['body'], 'source':f'Заметка #{r["id"]}',
              'viewer_id':r['viewer_id'] or '', 'fact_key':r['fact_key'] or '', 'revision':r['revision'],
              'memory_type':r['memory_type'] or 'fact', 'importance':safe_float(r['importance'], 0.5),
              'source_kind':r['source_kind'] or 'owner', 'confidence':safe_float(r['confidence'], 1.0),
              'created':r['created']} for r in notes] + [
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
                importance,source_kind,confidence) VALUES(?,?,?,?,?,?,?,?)''',
                (text,now,viewer_id,fact_key,memory_type,importance,source_kind,confidence))
            note_id=int(cur.lastrowid or 0)
            self.db.execute('''INSERT INTO note_versions(note_id,revision,body,viewer_id,fact_key,
                memory_type,importance,source_kind,confidence,created) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (note_id,1,text,viewer_id,fact_key,memory_type,importance,source_kind,confidence,now))
        self.rebuild_index()
        return note_id

    def update_note(self, note_id: int, text: str, viewer_id: str | None, fact_key: str | None,
                    revision: int, memory_type: str = 'fact', importance: float = 0.5,
                    source_kind: str = 'owner', confidence: float = 1.0) -> bool:
        """Обновить заметку только если её версия ещё актуальна."""
        viewer_id=(viewer_id or '').strip() or None
        fact_key=(fact_key or '').strip().casefold() or None
        memory_type=normalize_memory_type(memory_type)
        importance=clamp_score(importance)
        source_kind=(source_kind or 'owner').strip()[:40] or 'owner'
        confidence=clamp_score(confidence, 1.0)
        with self.lock, self.db:
            row=self.db.execute('SELECT * FROM notes WHERE id=?',(note_id,)).fetchone()
            if not row: return False
            if row['revision']!=revision: raise ValueError('Заметка уже изменена. Обновите страницу и повторите.')
            self.db.execute('''INSERT OR IGNORE INTO note_versions(note_id,revision,body,viewer_id,fact_key,
                memory_type,importance,source_kind,confidence,created) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (note_id,row['revision'],row['body'],row['viewer_id'],row['fact_key'],
                 row['memory_type'] or 'fact',safe_float(row['importance'], 0.5),row['source_kind'] or 'owner',
                 safe_float(row['confidence'], 1.0),row['created']))
            self.db.execute('''UPDATE notes SET body=?,viewer_id=?,fact_key=?,memory_type=?,
                importance=?,source_kind=?,confidence=?,revision=revision+1 WHERE id=?''',
                (text,viewer_id,fact_key,memory_type,importance,source_kind,confidence,note_id))
            self.db.execute('''INSERT OR REPLACE INTO note_versions(note_id,revision,body,viewer_id,fact_key,
                memory_type,importance,source_kind,confidence,created) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (note_id,revision+1,text,viewer_id,fact_key,memory_type,importance,source_kind,confidence,time.time()))
        self.rebuild_index()
        return True

    def notes(self) -> list[dict[str, Any]]:
        """Вернуть заметки владельца."""
        with self.lock:
            return [dict(r) for r in self.db.execute('''SELECT id,body,created,viewer_id,fact_key,revision,
                memory_type,importance,source_kind,confidence FROM notes ORDER BY id DESC LIMIT 100''')]

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

    def delete_note(self, note_id: int) -> bool:
        """Удалить заметку и её копию из поискового индекса."""
        with self.lock, self.db:
            deleted=self.db.execute('DELETE FROM notes WHERE id=?',(note_id,)).rowcount > 0
        self.rebuild_index()
        return deleted

    def search(self, query: str, limit: int=5, viewer_id: str | None=None) -> list[dict[str, Any]]:
        """Словесный резервный поиск по разрешённым текущим записям."""
        from .semantic import lexical_rank
        with self.lock:
            records=[r for r in self._records() if viewer_id is None or not r.get('viewer_id') or r['viewer_id']==viewer_id]
            return lexical_rank(query,records,limit)

    def counts(self) -> dict[str, int]:
        """Вернуть количество записей по типам."""
        with self.lock:
            type_rows=self.db.execute('SELECT memory_type,COUNT(*) AS count FROM notes GROUP BY memory_type').fetchall()
            return {'messages':self.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],
                    'viewers':self.db.execute('SELECT COUNT(*) FROM viewers').fetchone()[0],
                    'notes':self.db.execute('SELECT COUNT(*) FROM notes').fetchone()[0],
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
