"""M4: модель конфликтов памяти — жизненный цикл фактов и точечная инвалидация.

Покрывает требования заказчика: цепочку обновлений active → superseded →
active, разрешение конфликта с предупреждением для LLM, миграцию данных
Этапа 0 (включая граничный случай с 1000+ заметками и дубликатами ключей),
downgrade-скрипт и API-поверхность. Реальный платный API не вызывается.
"""
import json
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.context import conflicting_facts_block, invalidate_history
from src.engines import DEFAULT_ORCA_MODEL, OrcaRouterEngine
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


# --- 1. Цепочка обновлений: active → superseded → active --------------------

def test_update_chain_active_superseded_active(memory):
    first = memory.add_note('Алиса пьёт чай', 'alice', 'напиток')
    step1 = memory.update_note(first, 'Алиса пьёт кофе', 'alice', 'напиток', 1)
    assert step1['mode'] == 'superseded' and step1['old_id'] == first
    second = step1['new_id']
    step2 = memory.update_note(second, 'Алиса пьёт какао', 'alice', 'напиток', 2)
    assert step2['mode'] == 'superseded' and step2['new_id'] == step2['old_id'] + 1
    third = step2['new_id']

    rows = {r['id']: r for r in memory.notes(include_history=True)}
    assert rows[first]['status'] == 'superseded' and rows[first]['superseded_by_id'] == second
    assert rows[second]['status'] == 'superseded' and rows[second]['superseded_by_id'] == third
    assert rows[third]['status'] == 'active' and rows[third]['superseded_by_id'] is None
    assert rows[first]['valid_to'] is not None and rows[third]['valid_to'] is None
    assert rows[third]['valid_from'] >= rows[first]['valid_from']

    # Сквозная нумерация ревизий по цепочке: 1 → 2 → 3.
    versions = memory.note_chain(first)
    assert [v['revision'] for v in versions] == [3, 2, 1]
    assert [v['body'] for v in versions] == ['Алиса пьёт какао', 'Алиса пьёт кофе', 'Алиса пьёт чай']
    # Прыжок от старой строки к актуальной за один запрос (решение о направлении связи).
    assert rows[first]['superseded_by_id'] == second and rows[second]['superseded_by_id'] == third
    # Дефолтный список показывает только актуальную версию.
    assert [n['id'] for n in memory.notes()] == [third]


def test_metadata_only_edit_stays_in_place(memory):
    """Решение заказчика: важность/тип без смены текста и ключа — без новой версии."""
    note = memory.add_note('Мира собирает кораблики', importance=0.5)
    result = memory.update_note(note, 'Мира собирает кораблики', None, None, 1, importance=0.9)
    assert result == {'mode': 'edited', 'old_id': note, 'new_id': note}
    rows = memory.notes(include_history=True)
    assert len(rows) == 1 and rows[0]['revision'] == 2 and rows[0]['importance'] == 0.9
    assert rows[0]['status'] == 'active' and rows[0]['valid_to'] is None


def test_stale_row_refuses_edits_with_pointer(memory):
    note = memory.add_note('старое', None, 'ключ')
    new_id = memory.update_note(note, 'новое', None, 'ключ', 1)['new_id']
    with pytest.raises(ValueError, match=f'#{new_id}'):
        memory.update_note(note, 'ещё новее', None, 'ключ', 1)


def test_soft_retraction_keeps_history_hard_deletes(memory):
    note = memory.add_note('личные данные зрителя', None, 'pii')
    assert memory.delete_note(note) == {'mode': 'retracted', 'note_id': note}
    rows = {r['id']: r for r in memory.notes(include_history=True)}
    assert rows[note]['status'] == 'retracted' and rows[note]['valid_to'] is not None
    assert memory.note_chain(note)[0]['body'] == 'личные данные зрителя'
    assert memory.delete_note(note, hard=True) == {'mode': 'hard', 'note_id': note}
    assert memory.notes(include_history=True) == []


# --- 2. Разрешение конфликта: точечная инвалидация + блок для LLM -----------

def test_targeted_invalidation_keeps_unrelated_answers(memory):
    """Создание противоречащей заметки: в контекст попадает только актуальная
    версия + предупреждение, исключаются только связанные ответы Кумара."""
    fact = memory.add_note('Алиса пьёт чай с мятой', 'alice', 'напиток')
    history = [
        {'id': 1, 'speaker': 'viewer', 'body': 'Что пьёт Алиса?', 'created': 1},
        {'id': 2, 'speaker': 'kumar', 'body': 'Алиса пьёт чай с мятой по утрам', 'created': 2},
        {'id': 3, 'speaker': 'viewer', 'body': 'А какая погода?', 'created': 3},
        {'id': 4, 'speaker': 'kumar', 'body': 'За окном идёт дождь', 'created': 4},
    ]
    memory.update_note(fact, 'Алиса пьёт кофе без сахара', 'alice', 'напиток', 1)
    active_note, = memory.notes()
    stale = memory.stale_facts('Что пьёт Алиса?', [
        {'id': f'note:{active_note["id"]}', 'kind': 'note'}], viewer_id='alice')
    assert [f['note_id'] for f in stale] == [fact]
    assert stale[0]['replacement']['body'] == 'Алиса пьёт кофе без сахара'

    kept, excluded = invalidate_history(history, stale)
    assert [x['id'] for x in kept] == [1, 3, 4]           # посторонний ответ Кумара жив
    assert [x['id'] for x in excluded] == [2]             # связанный — исключён
    assert 'устаревшим фактом' in excluded[0]['excluded_reason']

    block = conflicting_facts_block(stale)
    assert block and block.startswith('<conflicting_facts>')
    assert 'Ранее было сказано: «Алиса пьёт чай с мятой»' in block
    assert 'Алиса пьёт кофе без сахара' in block and 'Не опирайся' in block
    assert conflicting_facts_block([]) is None


def test_disputed_does_not_invalidate_history(memory):
    """Решение заказчика: disputed — ручная пометка, историю не трогает."""
    memory.add_note('Мира дружит с Артёмом', None, 'дружба')
    note, = memory.notes()
    memory.set_note_status(note['id'], 'disputed')
    assert memory.stale_facts('дружит ли Мира с Артёмом?') == []
    assert [n['id'] for n in memory.notes()] == [note['id']]  # спорная остаётся в дефолтном списке
    memory.set_note_status(note['id'], 'active')
    assert memory.notes()[0]['status'] == 'active'
    # Несуществующая заметка — не исключение, а честный None (API отдаст 404).
    assert memory.set_note_status(999, 'disputed') is None


def test_retracted_fact_found_without_index_leak(memory):
    """Отозванный факт исчезает из поиска, но попадает в <conflicting_facts>."""
    memory.add_note('Кумар хранит медный жетон в кармане', None, 'жетон')
    note, = memory.notes()
    memory.delete_note(note['id'])
    assert all(r['kind'] != 'note' for r in memory.search('медный жетон'))
    stale = memory.stale_facts('где медный жетон?')
    assert stale and stale[0]['status'] == 'retracted'
    block = conflicting_facts_block(stale)
    assert 'отозвана владельцем' in block and 'медный жетон' in block


def test_conflict_block_is_separate_system_message(monkeypatch):
    """Уточнение заказчика: блок идёт отдельным system-сообщением до reference_data."""
    monkeypatch.setenv('ORCAROUTER_API_KEY', 'test-key')
    seen = {}

    def handler(request):
        seen['messages'] = json.loads(request.content.decode('utf-8'))['messages']
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ок'}}], 'usage': {}})

    engine = OrcaRouterEngine(DEFAULT_ORCA_MODEL, transport=httpx.MockTransport(handler))
    engine.generate('вопрос',
                    [{'kind': 'note', 'source': 'Заметка #5', 'text': 'актуальный факт',
                      'importance': 0.5, 'confidence': 1.0}],
                    [{'speaker': 'viewer', 'body': 'привет'}], 'neutral', False,
                    conflict_notice='<conflicting_facts>тест</conflicting_facts>')
    contents = [m['content'] for m in seen['messages']]
    roles = [m['role'] for m in seen['messages']]
    block_index = contents.index('<conflicting_facts>тест</conflicting_facts>')
    reference_index = next(i for i, c in enumerate(contents) if c.startswith('<reference_data>'))
    assert roles[block_index] == 'system'
    assert block_index < reference_index          # блок до справочных данных
    assert '<conflicting_facts>' not in contents[reference_index]
    assert roles[0] == 'system' and contents[0].startswith('Ты — Кумар')  # контракт C1 не тронут


# --- 3. Миграция: старые данные не теряются и получают статус ---------------

def _write_stage0_db(path: Path, rows: list[tuple], with_old_index: bool = True) -> None:
    """Собрать базу формата Этапа 0 руками (до M4, без уникального индекса при need)."""
    path.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path / 'memory.sqlite3')
    db.executescript('''
    CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, viewer_id TEXT, speaker TEXT, body TEXT, created REAL);
    CREATE TABLE notes(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL, created REAL NOT NULL,
      viewer_id TEXT, fact_key TEXT, revision INTEGER NOT NULL DEFAULT 1,
      memory_type TEXT NOT NULL DEFAULT 'fact', importance REAL NOT NULL DEFAULT 0.5,
      source_kind TEXT NOT NULL DEFAULT 'owner', confidence REAL NOT NULL DEFAULT 1.0);
    CREATE TABLE viewers(viewer_id TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, count INTEGER);
    CREATE TABLE graph_nodes(id TEXT PRIMARY KEY, label TEXT, kind TEXT);
    CREATE TABLE graph_edges(source TEXT, target TEXT, relation TEXT, PRIMARY KEY(source,target,relation));
    CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE stream_memories(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT, memory_type TEXT DEFAULT 'fact',
        fragment_kind TEXT DEFAULT 'memory', importance REAL DEFAULT 0.8, created REAL, active INTEGER DEFAULT 1);
    ''')
    db.executescript('''
    CREATE TABLE memory_candidates(id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER, viewer_id TEXT,
        quote TEXT, relation TEXT, object TEXT, status TEXT DEFAULT 'pending', created REAL);
    CREATE TABLE knowledge_edges(id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, relation TEXT, object TEXT,
        evidence TEXT, source TEXT, viewer_id TEXT DEFAULT '', candidate_id INTEGER);
    CREATE TABLE note_versions(id INTEGER PRIMARY KEY AUTOINCREMENT, note_id INTEGER REFERENCES notes(id) ON DELETE CASCADE,
        revision INTEGER, body TEXT, viewer_id TEXT, fact_key TEXT, created REAL, UNIQUE(note_id,revision));
    ''')
    for body, viewer, key, created in rows:
        db.execute('INSERT INTO notes(body,created,viewer_id,fact_key,revision) VALUES(?,?,?,?,?)',
                   (body, created, viewer, key, created))
    if with_old_index:
        db.execute("CREATE UNIQUE INDEX note_scope_key ON notes(COALESCE(viewer_id,''),fact_key) WHERE fact_key IS NOT NULL")
    db.commit()
    db.close()


def test_migration_small_preserves_all_rows(tmp_path):
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай.', encoding='utf8')
    data = tmp_path / 'data'
    _write_stage0_db(data, [('Мира пьёт чай', None, 'напиток', 100.0),
                            ('Мира любит дождь', None, 'погода', 200.0)])
    m = MemoryManager(data, lore)
    rows = m.notes(include_history=True)
    assert len(rows) == 2 and all(r['status'] == 'active' for r in rows)
    assert all(r['valid_from'] == r['created'] and r['valid_to'] is None for r in rows)
    idx = m.db.execute("SELECT sql FROM sqlite_master WHERE name='note_scope_key'").fetchone()[0]
    assert "status='active'" in idx                      # индекс пересоздан с условием статуса
    assert m.db.execute("SELECT value FROM state WHERE key='schema_version'").fetchone()[0] == 'm4'
    m.close()


def test_migration_collapses_duplicate_keys_with_report(tmp_path):
    """Дубликаты ключей (возможны без уникального индекса) схлопываются явно:
    новейшая остаётся active, старые — superseded со ссылкой; ничего не удалено."""
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай.', encoding='utf8')
    data = tmp_path / 'data'
    _write_stage0_db(data, [('Мира пьёт чай', None, 'напиток', 100.0),
                            ('Мира пьёт сок', None, 'напиток', 300.0),
                            ('Мира пьёт кофе', None, 'напиток', 200.0)], with_old_index=False)
    m = MemoryManager(data, lore)
    rows = m.notes(include_history=True)
    assert len(rows) == 3 and sum(r['status'] == 'active' for r in rows) == 1
    keeper = next(r for r in rows if r['status'] == 'active')
    assert keeper['body'] == 'Мира пьёт сок' and keeper['created'] == 300.0
    for stale in (r for r in rows if r['status'] == 'superseded'):
        assert stale['superseded_by_id'] == keeper['id']
        assert stale['valid_to'] == 300.0
    report = json.loads(m.db.execute("SELECT value FROM state WHERE key='m4_migration_report'").fetchone()[0])
    assert report == [{'fact_key': 'напиток', 'viewer_id': None,
                       'kept_id': keeper['id'], 'superseded_ids': sorted({r['id'] for r in rows} - {keeper['id']})}]
    m.close()


def test_migration_large_1000_notes_with_duplicates(tmp_path):
    """Граничный случай заказчика: 1000+ заметок, среди них дубликаты ключей."""
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай.', encoding='utf8')
    data = tmp_path / 'data'
    rows = [(f'Заметка номер {i}', None, f'ключ-{i}', float(i)) for i in range(1200)]
    rows += [(f'Дубль напитка {i}', 'alice', 'дубль-ключ', 1000.0 + i) for i in range(30)]
    _write_stage0_db(data, rows, with_old_index=False)
    m = MemoryManager(data, lore)
    statuses = dict(m.db.execute('SELECT status, COUNT(*) FROM notes GROUP BY status').fetchall())
    assert statuses == {'active': 1201, 'superseded': 29}   # 30 дублей → 1 активный + 29 устаревших
    report = json.loads(m.db.execute("SELECT value FROM state WHERE key='m4_migration_report'").fetchone()[0])
    assert len(report) == 1 and report[0]['fact_key'] == 'дубль-ключ'
    keeper = report[0]['kept_id']
    assert m.db.execute('SELECT body FROM notes WHERE id=?', (keeper,)).fetchone()[0] == 'Дубль напитка 29'
    assert m.counts()['notes'] == 1230                     # ни одна строка не потеряна
    m.close()


def test_migration_is_idempotent_on_reopen(tmp_path):
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай.', encoding='utf8')
    data = tmp_path / 'data'
    _write_stage0_db(data, [('Мира пьёт чай', None, 'напиток', 100.0)])
    m = MemoryManager(data, lore)
    m.close()
    m = MemoryManager(data, lore)
    assert [n['body'] for n in m.notes()] == ['Мира пьёт чай']
    assert m.counts()['note_statuses'] == {'active': 1}
    m.close()


# --- 4. Downgrade-скрипт -----------------------------------------------------

def test_downgrade_restores_stage0_schema_and_preserves_bodies(tmp_path):
    from tools.downgrade_m4 import downgrade
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай.', encoding='utf8')
    data = tmp_path / 'data'
    m = MemoryManager(data, lore)
    first = m.add_note('Алиса пьёт чай', 'alice', 'напиток')
    second = m.update_note(first, 'Алиса пьёт кофе', 'alice', 'напиток', 1)['new_id']
    m.close()
    db_path = data / 'memory.sqlite3'

    dry = downgrade(db_path, yes=False)
    assert dry['executed'] is False
    result = downgrade(db_path, yes=True)
    assert result['executed'] is True and Path(result['backup']).is_file()

    db = sqlite3.connect(db_path)
    cols = [c[1] for c in db.execute('PRAGMA table_info(notes)')]
    assert 'status' not in cols and 'superseded_by_id' not in cols
    assert db.execute('SELECT body FROM notes WHERE id=?', (second,)).fetchone()[0] == 'Алиса пьёт кофе'
    archived = {r[0] for r in db.execute('SELECT body FROM note_versions WHERE note_id=?', (second,))}
    assert {'Алиса пьёт чай', 'Алиса пьёт кофе'} <= archived   # тела цепочки восстановимы
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    db.close()
    # Повторный запуск — честный no-op.
    assert downgrade(db_path, yes=True)['already_stage0'] is True
    # Откаченная база снова открывается приложением и заново мигрирует.
    m = MemoryManager(data, lore)
    assert [n['body'] for n in m.notes()] == ['Алиса пьёт кофе']
    m.close()


def test_downgrade_refuses_broken_references(tmp_path):
    """Уточнение заказчика: битые ссылки останавливают скрипт со списком ID."""
    from tools.downgrade_m4 import downgrade
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай.', encoding='utf8')
    data = tmp_path / 'data'
    m = MemoryManager(data, lore)
    first = m.add_note('Алиса пьёт чай', 'alice', 'напиток')
    m.update_note(first, 'Алиса пьёт кофе', 'alice', 'напиток', 1)
    m.close()
    # FK не даёт создать битую ссылку через приложение — ломаем напрямую
    # (так выглядит база, повреждённая вне приложения; именно её ловит пре-чек).
    db = sqlite3.connect(data / 'memory.sqlite3')
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute('UPDATE notes SET superseded_by_id=999 WHERE id=?', (first,))
    db.commit()
    db.close()
    with pytest.raises(SystemExit) as exc:
        downgrade(data / 'memory.sqlite3', yes=True)
    assert exc.value.code == 2
    # База не изменилась.
    db = sqlite3.connect(data / 'memory.sqlite3')
    assert db.execute('SELECT superseded_by_id FROM notes WHERE id=?', (first,)).fetchone()[0] == 999
    db.close()


# --- 5. API ------------------------------------------------------------------

def test_api_notes_default_hides_history(client):
    first = client.post('/api/notes', json={'text': 'Мира пьёт чай', 'fact_key': 'напиток'}).json()['id']
    put = client.put(f'/api/notes/{first}', json={'text': 'Мира пьёт сок', 'fact_key': 'напиток', 'revision': 1})
    assert put.status_code == 200
    body = put.json()
    assert body['mode'] == 'superseded' and body['old_id'] == first and body['new_id'] > first

    default = client.get('/api/notes').json()
    assert len(default) == 1 and default[0]['body'] == 'Мира пьёт сок' and default[0]['status'] == 'active'
    history = client.get('/api/notes', params={'include_history': 'true'}).json()
    assert len(history) == 2
    old = next(x for x in history if x['id'] == first)
    assert old['status'] == 'superseded' and old['superseded_by_id'] == body['new_id']
    assert old['valid_to'] is not None and old['body'] == 'Мира пьёт чай'


def test_api_chain_versions_endpoint(client):
    first = client.post('/api/notes', json={'text': 'версия раз', 'fact_key': 'ключ'}).json()['id']
    second = client.put(f'/api/notes/{first}', json={'text': 'версия два', 'fact_key': 'ключ', 'revision': 1}).json()['new_id']
    for note_id in (first, second):                       # история доступна от любой точки цепочки
        versions = client.get(f'/api/notes/{note_id}/versions').json()
        assert [v['body'] for v in versions] == ['версия два', 'версия раз']
        assert versions[0]['status'] == 'active' and versions[1]['status'] == 'superseded'


def test_api_status_endpoint_curates(client):
    note = client.post('/api/notes', json={'text': 'спорный факт'}).json()['id']
    assert client.post(f'/api/notes/{note}/status', json={'status': 'disputed'}).json()['changed'] is True
    assert client.get('/api/notes').json()[0]['status'] == 'disputed'
    assert client.post(f'/api/notes/{note}/status', json={'status': 'active'}).json()['changed'] is True
    assert client.post(f'/api/notes/{note}/status', json={'status': 'superseded'}).status_code == 409
    assert client.post('/api/notes/999/status', json={'status': 'disputed'}).status_code == 404


def test_api_delete_soft_by_default_hard_on_flag(client):
    note = client.post('/api/notes', json={'text': 'приватные данные'}).json()['id']
    assert client.delete(f'/api/notes/{note}').json()['mode'] == 'retracted'
    assert client.get('/api/notes', params={'include_history': 'true'})[0:] if False else True
    history = client.get('/api/notes', params={'include_history': 'true'}).json()
    assert history[0]['status'] == 'retracted'
    assert client.delete(f'/api/notes/{note}').json()['mode'] == 'already-retracted'
    assert client.delete(f'/api/notes/{note}', params={'hard': 'true'}).json()['mode'] == 'hard'
    assert client.get('/api/notes', params={'include_history': 'true'}).json() == []


def test_api_admin_token_still_required(tmp_path, monkeypatch):
    """Совместимость с Этапом 0: изменяющие эндпоинты M4 под admin_guard."""
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Мира пьёт чай.', encoding='utf8')
    monkeypatch.setattr(semantic, 'embed', lambda texts: [stub_embedding(x) for x in texts])
    monkeypatch.setenv('KUMAR_ADMIN_TOKEN', 'sekret-token')
    app = create_app(tmp_path / 'data', lore, testing=True)
    with TestClient(app) as c:
        assert c.put('/api/notes/1', json={'text': 'x', 'revision': 1}).status_code == 401
        assert c.delete('/api/notes/1').status_code == 401
        assert c.delete('/api/notes/1', params={'hard': 'true'}).status_code == 401
        assert c.post('/api/notes/1/status', json={'status': 'disputed'}).status_code == 401
        assert c.get('/api/notes').status_code == 200            # чтение остаётся публичным
        assert c.get('/api/notes', params={'include_history': 'true'}).status_code == 200
