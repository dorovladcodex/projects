from __future__ import annotations

from html import unescape
import re


_MOJIBAKE_REPLACEMENTS = {
    "\u00e2\u20ac\u2122": "\u2019",  # right apostrophe
    "\u00e2\u20ac\u02dc": "\u2018",  # left apostrophe
    "\u00e2\u20ac\u0153": "\u201c",  # left quote
    "\u00e2\u20ac\u009d": "\u201d",  # right quote from C1 decode
    "\u00e2\u20ac\ufffd": "\u201d",  # right quote with replacement char
    "\u00e2\u20ac\u201c": "\u2013",  # en dash
    "\u00e2\u20ac\u201d": "\u2014",  # em dash
    "\u00e2\u20ac\u00a6": "\u2026",  # ellipsis
}


def clean_news_text(value: str) -> str:
    """Normalize RSS text without changing already-valid Unicode."""
    text = unescape(re.sub(r"<[^>]+>", " ", value))
    for broken, repaired in _MOJIBAKE_REPLACEMENTS.items():
        text = text.replace(broken, repaired)

    # Repair other UTF-8 bytes that were decoded as Windows-1252. Only attempt
    # this when strong mojibake markers are present, so valid Unicode is kept.
    if any(marker in text for marker in ("\u00c3", "\u00c2", "\u00e2\u20ac")):
        try:
            repaired = text.encode("cp1252").decode("utf-8")
            if "\ufffd" not in repaired:
                text = repaired
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass

    # Some feeds lose the middle bytes of the possessive apostrophe entirely.
    text = re.sub("\u00e2s\\b", "\u2019s", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()
