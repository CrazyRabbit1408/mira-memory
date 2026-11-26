"""Нулевые importance/confidence — легитимные значения, а не «нет данных» (C2).

Раньше конструкция ``float(x or default)`` превращала 0.0 в значение по
умолчанию, и оператор не мог надёжно понизить приоритет записи.
"""
import pytest

from src.context import resolve_context
from src.memory import MemoryManager
from src.scoring import safe_float
from src import semantic
from src.embeddings import stub_embedding


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Локальная панель на тестовой заглушке без внешних зависимостей."""
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Кумар пьёт чай с мятой.', encoding='utf8')
    monkeypatch.setattr(semantic, 'embed', lambda texts: [stub_embedding(x) for x in texts])
    monkeypatch.delenv('KUMAR_ADMIN_TOKEN', raising=False)
    from fastapi.testclient import TestClient
    from src.app import create_app
    app = create_app(tmp_path / 'data', lore, testing=True)
    with TestClient(app) as c:
        yield c


def test_safe_float_zero_is_kept():
    assert safe_float(0.0, 0.5) == 0.0
    assert safe_float(0, 0.5) == 0.0
    assert safe_float('0.0', 0.5) == 0.0
    assert safe_float(None, 0.5) == 0.5
    assert safe_float('', 0.5) == 0.5
    assert safe_float('мусор', 1.0) == 1.0


def test_zero_scores_stored_and_returned_verbatim(tmp_path):
    m = MemoryManager(tmp_path / 'data', tmp_path / 'lore')
    note_id = m.add_note('нулевая заметка', importance=0.0, confidence=0.0)
    stored = m.notes()[0]
    assert stored['importance'] == 0.0 and stored['confidence'] == 0.0
    records = {r['id']: r for r in m._records()}
    record = records[f'note:{note_id}']
    assert record['importance'] == 0.0 and record['confidence'] == 0.0
    m.close()


def test_zero_importance_ranks_below_low_but_real_importance():
    """Без бага 0.0 важности ниже 0.3; с багом 0.0 поднималась до 0.5."""
    items = [
        {'kind': 'note', 'id': 'note:low', 'text': 'запись с низкой важностью',
         'source_kind': 'owner', 'importance': 0.3, 'confidence': 1.0},
        {'kind': 'note', 'id': 'note:zero', 'text': 'запись с нулевой важностью',
         'source_kind': 'owner', 'importance': 0.0, 'confidence': 1.0},
    ]
    out, _ = resolve_context('запись', items, limit=7)
    assert [x['id'] for x in out] == ['note:low', 'note:zero']


def test_zero_confidence_note_does_not_suppress_lore():
    """Заметка с уверенностью 0 не должна вытеснять лор по своей теме."""
    items = [
        {'kind': 'note', 'id': 'note:1', 'text': 'любимый напиток Кумара чай',
         'source_kind': 'owner', 'fact_key': 'напиток', 'importance': 0.9,
         'confidence': 0.0},
        {'kind': 'lore', 'id': 'lore:1',
         'text': 'Кумар пьёт кофе. Любимый напиток не подтверждён.',
         'source': 'config/lore/character/00-kumar-core.md'},
    ]
    out, suppressed = resolve_context('Что любимый напиток?', items, limit=7)
    assert suppressed == 0
    assert any(x['id'] == 'lore:1' for x in out)


def test_note_versions_keep_zero_scores(client):
    """Ревизия не должна подменять нулевые значения старой версии на default."""
    note_id = client.post('/api/notes', json={
        'text': 'нулевая версия', 'importance': 0, 'memory_type': 'fact',
    }).json()['id']
    assert client.put(f'/api/notes/{note_id}', json={
        'text': 'обновлённая версия', 'importance': 0.7,
        'memory_type': 'fact', 'revision': 1,
    }).status_code == 200
    versions = client.get(f'/api/notes/{note_id}/versions').json()
    assert versions[1]['importance'] == 0.0  # прежняя версия сохранена как была


def test_stream_memory_zero_importance_reaches_engine_verbatim(client, monkeypatch):
    """Backstage-фрагмент с важностью 0 не превращается в 0.8 в контексте модели."""
    from src.engines import StubLLM
    seen_contexts = []

    def fake_generate(self, text, context, history, emotion, thinking, conflict_notice=None):
        seen_contexts.append(context)
        return 'ответ Кумара'

    monkeypatch.setattr(StubLLM, 'generate', fake_generate)
    assert client.post('/api/runtime/stream-memories', json={
        'text': 'фрагмент с нулевой важностью', 'importance': 0,
    }).status_code == 200
    assert client.post('/api/chat', json={'viewer': 'alice', 'text': 'привет'}).status_code == 200
    stream_items = [x for x in seen_contexts[-1] if x['kind'] == 'stream_memory']
    assert stream_items and stream_items[0]['importance'] == 0.0
