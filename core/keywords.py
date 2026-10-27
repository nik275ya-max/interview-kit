"""Поиск ключевых слов в тексте и маппинг на категории перебивок."""
from __future__ import annotations

import json
import string
from typing import Dict, List

from .paths import keywords_path


class KeywordMatcher:
    def __init__(self):
        self.mapping: Dict[str, List[str]] = {}
        self.load()

    def load(self) -> None:
        p = keywords_path()
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    self.mapping = json.load(f)
            except Exception:
                self.mapping = {}
        else:
            self.mapping = {}

    def save(self) -> None:
        with open(keywords_path(), "w", encoding="utf-8") as f:
            json.dump(self.mapping, f, indent=2, ensure_ascii=False)

    def find_categories(self, text: str) -> List[str]:
        """Возвращает список категорий, чьи ключевые слова есть в тексте."""
        if not text or not self.mapping:
            return []
        lower = text.lower()
        found: List[str] = []
        seen = set()
        for keyword, cats in self.mapping.items():
            if self._contains_word(lower, keyword.lower()):
                for c in cats:
                    if c not in seen:
                        found.append(c)
                        seen.add(c)
        return found

    @staticmethod
    def _contains_word(text: str, word: str) -> bool:
        """Поиск слова с проверкой границ (корректно для кириллицы)."""
        if not word:
            return False
        idx = text.find(word)
        while idx != -1:
            before = text[idx - 1] if idx > 0 else " "
            after = text[idx + len(word)] if idx + len(word) < len(text) else " "
            # граница = не-буква/не-цифра с обеих сторон
            if not before.isalnum() and not after.isalnum():
                return True
            idx = text.find(word, idx + 1)
        return False

    def all_keywords(self) -> List[str]:
        return list(self.mapping.keys())
