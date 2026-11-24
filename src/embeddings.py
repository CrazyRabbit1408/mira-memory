"""Тестовые векторы: совместимы с Chroma, не понимают смысл слов."""
from __future__ import annotations
import hashlib
import math
import re

DIMENSIONS = 128

def tokens(text: str) -> list[str]:
    """Разбить текст на слова, включая кириллицу."""
    return re.findall(r'[\w]+', text.casefold(), flags=re.UNICODE)

def stub_embedding(text: str) -> list[float]:
    """Детерминированно превратить слова в вектор; это НЕ языковая модель."""
    out = [0.0] * DIMENSIONS
    for word in tokens(text):
        h = hashlib.blake2b(word.encode('utf-8'), digest_size=8).digest()
        index = int.from_bytes(h[:4], 'little') % DIMENSIONS
        out[index] += 1.0 if h[4] & 1 else -1.0
    norm = math.sqrt(sum(x*x for x in out)) or 1.0
    return [x / norm for x in out]
