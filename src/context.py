"""Безопасная сборка контекста перед вызовом языковой модели.

Поиск находит кандидатов, но не должен сам решать, какой из двух
противоречащих фрагментов считать актуальным. Здесь зафиксирован небольшой
детерминированный слой приоритетов: заметка владельца важнее лора, а
подтверждённое утверждение зрителя не превращается автоматически в канон.
"""
from __future__ import annotations

import re
from typing import Any


TOPIC_HINTS: dict[str, tuple[str, ...]] = {
    'drink': ('пь', 'пит', 'напит', 'чай', 'коф', 'сок', 'вод'),
    'preference': ('люб', 'нрав', 'предпоч', 'вкус'),
    'name': ('имя', 'зовут', 'как зовут'),
    'mother': ('матер', 'мать', 'мам'),
    'location': ('где', 'жив', 'наход', 'мест'),
    'hobby': ('собир', 'коллекц', 'увлеч', 'занима'),
    'work': ('работ', 'чин', 'ремонт', 'дела'),
    'relationship': ('друж', 'знаком', 'отношен'),
}

OWNER_KINDS = frozenset({'owner_note', 'note'})
LOWER_PRIORITY_KINDS = frozenset({'lore', 'graph', 'viewer_report', 'message'})


def _fold(value: Any) -> str:
    return str(value or '').casefold().replace('ё', 'е')


def topics(text: str, fact_key: str | None = None) -> set[str]:
    """Вывести грубые тематические слоты из запроса или записи."""
    value = _fold(text) + ' ' + _fold(fact_key)
    return {name for name, hints in TOPIC_HINTS.items() if any(hint in value for hint in hints)}


def _priority(item: dict[str, Any]) -> int:
    kind = item.get('kind')
    source = _fold(item.get('source_kind'))
    if kind == 'stream_memory':
        return 0
    if kind in OWNER_KINDS and source not in {'viewer', 'chat', 'message'}:
        return 1
    if kind == 'viewer_report':
        return 2
    if kind == 'graph':
        return 3
    if kind == 'lore':
        return 4
    if kind == 'message':
        return 5
    return 6


def _explicit_unknown(text: str, query_topics: set[str]) -> bool:
    value = _fold(text)
    if 'mother' in query_topics or 'name' in query_topics:
        return bool(re.search(r'не\s+(?:помнит|известн)|остает(?:ся|ся)\s+неизвест|открыт', value))
    return False


def answer_directive(query: str, matches: list[dict[str, Any]]) -> str:
    """Дать модели короткую политику точного ответа."""
    qtopics = topics(query)
    if not qtopics:
        return (
            'Отвечай только по относящимся к вопросу записям. Если точного '
            'факта нет, так и скажи; не придумывай имена, даты и детали.'
        )

    relevant = [x for x in matches if topics(str(x.get('text', '')), x.get('fact_key')) & qtopics]
    if ('mother' in qtopics or 'name' in qtopics) and (
        not relevant or all(_explicit_unknown(str(x.get('text', '')), qtopics) for x in relevant)
    ):
        return (
            'Это вопрос на точный факт. В доступных записях нет подтверждённого '
            'имени. Прямо скажи, что имя неизвестно или остаётся открытым. '
            'Не создавай имя по аналогии и не выдавай догадку за память.'
        )
    return (
        'Отвечай только по относящимся к вопросу записям. Если точного факта '
        'нет, скажи, что не знаешь. Не придумывай имена, даты и детали. '
        'Заметка владельца — актуальная запись и имеет приоритет над '
        'противоречащим ей лором.'
    )


def resolve_context(query: str, matches: list[dict[str, Any]], limit: int = 7) -> tuple[list[dict[str, Any]], int]:
    """Убрать устаревший лор, когда найдено актуальное owner-примечание."""
    qtopics = topics(query)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in matches:
        text = ' '.join(str(item.get('text', '')).split())
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(item)

    active_topics: set[str] = set()
    for item in unique:
        if item.get('kind') in OWNER_KINDS and _fold(item.get('source_kind')) not in {'viewer', 'chat', 'message'}:
            item_topics = topics(str(item.get('text', '')), item.get('fact_key'))
            if item_topics & qtopics and float(item.get('confidence', 1.0) or 1.0) >= 0.5:
                active_topics.update(item_topics & qtopics)

    filtered: list[dict[str, Any]] = []
    suppressed = 0
    for item in unique:
        item_topics = topics(str(item.get('text', '')), item.get('fact_key'))
        if (
            active_topics
            and item.get('kind') in {'lore', 'graph'}
            and item_topics & active_topics
        ):
            suppressed += 1
            continue
        filtered.append(item)

    def sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
        item_topics = topics(str(item.get('text', '')), item.get('fact_key'))
        overlap = len(item_topics & qtopics)
        return (
            _priority(item),
            -overlap,
            -float(item.get('importance', 0.5) or 0.5),
            -float(item.get('confidence', 1.0) or 1.0),
            -float(item.get('word_score', 0.0) or 0.0),
            str(item.get('id', '')),
        )

    filtered.sort(key=sort_key)
    return filtered[:limit], suppressed