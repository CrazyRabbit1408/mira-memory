"""M1: честный rebuild_index — полная пересборка FTS5-индекса из notes.

Аудит P1: rebuild_index() раньше только считал записи (misleading-заглушка),
а после каждой мутации вызывался впустую. Теперь метод действительно
пересоздаёт производный индекс, горячие вызовы убраны (индекс ведут
триггеры), и есть кнопка «Перестроить поисковый индекс»
(POST /api/rebuild-index под admin_guard).
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.memory import MemoryManager
from src import semantic
from src.embeddings import stub_embedding


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


# --- 1. Требование заказчика: DROP FTS5 → rebuild → поиск работает ----------

def test_rebuild_recovers_after_fts_table_drop(memory):
    memory.add_note('Кумар собирает модели кораблей', None, 'хобби')
    memory.add_note('Алиса читает книги по ночам', 'alice', 'чтение')
    # Принудительное удаление индекса мимо API (симуляция внешнего вмешательства).
    memory.db.execute('DROP TABLE notes_fts')
    memory.db.commit()
    assert memory.fts_count() == 0
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        memory.search_notes('корабл')

    indexed = memory.rebuild_index()
    assert indexed == 2 == memory.counts()['notes'] == memory.fts_count()
    assert len(memory.search_notes('корабл')) == 1
    # Триггеры восстановлены: обычные правки снова синхронизируют индекс.
    memory.add_note('Кумар строит модели дирижаблей', None, 'летательные аппараты')
    assert memory.fts_count() == 3
    assert len(memory.search_notes('дирижабл')) == 1


def test_rebuild_recovers_after_manual_empty(memory):
    memory.add_note('Кумар собирает модели кораблей', None, 'хобби')
    memory.db.execute('DELETE FROM notes_fts')
    memory.db.commit()
    assert memory.search_notes('корабл') == []
    assert memory.rebuild_index() == 1
    assert len(memory.search_notes('корабл')) == 1


def test_rebuild_does_not_change_notes(memory):
    """Пересборка касается только производного индекса, не источника истины."""
    first = memory.add_note('Кумар собирает модели кораблей', None, 'хобби')
    memory.update_note(first, 'Кумар строит модели дирижаблей', None, 'хобби', 1)
    before = memory.notes(include_history=True)
    indexed = memory.rebuild_index()
    assert indexed == len(before)  # все строки, включая исторические
    assert memory.notes(include_history=True) == before


# --- 2. Гигиена горячих вызовов ---------------------------------------------

def test_mutations_do_not_call_full_rebuild(memory, monkeypatch):
    """Обычные правки полагаются на триггеры, а не на полную пересборку (M1)."""
    monkeypatch.setattr(MemoryManager, '_rebuild_fts',
                        lambda self: (_ for _ in ()).throw(AssertionError(
                            'полная пересборка не должна вызываться на горячем пути')))
    note_id = memory.add_note('Кумар собирает модели кораблей', None, 'хобби')
    step = memory.update_note(note_id, 'Кумар строит модели дирижаблей', None, 'хобби', 1)
    new_id = step['new_id']
    memory.set_note_status(new_id, 'disputed')
    memory.delete_note(new_id)           # мягкое отзывание
    memory.save_exchange('bob', 'привет', 'тестовый ответ')   # сообщения не в FTS
    # FTS при этом остаётся актуальным (триггеры).
    assert memory.fts_count() == memory.counts()['notes'] == 2
    assert memory.search_notes('дирижабл') == []  # отозвана — не в текущей правде


# --- 3. API и admin_guard ---------------------------------------------------

def test_api_rebuild_index_endpoint(client):
    client.post('/api/notes', json={'text': 'Кумар собирает модели кораблей'})
    r = client.post('/api/rebuild-index')
    assert r.status_code == 200
    data = r.json()
    assert data == {'indexed': 1, 'mode': 'fts5'}


def test_api_rebuild_index_requires_admin_token(client, monkeypatch):
    monkeypatch.setenv('KUMAR_ADMIN_TOKEN', 'secret-token')
    denied = client.post('/api/rebuild-index')
    assert denied.status_code == 401
    allowed = client.post('/api/rebuild-index', headers={'X-Admin-Token': 'secret-token'})
    assert allowed.status_code == 200 and allowed.json()['mode'] == 'fts5'


def test_api_status_shows_indexed_notes_after_rebuild(client):
    client.post('/api/notes', json={'text': 'Кумар собирает модели кораблей'})
    client.post('/api/rebuild-index')
    counts = client.get('/api/status').json()['counts']
    assert counts['fts_notes'] == 1


# --- 4. Совместимость с Этапом 0 и M4 ----------------------------------------

def test_lore_reindex_returns_honest_fts_count(client):
    """«Обновить индекс» в лоре теперь возвращает честное число проиндексированных заметок."""
    client.post('/api/notes', json={'text': 'Кумар собирает модели кораблей'})
    r = client.post('/api/lore/reindex')
    assert r.status_code == 200 and r.json()['indexed'] == 1
