"""Hallucination guard: a note may only name teams and players that were in the input.

The check is intentionally cheap and conservative: a capitalised word in the middle of a
sentence is treated as a proper name and must occur (modulo accents, apostrophes and Uzbek
suffixes such as ``Chelsea'ning``) in the text the model was shown. Sentence-initial words
are skipped because ordinary words are capitalised there; the price is that an unknown name
at the very start of a sentence is not caught. A failing note is dropped, never edited.
"""

import re
from typing import Final

from app.domain.entities import normalize_name

# A "word" may contain apostrophes (Uzbek tutuq belgisi): O'rta, Ma'lumot, Chelsea'ning.
_APOSTROPHES: Final = "".join(chr(code) for code in (0x27, 0x2019, 0x60, 0xB4))
_WORD: Final = re.compile(rf"[^\W\d_]+(?:[{_APOSTROPHES}][^\W\d_]+)*")
_SENTENCE_END: Final = frozenset(".!?")
MIN_NAME_LENGTH: Final = 3
# Shorter known words would make the suffix rule accept almost anything.
MIN_STEM_LENGTH: Final = 4


def _known_words(grounding: str) -> frozenset[str]:
    return frozenset(normalize_name(grounding).split())


def _is_sentence_initial(text: str, start: int) -> bool:
    before = text[:start].rstrip()
    return not before or before[-1] in _SENTENCE_END


def _is_known(word: str, known: frozenset[str]) -> bool:
    if word in known:
        return True
    return any(len(stem) >= MIN_STEM_LENGTH and word.startswith(stem) for stem in known)


def unknown_names(note: str, grounding: str) -> tuple[str, ...]:
    """Capitalised mid-sentence words of ``note`` that do not occur in ``grounding``."""
    known = _known_words(grounding)
    found: list[str] = []
    for match in _WORD.finditer(note):
        word = match.group()
        if len(word) < MIN_NAME_LENGTH or not word[0].isupper():
            continue
        if _is_sentence_initial(note, match.start()):
            continue
        if not _is_known(normalize_name(word), known) and word not in found:
            found.append(word)
    return tuple(found)
