from __future__ import annotations

import re
from collections.abc import Iterable


class KeywordMatcher:
    """Case-insensitive matching for complete keywords and complete phrases."""

    def contains(self, text: str, keyword: str) -> bool:
        return bool(self.pattern(keyword).search(text))

    def find_matches(self, text: str, keywords: Iterable[str]) -> list[str]:
        return [keyword for keyword in keywords if self.contains(text, keyword)]

    @staticmethod
    def pattern(keyword: str) -> re.Pattern[str]:
        # Whitespace inside a phrase is flexible, but each end must be a token boundary.
        escaped = re.escape(keyword.strip()).replace(r"\ ", r"\s+")
        return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)
