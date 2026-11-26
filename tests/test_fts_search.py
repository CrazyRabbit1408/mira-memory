"""M2: полнотекстовый поиск по заметкам (FTS5) и его интеграция с M4.

Покрывает требования заказчика: виртуальную таблицу notes_fts
(body, fact_key, viewer_id; tokenizer=unicode61), миграцию существующих
данных, триггеры автосинхронизации, BM25-ранжирование, морфологию
русского языка, интеграцию с моделью конфликтов M4 (поиск только по
текущей правде, история — по include_history), API /api/notes/search,
производительность на 1000+ заметках (<100 мс) и обратимость
(tools/downgrade_m2.py). Реальный платный API не вызывается.
"""
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.memory import MemoryManager, SNIPPET_OPEN, SNIPPET_CLOSE
from src import semantic
from src.embeddings import stub_embedding
from tools.downgrade_m2 import downgrade


@pytest.fixture
def memory(tmp_path):
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай с мятой.', encoding='utf8')
    m = MemoryManager(tmp_path / 'data', lore)
    yield m
    m.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай с мятой.', encoding='utf8')
    monkeypatch.setattr(semantic, 'embed', lambda texts: [stub_embedding(x) for x in texts])
    app = create_app(tmp_path / 'data', lore, testing=True)
    with TestClient(app) as c:
        yield c


# --- 1. Миграция: индекс создаётся и заполняется -----------------------------

def test_fts_table_created_and_populated(memory):
    memory.add_note('Любимый напиток Кумара — облепиховый чай', None, 'напиток')
    memory.add_note('Кумар собирает модели кораблей', 'alice', 'хобби')
    assert memory.fts_count() == memory.counts()['notes'] == 2
    # Виртуальная таблица существует и возвращает тело заметки.
    row = memory.db.execute(
        "SELECT body FROM notes_fts WHERE notes_fts MATCH '\"облепих\"*'").fetchone()
    assert row is not None and 'облепиховый' in row['body']


def test_fts_migration_populates_existing_notes(tmp_path):
    """База, созданная ДО M2 (симуляция Этапа 0), получает заполненный индекс."""
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай.', encoding='utf8')
    data = tmp_path / 'data'
    m = MemoryManager(data, lore)
    m.add_note('Алиса пьёт облепиховый чай', 'alice', 'напиток')
    # Ручная имитация «базы до M2»: индекс ещё не знает о заметках.
    m.db.execute('DELETE FROM notes_fts')
    m.db.commit()
    m.close()
    m = MemoryManager(data, lore)  # миграция при старте
    assert m.fts_count() == 1
    found = m.search_notes('облепиховый', viewer_id='alice')
    assert len(found) == 1 and 'облепиховый' in found[0]['body']
    m.close()


def test_fts_migration_is_idempotent_on_reopen(tmp_path):
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай.', encoding='utf8')
    data = tmp_path / 'data'
    m = MemoryManager(data, lore)
    m.add_note('Кумар собирает модели кораблей', None, 'хобби')
    m.close()
    m = MemoryManager(data, lore)
    assert m.fts_count() == 1
    m.close()
    m = MemoryManager(data, lore)
    assert m.fts_count() == 1
    m.close()


def test_fts_auto_rebuild_on_desync(tmp_path):
    """Рассинхронизация (падение питания, внешние инструменты) чинится при старте."""
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай.', encoding='utf8')
    data = tmp_path / 'data'
    m = MemoryManager(data, lore)
    m.add_note('Кумар собирает модели кораблей', None, 'хобби')
    m.close()
    # Внешняя порча: одна строка индекса удалена мимо триггеров.
    db = sqlite3.connect(data / 'memory.sqlite3')
    db.execute("DELETE FROM notes_fts WHERE rowid=(SELECT MIN(id) FROM notes)")
    db.commit()
    db.close()
    m = MemoryManager(data, lore)
    assert m.fts_count() == 1  # миграция пересобрала индекс из notes
    assert len(m.search_notes('корабл')) == 1
    m.close()


# --- 2. Морфология русского языка -------------------------------------------

def test_morphology_declension_prefix(memory):
    """Префиксные запросы: «кумара» находит «Кумар», «корабли» — «кораблей»."""
    memory.add_note('Кумар собирает модели кораблей', None, 'хобби')
    assert len(memory.search_notes('кумара')) == 1
    assert len(memory.search_notes('корабли')) == 1
    assert len(memory.search_notes('кораблей')) == 1
    assert len(memory.search_notes('собирает модели')) == 1


def test_morphology_root_prefix(memory):
    """Трёхсимвольная основа: «люблю» находит «Любимый», «читал» — «читает»."""
    memory.add_note('Любимый напиток Кумара — облепиховый чай', None, 'напиток')
    memory.add_note('Алиса читает книги по ночам', 'alice', 'чтение')
    hits_love = memory.search_notes('люблю')
    assert len(hits_love) == 1 and 'Любимый' in hits_love[0]['body']
    hits_read = memory.search_notes('читал', viewer_id='alice')
    assert len(hits_read) == 1 and 'читает' in hits_read[0]['body']


def test_query_with_fts_operators_is_safe(memory):
    """Операторы FTS5 в пользовательском запросе не ломают поиск и не инъектируются."""
    memory.add_note('Кумар собирает модели кораблей', None, 'хобби')
    for query in ('" OR body MATCH \'', 'кораб* NOT кумар', 'NEAR(кораб кумар)',
                  '??? !!! ...', 'хобби:кумар', '"несбалансированная кавычка'):
        memory.search_notes(query)  # не должно подниматься исключение


def test_empty_query_returns_nothing(memory):
    memory.add_note('Кумар собирает модели кораблей', None, 'хобби')
    assert memory.search_notes('') == []
    assert memory.search_notes('   ') == []


# --- 3. BM25-ранжирование ----------------------------------------------------

def test_bm25_ranks_exact_match_above_partial(memory):
    """Заметка с большим числом точных совпадений — выше заметки с частичным."""
    memory.add_note('Кумар собирает модели кораблей', None, 'хобби')
    for i in range(5):
        memory.add_note(f'Заметка про кумовство, тема {i} не про корабли вообще', None, None)
    memory.add_note('Модель корабля стоит на полке', None, None)
    hits = memory.search_notes('кумар корабл', limit=10)
    assert hits[0]['body'] == 'Кумар собирает модели кораблей'
    assert hits[0]['fts_rank'] < 0  # bm25: меньше — лучше


def test_search_limit_and_order(memory):
    for i in range(30):
        memory.add_note(f'Однокоренное слово корабль номер {i}', None, None)
    hits = memory.search_notes('корабль', limit=5)
    assert len(hits) == 5


# --- 4. Триггеры синхронизации ----------------------------------------------

def test_triggers_keep_index_in_sync(memory):
    note_id = memory.add_note('Кумар собирает модели кораблей', None, 'хобби')
    # INSERT
    assert len(memory.search_notes('корабл')) == 1
    # UPDATE (суперсессия создаёт новую строку, триггер её индексирует)
    memory.update_note(note_id, 'Кумар строит модели дирижаблей', None, 'хобби', 1)
    assert memory.search_notes('корабл') == []
    assert len(memory.search_notes('дирижабл')) == 1
    # UPDATE на месте (метаданные) не теряет тело из индекса
    new_id = memory.search_notes('дирижабл')[0]['id']
    memory.update_note(new_id, 'Кумар строит модели дирижаблей', None, 'хобби', 2,
                       importance=0.9)
    assert len(memory.search_notes('дирижабл')) == 1
    # DELETE (физическое удаление) убирает строку из индекса
    memory.delete_note(new_id, hard=True)
    assert memory.search_notes('дирижабл') == []
    assert memory.fts_count() == memory.counts()['notes']


def test_trigger_metadata_edit_keeps_searchable(memory):
    """Правка важности на месте (M4: без новой версии) не выбрасывает заметку из индекса."""
    note_id = memory.add_note('Кумар собирает модели кораблей', None, 'хобби', importance=0.5)
    memory.update_note(note_id, 'Кумар собирает модели кораблей', None, 'хобби', 1,
                       importance=0.9)
    hits = memory.search_notes('корабл')
    assert len(hits) == 1 and hits[0]['importance'] == 0.9


def test_trigger_viewer_change_updates_scope(memory):
    """Перевод заметки на другого зрителя (in-place) меняет область видимости поиска."""
    note_id = memory.add_note('Кумар собирает модели кораблей', None, 'хобби')
    memory.update_note(note_id, 'Кумар собирает модели кораблей', 'bob', 'хобби', 1)
    assert memory.search_notes('корабл', viewer_id='alice') == []
    assert len(memory.search_notes('корабл', viewer_id='bob')) == 1


# --- 5. Интеграция с M4 ------------------------------------------------------

def test_m4_superseded_hidden_by_default(memory):
    first = memory.add_note('Кумар собирает модели кораблей', None, 'хобби')
    memory.update_note(first, 'Кумар строит модели дирижаблей', None, 'хобби', 1)
    assert memory.search_notes('корабл') == []            # старая версия не «текущая правда»
    history = memory.search_notes('корабл', include_history=True)
    assert len(history) == 1 and history[0]['status'] == 'superseded'
    assert history[0]['superseded_by_id'] is not None


def test_m4_retracted_hidden_until_history(memory):
    note_id = memory.add_note('Секретная заметка про жетон', None, 'жетон')
    memory.delete_note(note_id)  # мягкое отзывание
    assert memory.search_notes('жетон') == []
    history = memory.search_notes('жетон', include_history=True)
    assert len(history) == 1 and history[0]['status'] == 'retracted'


def test_m4_disputed_still_searchable(memory):
    """Спор не инвалидирует факт: disputed остаётся текущей правдой (M4)."""
    note_id = memory.add_note('Кумар собирает модели кораблей', None, 'хобби')
    memory.set_note_status(note_id, 'disputed')
    hits = memory.search_notes('корабл')
    assert len(hits) == 1 and hits[0]['status'] == 'disputed'


def test_viewer_scoping_alien_notes_invisible(memory):
    memory.add_note('Секрет Алисы: любит облепиховый чай', 'alice', 'напиток')
    memory.add_note('Общая заметка про чаепитие Кумара', None, None)
    own = memory.search_notes('чай', viewer_id='alice')
    assert {x['viewer_id'] for x in own} <= {None, '', 'alice'}
    other = memory.search_notes('облепих', viewer_id='bob')
    assert other == []  # чужая заметка не выдаётся


def test_snippet_markers_around_matches(memory):
    memory.add_note('Любимый напиток Кумара — облепиховый чай', None, 'напиток')
    hit = memory.search_notes('кумара')[0]
    assert SNIPPET_OPEN in hit['snippet'] and SNIPPET_CLOSE in hit['snippet']
    assert hit['snippet'].count(SNIPPET_OPEN) == hit['snippet'].count(SNIPPET_CLOSE)


# --- 6. API ------------------------------------------------------------------

def test_api_notes_search_endpoint(client):
    client.post('/api/notes', json={'text': 'Кумар собирает модели кораблей', 'fact_key': 'хобби'})
    client.post('/api/notes', json={'text': 'Алиса читает книги по ночам',
                                    'viewer_id': 'alice', 'fact_key': 'чтение'})
    r = client.get('/api/notes/search', params={'q': 'корабли'})
    assert r.status_code == 200
    data = r.json()
    assert data['mode'] == 'fts5' and data['count'] == 1
    assert data['query'] == 'корабли'
    assert data['results'][0]['fact_key'] == 'хобби'
    assert SNIPPET_OPEN in data['results'][0]['snippet']
    # Морфология через API
    assert client.get('/api/notes/search', params={'q': 'корабля'}).json()['count'] == 1
    # M4-интеграция
    client.put('/api/notes/1', json={'text': 'Кумар строит дирижабли', 'fact_key': 'хобби',
                                     'revision': 1, 'memory_type': 'fact', 'importance': 0.5})
    assert client.get('/api/notes/search', params={'q': 'корабли'}).json()['count'] == 0
    hist = client.get('/api/notes/search',
                      params={'q': 'корабли', 'include_history': 'true'}).json()
    assert hist['count'] == 1 and hist['results'][0]['status'] == 'superseded'


def test_api_notes_search_no_admin_token_needed(client, monkeypatch):
    """Только чтение — без admin-токена, как /api/search (совместимость C4)."""
    monkeypatch.setenv('KUMAR_ADMIN_TOKEN', 'secret-token')
    client.post('/api/notes', json={'text': 'Кумар собирает модели кораблей'},
                headers={'X-Admin-Token': 'secret-token'})
    r = client.get('/api/notes/search', params={'q': 'корабл'})
    assert r.status_code == 200 and r.json()['count'] == 1


def test_api_notes_search_limit_and_viewer(client):
    for i in range(7):
        client.post('/api/notes', json={'text': f'корабль номер {i}'})
    data = client.get('/api/notes/search', params={'q': 'корабль', 'limit': 3}).json()
    assert data['count'] == 3
    client.post('/api/notes', json={'text': 'личная заметка bob про чай', 'viewer_id': 'bob'})
    assert client.get('/api/notes/search',
                      params={'q': 'чай', 'viewer_id': 'alice'}).json()['count'] == 0


def test_status_reports_fts_count(client):
    client.post('/api/notes', json={'text': 'Кумар собирает модели кораблей'})
    counts = client.get('/api/status').json()['counts']
    assert counts['fts_notes'] == counts['notes'] == 1


# --- 7. Производительность ---------------------------------------------------

def test_performance_1000_notes_under_100ms(tmp_path):
    """Требование заказчика: поиск на 1000+ заметках быстрее 100 мс."""
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай.', encoding='utf8')
    m = MemoryManager(tmp_path / 'data', lore)
    now = time.time()
    with m.lock, m.db:
        rows = [(f'Заметка Кумара номер {i} про корабль и облепиховый чай с мятой',
                 now + i, None if i % 3 else 'alice', f'заметка-{i}' if i % 2 else None,
                 'fact', 0.5, 'owner', 1.0, 'active', now + i, None, None)
                for i in range(1200)]
        m.db.executemany('''INSERT INTO notes(body,created,viewer_id,fact_key,memory_type,
            importance,source_kind,confidence,status,valid_from,valid_to,superseded_by_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', rows)
        m.db.executemany('''INSERT INTO notes_fts(rowid,body,fact_key,viewer_id)
            SELECT id,body,COALESCE(fact_key,''),COALESCE(viewer_id,'') FROM notes''', [])
    assert m.fts_count() == 1200
    try:
        # Холодный и повторный запросы — каждый быстрее 100 мс.
        for query in ('корабл чай', 'кумара облепиховый', 'заметка номер 999'):
            start = time.perf_counter()
            hits = m.search_notes(query, limit=20)
            elapsed = time.perf_counter() - start
            assert hits, f'запрос {query!r} должен что-то находить'
            assert elapsed < 0.1, f'поиск {query!r} занял {elapsed*1000:.1f} мс'
    finally:
        m.close()


# --- 8. Downgrade ------------------------------------------------------------

def test_downgrade_m2_drops_fts_and_preserves_notes(tmp_path):
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай.', encoding='utf8')
    data = tmp_path / 'data'
    m = MemoryManager(data, lore)
    m.add_note('Кумар собирает модели кораблей', None, 'хобби')
    m.close()
    db_path = data / 'memory.sqlite3'

    dry = downgrade(db_path, yes=False)
    assert dry['executed'] is False and dry['fts_table'] is True
    result = downgrade(db_path, yes=True)
    assert result['executed'] is True and Path(result['backup']).is_file()

    db = sqlite3.connect(db_path)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'notes_fts' not in tables
    triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {'notes_fts_ai', 'notes_fts_ad', 'notes_fts_au'} & triggers == set()
    assert db.execute('SELECT body FROM notes').fetchone()[0] == 'Кумар собирает модели кораблей'
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    db.close()
    # Повторный запуск — честный no-op.
    assert downgrade(db_path, yes=True)['already_downgraded'] is True
    # Откаченная база снова открывается приложением: индекс пересоздаётся.
    m = MemoryManager(data, lore)
    assert m.fts_count() == 1
    assert len(m.search_notes('корабл')) == 1
    m.close()
