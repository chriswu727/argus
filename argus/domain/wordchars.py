"""Word-boundary character class shared by text matching and element resolution."""

from __future__ import annotations

# Scripts written without spaces between words have no word boundary to test:
# "已完成" inside "任务已完成" is a real occurrence, not a mid-word match.
UNSPACED = (
    "\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff"  # Thai, Lao, Myanmar, Khmer
    "\u3040-\u30ff\u31f0-\u31ff"  # Hiragana, Katakana
    "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"  # CJK ideographs
    "\uac00-\ud7af"  # Hangul syllables (particles attach without a space)
    "\U00020000-\U0003134f"  # CJK extensions
)
WORD_CHAR = f"[^\\W{UNSPACED}]"
