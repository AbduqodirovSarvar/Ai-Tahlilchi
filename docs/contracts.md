# Interfeys shartnomalari

> Qoidalar: [agentlar_rejasi.md, 3-bo'lim](../agentlar_rejasi.md). Qotirilgan shartnomaga **faqat yangi narsa qo'shiladi**; buzuvchi o'zgarish orkestr ruxsati bilan va barcha foydalanuvchi agentlar yangilangandan keyin.
> Har bir o'zgarish pastdagi "O'zgarishlar jurnali"ga yoziladi.

**Umumiy holat:** 1–3 (domen, `SportAdapter`, baza sxemasi) — **frozen v1, orkestr tomonidan tasdiqlangan (2026-10-21)**. 4–7 keyingi to'lqinlarda.

| # | Shartnoma | Fayl | Egasi | Foydalanuvchilar | Holat |
|---|---|---|---|---|---|
| 1 | Domen modellari | `src/app/domain/*.py` | data-engineer | hamma | qotirilgan (v1) |
| 2 | Sport adapteri | `src/app/sports/base.py` (`SportAdapter`) | data-engineer | pipeline, quant | qotirilgan (v1) |
| 3 | Baza sxemasi | `src/app/db/schema.py` | data-engineer | pipeline, bot (DTO orqali) | qotirilgan (v1) |
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
**Holat:** qotirilgan (frozen v1) · **Versiya:** 1 (Alembic `0001`) · **Egasi:** data-engineer · Qarorlar: [ADR 0002](adr/0002-domain-and-schema.md)

Foydalanish: faqat `pipeline` (va `db` ning o'zi). `bot`/`services` bazani bevosita import qilmaydi, DTO orqali oladi.

| Jadval (ORM klass) | Kalit maydonlar | Unique / tekshiruvlar | Indekslar |
|---|---|---|---|
| `leagues` (`LeagueRow`) | sport, source, external_id, name, country | `(source, external_id)` | sport |
| `teams` (`TeamRow`) | sport, source, external_id, name (manba yozilishi), normalized_name, country | `(source, external_id)` | `(sport, normalized_name)` |
| `fixtures` (`FixtureRow`) | sport, source, external_id, league_id, season, kickoff_at, home/away_team_id, status, home/away_score, home/away_ht_score, home/away_xg | `(source, external_id)`; uy ≠ mehmon; hisob ≥ 0 | kickoff_at; `(league_id, season, kickoff_at)`; `(home_team_id, kickoff_at)`; `(away_team_id, kickoff_at)`; `(status, kickoff_at)` |
| `odds_snapshots` (`OddsSnapshotRow`) | fixture_id, bookmaker, market, selection, line, odds, kind, taken_at | `(fixture_id, bookmaker, market, selection, line, kind, taken_at)`; odds > 1; line ≠ 0 faqat totals/AH da | taken_at (+ unique indeks fixture_id bo'yicha) |
| `users` (`UserRow`) | id (= Telegram user id), is_admin, timezone, bank, unit | | |
| `user_settings` (`UserSettingsRow`) | user_id (PK), sports/leagues/markets (JSON), max_signals, min_prob, min_ev, odds_min, odds_max, risk_profile, send_time, prematch_minutes, quiet_hours_start/end | 0 ≤ min_prob ≤ 1; 1 < odds_min < odds_max; max_signals ≥ 1 | |
| `watchlist` (`WatchlistRow`) | user_id, kind (team/league), sport, entity_id, notify_flags (JSON) | `(user_id, kind, entity_id)` | |
| `saved_queries` (`SavedQueryRow`) | user_id, name, date_mode, days, date_from, date_to, sports, min_prob, min_ev, scope, scope_ids, markets, max_signals, sort, schedule_cron | `(user_id, name)`; date_to ≥ date_from | |
| `signals` (`SignalRow`) | user_id, fixture_id, market, selection, line, odds, p_model, ev, confidence, stake_units, llm_verdict, llm_note, source, status | `(user_id, fixture_id, market, selection, line)`; odds > 1; 0 ≤ p_model ≤ 1 | `(status, created_at)`; `(user_id, created_at)`; fixture_id |
| `signal_versions` (`SignalVersionRow`) | signal_id, version, odds, p_model, ev, reason | `(signal_id, version)`; version ≥ 1 | |
| `results` (`SignalResultRow`) | signal_id, outcome, source, profit_units, closing_odds, clv, user_note, settled_at | signal_id (bitta natija) | |
| `scheduled_recalcs` (`ScheduledRecalcRow`) | signal_id, run_at, kind, done, done_at | `(signal_id, kind)` | `(done, run_at)` |
| `user_bets` (`UserBetRow`) | user_id, signal_id (NULL bo'lishi mumkin), odds_taken, stake, outcome, profit, placed_at, settled_at | odds_taken > 1 | `(user_id, placed_at)` |
| `postmortems` (`PostmortemRow`) | signal_id, category, reason, tags (JSON) | | signal_id |
| `lessons` (`LessonRow`) | text, tags (JSON), confirmations, status, first_seen, last_seen | confirmations ≥ 0 | status |
| `segment_stats` (`SegmentStatRow`) | sport, league_id, market, odds_bucket, n, roi, clv, calib_bias, ev_threshold, enabled | `(sport, league_id, market, odds_bucket)` | |
| `calibration_versions` (`CalibrationVersionRow`) | version, method, params (JSON), n_samples, brier_before, brier_after, active | version | active |
| `api_usage` (`ApiUsageRow`) | usage_date, provider, sport (`''` = sportga bog'liq emas), requests_used, reserved_manual | `(usage_date, provider, sport)`; hisoblagichlar ≥ 0 | |

**Umumiy qoidalar:**
- Vaqtlar `UTCDateTime` (Python'da doim tz-aware UTC; naive vaqt yozilmaydi). Pul va unit (`bank`, `unit`, `stake`, `profit`, `stake_units`, `profit_units`) `Money` → `Decimal` (4 xona). Koeffitsient va ehtimollik `float`.
- Chiziqsiz bozorlarda `line = 0` (`NO_LINE`); domen ⇄ baza o'tkazish faqat `app.db.mappers` orqali (`line_to_db`, `market_from_db`, `*_values`, `*_from_row`).
- Enumlar qiymat sifatida saqlanadi: domen enumlari (`Sport`, `FixtureStatus`, `MarketType`, `Selection`, `OddsKind`) va `app.db.enums` (`SignalSource`, `SignalStatus`, `Confidence`, `LlmVerdict`, `SignalVersionReason`, `Outcome` (win/loss/void/half_win/half_loss/cashout), `ResultSource`, `PostmortemCategory`, `LessonStatus`, `WatchKind`, `RiskProfile`, `DateMode`, `QueryScope`, `SortOrder`, `RecalcKind`).
- Munosabatlar `lazy="raise"`: bog'liq qatorlarni `selectinload`/`joinedload` bilan aniq yuklang.
- Engine: `app.db.engine.create_engine(db_path)` / `create_engine_from_settings(settings)`, `create_session_factory(engine)` (`expire_on_commit=False`). Har ulanishda WAL, `busy_timeout=5000`, `foreign_keys=ON`.
- Idempotent yozish: `app.db.history.upsert_batch(session, HistoryBatch) -> UpsertSummary` (chaqiruvchining tranzaksiyasida); qamrov: `season_coverage(session, source=None) -> list[SeasonCoverage]`.
- Migratsiya: `uv run alembic upgrade head` (yo'l: `DB_PATH` yoki `-x db_path=...`).

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
| 2026-10-21 | 3. Baza sxemasi | v1 yozildi, Alembic `0001` (T2, F2.1) | data-engineer | orkestr ko'rib chiqadi |
