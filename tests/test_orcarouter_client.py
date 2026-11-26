"""Тесты OrcaRouter-клиента без реального платного API (мок HTTP-транспорт).

Покрывает контракт системного промпта (C1), нулевые значения в payload (C2)
и поведение клиента на ошибки API (C6). Сеть не используется: запрос
перехватывается httpx.MockTransport.
"""
import json

import httpx
import pytest

from src.engines import DEFAULT_ORCA_MODEL, EngineError, OrcaRouterEngine

API_KEY = 'test-key-abc123'


@pytest.fixture
def key_env(monkeypatch):
    """Ключ и адрес только в окружении теста; реальный API не вызывается."""
    monkeypatch.setenv('ORCAROUTER_API_KEY', API_KEY)
    monkeypatch.setenv('ORCAROUTER_BASE_URL', 'https://mock.orcarouter.invalid/v1')


def make_engine(handler, **kwargs):
    return OrcaRouterEngine(DEFAULT_ORCA_MODEL, transport=httpx.MockTransport(handler), **kwargs)


def capture(handler, **kwargs):
    """Обернуть handler, чтобы запомнить запрос и распарсить payload."""
    seen = {}

    def wrapped(request):
        seen['url'] = str(request.url)
        seen['headers'] = dict(request.headers)
        seen['raw'] = request.content.decode('utf-8')
        seen['payload'] = json.loads(seen['raw'])
        return handler(request)

    return make_engine(wrapped, **kwargs), seen


def ok_response(content='Ответ модели'):
    return {
        'id': 'chatcmpl-test',
        'object': 'chat.completion',
        'model': DEFAULT_ORCA_MODEL,
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content},
                     'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 42, 'completion_tokens': 7, 'total_tokens': 49},
    }


# --- C1: контракт системного промпта -------------------------------------

def test_payload_sends_local_character_prompt(key_env):
    """Персонаж собирается локально из config/character.md и уходит в каждом запросе."""
    engine, seen = capture(lambda request: httpx.Response(200, json=ok_response()))
    answer = engine.generate('привет', [], [], 'neutral', False)

    assert answer == 'Ответ модели'
    payload = seen['payload']
    system = payload['messages'][0]
    assert system['role'] == 'system'
    assert 'Ты — Кумар' in system['content']            # текст config/character.md
    assert 'дерзкий' in system['content']               # характер из локального файла
    assert 'Backstage-фрагмент текущего эфира' in system['content']  # BASE_RULES
    assert 'Текущее эмоциональное состояние: нейтральное' in system['content']
    assert 'привязан к API-ключу' not in seen['raw']
    assert engine.persona_sha and len(engine.persona_sha) == 12


def test_payload_structure_and_headers(key_env):
    engine, seen = capture(lambda request: httpx.Response(200, json=ok_response()),
                           session_id='custom-session')
    engine.generate('вопрос зрителя', [], [], 'joy', False)

    assert seen['url'].endswith('/chat/completions')
    assert seen['headers']['authorization'] == f'Bearer {API_KEY}'
    assert seen['headers']['x-orcarouter-session-id'] == 'custom-session'
    payload = seen['payload']
    assert payload['model'] == DEFAULT_ORCA_MODEL
    assert payload['stream'] is False
    assert payload['max_tokens'] == 400
    assert payload['messages'][-1] == {'role': 'user', 'content': 'вопрос зрителя'}
    # Эмоция попадает в системный блок.
    assert 'Текущее эмоциональное состояние: радость' in payload['messages'][0]['content']


def test_reference_data_block_appended_as_second_system(key_env):
    engine, seen = capture(lambda request: httpx.Response(200, json=ok_response()))
    context = [{
        'id': 'note:1', 'kind': 'note', 'source': 'Заметка #1', 'text': 'Кумар пьёт чай.',
        'memory_type': 'fact', 'importance': 0.9, 'confidence': 1.0,
    }]
    engine.generate('что ты пьёшь?', context, [], 'neutral', False)

    payload = seen['payload']
    assert payload['messages'][1]['role'] == 'system'
    assert '<reference_data>' in payload['messages'][1]['content']
    assert 'Кумар пьёт чай.' in payload['messages'][1]['content']
    assert payload['messages'][-1]['role'] == 'user'


def test_api_key_never_enters_request_body(key_env):
    engine, seen = capture(lambda request: httpx.Response(200, json=ok_response()))
    engine.generate('привет', [], [], 'neutral', False)
    assert API_KEY not in seen['raw']


def test_missing_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv('ORCAROUTER_API_KEY', raising=False)
    with pytest.raises(EngineError, match='ORCAROUTER_API_KEY'):
        OrcaRouterEngine(DEFAULT_ORCA_MODEL)
