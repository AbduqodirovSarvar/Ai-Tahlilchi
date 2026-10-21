# Interfeys shartnomalari

> Qoidalar: [agentlar_rejasi.md, 3-bo'lim](../agentlar_rejasi.md). Qotirilgan shartnomaga **faqat yangi narsa qo'shiladi**; buzuvchi o'zgarish orkestr ruxsati bilan va barcha foydalanuvchi agentlar yangilangandan keyin.
> Har bir o'zgarish pastdagi "O'zgarishlar jurnali"ga yoziladi.

**Umumiy holat:** T2 da yozilmoqda: 1–2 (domen, `SportAdapter`) frozen v1 sifatida taklif qilingan, orkestr tasdig'i kutilmoqda.

| # | Shartnoma | Fayl | Egasi | Foydalanuvchilar | Holat |
|---|---|---|---|---|---|
| 1 | Domen modellari | `src/app/domain/*.py` | data-engineer | hamma | qotirilgan (v1) |
| 2 | Sport adapteri | `src/app/sports/base.py` (`SportAdapter`) | data-engineer | pipeline, quant | qotirilgan (v1) |
| 3 | Baza sxemasi | `src/app/db/schema.py` | data-engineer | pipeline, bot (DTO orqali) | hali qotirilmagan |
| 4 | Tahlil servisi | `src/app/services/interfaces.py` | pipeline-engineer | bot | hali qotirilmagan |
| 5 | Soxta servis | `src/app/services/fake.py` (`FakeAnalysisService`) | pipeline-engineer | bot | hali qotirilmagan |
| 6 | Model API si | `src/app/models/__init__.py` | quant-modeler | pipeline | hali qotirilmagan |
| 7 | LLM API si | `src/app/llm/service.py` | llm-engineer | pipeline | hali qotirilmagan |

## Platforma (T1 da mavjud, infra-engineer)
Bular rasmiy shartnoma emas, lekin barcha agentlar ishlatadi; o'zgartirish faqat qo'shish orqali:
- `app.config.Settings` / `get_settings()`; maxfiy kalitlar `Settings.require_secret(name)` orqali (kalit yo'q bo'lsa `ConfigurationError`).
- `app.errors`: `AppError` → `ConfigurationError`, `ExternalServiceError`, `DataError`.
- `app.logging.get_logger(name)`.
- Qatlam chegaralari: `pyproject.toml` → `[tool.importlinter]`.

---

## 1. Domen modellari — `src/app/domain/`
**Holat:** qotirilgan (frozen v1) · **Versiya:** 1 · **Egasi:** data-engineer · **Import:** `from app.domain import ...`

Barcha turlar **o'zgarmas** (`@dataclass(frozen=True, slots=True)`), I/O yo'q. Invariant buzilsa konstruktor `DomainValidationError` (`app.errors.DataError` va `ValueError` ning vorisi) beradi: noto'g'ri obyekt umuman yaratilmaydi.

| Nom | Turi | Maydonlar / qiymatlar | Invariantlar |
|---|---|---|---|
| `Sport` | `StrEnum` | `football`, `hockey`, `basketball`, `volleyball`, `tennis` | v1 da faqat futbol adapteri bor |
| `ExternalRef` | dataclass | `source: str`, `external_id: str` | `source` kichik harfli slug (`api-football`, `football-data`); `external_id` bo'sh emas, ≤ 255 belgi, chetdagi bo'shliqlar olib tashlanadi. `str(ref)` → `source:external_id` |
| `League` | dataclass (kw) | `ref`, `sport`, `name`, `country: str \| None` | nom bo'sh emas (strip) |
| `Team` | dataclass (kw) | `ref`, `sport`, `name`, `country: str \| None`; xossa `normalized_name` | nom manbadagi yozilishda saqlanadi; `normalized_name = normalize_name(name)` |
| `normalize_name(name) -> str` | funksiya | | aksent, katta-kichik harf, apostrof va tinish belgisiz kalit: `"Nott'm Forest"` → `"nottm forest"`. Idempotent |
| `FixtureStatus` | `StrEnum` | `scheduled`, `live`, `finished`, `postponed`, `cancelled`, `abandoned` | |
| `Fixture` | dataclass (kw) | `ref`, `sport`, `league: League`, `season: int`, `kickoff: datetime`, `home: Team`, `away: Team`, `status=SCHEDULED` | `kickoff` tz-aware, **UTC ga o'tkaziladi** (naive → xato); `season` = mavsum boshlangan yil (2024/25 → `2024`), `MIN_SEASON..MAX_SEASON` (1850..2200); `home.ref != away.ref`; liga, jamoalar va o'yin sporti bir xil |
| `ResultStatus` | `StrEnum` | `finished`, `postponed`, `cancelled`, `abandoned` | |
| `FixtureResult` | dataclass (kw) | `fixture: ExternalRef`, `status`, `home_score`, `away_score`, `home_ht_score`, `away_ht_score` (`int \| None`), `home_xg`, `away_xg` (`float \| None`) | asosiy vaqt hisobi (qo'shimcha vaqt/penaltisiz). `finished` → ikkala hisob majburiy; `postponed`/`cancelled` → hisob yo'q; `abandoned` → ixtiyoriy; hisoblar ≥ 0, ikkalasi yoki hech biri; HT ≤ FT; xG chekli va ≥ 0 |
| `MarketType` | `StrEnum` | `1x2` (`MATCH_RESULT`), `double_chance`, `totals`, `btts`, `asian_handicap` | qiymatlar bazada saqlanadi, o'zgarmaydi |
| `Selection` | `StrEnum` | `home`, `draw`, `away`, `home_draw`, `home_away`, `draw_away`, `over`, `under`, `yes`, `no` | qaysi biri qaysi bozorga tegishli: `SELECTIONS_BY_MARKET` |
| `Market` | dataclass | `type: MarketType`, `line: float \| None`; konstruktorlar `match_result()`, `double_chance()`, `totals(line)`, `btts()`, `asian_handicap(home_line)`; `selections`, `require_selection(sel)` | `line` faqat `LINE_MARKETS` (`totals`, `asian_handicap`) da va u yerda majburiy; `LINE_STEP = 0.25` ga karrali, setkaga "yopishtiriladi" (`2.4999999999` → `2.5`); totals line > 0. **Fora (AH) line har doim uy jamoasi tomonidan:** `-0.5` = uy -0.5 / mehmon +0.5. "Total 2.5" va "Total 3.5" — turli bozorlar |
| `normalize_line(x) -> float` | funksiya | | 0.25 setkasiga tekshirish va yaxlitlash; `-0.0` → `0.0` |
| `OddsKind` | `StrEnum` | `opening`, `morning`, `recalc`, `prematch`, `closing` | |
| `OddsSnapshot` | dataclass (kw) | `fixture: ExternalRef`, `bookmaker: str`, `market: Market`, `selection: Selection`, `odds: float`, `taken_at: datetime`, `kind: OddsKind`; xossa `line` | `odds` chekli va **> 1.0** (`MIN_DECIMAL_ODDS_EXCLUSIVE`); `selection` bozorga tegishli; `taken_at` tz-aware → UTC; `bookmaker` slug |
| `BOOKMAKER_PINNACLE`, `BOOKMAKER_BET365`, `BOOKMAKER_MARKET_AVERAGE` (`market_avg`), `BOOKMAKER_MARKET_MAXIMUM` (`market_max`) | konstantalar | | o'rtacha/maksimal — bukmekerlar bo'yicha agregat (football-data.co.uk), "psevdo-bukmeker" sifatida saqlanadi |
| `HistoryBatch` | dataclass (kw) | `fixtures`, `results`, `odds` (tuple); xossalar `leagues`, `teams` (birinchi uchrash tartibida, takrorsiz) | adapter/importer → baza o'rtasidagi paket |

**Identifikatsiya:** tizim ichida obyektlar baza `id` si bilan emas, `ExternalRef` bilan aniqlanadi; baza `(source, external_id)` bo'yicha unique.

## 2. Sport adapteri — `src/app/sports/base.py`
**Holat:** qotirilgan (frozen v1) · **Versiya:** 1 · **Egasi:** data-engineer

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class FixtureQuery:
    date_from: date                                   # UTC sanasi, ikkala chegara kiradi
    date_to: date                                     # date_to < date_from → DomainValidationError
    leagues: frozenset[ExternalRef] = frozenset()     # bo'sh = adapter kuzatadigan barcha ligalar
    teams: frozenset[ExternalRef] = frozenset()       # ikkalasi berilsa: liga YOKI jamoa (birlashma)

class SportAdapter(Protocol):
    @property
    def sport(self) -> Sport: ...
    def supported_markets(self) -> frozenset[MarketType]: ...
    async def fetch_fixtures(self, query: FixtureQuery) -> list[Fixture]: ...
    async def fetch_odds(self, fixtures: Sequence[ExternalRef], *, kind: OddsKind) -> list[OddsSnapshot]: ...
    async def fetch_results(self, fixtures: Sequence[ExternalRef]) -> list[FixtureResult]: ...   # tugamaganlari qaytmaydi
    async def refresh_history(self, leagues: Collection[ExternalRef], season: int) -> HistoryBatch: ...
```
- Adapter faqat **olib keladi va domen turlariga o'giradi**. Bazaga yozmaydi (`sports` → `db` importi taqiqlangan; saqlash pipeline'da), bashorat qilmaydi (`predict` asosiy rejadagidan farqli ravishda adapterda **yo'q**: modellar `app.models` da).
- Nosozlik: provayder/tarmoq xatosi → `app.errors.ExternalServiceError`; bitta buzilgan yozuv loglanadi va o'tkazib yuboriladi, butun chaqiruvni yiqitmaydi.
- Takroriy chaqiruv xavfsiz: natija qiymatlar, saqlash idempotent (upsert).

## 3. Baza sxemasi — `src/app/db/schema.py`
**Holat:** hali qotirilmagan · **Versiya:** —
- Jadvallar: asosiy rejaning 7-bo'limi (to'ldiriladi)

## 4. Tahlil servisi — `src/app/services/interfaces.py`
**Holat:** hali qotirilmagan · **Versiya:** —
- `AnalysisQuery`, `AnalysisService`, `SignalView`, `RecalcDiff`: (to'ldiriladi)

## 5. Soxta servis — `src/app/services/fake.py`
**Holat:** hali qotirilmagan · **Versiya:** —
- `FakeAnalysisService`: (to'ldiriladi)

## 6. Model API si — `src/app/models/__init__.py`
**Holat:** hali qotirilmagan · **Versiya:** —
- `predict_markets`, `compute_ev`, `stake`, `confidence`: (to'ldiriladi)

## 7. LLM API si — `src/app/llm/service.py`
**Holat:** hali qotirilmagan · **Versiya:** —
- `review_candidates`, `postmortem`: (to'ldiriladi)

---

## O'zgarishlar jurnali
| Sana | Shartnoma | O'zgarish | Kim | Ruxsat |
|---|---|---|---|---|
| 2026-10-21 | 1. Domen, 2. `SportAdapter` | v1 yozildi (T2, F2.4) | data-engineer | orkestr ko'rib chiqadi |
