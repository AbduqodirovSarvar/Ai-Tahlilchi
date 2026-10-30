"""Hallucination guard: a note may only name teams and players that were in the input.

The check is intentionally cheap and conservative: every capitalised word of a note is
treated as a proper name and must occur (modulo accents, apostrophes and Uzbek suffixes such
as ``Chelsea'ning``) among the *values* the model was shown. JSON keys, ids, markets and
instructions are not grounding, otherwise an invented ``Homer`` would pass because the
prompt contains the key ``home``.

A sentence-initial word is checked too (models love to open a note with a name), except for
a small list of ordinary Uzbek sentence starters (:data:`SENTENCE_STARTERS`) that are
capitalised only because of their position. The price is a false positive when a note opens
with an uncommon ordinary word: the note is dropped, the verdict stays. A failing note is
dropped, never edited.

Names are compared through a normaliser of their own that keeps Cyrillic (the shared domain
normaliser is Latin-only), so a Cyrillic team name present in the input is recognised.
"""

import json
import re
import unicodedata
from typing import Final

from app.llm.builder import DATA_CLOSE, DATA_OPEN

# A "word" may contain apostrophes (Uzbek tutuq belgisi): O'rta, Ma'lumot, Chelsea'ning.
_APOSTROPHES: Final = "".join(chr(code) for code in (0x27, 0x2019, 0x60, 0xB4, 0x2BC))
_WORD: Final = re.compile(rf"[^\W\d_]+(?:[{_APOSTROPHES}][^\W\d_]+)*")
_FOLDED_WORD: Final = re.compile(r"[^\W_]+")
_APOSTROPHE_CHARS: Final = re.compile(f"[{_APOSTROPHES}]")
_SENTENCE_END: Final = frozenset(".!?")
MIN_NAME_LENGTH: Final = 3
# Shorter known words would make the suffix rule accept almost anything.
MIN_STEM_LENGTH: Final = 4

# Keys whose values are protocol words, not names: they must not ground a name.
_NON_GROUNDING_KEYS: Final = frozenset(
    {"id", "market", "selection", "status", "last", "level", "kickoff_utc"}
)

# Ordinary words that open an Uzbek sentence (folded: lower case, no apostrophes). Seeded
# from the golden notes plus the usual connectives and domain nouns of a match review.
SENTENCE_STARTERS: Final = frozenset(
    """
    bu ushbu shu shunday shuning bunday bundan bunga buning u ular uning ularning
    hozir hozircha ammo lekin biroq balki agar chunki yani yana ham hamda va yoki
    ikkala ikki bitta har hech barcha bazi qolgan oxirgi songgi avvalgi ayni hozirgi
    mezbon mezbonlar mehmon mehmonlar jamoa jamoalar oyin oyinda oyinchi oyinchilar
    himoya himoyachilar hujum hujumchi hujumchilar darvozabon markaz yarim
    forma natija natijalar kvota kvotalar model tarkib asosiy muhim ehtimol ehtimollik
    malumot malumotlar jarohat jarohatlar diskvalifikatsiya rotatsiya motivatsiya derbi
    tajriba saboq ishonch xavf omil omillar yetarli yetarlicha salbiy ijobiy
    kubok liga chempionat mavsum uchrashuv raqib raqiblar hisob gol goller
    yaxshi yomon kuchli zaif barqaror birinchi ikkinchi uchinchi
    over under btts
    taktik taktika temp pressing kontrhujum ehtiyotkor ochiq hujumkor himoyaviy uslub
    uyda safarda mezbonning mehmonning tarkiblar sxema xulosa tendensiya ssenariy kutilmoqda
    statistik gollar uy safar formasi jadvalda ozaro
    """.split()  # noqa: SIM905 - a word list reads better than a 100-item literal
)


def fold(text: str) -> str:
    """Case-, accent- and apostrophe-insensitive form that keeps non-Latin letters."""
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _APOSTROPHE_CHARS.sub("", without_marks.casefold())


def _walk_values(value: object, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, list):
        for item in value:
            _walk_values(item, out)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in _NON_GROUNDING_KEYS:
                _walk_values(item, out)


def grounding_text(data: str) -> str:
    """The text values of the data block; the whole text if it is not a data block."""
    start, end = data.find(DATA_OPEN), data.rfind(DATA_CLOSE)
    if start < 0 or end <= start:
        return data
    try:
        payload: object = json.loads(data[start + len(DATA_OPEN) : end])
    except ValueError:
        return data
    values: list[str] = []
    _walk_values(payload, values)
    return "\n".join(values)


def _known_words(grounding: str) -> frozenset[str]:
    return frozenset(_FOLDED_WORD.findall(fold(grounding_text(grounding))))


def _is_sentence_initial(text: str, start: int) -> bool:
    before = text[:start].rstrip()
    return not before or before[-1] in _SENTENCE_END


def _is_known(word: str, known: frozenset[str]) -> bool:
    if word in known:
        return True
    return any(len(stem) >= MIN_STEM_LENGTH and word.startswith(stem) for stem in known)


def unknown_names(note: str, grounding: str) -> tuple[str, ...]:
    """Capitalised words of ``note`` that are neither starters nor in ``grounding``."""
    known = _known_words(grounding)
    found: list[str] = []
    for match in _WORD.finditer(note):
        word = match.group()
        if len(word) < MIN_NAME_LENGTH or word[0] == word[0].lower():
            continue
        folded = fold(word)
        if _is_sentence_initial(note, match.start()) and folded in SENTENCE_STARTERS:
            continue
        if not _is_known(folded, known) and word not in found:
            found.append(word)
    return tuple(found)
