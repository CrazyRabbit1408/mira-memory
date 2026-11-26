# L2-prep — план потокового вывода (streaming) для /api/chat

Дата: 2026-11-26. Статус: **анализ и план, без кода** (постановка L2-prep).
Цель по таймингу: 1–3 секунды до первого «голоса» (T1/TTS) — это требует,
чтобы текст Кумара появлялся по мере генерации, а не одним куском после
30–90 секунд работы модели.

Предыдущие этапы: Этап 0 (C1–C6), M4 (конфликты), M2 (FTS5), M1 (rebuild).
Не затрагивается: M3 (смысловое ядро), M5 (граф связей), T1 (реальный TTS), A1–A3.

---

## 1. Текущее состояние (что мешает стримингу)

### 1.1 src/engines.py — OrcaRouterEngine

`OrcaRouterEngine.generate()` (строки ~183–269) — синхронный метод «вопрос → полный текст»:

1. Собирает `messages`: system-персонаж (config/character.md, контракт C1) +
   базовые правила + эмоция; история (последние 20 реплик, фильтр заглушек);
   отдельное system-сообщение `<conflicting_facts>` (M4); отдельное
   `<reference_data>`; user-сообщение. Сохраняет в `self.last_messages`.
2. Отправляет `POST {base_url}/chat/completions` с `"stream": False`
   (engines.py:232) через **синхронный** `httpx.Client(timeout=120)`.
3. Парсит полный JSON: `choices[0].message.content`, `usage` → `self.last_usage`.
4. Чистит `<think>...</think>`-блоки, применяет `protect_secret_echo()` (C2)
   к **полному** тексту и возвращает строку.

Потоковость отсутствует на двух уровнях: запрос уходит с `stream: False`, и
даже с `stream: True` текущий код прочитал бы SSE-поток до конца, потому что
`httpx.Client.post()` буферизует весь ответ.

### 1.2 src/app.py — Pipeline.chat() и /api/chat

`Pipeline.chat()` (строки ~115–185) — тоже синхронный, цельный:

1. Под **общим `self.lock`** выполняет: retrieval (semantic.search, M3),
   graph_matches, stream-фрагменты, `resolve_context`, историю,
   `stale_facts` + точечную инвалидацию (M4), выбор движка.
2. Не отпуская lock, вызывает `engine.generate(...)` — то есть держит
   блокировку 30–90 секунд генерации.
3. После ответа: `save_exchange()` (обе реплики в SQLite), `semantic.schedule()`,
   `tts.synthesize(answer)` (text_stub) и возвращает **одним JSON**:
   `{response, matches, mode, search_mode, search_warning, conflicts,
   prompt_messages, usage, prompt_source}`.

Эндпоинт `POST /api/chat` (строки ~383–387) просто вызывает `pipeline.chat()`
и сериализует dict. Фронтенд (`app.js:33`) ждёт весь ответ и затем рендерит
сообщение, совпадения и трейс промпта разом.

Вывод: для стриминга нужно менять три слоя — движок (SSE-клиент), Pipeline
(разделение фаз и протокол событий), роутер (StreamingResponse) — плюс
фронтенд. Контекстная сборка (M4/M3/C1) трогать не нужно: она целиком
происходит до первого токена.

---

## 2. Поддерживает ли OrcaRouter стриминг? Да

OrcaRouter — OpenAI-compatible шлюз (docs.orcarouter.ai). По официальной
документации раздела Advanced/Streaming:

- `stream: true` поддерживается в OpenAI-compatible Chat/Completions
  (и в Anthropic Messages) эндпоинтах.
- Формат провода — Server-Sent Events: каждая строка `data: {JSON}`,
  завершение потока — `data: [DONE]`.
- `stream_options: {include_usage: true}` поддерживается: последний чанк перед
  `[DONE]` содержит объект `usage` — метрики токенов сохранятся без второго запроса.
- Важный нюанс из документации: **после первого отправленного байта OrcaRouter
  уже не может переключиться на следующий шлюз своего fallback-цепочки** —
  если апстрим упал в середине потока, ошибка придёт внутри потока
  (in-band error), а не HTTP-статусом.
- Заголовок сессии `X-OrcaRouter-Session-Id` в стриминговом режиме отдельно
  не оговорён — **требуется проверка шагом-зондом (шаг 0 ниже)**: продолжают ли
  чанки приходить с нашим Session-Id и сохраняется ли контекст сессии.

У бесплатных моделей из ORCA_MODELS (deepseek-v4.1-flash-free и др.) стриминг
может быть отключён самим провайдером — это тоже проверяет зонд, и именно на
этот случай план имеет честный fallback (раздел 6.1).

---

## 3. Что нужно изменить в generate() для async yield

Текущий `generate()` остаётся как есть (fallback-режим). Рядом добавляется
потоковый вариант — эскиз контракта:

```python
class OrcaRouterEngine:
    async def generate_stream(self, text, context, history, emotion, thinking,
                              conflict_notice=None) -> AsyncIterator[str]:
        """yield дельт видимого текста; last_messages/last_usage как у generate()."""
```

Ключевые изменения относительно `generate()`:

1. **httpx.AsyncClient + client.stream()**: POST уходит тем же payload, но с
   `"stream": True` и `"stream_options": {"include_usage": True}`;
   тело читается через `async with client.stream('POST', ...) as response:
   async for line in response.aiter_lines()`.
2. **Парсер SSE**: отбрасывать пустые строки и `event:`-префиксы, снимать
   `data: `, `json.loads` каждого чанка, извлекать
   `choices[0].delta.content` (у финального чанка может быть `choices == []`,
   но заполнен `usage` — его нужно поймать в `last_usage`), останавливаться
   на `[DONE]`.
3. **Фильтр `<think>` — конечный автомат, не regex**: сейчас `<think>...</think>`
   снимается одним `re.sub` по готовому тексту. В потоке тег может быть разрезан
   между чанками (`<thi|nk>`). Автомат с состояниями `normal / maybe_open / in_think /
   maybe_close` буферизует только переходные символы (до 8) и не пропускает
   «мысли» в выдачу. Пока модель в think — yield не происходит, зритель видит
   «…думает».
4. **protect_secret_echo (C2) — буферизация по предложениям**: проверка
   «ответ содержит секрет» по полному тексту в потоке невозможна заранее.
   Стратегия: копить буфер до конца предложения (`. ! ? \n`, порог 40 символов),
   прогонять защиту и только потом выдавать накопленное. Первый токен при этом
   задерживается до первой границы предложения — что и нужно для TTS (см. шаг 5).
   Если секрет обнаружен — emit заменяющей фразы и остановка потока.
5. **Ошибки**: HTTP ≥ 400 до старта тела → EngineError как сейчас; ошибка
   внутри потока (in-band `data: {"error": ...}` или обрыв соединения) →
   yield специальной дельты-маркера и поднятие исключения после очистки.
6. **Протокол движка**: возвращается `AsyncIterator[str]` по дельтам; метаданные
   (`last_messages`, `last_usage`, `persona_sha`) заполняются по ходу/в конце.
   `StubLLM` получает честный потоковый дубль (`async for x in [полный_ответ]`) —
   чтобы тесты и заглушка шли через один и тот же путь.

Транспорт для мока: текущая точка внедрения `self._transport` (mock-тесты C6)
сохраняется — httpx.MockTransport работает и с AsyncClient (мок отдаёт готовый
SSE-текст ответа).

---

## 4. Какие эндпоинты переводить на стриминг и как

### 4.1 Только /api/chat

Поток нужен там, где уникальная задержка создаётся LLM. Остальные эндпоинты —
локальные чтения/записи SQLite с ответом за миллисекунды (заметки, поиск FTS5
— уже < 5 мс, статус, метрики): их перевод на SSE усложнил бы фронтенд без
выигрыша. `semantic-reindex` и `backup` — долгие, но это фоновые/файловые
операции с собственным UX; остаются как есть.

### 4.2 Изменение роутера

`POST /api/chat` принимает `ChatInput` с опциональным `stream: bool = False`
(обратная совместимость: старый фронтенд и тесты без поля получают прежний
JSON-ответ). При `stream=True` роутер возвращает:

```python
from fastapi.responses import StreamingResponse

@app.post('/api/chat')
async def chat(payload: ChatInput):
    if not payload.stream:
        return app.state.pipeline.chat(payload.viewer, payload.text)  # как сейчас
    return StreamingResponse(
        app.state.pipeline.chat_stream(payload.viewer, payload.text),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
```

Особенности, которые надо учесть:

- **Middleware `local_origin`** пропускает StreamingResponse без изменений
  (заголовок X-Content-Type-Options навешивается на итоговый response) —
  проверяется тестом.
- **Кросс-слойные исключения**: ValueError (валидация) и EngineError должны
  упасть ДО первого байта потока — поэтому фаза контекста выполняется до
  создания генератора ответа (см. chat_stream ниже), иначе ошибка уйдёт
  внутрь потока и фронтенд её увидит как обрыв.
- **TestClient**: httpx-поток читается `with client.stream('POST', '/api/chat', ...)
  as r: for line in r.iter_lines()` — тесты пишутся на кадры SSE.

### 4.3 Протокол кадров (контракт для app.js)

Кадры SSE с именованными событиями — метаданные контекста идут одним кадром
до генерации, а не после ответа, как в JSON:

```
event: meta       data: {"matches":[...], "search_mode":"words", "search_warning":"",
                         "conflicts":[...], "prompt_messages":[...], "mode":"orcarouter",
                         "prompt_source":"local: config/character.md (…)"}
event: delta      data: {"text":"Ку"}                       (много раз)
event: usage      data: {"usage":{...}, "prompt_sha":"…"}   (после [DONE])
event: done       data: {"response":"полный текст", "conflicts":[...]}
event: error      data: {"detail":"OrcaRouter оборвала поток…"}
```

`done` повторяет полный текст — фронтенду не нужно собирать финальную строку
из дельт (устойчивость к потерянным кадрам), а `save_exchange` на бэкенде
гарантирует, что SQLite и UI сойдутся.

### 4.4 Pipeline.chat_stream() — разделение фаз

```python
async def chat_stream(self, viewer, text):
    with self.lock:                        # фаза 1: контекст — быстрый путь SQLite
        retrieval, matches, history, stale, engine_cfg = self._prepare(viewer, text)
    yield sse('meta', {...})               # кадр метаданных
    # фаза 2: генерация — lock НЕ держим (параллельные диалоги не блокируются)
    chunks = []
    async for delta in engine.generate_stream(...):
        chunks.append(delta)
        yield sse('delta', {'text': delta})
    answer = ''.join(chunks)               # полный текст после [DONE]
    self.memory.save_exchange(viewer, text, answer)   # только при полном ответе
    self.semantic.schedule()
    self.tts.synthesize(answer)            # text_stub; T1 подменит
    yield sse('done', {'response': answer, ...})
```

Существующий `Pipeline.chat()` реализуется как тонкая обёртка над `_prepare()`
— сборка контекста остаётся одной (без дублирования M4-логики).

---

## 5. План реализации L2 (5 шагов)

**Шаг 0 (зонд, 30 минут работы, без кода в main).** Ручной curl-скрипт в
`tools/probe_stream.sh`: два запроса к OrcaRouter с реальным ключом из .env —
`stream: true` без `stream_options` и с ним; проверяем кадры SSE, `[DONE]`,
usage-чанк, поведение бесплатных моделей и сохранность Session-Id. Результат
фиксируется в этом файле; если зонд показал, что модель/шлюз не стримит —
шаги 1–5 всё равно безопасны, срабатывает fallback 6.1.

**Шаг 1. OrcaRouterEngine.generate_stream() + StubLLM.generate_stream()**
(файл src/engines.py). SSE-клиент на httpx.AsyncClient, автомат `<think>`,
буферная защита секретов, обработка in-band ошибок, `last_usage` из
include_usage. Тесты: mock-транспорт отдаёт chunk-поток (в т.ч. разрезанный
`<think>`, секрет в середине предложения, обрыв без `[DONE]`, in-band error).

**Шаг 2. Pipeline.chat_stream() + Pipeline._prepare()** (файл src/app.py).
Вынести сборку контекста (retrieval, M4-инвалидация, история, эмоция, выбор
движка) из chat() в _prepare(); chat_stream() = _prepare + генерация + кадры
meta/delta/usage/done; save_exchange только при полном ответе; lock только на
фазе контекста. Тесты: последовательность кадров, сохранение обмена, поведение
при EngineError до первого кадра (400/502) и внутри потока (event: error).

**Шаг 3. Роутер /api/chat: StreamingResponse + ChatInput.stream** (файл
src/app.py). Обратная совместимость JSON-режима, заголовки no-cache,
проверка middleware и admin-семантики (chat остаётся без admin_guard —
это не изменяющий память эндпоинт). Тесты: TestClient-стрим, оба режима,
TrustedHost/origin не ломают SSE.

**Шаг 4. Фронтенд app.js** — `fetch` + `ReadableStream.getReader()` вместо
одиночного `await api('/api/chat')`: прогресс-рендер сообщения по кадрам delta,
панель «Что нашла память» наполняется из meta сразу, трейс промпта и usage —
из usage/done, ошибка — event: error → toast + частичный текст помечается как
незавершённый. Кнопка отправки блокируется до done/error. Отправка формы —
через опцию `stream:true`; серверная совместимость сохраняется для старых клиентов.

**Шаг 5. Интеграция с T1 (реальный TTS) — буферизация первого предложения.**
Движок уже отдаёт дельты; TTS-слой получает **первое предложение до начала
озвучки**: буфер копится до границы `.!?…` (та же точка разреза, что и у
защиты секретов в шаге 1 — использовать общий SentenceBuffer из engines.py),
затем первое предложение уходит в TTS, остальное — по мере накопления.
Бюджет: первый токен ≤ ~1.5 с (зависит от провайдера), озвучка стартует на
первом предложении — суммарно 1–3 с до голоса. Начиная с шага 5 стоит
завести метрику `time_to_first_delta` в ответ Pipeline (диагностика панели).

---

## 6. Риски и решения

### 6.1 OrcaRouter не стримит данную модель / зонд показал отсутствие SSE

- **Не нужен polling.** Polling (опрос готового ответа по id) — лишняя
  сущность: у нас один локальный процесс-потребитель, а не распределённая
  система; долгие опросы уйдут в таймауты и удвоят трафик.
- **Fallback — прежний полный путь**: generate_stream() при HTTP 4xx на
  стриминговый запрос (или отсутствии `data:`-кадров в теле) автоматически
  вызывает синхронный `generate()` и выдаёт его результат **одной дельтой**.
  Пайплайн, роутер и фронтенд не меняются — зритель просто получает ответ
  одним куском, как сегодня. Флаг «стрим не поддержан, отдано целиком» пишется
  в `search_warning`/лог для оператора.
- Смена модели в панели не требует кода: fallback срабатывает автоматически
  на любой модели.

### 6.2 Ошибка в середине потока

- Апстрим оборвался (in-band error, обрыв TCP, таймаут 120 с): движок
  завершает автомат, Pipeline шлёт `event: error` с человеческим текстом
  и помечает ответ как неполный.
- **Политика сохранения**: частичный ответ в SQLite **не сохраняется** —
  `save_exchange` вызывается только при `[DONE]`. Обоснование: незавершённая
  фраза попала бы в историю и в контекст следующих ответов, а это
  contradicts контракту C1 (история — то, что Кумар действительно сказал).
  Частичный текст остаётся на экране с пометкой «ответ оборван» и в логе.
- Повтор не автоматический (оператор resend'ит вручную) — автоматический ретрай
  при частично произнесённом ответе дал бы дубли в UI.

### 6.3 Прочие, с планом на каждый

| Риск | Решение |
| --- | --- |
| Секрет в середине потока (C2) | SentenceBuffer: защита до выпуска кадра; детект — стоп + заменяющая фраза |
| `<think>` разрезан между чанками | Конечный автомат вместо regex; покрытие тестом «разрезанный тег» |
| Блокировка RLock на время генерации | lock только в фазе контекста (_prepare) |
| usage-чанк без choices | Парсер учитывает финальный чанк с пустым choices и usage |
| TrustedHost/origin middleware проглатывает SSE | Тест на заголовки и полный проход кадра |
| Фронтенд теряет соединение (sleep ноутбука) | fetch reader бросает исключение → кнопка разблокируется, toast |
| include_usage не поддержан конкретной моделью | usage-кадр опционален; отсутствие не ломает done |
| Session-Id не сохраняется при streaming | Выясняется зондом шага 0; если да — новая сессия только через «Сбросить сессию LLM» |

### 6.4 Что стриминг НЕ меняет

Контекст (M3-retrieval, M4-инвалидация, `<conflicting_facts>` отдельным
system-сообщением, C1-промпт из character.md) собирается до первого токена и
целиком попадает в `event: meta` — семантика ответа идентична JSON-режиму.
FTS5/M2, заметки, статусы и admin_guard не затрагиваются.

## 7. Оценка объёма

| Шаг | Файлы | Новые тесты | Оценка |
| --- | --- | --- | --- |
| 0 | tools/probe_stream.sh | — | 0.5 ч |
| 1 | src/engines.py | ~8 | 4–6 ч |
| 2 | src/app.py | ~6 | 4–5 ч |
| 3 | src/app.py | ~4 | 1–2 ч |
| 4 | src/static/app.js | ручной + 1–2 e2e | 3–4 ч |
| 5 | (T1-зависимый) SentenceBuffer общий | ~2 | 2 ч (без реального TTS) |

## 8. Источники

- docs.orcarouter.ai — Advanced/Streaming (SSE-формат, `[DONE]`, include_usage,
  in-band ошибки, ограничение fallback после первого байта), Introduction
  (OpenAI-compatible шлюз). Проверено 2026-11-26.
- src/engines.py (OrcaRouterEngine.generate, protect_secret_echo, mock-транспорт),
  src/app.py (Pipeline.chat, /api/chat, middleware), src/static/app.js (чат-форма).
