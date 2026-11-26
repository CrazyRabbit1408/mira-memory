"""Корректное чтение числовых значений памяти.

Ноль — легитимное значение (например importance=0 или confidence=0), а не
«нет данных». Конструкция ``float(x or default)`` превращала 0 в default и
искажала ранжирование контекста; здесь используется явная проверка None.
"""
from __future__ import annotations

from typing import Any


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
