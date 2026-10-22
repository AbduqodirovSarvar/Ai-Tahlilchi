"""Common club names that transliteration alone cannot turn into the data source's spelling.

Users type Uzbek Latin or Cyrillic (and Russian) names: ``Аякс`` is ``ayaks`` after
transliteration, which is nowhere near ``Ajax``; ``ПСЖ`` is an abbreviation; ``Ман Сити`` is
a short form. Each entry maps the spellings people type to the names under which the data
sources store the club (API-Football and football-data.co.uk spell some clubs differently,
so there may be several targets). Used by :func:`app.db.search.rank` to widen a query; it
only ever adds candidates, so a missing entry degrades to plain fuzzy matching.

Extend it from the "no result" queries seen in production; keep targets to names the
sources really use.
"""

# Cyrillic/Latin look-alikes are the point of this table.
# ruff: noqa: RUF001

from collections.abc import Mapping
from typing import Final

# (names in the data sources) -> (spellings typed by users)
_ENTRIES: Final[tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]] = (
    # England
    (
        ("Manchester City", "Man City"),
        ("Ман Сити", "Манчестер Сити", "Man Siti", "Manchester Siti"),
    ),
    (
        ("Manchester United", "Man United"),
        ("Ман Юнайтед", "Манчестер Юнайтед", "МЮ", "Man Yunayted", "Manchester Yunayted"),
    ),
    (("Liverpool",), ("Ливерпуль", "Ливерпул", "Liverpul")),
    (("Chelsea",), ("Челси", "Chelsi")),
    (("Arsenal",), ("Арсенал",)),
    (
        ("Tottenham", "Tottenham Hotspur"),
        ("Тоттенхэм", "Тоттенхем", "Тоттенхэм Хотспур", "Tottenxem"),
    ),
    (("Newcastle", "Newcastle United"), ("Ньюкасл", "Nyukasl")),
    (("West Ham", "West Ham United"), ("Вест Хэм", "Вест Хем", "Vest Xem")),
    (("Everton",), ("Эвертон", "Everton")),
    (("Aston Villa",), ("Астон Вилла", "Aston Villa")),
    (("Leicester", "Leicester City"), ("Лестер",)),
    (("Nott'm Forest", "Nottingham Forest"), ("Ноттингем Форест", "Ноттингем")),
    # Spain
    (("Real Madrid",), ("Реал", "Реал Мадрид", "Real")),
    (("Barcelona", "FC Barcelona"), ("Барселона", "Барса", "Barsa", "Barselona")),
    (("Atletico Madrid", "Ath Madrid"), ("Атлетико", "Атлетико Мадрид", "Atletiko")),
    (("Sevilla", "Sevilla FC"), ("Севилья", "Sevilya")),
    (("Valencia", "Valencia CF"), ("Валенсия",)),
    (("Villarreal", "Villarreal CF"), ("Вильярреал", "Villareal")),
    (("Real Sociedad", "Sociedad"), ("Реал Сосьедад", "Сосьедад")),
    (("Athletic Club", "Ath Bilbao", "Athletic Bilbao"), ("Атлетик", "Атлетик Бильбао")),
    (("Real Betis", "Betis"), ("Бетис",)),
    # Germany
    (
        ("Bayern München", "Bayern Munich"),
        ("Бавария", "Бавария Мюнхен", "Bavariya", "Bavariya Myunxen"),
    ),
    (
        ("Borussia Dortmund", "Dortmund"),
        ("Боруссия Д", "Боруссия Дортмунд", "Борусия Дортмунд", "Borussiya D", "Dortmund"),
    ),
    (
        ("Borussia Mönchengladbach", "M'gladbach", "Borussia Monchengladbach"),
        ("Боруссия М", "Боруссия Менхенгладбах", "Borussiya M", "Gladbax"),
    ),
    (("Bayer Leverkusen", "Leverkusen"), ("Байер", "Байер Леверкузен", "Bayer", "Leverkuzen")),
    (("RB Leipzig",), ("РБ Лейпциг", "Лейпциг", "Leypsig")),
    (("Eintracht Frankfurt", "Ein Frankfurt"), ("Айнтрахт", "Айнтрахт Франкфурт", "Ayntraxt")),
    (("Schalke 04", "FC Schalke 04"), ("Шальке", "Shalke")),
    (("VfL Wolfsburg", "Wolfsburg"), ("Вольфсбург", "Volfsburg")),
    # Italy
    (("Juventus",), ("Ювентус", "Yuventus", "Juve")),
    (("Inter", "Internazionale"), ("Интер", "Интер Милан", "Inter Milan")),
    (("AC Milan", "Milan"), ("Милан", "Милан")),
    (("Napoli",), ("Наполи",)),
    (("AS Roma", "Roma"), ("Рома",)),
    (("Lazio",), ("Лацио", "Latsio")),
    (("Atalanta",), ("Аталанта",)),
    (("Fiorentina",), ("Фиорентина",)),
    # France
    (
        ("Paris Saint-Germain", "Paris Saint Germain", "Paris SG", "PSG"),
        ("ПСЖ", "Пари Сен-Жермен", "Пари Сен Жермен", "Пари СЖ", "PSJ", "Pari Sen-Jermen"),
    ),
    (("AS Monaco", "Monaco"), ("Монако",)),
    (("Olympique Marseille", "Marseille"), ("Марсель", "Олимпик Марсель", "Marsel")),
    (("Olympique Lyonnais", "Lyon"), ("Лион", "Lion")),
    (("Lille", "Lille OSC"), ("Лилль", "Лиль")),
    (("OGC Nice", "Nice"), ("Ницца", "Nitsa")),
    # Netherlands, Portugal, Belgium, Turkey
    (("Ajax",), ("Аякс", "Ayaks")),
    (("PSV Eindhoven", "PSV"), ("ПСВ", "ПСВ Эйндховен")),
    (("Feyenoord",), ("Фейеноорд",)),
    (("Benfica", "SL Benfica"), ("Бенфика",)),
    (("FC Porto", "Porto"), ("Порту",)),
    (("Sporting CP", "Sp Lisbon"), ("Спортинг", "Спортинг Лиссабон")),
    (("Club Brugge", "Club Brugge KV"), ("Брюгге", "Клуб Брюгге")),
    (("Anderlecht", "RSC Anderlecht"), ("Андерлехт",)),
    (("Galatasaray",), ("Галатасарай",)),
    (("Fenerbahce", "Fenerbahçe"), ("Фенербахче", "Fenerbaxche")),
    (("Besiktas", "Beşiktaş"), ("Бешикташ", "Beshiktash")),
)


def _build() -> Mapping[str, tuple[str, ...]]:
    # Keys are raw spellings here; app.db.search normalizes them with its own search_key
    # (this module must stay free of imports from it).
    table: dict[str, list[str]] = {}
    for targets, spellings in _ENTRIES:
        for spelling in spellings:
            known = table.setdefault(spelling, [])
            known.extend(target for target in targets if target not in known)
    return {spelling: tuple(targets) for spelling, targets in table.items()}


SEARCH_ALIASES: Final[Mapping[str, tuple[str, ...]]] = _build()
