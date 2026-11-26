"""C5: честный сброс runtime-контекста LLM.

Панель либо реально меняет Session-Id OrcaRouter, либо прямо говорит, что
сбрасывать нечего. Никаких сообщений «сессия сброшена» без реального сброса.
"""
import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src import semantic
from src.embeddings import stub_embedding

ORCA = {'engine': 'orcarouter', 'model': 'deepseek/deepseek-v4.1-flash-free', 'thinking': False}


@pytest.fixture
def client(tmp_path, monkeypatch):
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Кумар пьёт чай.', encoding='utf8')
    monkeypatch.setattr(semantic, 'embed', lambda texts: [stub_embedding(x) for x in texts])
    monkeypatch.delenv('KUMAR_ADMIN_TOKEN', raising=False)
    with TestClient(create_app(tmp_path / 'data', lore, testing=True)) as c:
        yield c


def test_stub_reload_is_honest(client):
    r = client.post('/api/runtime/reload').json()
    assert r['session_reset'] is False
    assert r['session_id'] is None
    assert 'заглушка' in r['message'].lower()
    assert 'сброшена' not in r['message'].lower()


def test_orca_reload_creates_new_session(client):
    assert client.post('/api/engine-settings', json=ORCA).status_code == 200
    r1 = client.post('/api/runtime/reload').json()
    assert r1['session_reset'] is True
    sid1 = r1['session_id']
    assert sid1 and sid1.startswith('kumar-panel-') and sid1 != 'kumar-panel'
    r2 = client.post('/api/runtime/reload').json()
    assert r2['session_reset'] is True
    assert r2['session_id'] != sid1
    assert 'сохранена' in r2['message']  # честно: локальная история не удалялась
    status = client.get('/api/status').json()
    assert status['runtime']['session_id'] == r2['session_id']


def test_chat_uses_pipeline_session_after_reload(client, monkeypatch):
    """После сброса чат уходит с новым X-OrcaRouter-Session-Id."""
    from src import app as app_module
    captured = {}

    class FakeOrcaEngine:
        def __init__(self, model, session_id=None):
            captured['model'] = model
            captured['session_id'] = session_id
            self.last_messages = []
            self.last_usage = {}
            self.persona_sha = 'deadbeef'

        def generate(self, text, context, history, emotion, thinking, conflict_notice=None):
            return 'фейковый ответ'

    monkeypatch.setattr(app_module, 'OrcaRouterEngine', FakeOrcaEngine)
    client.post('/api/engine-settings', json=ORCA)
    sid = client.post('/api/runtime/reload').json()['session_id']
    r = client.post('/api/chat', json={'viewer': 'alice', 'text': 'привет'})
    assert r.status_code == 200
    assert captured['session_id'] == sid
    assert 'local: config/character.md' in r.json()['prompt_source']
