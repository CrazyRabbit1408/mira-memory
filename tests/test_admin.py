"""C4: минимальная защита панели admin-токеном.

Когда KUMAR_ADMIN_TOKEN задан в окружении, все изменяющие (destructive)
эндпоинты требуют заголовок X-Admin-Token. Чтение (/api/status, /api/notes,
/api/messages, /api/graph, /api/chat) остаётся публичным. Если токен не задан
(локальный прототип на 127.0.0.1), поведение не меняется.
"""
import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src import semantic
from src.embeddings import stub_embedding

HEADER = {'X-Admin-Token': 'sekret-token'}


def make_client(tmp_path, monkeypatch, token: str | None):
    lore = tmp_path / 'lore'
    lore.mkdir()
    (lore / 'world.md').write_text('Кумар пьёт чай с мятой.', encoding='utf8')
    monkeypatch.setattr(semantic, 'embed', lambda texts: [stub_embedding(x) for x in texts])
    if token is None:
        monkeypatch.delenv('KUMAR_ADMIN_TOKEN', raising=False)
    else:
        monkeypatch.setenv('KUMAR_ADMIN_TOKEN', token)
    app = create_app(tmp_path / 'data', lore, testing=True)
    return TestClient(app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, token=None) as c:
        yield c


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, token='sekret-token') as c:
        yield c


def test_without_token_configured_local_mode_works(client):
    """Пустой KUMAR_ADMIN_TOKEN — прежнее локальное поведение."""
    assert client.post('/api/notes', json={'text': 'локальный режим'}).status_code == 200
    assert client.post('/api/emotion', json={'emotion': 'joy'}).status_code == 200
    assert client.get('/api/backup').status_code == 200
    assert client.post('/api/runtime/reload').status_code == 200


def test_read_only_endpoints_stay_public(guarded):
    assert guarded.get('/api/status').status_code == 200
    assert guarded.get('/api/notes').status_code == 200
    assert guarded.get('/api/messages').status_code == 200
    assert guarded.get('/api/graph').status_code == 200
    assert guarded.get('/api/lore').status_code == 200
    assert guarded.get('/api/runtime/stream-memories').status_code == 200


def test_chat_stays_public(guarded):
    assert guarded.post('/api/chat', json={'viewer': 'alice', 'text': 'привет'}).status_code == 200


def test_destructive_endpoints_require_token(guarded):
    assert guarded.post('/api/notes', json={'text': 'x'}).status_code == 401
    assert guarded.post('/api/notes', json={'text': 'x'},
                        headers={'X-Admin-Token': 'wrong'}).status_code == 401
    assert guarded.put('/api/notes/1', json={'text': 'x', 'revision': 1}).status_code == 401
    assert guarded.delete('/api/notes/1').status_code == 401
    assert guarded.delete('/api/messages/1').status_code == 401
    assert guarded.post('/api/emotion', json={'emotion': 'joy'}).status_code == 401
    assert guarded.post('/api/demo').status_code == 401
    assert guarded.post('/api/engine-settings', json={
        'engine': 'orcarouter', 'model': 'deepseek/deepseek-v4.1-flash-free',
        'thinking': False}).status_code == 401
    assert guarded.post('/api/runtime/reload').status_code == 401
    assert guarded.post('/api/runtime/tts/stop').status_code == 401
    assert guarded.post('/api/runtime/tts/resume').status_code == 401
    assert guarded.post('/api/runtime/stream-memories', json={'text': 'x'}).status_code == 401
    assert guarded.delete('/api/runtime/stream-memories').status_code == 401
    assert guarded.post('/api/candidates/1/approved').status_code == 401
    assert guarded.post('/api/lore/reindex').status_code == 401
    assert guarded.post('/api/semantic-reindex').status_code == 401
    assert guarded.get('/api/backup').status_code == 401


def test_correct_token_grants_access(guarded):
    note_id = guarded.post('/api/notes', json={'text': 'заметка'}, headers=HEADER).json()['id']
    assert note_id >= 1
    assert guarded.post('/api/emotion', json={'emotion': 'joy'}, headers=HEADER).status_code == 200
    assert guarded.post('/api/runtime/reload', headers=HEADER).status_code == 200
    assert guarded.post('/api/runtime/stream-memories', json={'text': 'фрагмент'},
                        headers=HEADER).status_code == 200
    assert guarded.get('/api/backup', headers=HEADER).status_code == 200
    # Авторизация прошла, а записи нет — 404, а не 401.
    assert guarded.post('/api/candidates/999/approved', headers=HEADER).status_code == 404
    assert guarded.delete(f'/api/notes/{note_id}', headers=HEADER).status_code == 200


def test_token_value_never_appears_in_responses(guarded):
    r = guarded.post('/api/notes', json={'text': 'x'}).json()
    assert 'sekret-token' not in str(r)
    r401 = guarded.get('/api/backup')
    assert 'sekret-token' not in r401.text
