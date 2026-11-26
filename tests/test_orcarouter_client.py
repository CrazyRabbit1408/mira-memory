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


# --- C2: нулевые значения в payload --------------------------------------

def test_zero_importance_and_confidence_in_payload(key_env):
    """importance=0/confidence=0 не должны превращаться в 0.5/1.0 (C2)."""
    engine, seen = capture(lambda request: httpx.Response(200, json=ok_response()))
    context = [{
        'id': 'note:1', 'kind': 'note', 'source': 'Заметка #1', 'text': 'ноль важности',
        'memory_type': 'fact', 'importance': 0.0, 'confidence': 0.0,
    }]
    engine.generate('привет', context, [], 'neutral', False)
    reference = seen['payload']['messages'][1]['content']
    assert '"важность": 0.0' in reference
    assert '"уверенность": 0.0' in reference


# --- C5: честный сброс сессии --------------------------------------------

def test_reset_session_rotates_session_id(key_env):
    """reset_session() выдаёт новый Session-Id, и он попадает в заголовок."""
    engine, seen = capture(lambda request: httpx.Response(200, json=ok_response()),
                           session_id='kumar-panel')
    engine.generate('раз', [], [], 'neutral', False)
    first = seen['headers']['x-orcarouter-session-id']
    assert first == 'kumar-panel'
    new_id = engine.reset_session()
    assert new_id != first and new_id.startswith('kumar-panel-')
    engine.generate('два', [], [], 'neutral', False)
    assert seen['headers']['x-orcarouter-session-id'] == new_id


# --- C6: ошибки HTTP, транспорта и некорректных ответов -------------------

@pytest.mark.parametrize('status,expected', [
    (401, 'API-ключ'),
    (403, 'HTTP 403'),
    (429, 'ограничила'),
    (500, 'HTTP 500'),
    (503, 'HTTP 503'),
])
def test_http_error_statuses(key_env, status, expected):
    """Ошибки API превращаются в понятные EngineError без утечки ключа."""
    engine, _ = capture(lambda request, status=status: httpx.Response(
        status, json={'error': {'message': f'подробности сервера {status}'}}))
    with pytest.raises(EngineError) as exc:
        engine.generate('вопрос', [], [], 'neutral', False)
    message = str(exc.value)
    assert expected in message
    assert API_KEY not in message


def test_rate_limit_message_includes_detail(key_env):
    engine, _ = capture(lambda request: httpx.Response(
        429, json={'error': {'message': 'много запросов, подождите'}}))
    with pytest.raises(EngineError, match='много запросов, подождите'):
        engine.generate('вопрос', [], [], 'neutral', False)


def test_timeout_raises_engine_error(key_env):
    def handler(request):
        raise httpx.ReadTimeout('simulated timeout', request=request)
    engine, _ = make_engine(handler), {}
    with pytest.raises(EngineError, match='120 секунд') as exc:
        engine.generate('вопрос', [], [], 'neutral', False)
    assert API_KEY not in str(exc.value)


def test_connection_error_raises_engine_error(key_env):
    def handler(request):
        raise httpx.ConnectError('network unreachable', request=request)
    engine = make_engine(handler)
    with pytest.raises(EngineError, match='Не удалось подключиться') as exc:
        engine.generate('вопрос', [], [], 'neutral', False)
    assert API_KEY not in str(exc.value)


def test_invalid_json_raises_engine_error(key_env):
    engine, _ = capture(lambda request: httpx.Response(200, content='{это не json'.encode('utf-8')))
    with pytest.raises(EngineError, match='некорректный ответ') as exc:
        engine.generate('вопрос', [], [], 'neutral', False)
    assert API_KEY not in str(exc.value)


def test_empty_choices_raises_engine_error(key_env):
    engine, _ = capture(lambda request: httpx.Response(200, json={'choices': []}))
    with pytest.raises(EngineError, match='без текста'):
        engine.generate('вопрос', [], [], 'neutral', False)


def test_message_without_text_raises_engine_error(key_env):
    engine, _ = capture(lambda request: httpx.Response(
        200, json={'choices': [{'message': {'role': 'assistant'}}]}))
    with pytest.raises(EngineError, match='без текста'):
        engine.generate('вопрос', [], [], 'neutral', False)


def test_thinking_only_answer_raises_engine_error(key_env):
    """Ответ из одних тегов <think> не считается видимым ответом."""
    engine, _ = capture(lambda request: httpx.Response(
        200, json=ok_response('<think>рассуждение без ответа</think>')))
    with pytest.raises(EngineError, match='не вернула видимый ответ'):
        engine.generate('вопрос', [], [], 'neutral', False)


def test_usage_is_recorded_on_success(key_env):
    engine, _ = capture(lambda request: httpx.Response(200, json=ok_response()))
    engine.generate('привет', [], [], 'neutral', False)
    assert engine.last_usage == {'prompt_tokens': 42, 'completion_tokens': 7, 'total_tokens': 49}


def test_successful_answer_is_returned_verbatim(key_env):
    engine, _ = capture(lambda request: httpx.Response(
        200, json=ok_response('Кумар: ну и вопрос, дружок.')))
    assert engine.generate('привет', [], [], 'neutral', False) == 'Кумар: ну и вопрос, дружок.'
