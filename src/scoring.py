"""Корректное чтение числовых значений памяти и детерминированные хелперы фактов.

Ноль — легитимное значение (например importance=0 или confidence=0), а не
«нет данных». Конструкция ``float(x or default)`` превращала 0 в default и
искажала ранжирование контекста; здесь используется явная проверка None.

Хелперы фактов (M4) — чистые функции без эмбеддингов: точечная инвалидация
истории не должна зависеть от смыслового индекса (M3). Используются и
src/memory.py, и src/context.py, поэтому живут в нейтральном модуле.
"""
from __future__ import annotations

import re
from typing import Any

# Служебные слова, которые не должны связывать сообщение с фактом.
_STOP_STEMS = frozenset({
    'это', 'как', 'что', 'его', 'она', 'они', 'был', 'была', 'было', 'для',
    'или', 'при', 'про', 'под', 'над', 'тем', 'чем', 'все', 'всё', 'мой',
    'тво', 'есть', 'быть', 'кто', 'где', 'когда', 'поче', 'заче', 'так',
    'тут', 'там', 'уже', 'ещё', 'еще', 'очен', 'очень',
})


def fact_stems(text: str, fact_key: str | None = None) -> set[str]:
    """Значимые 4-символьные основы слов факта (детерминированно, без эмбеддингов).

    Русская морфология грубо срезается до четырёх букв — того же подхода
    придерживается обход графа в knowledge.graph_matches. Служебные слова
    исключаются, чтобы «что ты говорил про это» не связывалось с фактом.
    """
    value = str(text or '').casefold().replace('ё', 'е') + ' ' + str(fact_key or '').casefold().replace('ё', 'е')
    stems: set[str] = set()
    for token in re.findall(r'[а-яa-z0-9]+', value):
        stem = token[:4] if len(token) > 4 else token
        if stem and stem not in _STOP_STEMS:
            stems.add(stem)
    return stems


def message_relates_to_fact(message_text: str, fact_text: str, fact_key: str | None = None) -> bool:
    """Точечная связь ответа Кумара с устаревшим фактом.

    Правило (намеренно консервативное — против ложных удалений истории):
    совпадение основы ключа факта ИЛИ не менее двух общих значимых основ
    тела факта. Один общий стем тела считается случайным пересечением.
    """
    message = fact_stems(message_text)
    if not message:
        return False
    if fact_key:
        key_stems = fact_stems(fact_key)
        # Ключ — это слот ('напиток'), а не произвольный текст: пересечение
        # с ним считаем связью только вместе с одной основой из тела факта,
        # чтобы ключ-метка не притягивал посторонние ответы о том же слоте.
        if key_stems and message & key_stems and message & fact_stems(fact_text):
            return True
    return len(message & fact_stems(fact_text)) >= 2


def safe_float(value: Any, default: float) -> float:
    """Вернуть float(value); default только для None и нечисловых значений.

    Примеры:
        safe_float(0.0, 0.5)   -> 0.0   (ноль сохраняется как ноль)
        safe_float(None, 0.5)  -> 0.5   (нет данных — берём умолчание)
        safe_float('0.2', 0.5) -> 0.2   (строка БД корректно парсится)
        safe_float('мусор', 1.0) -> 1.0 (нечисловое значение — умолчание)
    """
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
