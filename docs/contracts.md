# Interfeys shartnomalari

> Qoidalar: [agentlar_rejasi.md, 3-bo'lim](../agentlar_rejasi.md). Qotirilgan shartnomaga **faqat yangi narsa qo'shiladi**; buzuvchi o'zgarish orkestr ruxsati bilan va barcha foydalanuvchi agentlar yangilangandan keyin.
> Har bir o'zgarish pastdagi "O'zgarishlar jurnali"ga yoziladi.

**Umumiy holat (2026-10-23, T6):** 1–2 (domen, `SportAdapter`) — **frozen v1** (orkestr tasdiqlagan, 2026-10-21). 3 (baza sxemasi va repository) — **v1.11, Alembic `0010`**: frozen v1 + qo'shimchalar v1.1–v1.11. 4 (servis shartnomasi) — **v1.10**; 5 (soxta servis) — **v1.10**: frozen v1 + qo'shimchalar v1.1–v1.10; v1.5–v1.10 orkestr ko'rib chiqishini kutadi. 6 (model API) — **v1.3**: frozen v1 + qo'shimchalar v1.1–v1.3. 7 (LLM API) — **v1.2, frozen** (v1 taklifi T4 da yozilgan; keyingi qo'shimchalar v1.1–v1.2).
**Umumiy holat (2026-10-23, T6):** 1–2 (domen, `SportAdapter`) — **frozen v1** (orkestr tasdiqlagan, 2026-10-21). 3 (baza sxemasi va repository) — **v1.7, Alembic `0009`**: frozen v1 + qo'shimchalar v1.1–v1.7. 4 (servis shartnomasi) — **v1.11**; 5 (soxta servis) — **v1.11**: frozen v1 + qo'shimchalar v1.1–v1.11; v1.5–v1.11 orkestr ko'rib chiqishini kutadi. 6 (model API) — **v1.3**: frozen v1 + qo'shimchalar v1.1–v1.3. 7 (LLM API) — **v1.2, frozen** (v1 taklifi T4 da yozilgan; keyingi qo'shimchalar v1.1–v1.2).

| # | Shartnoma | Fayl | Egasi | Foydalanuvchilar | Holat |
|---|---|---|---|---|---|
| 1 | Domen modellari | `src/app/domain/*.py` | data-engineer | hamma | qotirilgan (v1) |
| 2 | Sport adapteri | `src/app/sports/base.py` (`SportAdapter`) | data-engineer | pipeline, quant | qotirilgan (v1) |
| 3 | Baza sxemasi va repository | `src/app/db/schema.py`, `src/app/db/repo/` | data-engineer | pipeline (bot/servislar DTO orqali) | qotirilgan (v1), qo'shimchalar v1.11 gacha (Alembic `0010`) |
| 4 | Servis shartnomasi | `src/app/services/interfaces.py`, `src/app/services/errors.py` | pipeline-engineer | bot | frozen v1 (orkestr tasdiqlagan, 2026-10-21), qo'shimchalar v1.10 gacha |
| 3 | Baza sxemasi va repository | `src/app/db/schema.py`, `src/app/db/repo/` | data-engineer | pipeline (bot/servislar DTO orqali) | qotirilgan (v1), qo'shimchalar v1.7 gacha (Alembic `0009`) |
| 4 | Servis shartnomasi | `src/app/services/interfaces.py`, `src/app/services/errors.py` | pipeline-engineer | bot | frozen v1 (orkestr tasdiqlagan, 2026-10-21), qo'shimchalar v1.11 gacha |
| 5 | Soxta servis | `src/app/services/fake.py` (`create_fake_services`, `FakeAnalysisService`, ...) | pipeline-engineer | bot | frozen v1 (orkestr tasdiqlagan, 2026-10-21), qo'shimchalar v1.10 gacha |
| 6 | Model API si | `src/app/models/__init__.py` | quant-modeler | pipeline | frozen v1 (orkestr tasdiqlagan, 2026-10-21), qo'shimchalar v1.3 gacha |
| 7 | LLM API si | `src/app/llm/service.py`, `src/app/clients/gemini.py` | llm-engineer | pipeline | frozen, v1.2 |

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
| `BOOKMAKER_PINNACLE`, `BOOKMAKER_BET365`, `BOOKMAKER_1XBET` (`1xbet`, T3 qo'shimcha), `BOOKMAKER_MARKET_AVERAGE` (`market_avg`), `BOOKMAKER_MARKET_MAXIMUM` (`market_max`) | konstantalar | | o'rtacha/maksimal — bukmekerlar bo'yicha agregat (football-data.co.uk), "psevdo-bukmeker" sifatida saqlanadi |
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
**Holat:** qotirilgan (frozen v1, qo'shimchalar bilan) · **Versiya:** 1.11 (Alembic `0010`) · **Egasi:** data-engineer · Qarorlar: [ADR 0002](adr/0002-domain-and-schema.md), [ADR 0007](adr/0007-api-client-and-repositories.md)

Foydalanish: faqat `pipeline` (va `db` ning o'zi). `bot`/`services` bazani bevosita import qilmaydi, DTO orqali oladi.

| Jadval (ORM klass) | Kalit maydonlar | Unique / tekshiruvlar | Indekslar |
|---|---|---|---|
| `leagues` (`LeagueRow`) | sport, source, external_id, name, country | `(source, external_id)` | sport |
| `teams` (`TeamRow`) | sport, source, external_id, name (manba yozilishi), normalized_name, country | `(source, external_id)` | `(sport, normalized_name)` |
| `fixtures` (`FixtureRow`) | sport, source, external_id, league_id, season, kickoff_at, home/away_team_id, status, home/away_score, home/away_ht_score, home/away_xg | `(source, external_id)`; uy ≠ mehmon; hisob ≥ 0 | kickoff_at; `(league_id, season, kickoff_at)`; `(home_team_id, kickoff_at)`; `(away_team_id, kickoff_at)`; `(status, kickoff_at)` |
| `odds_snapshots` (`OddsSnapshotRow`) | fixture_id, bookmaker, market, selection, line, odds, kind, taken_at | `(fixture_id, bookmaker, market, selection, line, kind, taken_at)`; **`opening`/`closing` uchun `taken_at` siz ham unique** (`uq_odds_snapshots_single_moment`, qisman indeks: bitta tanlovga bitta opening va bitta closing); odds > 1; line ≠ 0 faqat totals/AH da | taken_at (+ unique indeks fixture_id bo'yicha) |
| `users` (`UserRow`) | id (= Telegram user id), is_admin, timezone, bank, unit | | |
| `user_settings` (`UserSettingsRow`) | user_id (PK), sports/leagues/markets (JSON), max_signals, min_prob, min_ev, odds_min, odds_max, risk_profile, send_time, prematch_minutes, quiet_hours_start/end, daily_loss_limit, weekly_loss_limit (Money, NULL = limit yo'q; `0002`) | 0 ≤ min_prob ≤ 1; 1 < odds_min < odds_max; max_signals ≥ 1 | |
| `watchlist` (`WatchlistRow`) | user_id, kind (team/league), sport, entity_id, notify_flags (JSON) | `(user_id, kind, entity_id)` | |
| `saved_queries` (`SavedQueryRow`) | user_id, name, date_mode, days, date_from, date_to, sports, min_prob, min_ev, scope, scope_ids, markets, max_signals, sort, per_day, include_no_value, schedule_weekdays (JSON, ISO 1..7; bo'sh = jadvalsiz), schedule_at (mahalliy vaqt, NULL = jadvalsiz) (`0005`; `schedule_cron` o'rniga) | `(user_id, name)`; date_to ≥ date_from | |
| `signals` (`SignalRow`) | user_id, fixture_id, market, selection, line, odds, p_model, ev, confidence, stake_units, llm_verdict, llm_note, source (`analysis`/`daily`/`custom`/`watchlist`/`retro`), status, recommended_at (UTC, NULL = tavsiya emas; bir marta yoziladi), min_prob, min_ev, odds_min, odds_max (yaratilish/tavsiya paytidagi chegaralar; `0003`) | `(user_id, fixture_id, market, selection, line)`; odds > 1; 0 ≤ p_model ≤ 1 | `(status, created_at)`; `(user_id, created_at)`; fixture_id; `(user_id, recommended_at)` (`0003`) |
| `signal_versions` (`SignalVersionRow`) | signal_id, version, odds, p_model, ev, reason | `(signal_id, version)`; version ≥ 1 | |
| `results` (`SignalResultRow`) | signal_id, outcome, source, profit_units, closing_odds, clv, user_note, settled_at | signal_id (bitta natija) | |
| `scheduled_recalcs` (`ScheduledRecalcRow`) | signal_id, run_at, kind, done, done_at | `(signal_id, kind)` | `(done, run_at)` |
| `user_bets` (`UserBetRow`) | user_id, signal_id (NULL bo'lishi mumkin), odds_taken, stake, outcome, profit, placed_at, settled_at, client_token (NULL = tokensiz; `0006`) | odds_taken > 1 | `(user_id, placed_at)`; signal_id (`0002`); **unique `(user_id, client_token)`** (NULL lar to'qnashmaydi; `0006`) |
| `signal_notes` (`SignalNoteRow`, `0006`) | user_id, signal_id, text, created_at | | `(signal_id, created_at)` |
| `pipeline_ledger` (`LedgerRow`, `0004`) | job, day, user_id (0 = tizim; FK yo'q), subject, status, claimed_at, done_at, detail | `(job, day, user_id, subject)` | `(job, done_at)` |
| `postmortems` (`PostmortemRow`) | signal_id, category, reason, tags (JSON) | | signal_id |
| `lessons` (`LessonRow`) | text, tags (JSON), confirmations, status, first_seen, last_seen | confirmations ≥ 0 | status |
| `segment_stats` (`SegmentStatRow`) | sport, league_id, market, odds_bucket, n, roi, clv, calib_bias, ev_threshold, enabled | `(sport, league_id, market, odds_bucket)` | |
| `calibration_versions` (`CalibrationVersionRow`) | version, method, params (JSON), n_samples, brier_before, brier_after, active | version | active |
| `api_usage` (`ApiUsageRow`) | usage_date (UTC kuni), provider, sport (`''` = sportga bog'liq emas), requests_used (kunning barcha so'rovlari), reserved_manual (ulardan qo'lda qilinganlari; zaxira hajmi saqlanmaydi, ADR 0007) | `(usage_date, provider, sport)`; hisoblagichlar ≥ 0 | |

**Umumiy qoidalar:**
- Vaqtlar `UTCDateTime` (Python'da doim tz-aware UTC; naive vaqt yozilmaydi). Pul va unit (`bank`, `unit`, `stake`, `profit`, `stake_units`, `profit_units`) `Money` → `Decimal` (4 xona). Koeffitsient va ehtimollik `float`.
- Chiziqsiz bozorlarda `line = 0` (`NO_LINE`); domen ⇄ baza o'tkazish faqat `app.db.mappers` orqali (`line_to_db`, `market_from_db`, `*_values`, `*_from_row`).
- Enumlar qiymat sifatida saqlanadi: domen enumlari (`Sport`, `FixtureStatus`, `MarketType`, `Selection`, `OddsKind`) va `app.db.enums` (`SignalSource`, `SignalStatus`, `Confidence`, `LlmVerdict`, `SignalVersionReason`, `Outcome` (win/loss/void/half_win/half_loss/cashout), `ResultSource`, `PostmortemCategory`, `LessonStatus`, `WatchKind`, `RiskProfile`, `DateMode`, `QueryScope`, `SortOrder`, `RecalcKind`).
- Munosabatlar `lazy="raise"`: bog'liq qatorlarni `selectinload`/`joinedload` bilan aniq yuklang.
- Engine: `app.db.engine.create_engine(db_path)` / `create_engine_from_settings(settings)`, `create_session_factory(engine)` (`expire_on_commit=False`). Har ulanishda WAL, `busy_timeout=5000`, `foreign_keys=ON`.
- Idempotent yozish: `app.db.history.upsert_batch(session, HistoryBatch) -> UpsertSummary` (chaqiruvchining tranzaksiyasida); qamrov: `season_coverage(session, source=None) -> list[SeasonCoverage]`.
- O'yin holati/hisobi: `app.db.fixture_state.merge_outcome(stored, status, result) -> FixtureOutcome` — barcha yozish yo'llari uchun yagona qoida (natija hal qiluvchi; `finished` faqat hisob bilan; `scheduled`/`postponed`/`cancelled` hisobni tozalaydi; xG birlashtiriladi). `mappers.fixture_values(..., outcome=...)` (oldingi `result=` parametri o'rniga).
- Migratsiya: `uv run alembic upgrade head` (yo'l: `DB_PATH` yoki `-x db_path=...`).

### 3.1 Repository API — `from app.db.repo import ...` (v1.2, T3, faqat qo'shimcha)
Barcha funksiyalar `async`, birinchi argument `AsyncSession`, **chaqiruvchining tranzaksiyasida** ishlaydi (flush qiladi, commit qilmaydi). Domen obyektlari yoki `frozen` DTO (`app.db.repo.records`) qaytaradi, ORM qator hech qachon. Vaqt oralig'i — UTC, yarim ochiq `[start, end)`. Egalik: `user_id` WHERE ga kiradi, boshqa foydalanuvchining qatori "yo'q" kabi.

| Guruh | Funksiyalar | Izoh |
|---|---|---|
| O'yinlar | `list_fixtures(start, end, *, sport, leagues, teams, statuses, offset, limit) -> Page[FixtureRecord]`; `get_fixtures(refs) -> dict[ExternalRef, FixtureRecord]`; `get_fixture_by_id(id)`; `finished_fixtures(*, leagues, start, end, preferred_sources=()) -> list[FixtureRecord]`; `upsert_live(*, fixtures, results, odds) -> UpsertSummary` | liga YOKI jamoa (birlashma), kickoff bo'yicha; `FixtureRecord(id, fixture, result, home_xg, away_xg)`; model uchun tarix: faqat hisobli `finished`, `(liga, mavsum, uy, mehmon)` bo'yicha manbalar orasida takrorsiz; yozish `upsert_batch` + `merge_outcome` orqali |
| Koeffitsient | `latest_odds(fixtures, *, kinds=DEFAULT_KIND_PRIORITY, bookmakers=()) -> dict[ExternalRef, list[OddsSnapshot]]`; `odds_history(fixture, *, kinds, bookmakers) -> list[OddsSnapshot]` | (o'yin, bukmeker, bozor, chiziq, tanlov) uchun bitta narx: `kinds` dagi birinchi mavjud tur, tur ichida eng so'nggi `taken_at`; standart ustuvorlik closing → prematch → recalc → morning → opening |
| Signallar | `upsert_signal(draft: SignalDraft, *, reason) -> SignalRecord`; `get_signal(id, *, user_id=None)`; `list_user_signals(user_id, *, start, end, include_hidden=False, include_retro=False)`; `report_signals(user_id, *, start=None, end=None)`; `signals_for_fixtures(fixture_ids, *, statuses=(ACTIVE,))`; `unsettled_signals(*, kicked_off_before)`; `set_signal_status(id, status, *, user_id=None) -> bool`; `signal_versions(id)` | dedupe kaliti foydalanuvchi + o'yin + bozor + chiziq + tanlov; har chaqiruv yangi versiya (yangi signal — 1, `initial`); `recommended_at` va o'sha paytdagi `Thresholds` bir marta yoziladi; retro qoralama retro bo'lmagan signalni o'zgartirmaydi; **`report_signals`: `recommended_at` bor, `source != retro`, yashirilganlari kiradi** (hisobot, kalibratsiya, kun yakuni, eksport, `pending_results` uchun bir xil); `SignalRecord.retro`, `.version`, `.result` |
| Natija | `settle_signal(SignalResultRecord) -> SignalResultRecord`; `set_result_note(signal_id, note) -> bool` | signal uchun bitta; qo'lda (`manual`) natijani `api` almashtirmaydi |
| Foydalanuvchi | `ensure_user(id, *, timezone, is_admin=False)`; `get_user`; `update_user(id, *, timezone, bank, unit)`; `get_user_settings(id) -> UserSettingsRecord \| None`; `upsert_user_settings(UserSettingsRecord)` | sozlamalar to'liq almashtiriladi (zarar limitlari bilan); `None` → servis standartlarini qo'llaydi |
| Sevimlilar | `list_watchlist(user_id)`; `add_watch(user_id, *, kind, sport, entity_id, notify_flags=())` (idempotent); `remove_watch(...) -> bool`; `set_watch_notifications(...) -> WatchRecord \| None` | `entity_id` = `teams.id`/`leagues.id` |
| Shablonlar | `save_query(SavedQueryDraft) -> SavedQueryRecord` (bir xil nom — almashtiradi); `list_saved_queries(user_id)`; `get_saved_query(user_id, id)`; `delete_saved_query(user_id, id) -> bool` | |
| Qayta hisob navbati | `enqueue_recalc(signal_id, *, kind, run_at) -> RecalcTask`; `claim_due_recalcs(*, now, limit=50, lease=15 daq) -> list[RecalcTask]`; `complete_recalc(id, *, now) -> bool`; `release_recalc(id) -> bool`; `cancel_recalcs(signal_id, *, now) -> int` | lease asosida: egallash `done_at` ga yoziladi (`RecalcTask.claimed_at`), bitta shartli `UPDATE ... RETURNING`; muddati o'tgan egallash qayta olinadi; bir xil vaqt bilan qayta qo'shish — o'zgarishsiz, yangi vaqt — vazifa qayta ochiladi |
| Stavkalar | `create_bet(*, user_id, signal_id, odds_taken, stake, placed_at)`; `list_bets(user_id, *, open_only=False, limit=20)`; `bets_placed_between(user_id, *, start, end)`; `bets_for_signal(signal_id)`; `settle_bet(id, *, outcome, profit, settled_at)` | pul yig'indisi Python'da (ADR 0002) |
| API sarfi | `app.db.api_usage.SqlUsageStore(session_factory)` (`app.clients.quota.UsageStore`); `usage_on(session, day) -> list[ApiUsageRecord]` | har o'zgarish o'z tranzaksiyasida commit qilinadi |
| Qidiruv | `app.db.search.search_teams(session, text, *, sport=None, limit=10) -> list[TeamHit]`; sof: `to_latin`, `to_cyrillic`, `search_key`, `rank` | kirill/lotin, katta-kichik harf, diakritika va apostrofga sezgir emas; nom/so'z boshi ustun; `TeamHit(team_id, ref, sport, name, league, score)` |

**v1.3 (T3 fix, faqat qo'shimcha/aniqlashtirish):**
- Qoida: **tarmoqqa chiqishdan oldin commit qiling; da'volar (`claim_due_recalcs`) o'z qisqa tranzaksiyasida.** `SqlUsageStore` ni `create_usage_engine(db_path)` ustida quring: ochiq yozuv tranzaksiyasi bor bo'lsa ham `charge` kutmaydi, yozuv xotirada saqlanib keyin yoziladi (`flush()`).
- `complete_recalc(id, *, now, claimed_at)` va `release_recalc(id, *, claimed_at)` — egalik (`RecalcTask.claimed_at`) tekshiriladi; `settle_bet(..., user_id, ...)` va `set_result_note(signal_id, note, *, user_id)` — foydalanuvchi bo'yicha cheklangan (begonaniki "topilmadi").
- `finished_fixtures`: takror = bir xil `(liga, mavsum, uy, mehmon)` va o'yin vaqti +-2 kun (`DUPLICATE_KICKOFF_WINDOW`). Manbalar faqat ikkala jamoa moslashtirilganda birlashadi; **model tarixni faqat shu funksiya orqali o'qiydi.**
- `latest_odds`: tur (bukmeker, bozor, chiziq) guruhi bo'yicha tanlanadi, tanlovlar turlar bo'yicha aralashmaydi.
- `upsert_signal`: parallel birinchi tahlilda yutqazuvchi 2-versiya bo'ladi (xato emas).

**v1.4 (T4 data round; ADR [0011](adr/0011-t4-data-round.md)):**
- Qo'shildi: `app.db.repo.catalog` (`list_leagues`, `get_league`, `get_team`, `league_refs`, `team_refs`, `league_teams`, `user_ids`; `app.pipeline.queries` dan ko'chirildi, u yerda yupqa re-export qoldi); `app.db.repo.notes` (`add_signal_note(session, user_id, signal_id, text, *, created_at) -> SignalNoteRecord | None` (begona/yo'q signal -> `None`), `list_signal_notes(session, user_id, signal_id)`); `get_bet_by_token(session, user_id, token)`; `SignalNoteRecord`.
- `create_bet(..., client_token=None)`: token bilan **idempotent** — `(user_id, token)` takrorlansa birinchi stavka qaytadi, hech narsa yozilmaydi (poyga: unique indeks + savepoint). `UserBetRecord.client_token`.
- `SavedQueryDraft`: **`schedule_cron` olib tashlandi** (yagona iste'molchi `services/real/account.py` yangilandi; bu faqat-qo'shimcha qoidadan ongli istisno, ADR 0011 §2), o'rniga `per_day`, `include_no_value`, `schedule_weekdays: tuple[int, ...]`, `schedule_at: time | None` (ikkalasi birga yoki hech biri: `ValueError`).
- `pipeline_ledger` endi Alembic jadvali (`LedgerRow`); `Ledger` ochiq API si o'zgarmagan, `ensure_schema` faqat migratsiyadan o'tmagan baza uchun zaxira.

**v1.7 (T5 pipeline, faqat qo'shimcha; [ADR 0015](adr/0015-feedback-loop-pipeline.md)):**
- `FixtureRecord.league_id/home_team_id/away_team_id` (`int | None = None`; `to_record` to'ldiradi): katalog id lari (`leagues.id`, `teams.id`).
- Yangi modul `app.db.repo.feedback` (paket `__init__` ro'yxatida yo'q, to'g'ridan-to'g'ri import): `PostmortemRecord`, `LessonRecord`, `UserNote`; `lost_signal_ids`, `pick_postmortem`, `add_postmortem` (signal uchun idempotent), `list_postmortems`, `list_lessons`, `save_lesson`, `team_records`, `user_notes_for_teams`. Jadvallar (`postmortems`, `lessons`, `signal_notes`) o'zgarmagan, migratsiya yo'q.
- `app.db.repo.operations.bets_settled_between(session, user_id, *, start, end)`: yopilish vaqti bo'yicha yarim ochiq oraliq, foydasi ma'lum stavkalar (zarar limiti).

**v1.5 (T4 fix round, faqat qo'shimcha; [ADR 0008](adr/0008-pipeline-and-real-services.md) qo'shimchasi):**
- Alembic `0007` (downgrade bilan): `signals.recommended_odds` (birinchi tavsiya qilingan koeffitsient, `recommended_at` bilan bir marta yoziladi, o'zgarmaydi; mavjud qatorlar birinchi `signal_versions.odds` dan to'ldiriladi), `signals.fair_odds` (nol-EV narx), `signals.quoted_at` (narx olingan vaqt), `pipeline_ledger.attempts`/`retry_after`.
- Repository: `SignalDraft.fair_odds`/`quoted_at`, `SignalRecord.recommended_odds`/`fair_odds`/`quoted_at` + xossa `first_odds`; `reschedule_recalc(task_id, *, run_at, claimed_at)` (backoff bilan qaytarish), `open_bets_of_settled_signals()`, `touch_signal_quote_time(id, quoted_at)`, `latest_odds(..., taken_before=None)` (kickoffdan keyingi qayta yuborilgan narx yopilish sifatida olinmaydi); `report_signals`/`unsettled_signals` holatga qaraydi (bekor qilingan signal o'yin o'ynalmaguncha yoki butunlay bekor qilinmaguncha hisobotga/yopishga kirmaydi).
- Servis (§4): `SignalView.recommended_odds` (foyda va CLV shu koeffitsientda; `odds` — joriy, qayta hisob uni yangilaydi; qaror #17), `SignalView.zero_ev_odds` (`fair_odds` xossasi endi nol-EV narx, chorak chiziqli Osiyo uchun `1/probability` emas), `SignalView.profit_odds`; `RecalcVerdict.NO_PRICE` (bukmekerda yangi narx yo'q; model-only EV hech qachon `still_valid` emas); `AnalysisResult.skipped_no_price`/`skipped_stale_price`/`data_stale` (bot "narx yo'q" va "ma'lumot yangilanmadi" ni ko'rsatadi); `ApiQuota.manual_reserve`/`manual_used`/`manual_left` (`reserved_manual` eski o'quvchilar uchun qoldi); `SystemStatus.llm: LlmQuota | None` (`requests_used/limit`, `tokens_used/limit`).
- Qoidalar: `place_bet` yopilgan, bekor qilingan va boshlangan o'yinning signaliga `ValidationFailedError` beradi (bir xil `client_token` takrori esa birinchi stavkani qaytaradi); `data_as_of` — endi har doim bukmeker narxi olingan vaqt (qayta hisobda faqat yangi ma'lumot olinmaganda); `pending_results` qoldirilgan o'yinlarni o'tkazib yuboradi.
- Pipeline qoidalari (shartnoma emas): `Delivery`/`Notifier` `None` qaytarsa — "yetkazilmadi", kalit ochiq qoladi; ledger kaliti `fail` da urinishlar sonini va eksponensial `retry_after` ni yuritadi, `MAX_ATTEMPTS` (5) dan keyin `exhausted` + adminga (throttle bilan) ogohlantirish.

**v1.11 (T7-A, provayder chaqiruvlari jurnali; [ADR 0020](adr/0020-provider-call-log.md), faqat qo'shimcha):**
- Domen (`app.domain.call_log`, T7 boshida yozilgan, o'zgarmagan): `CallRecord`, `CallRecorder`, `Provider`, `CallOutcome`, `clip_text`, `MAX_LOGGED_TEXT`.
- Alembic `0010` (downgrade bilan, faqat yangi jadval): `provider_calls(id, at, provider, operation, outcome, duration_ms, request, response, http_status, quota_cost, size_bytes, error)`, indekslar `(at)` va `(provider, at)`. Sxema: `ProviderCallRow`.
- Repo (`app.db.repo`, modul `app.db.repo.calls`; chaqiruvchining tranzaksiyasida): `record_call(session, call: CallRecord) -> int`; `list_calls(session, *, provider=None, outcome=None, limit, offset) -> list[CallSummary]` (yangisi birinchi, tenglikda `id` kamayish tartibida; javob matni **qaytarilmaydi**, `request_preview` = so'rovning birinchi `REQUEST_PREVIEW_CHARS` (200) belgisi); `count_calls(session, *, provider=None, outcome=None) -> int`; `get_call(session, call_id) -> CallDetail | None` (to'liq `request`/`response`); `purge_calls_older_than(session, cutoff, *, limit=None) -> int`; `call_stats(session, since) -> list[CallStat]` (provider+operation bo'yicha `calls`, `errors`, `cached`, `blocked`, `avg_duration_ms` (faqat provayderga yetgan chaqiruvlar), `quota_cost`). DTO lar: `CallSummary`, `CallDetail`, `CallStat` (`app.db.repo.records`).
- `app.db.call_log.SqlCallRecorder(sessions, *, scrub)` (`CallRecorder`): o'z qisqa tranzaksiyasi; matn avval `scrub`, keyin `clip_text`; yozish xatosi chaqiruvni buzmaydi (turi va kontekst loglanadi, matn emas). `scrub` ni `pipeline/runtime` beradi (`pipeline.redaction.scrub_text`).
- Klientlar: `ApiClient(..., recorder=None)`, `create_api_football_client(..., recorder=None)`, `GeminiClient(..., recorder=None)`, `create_gemini_client(settings, recorder=None)`, `create_llm_service(settings, budget=None, recorder=None)`. API-Football: `operation` = endpoint yo'li, `request` = so'rov parametrlari JSON; har HTTP urinish alohida yozuv (`quota_cost=1`), keshdan javob `cached` (0), kvota rad etishi `quota_blocked` (0). Gemini: `operation` = javob sxemasi nomi (`response_schema.__name__`), `request` = `[system]` + `[user]` matnlari, bir `generate_json` chaqiruvi = bir yozuv (`quota_cost` = urinishlar soni).
- Sozlama `CALL_LOG_RETENTION_DAYS` (standart 30, 1–365) -> `PipelineConfig.call_log_retention`; haftalik `retention` job `provider_calls` ni ham tozalaydi (`RetentionReport.calls_deleted`).

### 3.2 API klient va futbol adapteri (T3, shartnoma emas, ma'lumot uchun)
- `app.clients.api_football.create_api_football_client(api_key, store, *, daily_limit=100, manual_reserve_share=0.30) -> (ApiFootballClient, httpx.AsyncClient)`; kvota holati `client.api.quota.status() -> QuotaStatus(remaining, automatic_available, reserve_remaining)`; `QuotaExceededError(provider, purpose, needed, available)` (`ExternalServiceError`); `app.clients.cost.estimate_requests(FetchPlan) -> int`.
- v1.3: `QuotaGuard.reconcile(key, *, provider_limit, provider_remaining)` (kalit — `charge` natijasidagi); `shared_guard(store, policy, *, provider, sport)`; `ApiClient.get_json(...).fetched_at`; `cache_ttl=0` keshni o'qimaydi; `ProviderConfig.retryable_body`; `TokenBucket.acquire(urgent=)`; `FetchPlan.max_xg_requests`; `FootballAdapter.with_expected_goals(results, *, max_requests=8)`; `match_leagues(...)`, `TeamMatchReport.fuzzy`; sozlama `HANDICAP_CONVENTION` (`home_line` | `own_line`, `HandicapConvention(settings.handicap_convention)`).
- `app.sports.football.FootballAdapter(client, *, catalog, identity, purpose, clock)` — `SportAdapter` + `with_expected_goals(results)`, `fetch_injuries(fixture)`, `fetch_lineups(fixture)`, `for_purpose(MANUAL)`, `with_identity(IdentityMap)`. Jamoa moslash: `app.sports.team_matching.match_teams(...) -> TeamMatchReport`, `IdentityMap.from_report(report, LeagueCatalog())`.

## 4. Servis shartnomasi — `src/app/services/interfaces.py`
**Holat:** frozen v1 (orkestr tasdiqlagan, 2026-10-21), qo'shimchalar v1.1–v1.11 (v1.2 dan keyingilari orkestr ko'rib chiqishini kutadi) · **Versiya:** 1.11 · **Egasi:** pipeline-engineer · Qarorlar: [ADR 0004](adr/0004-service-contract.md) · **Import:** `from app.services.interfaces import ...`, `from app.services.errors import ...`

Bot tizim bilan **faqat** shu modul orqali gaplashadi (domen qiymat turlari `Sport`, `Market`, `MarketType`, `Selection` ham ishlatiladi). Barcha DTO lar `@dataclass(frozen=True, slots=True)`; noto'g'ri kiritma konstruktorda `ValidationFailedError` beradi (noto'g'ri so'rov obyekti umuman yaratilmaydi). Barcha metodlar `async`.

**Konvensiyalar**
- Lahzalar (`kickoff`, `recalculated_at`, `data_as_of`) — tz-aware **UTC** `datetime`. Foydalanuvchi tanlaydigan **sanalar** (`DateSpec`, `DateWindow`, `ReportRequest`, `day_summary(day)`) — foydalanuvchi vaqt mintaqasidagi (`UserSettingsView.timezone`, standart `Asia/Tashkent`) kalendar sanalari; UTC ga servis o'tkazadi.
- Ehtimollik, koeffitsient, EV — `float` (`ev=0.05` = +5%); pul va unit — `Decimal`.
- ID lar shaffof emas: `SignalId`, `BetId`, `TemplateId` — `int`; liga/jamoa/o'yin/so'rov — `EntityId = str` (≤ 40 UTF-8 bayt — `MAX_ENTITY_ID_BYTES`, `is_valid_entity_id`; 64 baytlik `callback_data` da amal prefiksi uchun joy qoladi; DTO lar tekshiradi).
- Takroriy tahlil bir xil signal ID larini qaytaradi (kalit: foydalanuvchi + o'yin + bozor + tanlov).

**Nomlangan chegaralar (bot shulardan foydalanadi, raqamni takrorlamaydi)**

| Konstanta | Qiymat | Ma'nosi |
|---|---|---|
| `MIN_PROBABILITY_LOWER` / `_UPPER` | 0.30 / 0.95 | minimal ehtimollik oralig'i |
| `HIGH_PROBABILITY_WARNING` | 0.80 | §6.6 ogohlantirishi chegarasi |
| `MIN_EV_LOWER` / `_UPPER` | 0.0 / 0.20 | minimal EV oralig'i |
| `MAX_FUTURE_DAYS` | 14 | oyna bugundan ko'pi bilan 14 kun oldinga |
| `MAX_WINDOW_DAYS` | 31 | oyna uzunligi (retro uchun ham) |
| `MAX_RETRO_LOOKBACK_DAYS` | 90 | retro oyna bugundan ko'pi bilan shuncha kun oldin boshlanadi (qaror #12) |
| `PRELIMINARY_AFTER` | 3 kun | undan uzoq signal "⏳ Dastlabki", ishonch bir pog'ona past |
| `RECALC_COOLDOWN` | 10 daqiqa | bitta o'yin uchun qo'lda qayta hisob |
| `MANUAL_RESERVE_SHARE` | 0.30 | kunlik API limitining qo'lda so'rovlar zaxirasi |
| `QUERY_MAX_SIGNALS_UPPER` / `SETTINGS_MAX_SIGNALS_UPPER` | 100 / 30 | so'rov va sozlamadagi signal soni |
| `MAX_SCOPE_IDS`, `MAX_NOTE_LENGTH`, `MAX_TEMPLATE_NAME_LENGTH` | 50, 500, 64 | |
| `MIN_SEARCH_LENGTH`, `MAX_SEARCH_RESULTS`, `DEFAULT_PAGE_SIZE`, `MAX_PAGE_SIZE` | 2, 20, 8, 20 | qidiruv va sahifalash |
| `MIN_ODDS_LIMIT` / `MAX_ODDS_LIMIT` | 1.01 / 100 | koeffitsient kiritmasi |
| `PREMATCH_MINUTE_CHOICES` | {30, 60, 90} (yoki `None` = o'chiq) | |
| `CONSERVATIVE_PRESET`, `BALANCED_PRESET`, `AGGRESSIVE_PRESET`, `RISK_PRESETS` | §6.5 jadvali: 60%/3%/1.30–2.00 · 45%/5%/1.50–3.00 · 30%/8%/2.00–5.00 | |
| `DEFAULT_*` | sport futbol, bozorlar 1X2+Total, 10 signal, 09:00, pre-match 60, tinch soat 00:00–08:00 | |

**Enumlar** (`StrEnum`, servis qatlamiga xos; bazadagi `app.db.enums` bilan qiymatlari mos, xaritalashni real servis qiladi): `ScopeKind` (all/leagues/teams/fixtures), `SortKey` (ev/probability/kickoff), `DateMode` (today/tomorrow/weekend/next_days/range), `ConfidenceLevel` (high/medium/low), `Verdict` (confirm/caution/reject), `SignalState` (active/cancelled/hidden), `ResultMark` (`WON="win"`, `LOST="loss"`, `VOID`, `HALF_WON`, `HALF_LOST`, `CASHOUT`; qo'lda faqat `MANUAL_RESULT_MARKS` = won/lost/void/cashout), `RecalcVerdict` (still_valid/below_threshold/cancelled), `AnalysisStage` (collecting/analysing/reviewing/finished), `RiskProfile` (conservative/balanced/aggressive/custom), `WatchKind` (team/league), `WatchNotification` (lineups/odds_move/result), `FormResult` (win/draw/loss), `TeamSide` (home/away), `ReportGroup` (sport/league/market/risk_profile/confidence).

**Kiritma DTO lari**

| Nom | Maydonlar | Qoidalar |
|---|---|---|
| `DateSpec` | `mode`, `days`, `date_from`, `date_to`; konstruktorlar `today()`, `tomorrow()`, `weekend()`, `next_days(n)`, `between(a, b)`; `resolve(today) -> DateWindow` | nisbiy sana (shablonlar uchun); `next_days` 1..14; `weekend` = joriy/kelgusi shanba–yakshanba |
| `DateWindow` | `date_from`, `date_to`; `days`, `ensure_within_horizon(today)`, `is_retro(today)` | tartibli, ≤ 31 kun; o'tgan oyna (retro) har doim ruxsat |
| `AnalysisQuery` | `dates: DateSpec`, `sports`, `min_probability`, `min_ev`, `scope`, `scope_ids`, `markets: frozenset[MarketType]`, `max_signals: int \| None` (`None` = hammasi), `per_day`, `sort`, `include_no_value`; `from_settings(settings, dates)` | sport va bozor bo'sh emas; `scope=all` → `scope_ids` bo'sh, aks holda 1..50 ta; `per_day` faqat `max_signals` bilan; horizon tekshiruvi servisda (`resolve` vaqtida) |
| `UserSettingsView` | sports, league_ids, markets, max_signals, min_probability, min_ev, odds_min, odds_max, risk_profile, send_time, prematch_minutes, quiet_hours (`QuietHours(start, end)` \| None), timezone, bank, unit, daily_loss_limit, weekly_loss_limit; `zone`, `with_risk_profile(p)` | o'qish va yangilash uchun bitta DTO: `dataclasses.replace` bilan o'zgartiriladi; §6.5 oraliqlari; unit ≤ bank; IANA vaqt mintaqasi |
| `FixtureListRequest` | `dates`, `sports`, `league_ids`, `team_ids`, `page` (0 dan), `page_size` | liga va jamoa berilsa — "yoki" |
| `TemplateDraft` | `name`, `query`, `schedule: TemplateSchedule(weekdays 1..7, at) \| None` | nom 1..64 belgi |
| `WatchTarget` | `kind`, `entity_id` | |
| `BetDraft` | `signal_id`, `odds_taken`, `stake` (`None` → signal qiymati / `unit × stake_units`) | odds > 1, stake > 0 |
| `ResultMarkRequest` | `signal_id`, `mark`, `cashout_profit_units` | faqat qo'lda belgilanadiganlar; cashout summasi faqat cashout uchun va majburiy |
| `ReportRequest` | `date_to`, `date_from` (`None` = hammasi), `group_by`; `last_days(n, today)` | |

**Chiqish DTO lari**

| Nom | Asosiy maydonlar |
|---|---|
| `CostEstimate` | `window`, `fixtures_found`, `api_requests`, `budget_remaining`; `within_budget` |
| `AnalysisProgress` / `ProgressCallback` | `stage`, `done`, `total`; `Callable[[AnalysisProgress], Awaitable[None]]` — callback **xato bermasligi kerak** (Telegram xatolarini bot o'zi ushlaydi) |
| `SignalView` | `signal_id`, `fixture_id`, `sport`, `league`, `home_team`, `away_team`, `kickoff` (UTC), `market: Market`, `selection`, `odds`, `bookmaker`, `probability`, `ev`, `confidence`, `stake_units`, `llm_verdict`, `llm_note`, `risk_flags`, `preliminary`, `no_value`, `watchlist`, `experimental`, `state`, `result`, `version`, `data_as_of`; `fixture_label`, `fair_odds`, `negative_ev` |
| `AnalysisResult` | `query_id`, `window`, `signals`, `fixtures_analyzed`, `matched` (cheklovdan oldin), `requested`, `api_requests_used`, `failed_fixtures`, `retro`; `shortfall` |
| `SignalNumbers` / `RecalcDiff` | odds, probability, ev, stake_units, confidence / `signal` (yangilangan karta), `before`, `after`, `verdict`, `recalculated_at`; `changed` |
| `RecalcBatch` | `query_id`, `diffs`, `skipped_cooldown`, `skipped_started`, `failed` |
| `FixtureDetail` | forma (≤ 5, eskidan yangiga), `TeamStats` (uy/mehmon roli), `HeadToHead`, `Absence`, `ScoreProbability` (≤ 3, kamayish tartibida), `MarketQuote` (barcha bozorlar jadvali), `OddsMove` (ertalab → hozir), `lineups_confirmed`, hisob (tugagan bo'lsa), `data_as_of` |
| `LeagueOption`, `TeamOption`, `FixtureOption`, `FixturePage` | tanlash va qidiruv; `FixturePage(items, page, page_count, total)` |
| `TemplateView`, `WatchItem`, `BetView`, `BankView` (+ `BankPoint`), `DaySummary` | |
| `ReportView` | signallar, settled, won/lost/void, `profit_units`, `yield_rate` (signallar bo'yicha), `roi` (foydalanuvchi stavkalari bo'yicha), `clv` (o'rtacha), `clv_beat_rate`, `win_rate`, `longest_losing_streak`, `calibration: CalibrationBucket[]`, `segments: SegmentStat[]` |
| `ExportFile` | `filename`, `content: bytes`, `media_type` |
| `SystemStatus` | `quotas: ApiQuota[]` (`remaining`), `last_refresh_at`, `jobs: JobStatus[]`, `recent_errors: ErrorEntry[]` |

**Protokollar** (`Services` — botga startda beriladigan to'plam: `analysis`, `signals`, `catalog`, `settings`, `templates`, `watchlist`, `bets`, `results`, `reports`, `status`)

```python
class AnalysisService(Protocol):
    async def estimate_cost(self, user_id: int, query: AnalysisQuery) -> CostEstimate: ...
    async def analyze(self, user_id: int, query: AnalysisQuery, *, progress: ProgressCallback | None = None) -> AnalysisResult: ...
    async def recalc_signal(self, user_id: int, signal_id: SignalId) -> RecalcDiff: ...
    async def recalc_query(self, user_id: int, query_id: EntityId, *, progress: ProgressCallback | None = None) -> RecalcBatch: ...
    async def fixture_detail(self, fixture_id: EntityId) -> FixtureDetail: ...

class SignalService(Protocol):
    async def get_signal(self, user_id: int, signal_id: SignalId) -> SignalView: ...
    async def list_signals(self, user_id: int, dates: DateSpec, *, include_hidden: bool = False) -> tuple[SignalView, ...]: ...
    async def hide_signal(self, user_id: int, signal_id: SignalId) -> SignalView: ...

class CatalogService(Protocol):
    async def list_leagues(self, sport: Sport) -> tuple[LeagueOption, ...]: ...
    async def search_teams(self, text: str, *, sport: Sport | None = None, limit: int = 10) -> tuple[TeamOption, ...]: ...
    async def search_fixtures(self, text: str, *, limit: int = 10) -> tuple[FixtureOption, ...]: ...
    async def list_fixtures(self, user_id: int, request: FixtureListRequest) -> FixturePage: ...

class SettingsService(Protocol):
    async def get_settings(self, user_id: int) -> UserSettingsView: ...
    async def update_settings(self, user_id: int, settings: UserSettingsView) -> UserSettingsView: ...
    async def apply_risk_profile(self, user_id: int, profile: RiskProfile) -> UserSettingsView: ...

class TemplateService(Protocol):
    async def save_template(self, user_id: int, draft: TemplateDraft) -> TemplateView: ...
    async def list_templates(self, user_id: int) -> tuple[TemplateView, ...]: ...
    async def delete_template(self, user_id: int, template_id: TemplateId) -> None: ...

class WatchlistService(Protocol):
    async def list_watchlist(self, user_id: int) -> tuple[WatchItem, ...]: ...
    async def add_watch(self, user_id: int, target: WatchTarget) -> WatchItem: ...
    async def remove_watch(self, user_id: int, target: WatchTarget) -> bool: ...
    async def set_notifications(self, user_id: int, target: WatchTarget, notifications: frozenset[WatchNotification]) -> WatchItem: ...

class BetService(Protocol):
    async def place_bet(self, user_id: int, draft: BetDraft) -> BetView: ...
    async def list_bets(self, user_id: int, *, open_only: bool = False, limit: int = 20) -> tuple[BetView, ...]: ...
    async def bank_summary(self, user_id: int) -> BankView: ...

class ResultService(Protocol):
    async def pending_results(self, user_id: int) -> tuple[SignalView, ...]: ...
    async def mark_result(self, user_id: int, request: ResultMarkRequest) -> SignalView: ...
    async def add_note(self, user_id: int, signal_id: SignalId, text: str) -> None: ...
    async def day_summary(self, user_id: int, day: date) -> DaySummary: ...

class ReportService(Protocol):
    async def report(self, user_id: int, request: ReportRequest) -> ReportView: ...
    async def export(self, user_id: int) -> ExportFile: ...

class SystemStatusService(Protocol):
    async def system_status(self) -> SystemStatus: ...
```

**Semantika (real servis ham shunga amal qiladi; `tests/services/contract_suite.py` tekshiradi)**
- Oddiy tahlil: tanlov `probability ≥ min_probability`, `ev ≥ min_ev` va sozlamadagi `odds_min..odds_max` ga mos bo'lsa o'tadi; har bir o'yindan **eng yaxshi bitta** tanlov (saralash kaliti bo'yicha); saralanadi, keyin `max_signals` (jami yoki `per_day`) bilan kesiladi. Shartga mos kelmagan o'yin ro'yxatni to'ldirish uchun qo'shilmaydi (`matched`, `shortfall`).
- **Qamrov `scope=all`** = `settings.league_ids` dagi ligalar (bo'sh bo'lsa — barcha kuzatiladigan ligalar) **+ sevimli jamoa/ligalar** (sozlama ligalaridan tashqarida bo'lsa ham). `list_fixtures` filtrsiz chaqirilsa ham `settings.league_ids` qo'llanadi.
- `include_no_value=True` (maxsus tahlil, §6.3): filtrlar o'chadi, tanlangan bozorlarning barcha tanlovlari qaytadi, chegaradan pastdagilari `no_value=True`, `stake_units=0`.
- **Sevimlilar (§6.7, "har doim kiritiladi"):** `scope=all` da sevimli o'yinning signali (o'tgan bo'lsa — eng yaxshisi, o'tmagan bo'lsa — eng yuqori EV li tanlov, `no_value=True`) `watchlist=True` bilan **har doim** qo'shiladi: `max_signals` kesimidan tashqarida va `matched`/`shortfall` ga kirmaydi. Natija ro'yxati baribir saralash kaliti bo'yicha tartiblangan.
- **Boshlangan o'yinlar:** faqat hali boshlanmagan o'yinlar haqiqiy signal beradi. Oynadagi tugagan o'yinlar `source=retro` signal beradi (`result` bilan, `recommended_at=None`, hech qachon tavsiya emas); in-play o'yinlar tashlab ketiladi. Retro signallar `list_signals`, `pending_results`, hisobot, kalibratsiya, `day_summary` va eksportga **kirmaydi**; ularga `place_bet`/`mark_result` — `ValidationFailedError`, `recalc_*` — boshlangan o'yin qoidasi. Butunlay o'tgan oyna — `AnalysisResult.retro=True`, API sarfi yo'q.
- **Tavsiya identifikatsiyasi (`recommended_at`):** signal kaliti — foydalanuvchi + o'yin + bozor + tanlov (retro alohida). Tanlov birinchi marta biror so'rov chegaralaridan o'tganda `recommended_at` shu vaqtga qo'yiladi va **o'zgarmaydi**: keyingi qattiqroq so'rov uni `no_value` ga qaytarmaydi va hisobotdan chiqarmaydi. `no_value` signal keyinroq yumshoqroq so'rovda o'tsa, o'sha **keyingi** vaqt bilan tavsiya bo'ladi (orqaga sanab emas). Signal shu paytdagi chegaralarni (so'rovning `min_probability`/`min_ev` va sozlamadagi koeff. oralig'i) eslab qoladi.
- **Hisobotga nima kiradi:** faqat tavsiya qilingan (`recommended_at` bor), retro bo'lmagan signallar — **yashirilganlari ham** (yashirish faqat ko'rinish uchun; tanlov xatosi bo'lmasligi uchun). Shu to'plam `report`, kalibratsiya, `day_summary`, `export` va `pending_results` uchun bir xil. ROI — foydalanuvchining shu davrdagi signallarga qilgan **barcha** stavkalari bo'yicha. Yield va segment yield'ida void stavkalar "tikilgan" summaga kirmaydi.
- `kickoff` dan 3 kundan uzoq — `preliminary=True`, ishonch bir pog'ona past.
- **Horizon:** oyna bugundan boshlab ko'pi bilan 14 kalendar kunini qamraydi (bugun ham sanaladi): `date_to ≤ horizon_end(today) = today + 13`. `DateSpec.next_days(14)` aynan shu chegaraga yetadi; `between(...)` ham xuddi shu qoida bilan tekshiriladi (`resolve` paytida, servis "bugun"i bilan).
- **Vaqt o'tkazish:** mahalliy sanalar UTC ga faqat `DateWindow.utc_bounds(zone)` (yarim ochiq `[start, end)`, DST ga chidamli) / `DateWindow.contains(instant, zone)` orqali o'tkaziladi.
- Byudjet: `estimate_cost` hech narsa sarflamaydi; `analyze` qo'lda so'rovlar zaxirasidan oshsa `BudgetExceededError(needed, available)`. 10 daqiqa ichida olingan ma'lumot qayta so'ralmaydi.
- **Progress callback:** servis callback xatosini (har qanday `Exception`) ushlaydi, logga yozadi va ishni davom ettiradi; `asyncio.CancelledError` uzatiladi (bekor qilish ishlaydi). Callback har o'yinda chaqirilishi mumkin — bot xabar tahririni o'zi siyraklashtiradi (throttle, Telegram flood limiti).
- Qayta hisob: bitta o'yin uchun `RECALC_COOLDOWN` da bir marta (`CooldownActiveError(retry_after)`), boshlangan o'yin — `ValidationFailedError`. Zaxira tugagan bo'lsa raqamlar o'zgarmaydi va `data_as_of` ("09:00 holatiga") qo'yiladi. **Verdikt signal yaratilgan/tavsiya qilingan paytdagi chegaralar bilan** hisoblanadi (joriy sozlamalar bilan emas). `recalc_query` cooldown dagi va boshlangan signallarni o'tkazib yuboradi (`skipped_*`), bitta o'yindagi bir nechta signalni bitta hisob bilan yangilaydi; bitta xato butun to'plamni yiqitmaydi (`failed`).
- Egalik: boshqa foydalanuvchining signali, so'rovi yoki shabloni `NotFoundError` (mavjud emasdek). Noma'lum foydalanuvchi standart sozlamalarni oladi.
- `update_settings` to'liq almashtiradi va `risk_profile` ni chegaralardan qayta chiqaradi (preset bilan aynan mos → shu profil, aks holda `custom`).

**Xatolar — `app.services.errors`:** `ServiceError(AppError)` → `ValidationFailedError(field, message)` (+ `ValueError`), `NotFoundError(entity, entity_id)` (+ `LookupError`), `CooldownActiveError(retry_after: timedelta)`, `BudgetExceededError(needed, available)`.

**v1.1 qo'shimchalari (T3, faqat qo'shish; mavjud nomlar o'zgarmagan)**

| Nom | Turi | Ma'nosi |
|---|---|---|
| `SignalSource` | `StrEnum`: `analysis`, `daily`, `custom`, `watchlist`, `retro` | signal qaysi oqimdan |
| `SignalView.source: SignalSource = ANALYSIS`, `SignalView.recommended_at: datetime \| None = None`; xossalar `retro`, `recommended` | maydon (standart qiymat bilan) | invariant: `recommended_at` bor → `no_value=False`; retro → `recommended_at=None`; `recommended_at` UTC |
| `SportOption(sport, available, experimental, supported_markets)` + `CatalogService.list_sports() -> tuple[SportOption, ...]` | DTO + metod | bot sport va bozorlarni hardcode qilmaydi; barcha `Sport` lar enum tartibida |
| `DateWindow.utc_bounds(zone) -> tuple[datetime, datetime]`, `DateWindow.contains(instant, zone) -> bool` | metodlar | mahalliy sana → UTC yagona joyda |
| `horizon_end(today) -> date` | funksiya | oynaning oxirgi ruxsat etilgan kuni (`today + 13`); kalendar tugmalari uchun |
| `ReportRequest.recent_days: int \| None = None`, `ReportRequest.recent(days, group_by=None)`, `ReportRequest.all_time(group_by=None)`, `ReportRequest.resolve(today)`; `date_to` endi ixtiyoriy (`None` = servisdagi bugun) | maydon + konstruktorlar | bot "bugun"ni bilishi shart emas; eski `last_days(days, today)` ishlaydi |
| `MAX_ENTITY_ID_BYTES = 40`, `is_valid_entity_id(value) -> bool` | konstanta + funksiya | `scope_ids`, `league_ids`, `team_ids`, `WatchTarget.entity_id`, `SignalView.fixture_id`, `LeagueOption`/`TeamOption`/`FixtureOption` id lari tekshiriladi |

**Xatti-harakat o'zgarishlari (nomlar o'zgarmagan, semantika aniqlashtirildi):** horizon `today+14` dan `today+13` ga (14 kun, bugun bilan); `scope=all` endi `settings.league_ids` ga amal qiladi; hisobotlar yashirilgan signallarni ham sanaydi, retro va no-value signallarni sanamaydi; sevimlilar `max_signals`/`matched` dan tashqarida; `list_fixtures` filtrsiz bo'lsa `settings.league_ids`; recalc verdikti yaratilish chegaralari bo'yicha; progress callback xatosi ishni to'xtatmaydi.

**v1.2 qo'shimchalari (T4, faqat qo'shish; mavjud nomlar o'zgarmagan)** — qarorlar: [ADR 0004 §11](adr/0004-service-contract.md), [ADR 0008](adr/0008-pipeline-and-real-services.md)

| Nom | Turi | Ma'nosi |
|---|---|---|
| `Notifier` (Protocol) | port, pipeline → bot | `send_daily_digest(user_id, DailyDigest, *, key)`, `send_signal_update(user_id, SignalUpdate, *, key, reply_to=None)`, `send_signal_cancelled(user_id, SignalCancelled, *, key, reply_to=None)`, `send_watchlist_event(user_id, WatchlistEvent, *, key)`, `send_result_settled(user_id, SettledNotice, *, key)`, `alert_admin(AdminAlert)`. Xabar yuborilgan bo'lsa `MessageRef(chat_id, message_id)` qaytaradi (keyingi yangilanish unga javob sifatida), yetkazib bo'lmasa (foydalanuvchi botni bloklagan) `None`; bu holatda istisno **tashlamaydi** |
| `DailyDigest`, `SignalUpdate`, `SignalCancelled`, `WatchlistEvent`, `SettledNotice`, `AdminAlert`, `MessageRef`, `UpdateKind` (`before_24h`/`prematch`), `CancelReason` (`postponed`/`cancelled`/`abandoned`) | frozen DTO lar | xabar yuklari |
| `DeliveryKey(job, day, user_id, subject)` | frozen DTO | idempotentlik kaliti `(job, kun, foydalanuvchi, signal)`. **Idempotentlikni chaqiruvchi (pipeline) ta'minlaydi**: kalit ledger'da avval egallanadi, yuborilgach yakunlanadi; notifier kalitni faqat loglash uchun oladi, lekin takroriy kalitga ham bardoshli bo'lishi kerak |
| **Tinch soatlar** | qoida | **Notifier'ni chaqiruvchi (pipeline) qo'llaydi, notifier emas.** Kunlik xabar tinch soat tugaguncha ushlab turiladi (kechikish `daily_catch_up` = 12 soatgacha, undan keyin o'tkazib yuboriladi); 24 soat/pre-match/sevimlilar/natija xabarlari tinch soatda **tashlanadi** (qayta hisobning o'zi bajariladi, raqamlar yangilanadi); admin ogohlantirishlari hech qachon tinch emas |
| `ServiceUnavailableError(component, detail="")` | xato | tashqi API yoki baza ishlamaydi; bot "keyinroq urinib ko'ring" deydi, holat saqlanadi |
| `BusyError(user_id)` | xato | bir foydalanuvchi uchun bir vaqtda bitta `analyze`; ikkinchisi navbatga qo'yilmaydi, rad etiladi (ikki marta bosish API byudjetini ikki marta sarflamasligi uchun) |
| `AnalysisService.recalc_window(user_id, DateSpec, *, progress=None) -> RecalcBatch` | metod | so'rovsiz `/qayta`: oynadagi tavsiya qilingan signallar (yashirilganlar ham, retro hech qachon). `RecalcBatch.query_id` — oynaning tavsifiy id si |
| `AnalysisService.recalc_signals(user_id, signal_ids, *, progress=None) -> RecalcBatch` | metod | berilgan signallar; noma'lum yoki begona id lar o'tkazib yuboriladi. Cooldown va boshlangan o'yin qoidalari `recalc_query` bilan bir xil |
| `SignalService.get_signals(user_id, ids) -> tuple[SignalView, ...]` | metod | ommaviy olish; `ids` tartibida; noma'lum/begona id lar tashlab ketiladi (eskirgan tugmalar ro'yxati baribir ko'rsatiladi) |
| `BetDraft.client_token: str \| None` (≤ 64 belgi) | maydon | `place_bet` idempotentligi: bir foydalanuvchi bir xil token bilan qayta yuborsa, birinchi yozilgan stavka qaytadi, dublikat yaratilmaydi. Token bo'lmasa har chaqiruv yangi stavka. Tavsiya: callback query id |
| Bank semantikasi (`BankView` docstring) | qoida | `UserSettingsView.bank` — **boshlang'ich bank**; `balance` = boshlang'ich bank + barcha *yopilgan* stavkalar foydasi (ochiq stavkalar balansni kamaytirmaydi). Bankni o'zgartirish butun tarixni qayta asoslaydi (`history` yangi boshlang'ich bankdan qayta hisoblanadi, balans snapshotlari saqlanmaydi). Kunlar — stavka **yopilgan** mahalliy sana; zarar limitlari shu kunlar bo'yicha sof zarar |
| `DateWindow.ensure_within_lookback(today)`, `DateWindow.ensure_allowed(today)` | metodlar | `MAX_RETRO_LOOKBACK_DAYS` (90) va horizon qoidalari DTO'da; servis har so'rovda `ensure_allowed` ni chaqiradi (`estimate_cost`, `analyze`, `recalc_window`, `list_fixtures`). Oyna uzunligi (31) `DateWindow` konstruktorida allaqachon majburlanadi |
| `MAX_BET_TOKEN_LENGTH = 64` | konstanta | |

`app.services.report_math` (hisobot arifmetikasi: `build_report`, `day_summary`, `export_file`) fake va real servis uchun **umumiy** (avval `_fake_metrics`). Fake ga `FakeNotifier` (chaqiruvlarni yozib boradi) qo'shildi.

**v1.6 qo'shimchalari (T5 bot, faqat qo'shish; [ADR 0012](adr/0012-t5-bot-features.md))**

| Nom | Turi | Ma'nosi |
|---|---|---|
| `UndeliverableAware` (`@runtime_checkable` Protocol): `is_undeliverable(user_id) -> bool` | ixtiyoriy port, `Notifier` ning yo'ldoshi | Notifier `None` qaytarganda sabab ikki xil: vaqtinchalik (tarmoq, flood) yoki doimiy (foydalanuvchi botni bloklagan). Notifier shu protokolni ham amalga oshirsa, `NotifierDelivery` `None` dan keyin undan so'raydi; `True` bo'lsa pipeline'ga `app.pipeline.delivery.UNDELIVERABLE_REFERENCE` ("undeliverable") qaytaradi — pipeline buni oddiy tasdiqlangan havola deb biladi: kalit yakunlanadi, qayta urinish va admin ogohlantirishi yo'q. Havolada `chat:message` juftligi yo'q, shuning uchun keyingi yangilanish unga reply bo'lmaydi. `TelegramNotifier` buni 24 soatga (`BLOCKED_TTL_SECONDS`) xotirada eslab qoladi |
| `ExpressLeg(signal_id, fixture_label, market, selection, odds, probability)`, `ExpressView(legs, combined_odds, combined_probability, ev)` + xossa `fair_odds` | DTO | Ekspress (§6.11): tanlovlar mustaqil, shuning uchun ehtimollik va koeffitsient ko'paytiriladi, `ev = P·K − 1` |
| `MIN_EXPRESS_LEGS = 2`, `MAX_EXPRESS_LEGS = 4` | konstanta | |
| `ExpressService.evaluate(user_id, signal_ids) -> ExpressView` | protokol + `Services.express` | `ValidationFailedError(field="signal_ids")`: tanlov soni chegaradan tashqarida, takroriy signal, **bir o'yindan ikkita tanlov** (o'zaro bog'liq bozorlar birlashtirilmaydi), retro/tavsiya qilinmagan/bekor qilingan/yopilgan/boshlangan o'yin signali; `NotFoundError` — noma'lum yoki begona signal. Arifmetika `app.services.express_math` da (fake va real uchun bitta). **Eslatma:** `Services.express` majburiy maydon — `create_fake_services` va `create_real_services` yangilandi |
| `ReportService.export_bets(user_id) -> ExportFile` | metod | Foydalanuvchining barcha stavkalari (ochiq va yopilgan) `bets.csv`: `bet_id, signal_id, fixture, market, line, selection, odds_taken, stake, placed_at, outcome, profit` |

Hali qilinmagan (keyingi to'lqinlar): — (cashout summasi va `/ozgarishlar` v1.8 da bajarildi; v1.7 da bajarildi: jadvalli shablonlar, zarar limiti, `SignalView` ID lari — pastga qarang.)

**v1.7 qo'shimchalari (T5 pipeline, faqat qo'shish; [ADR 0015](adr/0015-feedback-loop-pipeline.md))**

| Nom | Turi | Ma'nosi |
|---|---|---|
| `SignalView.home_team_id`, `.away_team_id`, `.league_id` (`EntityId \| None = None`) | maydonlar | `CatalogService` dagi id lar bilan bir xil; karta ⭐ tugmasi nom bo'yicha qidirmaydi. DTO ularni `is_valid_entity_id` bilan tekshiradi (≤ 40 bayt). Fake ham, real ham to'ldiradi |
| `LimitPeriod` (`StrEnum`: `day`, `week`), `LossLimitNotice(period, limit, loss)` | enum + frozen DTO | Yetilgan zarar limiti (§6.9). `limit` — musbat pul, `loss >= limit` (Decimal, bank valyutasida) |
| `DailyDigest.template_name: str \| None = None` | maydon | Jadvalli shablon xabari: sarlavhada shablon nomi, "kecha yakuni" bloki yo'q |
| `DailyDigest.loss_limit: LossLimitNotice \| None = None` | maydon | Limit yangi signallarni to'xtatdi: `result` bo'sh, xabar kuniga bir marta "Zarar limitiga yetildi". Bot ko'rsatishi shart (hali ko'rsatmaydi) |
| `AnalysisResult.loss_limit: LossLimitNotice \| None = None` | maydon | Qo'lda/maxsus tahlil limit yetilganda ham ishlaydi; natija bannerli. Retro oynada `None` |
| **Zarar limiti qoidasi** | qoida | Zarar = foydalanuvchi stavkalarining **yopilgan** mahalliy kun / mahalliy ISO hafta (dushanba–bugun) bo'yicha sof salbiy foydasi, `Decimal`; `/bank` (`BankView.loss_today/loss_this_week/loss_limit_reached`) bilan bir xil. Aynan limitga teng — yetildi. Stavkasiz foydalanuvchi cheklanmaydi. Yetilganda kunlik xabar va jadvalli shablonlar tahlil qilmaydi va yangi signal yubormaydi |
| **Jadvalli shablonlar** | qoida | `TemplateSchedule(weekdays, at)` foydalanuvchi mintaqasida ishga tushadi: kuniga bir marta (`DeliveryKey(job="template", day, user, template id)`), tinch soatda ushlanadi (qaror #15), `daily_catch_up` ichida yetkaziladi, xato qayta uriniladi. Natija `DailyDigest(template_name=...)` |

Eslatma: bot `TelegramNotifier.send_daily_digest` yangi maydonlarni hali e'tiborsiz qoldiradi (qo'shimcha, buzmaydi); ularni ko'rsatish bot vazifasi.

**v1.8 qo'shimchalari (T5 bot fix, faqat qo'shish; [ADR 0016](adr/0016-t5-bot-fix-and-f8-ui.md))**

| Nom | Turi | Ma'nosi |
|---|---|---|
| `ResultMarkRequest.cashout_bet_id`, `.cashout_payout` (`Decimal`) | maydonlar | **Cashout = bukmeker to'lagan pul summasi** (qaror #18). Stavka foydasi aniq `payout - stake` (`Decimal`, qayta hisoblashsiz); signalning boshqa stavkalari shu daromad foizi bilan yopiladi, signal `profit_units = stake_units * (payout - stake) / stake`. `cashout_profit_units` (signal darajasidagi unit) saqlanadi; ikkalasi birga berilmaydi, payout uchun stavka majburiy, `payout >= 0` va chekli. Boshqa foydalanuvchining yoki signalga tegishli bo'lmagan stavka — `NotFoundError` |
| `BetService.get_bet(user_id, bet_id) -> BetView` | metod | Istalgan eski stavka (`list_bets` 100 bilan cheklangan); begona/noma'lum — `NotFoundError` |
| `BankTotals` (`settled`, `won`, `lost`, `void`, `cashout`, `profit`, `staked`; `yield_rate`) va `BankView.totals` | DTO + maydon | **Barcha** yopilgan stavkalar yig'indisi (sahifa emas); void stavka `staked` ga kirmaydi. Hisob `report_math.bank_totals` da (fake va real uchun bitta) |
| `ExpressLeg.fair_odds` (ixtiyoriy), `ExpressLeg.zero_ev_odds`; `ExpressView.fair_odds` | maydon, xossa | Ekspress EV = `prod(1 + ev_i) - 1` (ikkilik oyoqda `P*O - 1` bilan bir xil, choraklik Asian oyoqda ham aniq). `combined_probability` = oyoqlar yutish ehtimollarining ko'paytmasi (yarim yutuq "yutuq" deb sanaladi); adolatli koeffitsient = oyoqlarning adolatli koeffitsientlari ko'paytmasi |
| `LearningMode`, `LearningChangeKind`, `RollbackOutcome` | enumlar | O'rganish halqasi rejimi (`off`/`shadow`/`apply`), jurnal yozuvi turi, qaytarish natijasi (`rolled_back`, `not_found`, `already_rolled_back`, `not_undoable`, `stale`) |
| `LearningChangeView`, `LearningPage`, `LearningSummary` | frozen DTO | Jurnal yozuvi (`applied=False` — kuzatuv rejimi, `undoable` xossasi), sahifa (`hold_days` bilan), haftalik xulosa |
| `LearningService.list_changes(user_id, *, page, page_size)`, `.rollback_change(user_id, change_id)` + `Services.learning` | protokol | Jurnal tizim bo'yicha umumiy (model bitta); qaytarish pipeline'ning `rollback_change` i (STALE himoyasi, qo'lda ushlab turish) orqali. **`Services.learning` majburiy maydon** — `create_fake_services` va `create_real_services` yangilandi |
| `Notifier.send_learning_summary(user_id, LearningSummary, *, key)` | metod | Haftalik "model nimani o'rgandi" xabari. `TelegramNotifier`, `FakeNotifier`, `LogOnlyNotifier` amalga oshiradi |
| `SystemStatus.learning_mode`, `.learning_missed` | maydonlar | `/holat` uchun: rejim va haftalik ish kechikkanmi (oxirgi yozilgan run 8 kundan eski) |

Pipeline tomonidagi qo'shimchalar (egalik: pipeline-engineer, bot-engineer minimal tahrir qildi): `pipeline/settle.py` — `BetPayout` va `settle_manually(..., payout=)`, `_store(..., payout=)`; `pipeline/learning_digest.py` (yangi) — `announce_learning`, `SummaryDelivery` (ixtiyoriy port, `UndeliverableAware` kabi), `LearningDelivery`; `scheduler.py` — `PipelineJobs._learning` (haftalik run + `record_run` + xabar); `pipeline/data.py` — qayta eksportlar (`bets_for_signal`, `list_learning_changes`, `LearningChange`). `Delivery` protokoli o'zgarmadi.

Xatti-harakat o'zgarishi: `/eksport` CSV matn ustunlarida (liga, jamoalar, o'yin) `=`, `+`, `-`, `@`, tab yoki CR (\t, \r) bilan boshlangan qiymat oldiga `'` qo'yiladi (formula in'eksiyasi); fayl UTF-8 **BOM** bilan (`utf-8-sig`) yoziladi, raqam va sana ustunlari o'zgarmaydi.

**v1.9 qo'shimchalari (T6 fix A, faqat qo'shish; [ADR 0018](adr/0018-t6-fix-round-a.md))**

| Nom | Turi | Ma'nosi |
|---|---|---|
| `MAX_MONEY` (`Decimal`, `10**12`) | konstanta | Har qanday pul summasining yuqori chegarasi: `UserSettingsView` (bank, birlik, limitlar), `BetDraft.stake`, `ResultMarkRequest.cashout_payout` (`[0, MAX_MONEY]`) va `cashout_profit_units` (`abs <= MAX_MONEY`). Bot `parse_money` ham shuni qo'llaydi |
| Cashout chegaralari | xatti-harakat | To'lov `[0, stake * odds_taken]` ichida, signal darajasidagi unit `[-stake_units, stake_units * (odds - 1)]` ichida; aks holda `ValidationFailedError` |
| **Cashout bitta stavkaga tegishli (qaror #22)** | xatti-harakat o'zgarishi | v1.8 dagi "boshqa stavkalar shu daromad foizi bilan yopiladi" **bekor**: faqat `cashout_bet_id` stavkasi yopiladi, qolganlari ochiq. Signal natijasi (`CASHOUT`) faqat signalda natija yo'q va ochiq stavka qolmaganda yoziladi |
| **Ochiq stavkali signal yopiladi (qaror #19)** | xatti-harakat | Settlement tavsiya qilinmagan signalni ham yopadi, agar unda ochiq foydalanuvchi stavkasi bo'lsa. Bank/stavkalar ro'yxati/`bank_summary` shu stavkalarni hisoblaydi; **tavsiya hisoboti** (`report`, `export`, o'rganish) hisoblamaydi |
| **Aylanma = xavf ostidagi stavka (qaror #21)** | ta'rif | `ReportView.roi`, `BankTotals.staked`/`yield_rate` va backtest `roi`/`turnover`: void (push) stavka maxrajga kirmaydi. Bitta ta'rif, ADR 0005 yangilandi |
| `BetService.get_bet` | ichki | To'g'ridan-to'g'ri qidiruv (`db.repo.get_bet`), N+1 yo'q; xatti-harakat o'zgarmagan |
| Kelly | xatti-harakat | Stavka hajmi **joriy balansdan** (boshlang'ich bank + yopilgan stavkalar foydasi, `UserProfile.balance`); stavka qo'yilgan signalning `stake_units` qayta hisoblashda o'zgarmaydi |
| `db.repo`: `get_bet`, `signal_has_bets`, `settled_profit_total`; `unsettled_signals` kengaydi | funksiyalar | Qarang ADR 0018 |
| `clients.cache.TtlCache(max_entries=128, max_bytes=32 MiB)`, `ApiClient.get_json(..., shape=PayloadShape)`, `ApiFootballClient.fixtures_by_date(..., league_ids=)`, `odds_*(..., bookmakers=BookmakerFilter)` | klient | Kesh chegaralangan; tana keshdan oldin qisqartiriladi |
| `FootballAdapter(bookmakers=)`, `FootballProvider(bookmakers=)`, `map_odds(bookmakers=)`, `PipelineConfig.ingest_bookmakers`/`.extra_bookmakers` | sozlama | Faqat `pinnacle` + `bet_bookmaker` (+ qo'shimcha) saqlanadi |
| `pipeline.retention.run_retention(ctx) -> RetentionReport` | funksiya | Haftalik: 90 kundan eski oraliq narx suratlarini o'chiradi (scheduler ro'yxatiga infra qo'shadi) |
| `pipeline.learning.promote_shadow_state(ctx)` | funksiya | `shadow → apply` o'tishida holatni `applied=1` jurnal yozuvlari sifatida qayd etadi (qaror #23); `rollback_learning_change` o'rami olib tashlandi (`rollback_change` qoladi) |
| `pipeline.redaction.scrub_text`, `register_secrets` | funksiyalar | Xato matnlaridan maxfiy ma'lumotni tozalash (ledger, admin ogohlantirishi, `/holat`) |
| `pipeline.errors.ManualResultError` | xato | Mumkin bo'lmagan qo'lda natija (cashout chegaradan tashqarida) |

**v1.10 qo'shimchalari (T6 fix C, faqat qo'shish; [ADR 0019](adr/0019-t6-fix-round-c.md))**

| Nom | Turi | Ma'nosi |
|---|---|---|
| `SystemStatus.build: str = ""` | maydon | Ishlayotgan build yorlig'i (`/holat` da "Versiya: ..."). Real servis uchun `main.py` beradi (`create_real_services(..., build=)`, `app.ops.version.get_version().label`); soxta servis qat'iy `FAKE_BUILD` |
| `DigestResume`, `DigestProgress` (port), `DailyDigest.progress` | DTO / port / maydon | Ko'p xabarli kunlik xabar jarayoni: `resume()` (sarlavha havolasi + qayta yuborish kerak bo'lmagan pozitsiyalar), `header_sent(ref)`, `part_handled(position)`. Ixtiyoriy: `None` bo'lsa notifier eski xatti-harakat bilan ishlaydi; `compare=False` |
| `Notifier.send_daily_digest` xatti-harakati | o'zgarish | Doimiy rad etilgan karta (4xx) o'tkazib yuboriladi va signal id si bilan loglanadi; vaqtincha xato (429/tarmoq/5xx) bo'lgan karta qolsa `None` ("yetkazilmadi") qaytadi va qayta urinishda faqat yetishmaydiganlari yuboriladi. Bloklangan foydalanuvchida tsikl to'xtaydi (eski xatti-harakat) |
| `pipeline.delivery.DigestProgress`, `DailyDelivery.progress` | port / maydon | Yuqoridagining pipeline (satr havolali) ko'rinishi; `services.real.notifier` ularni xizmat DTO siga o'giradi |
| `Ledger.complete_many`, `Ledger.fail(..., delivery=)`, `ledger.DELIVERY_BACKOFFS`, `DELIVERY_MAX_ATTEMPTS`, `backoff(n, delivery=)` | funksiyalar / konstantalar | Bir tranzaksiyali ommaviy yozuv; yetkazish kalitlari uchun 1,2,4,8,15,30,60,120,120 daqiqa (10 urinish) |
| `digest_store.LedgerDigestProgress`, ledger ishi `daily.progress` | sinf / kalit | Jarayonni ledger'da saqlash (yosh bo'yicha tozalanadi) |
| `db.repo.get_signals_by_ids` | funksiya | Signallar id lar bo'yicha, 500 tadan paketlab (N+1 siz) |
| `review_safely(..., limit_seconds=, total_seconds=)`, `PipelineConfig.review_timeout/review_total_timeout` | sozlama | Vaqt chegarasi paket boshiga (30 s) va butun tahlilga (90 s) |
| `scheduler.JOB_RETENTION` (`retention`, yakshanba 04:00), `PipelineJobs(sleep=)`, `FAILURE_BACKOFF_SECONDS` | job / sozlama | `JOB_NAMES` 9 ta; yozib bo'lmaydigan xato job o'ramidan chiqmaydi |
| `Settings.ingest_extra_bookmakers` (`INGEST_EXTRA_BOOKMAKERS`) | sozlama | → `PipelineConfig.extra_bookmakers` |
| `app.logging.summarise_body` | funksiya | Uchinchi tomon javob tanasining qisqa, HTML-tegsiz ko'rinishi (loglar uchun) |

`app.pipeline.queries` (re-export) va `daily.next_send_after` olib tashlandi: o'qishlar `app.db.repo` dan.

**v1.11 qo'shimchalari (T7-B: DB-only tahlil va baza ko'rish, faqat qo'shish; [ADR 0021](adr/0021-db-only-analysis.md), qaror #25)**

*Xatti-harakat o'zgarishi:* `AnalysisService` (`analyze`, `recalc_*`, `estimate_cost`) endi provayderga **hech qachon** murojaat qilmaydi (`api_requests_used` doim 0, `BudgetExceededError` ko'tarilmaydi). Kelajakka tegadigan oyna uchun baza yangilanmagan bo'lsa `analyze` `DataStaleError` ko'taradi; retro (butunlay o'tmishdagi) oyna hech qachon.

| Nom | Turi | Ma'nosi |
|---|---|---|
| `errors.DataStaleError(last_refresh_at)` | xato | Baza yangilanmagan (`None` = hech qachon, aks holda UTC) yoki `max_quote_age` (30 soat) dan eski. Bot: "Baza yangilanmagan: oxirgi yangilanish ..., admin 'Bazani yangilash' ni bosing" |
| `errors.NotAuthorizedError(user_id, action)` | xato | `refresh_database` admin bo'lmagan foydalanuvchiga |
| `CostEstimate.data_stale`, `.last_refresh_at` | maydonlar | `estimate_cost` xato ko'tarmaydi, lekin bot "Boshlash" dan oldin ogohlantira oladi |
| `DataBrowserService` (port) + `Services.browser` | port / maydon | Faqat o'qish (`refresh_database` dan tashqari). Metodlar quyida |
| `database_overview(user_id) -> DatabaseOverview` | metod | `tables: tuple[StoredTable]` (`name`, `rows`, `last_updated_at`, `own`), `size_bytes`, `last_refresh_at`, `earliest_kickoff`, `latest_kickoff`, `upcoming_fixtures`, `data_stale`. `own=True` (`signals`, `user_bets`) faqat so'rovchining qatorlari; qolgan jadvallar umumiy |
| `list_stored_leagues(user_id, *, sport=None) -> tuple[StoredLeague]` | metod | `league_id`, `sport`, `name`, `country`, `source`, `fixtures`, `upcoming`, `first_kickoff`, `last_kickoff` (UTC). Nom bo'yicha; o'yini yo'q liga ham chiqadi. Bir liga ikki manbada (tarix/jonli) bo'lsa alohida qator |
| `list_stored_fixtures(user_id, StoredFixtureRequest) -> StoredFixturePage` | metod | Filtr: `date_from`/`date_to` (foydalanuvchi zonasidagi **mahalliy** sanalar, ikkalasi ham kiradi, ochiq bo'lishi mumkin), `league_ids`, `team_ids`, `statuses`, `with_odds`, sahifalash (`page` 0 dan, `page_size` 1..`MAX_PAGE_SIZE`). Filtrlar **VA** bilan. Eng erta o'yin birinchi. Oxiridan keyingi sahifa bo'sh (xato emas). Gorizont/retro chegarasi yo'q |
| `StoredFixture` | DTO | `fixture_id`, `sport`, `league_id`, `league`, `country`, `home_team`, `away_team`, `home_team_id`, `away_team_id`, `kickoff` (UTC), `status` (`FixtureStatus`), `home_score`, `away_score`, `has_odds`, `source`. Mahalliy vaqtga bot o'tkazadi |
| `get_stored_fixture(user_id, fixture_id) -> StoredFixtureDetail` | metod | O'yin + `season`, taym natijasi, xG, `odds: tuple[BookmakerOdds]` (bukmeker bo'yicha, har biri eskidan yangiga; ko'pi bilan `MAX_STORED_ODDS_IN_DETAIL` = 500 ta eng yangi surat), `odds_total`, `truncated`, `signals` (**faqat so'rovchining** signallari, retro/yashirinlar ham). `NotFoundError` |
| `list_stored_teams(user_id, StoredTeamRequest) -> StoredTeamPage` | metod | `text` (nom bo'lagi), `sport`, `league_ids`; `StoredTeam(team_id, sport, name, country, source, fixtures)` |
| `list_stored_odds(user_id, StoredOddsRequest) -> StoredOddsPage` | metod | Bitta o'yinning narx suratlari (yangidan eskiga), filtr `bookmaker`, `market`; `StoredOdds(bookmaker, market, selection, line, odds, kind, taken_at)`. `NotFoundError` |
| `refresh_database(user_id) -> RefreshReport` | metod | Admin: kunlik refresh qadamlari hozir, **qo'lda zaxira** hisobidan (`fixtures`, `results`, `odds` yozilgan soni, `api_requests`, `skipped`, `complete`). Xatolar: `NotAuthorizedError`; `CooldownActiveError(retry_after)` (oxirgisidan `MANUAL_REFRESH_MIN_MINUTES` = 30 daqiqa o'tmagan, ikki parallel bosish ham shu); `BudgetExceededError` (taxminiy narx zaxiradan ko'p, hech narsa yuborilmaydi); `ServiceUnavailableError` (kalit yo'q yoki provayder ishlamaydi) |
| `MANUAL_REFRESH_MIN_INTERVAL`, `MAX_STORED_ODDS_IN_DETAIL`, `MAX_STORED_TEAM_SEARCH_LENGTH` | konstantalar | Standart 30 daqiqa / 500 / 64 |
| `create_real_services(..., admin_ids=None)`, `create_fake_services(..., admin_ids=None)` | parametr | Kim bazani yangilay oladi; `None` (hozirgi `main.py`) = bot whitelist'iga ishoniladi (whitelist = `ADMIN_IDS` = admin ro'yxati); alohida admin ro'yxati kerak bo'lsa `main.py` shuni uzatadi |
| `pipeline.freshness` (`StaleDataError`, `mark_fresh`, `freshness`, `require_fresh`, `current_freshness`) | modul | Ledger muhri `db.refreshed`: faqat `fixtures` **va** `odds` qadamlari o'tgan refreshdan keyin yoziladi. Kunlik/shablon (avtomatik) tahlil to'xtamaydi, `AnalysisOutcome.data_stale=True` qo'yadi |
| `pipeline.refresh.refresh_manual(ctx) -> ManualRefreshReport`, `errors.RefreshTooSoonError`, `refresh.local_utc_days` | funksiya / xato | Oraliq ledger `claim(lease=interval)` bilan (parallel bosish va restart ham himoyalangan); hech narsa yozmagan urinish qulfni bo'shatadi |
| `PipelineConfig.display_zone`, `.refresh_at`, `.manual_refresh_min_interval`; `max_quote_age` 6 s dan 30 s ga; `fixtures_ttl`/`odds_ttl` olib tashlandi | sozlama | `Settings.refresh_at` (`REFRESH_AT`), `Settings.manual_refresh_min_minutes` (`MANUAL_REFRESH_MIN_MINUTES`) |
| `AnalysisRequest.fetch` olib tashlandi; `DataPlan.data_stale`/`.last_refresh_at` | o'zgarish / maydon | Engine hech qachon yuklamaydi. `ingest.is_covered`/`mark_covered` olib tashlandi (o'qiydigan yo'q) |
| `db.repo.browse` (`table_stats`, `database_size`, `kickoff_span`, `league_summaries`, `browse_fixtures`, `stored_fixture`, `stored_odds`, `browse_teams`, `FixtureFilter`, ...) | repo | Faqat o'qish; `pipeline.data` orqali xizmatlarga ochiq. Boshqa foydalanuvchilarning signal/stavkalarini qaytara olmaydi |

Kunlik qamrov: refresh mahalliy (`DISPLAY_TZ`) kunlarni oladi: `refresh_days_ahead` kunlik o'yinlar va `odds_days_ahead` kunlik narxlar, ularni qoplaydigan **barcha UTC kunlari** (Toshkent 01:00 o'yini oldingi UTC kuniga tushadi).

**v1.12 qo'shimchalari (T7-C: provayder chaqiruvlari jurnali botga, faqat qo'shish; [ADR 0020](adr/0020-provider-call-log.md))**

Hammasi **faqat admin** uchun: admin bo'lmaganga `NotAuthorizedError` (admin tekshiruvi `refresh_database` bilan bir xil: `admin_ids`, `None` = bot whitelist'i). Tekshiruv kirish tekshiruvidan oldin.

| Nom | Turi | Ma'nosi |
|---|---|---|
| `CallLogService` (port) + `Services.calls` | port / majburiy maydon | `list_calls`, `get_call`, `call_stats` |
| `list_calls(user_id, CallLogRequest) -> CallLogPage` | metod | Yangisi birinchi (`at`, tenglikda `id` kamayish). Filtr `provider`, `outcome` (VA). Oxiridan keyingi sahifa bo'sh (xato emas); `page_count` 0 = hech narsa topilmadi. Javob matni o'qilmaydi |
| `get_call(user_id, call_id) -> CallLogDetail` | metod | To'liq `request`/`response`/`error`/`size_bytes`. Noma'lum yoki tozalab yuborilgan id: `NotFoundError("call", id)` |
| `call_stats(user_id, *, days=1) -> tuple[CallLogStat, ...]` | metod | Oxirgi `days` (1..`MAX_CALL_STATS_DAYS` = 30, aks holda `ValidationFailedError`) uchun (provayder, operatsiya) bo'yicha, shu tartibda |
| `CallLogEntry` | DTO | `call_id`, `at` (UTC), `provider` (`Provider`), `operation`, `outcome` (`CallOutcome`), `duration_ms`, `http_status`, `quota_cost`, `request_preview` (~200 belgi) |
| `CallLogRequest` | DTO | `provider=None`, `outcome=None`, `page=0`, `page_size=DEFAULT_PAGE_SIZE` (1..`MAX_PAGE_SIZE`; xato: `ValidationFailedError`) |
| `CallLogPage` | DTO | `items`, `page`, `page_count`, `total` |
| `CallLogDetail(CallLogEntry)` | DTO | Entry maydonlari (jumladan `request_preview`) + `request`, `response` (keshdan/rad etilgan chaqiruvda `None`), `error`, `size_bytes` |
| `CallLogStat` | DTO | `provider`, `operation`, `calls`, `errors`, `cached`, `blocked`, `avg_duration_ms` (`int`, faqat provayderga yetgan chaqiruvlar; yo'q bo'lsa `None`), `quota_cost` |
| `MAX_CALL_STATS_DAYS`, `check_call_stats_days(days)` | konstanta / funksiya | 30; realizatsiyalar uchun umumiy tekshiruv |
| `RealCallLogService` (`services/real/calls.py`), `FakeCallLogService` (`services/_fake_calls.py`) | amalga oshirish | Real `pipeline.data.list_calls/count_calls/get_call/call_stats` ustida. Fake: 14 qatorli namunaviy jurnal (`sample_call_records(now)`; API-Football ok/cached/quota_blocked/error, Gemini ok/error, 3 kunlik tarix); `create_fake_services(admin_ids=...)` jurnalga ham ta'sir qiladi |
| `GeminiClient.generate_json(..., operation=None)`, `JsonGenerator` | parametr | Ixtiyoriy kalit so'zli argument: jurnal yozuvida `operation` (bo'lmasa javob sxemasi nomi). `LlmService` `"llm_review"` va `"post_mortem"` ni uzatadi (`llm.service.OPERATION_*`). Eski `JsonGenerator` fake lari `operation` ni qabul qilishi kerak (`**kwargs` yoki `operation: str | None = None`) |
| `pipeline.data` | re-export | `list_calls`, `count_calls`, `get_call`, `call_stats`, `CallSummary`, `CallDetail`, `CallStat` |

## 5. Soxta servis — `src/app/services/fake.py`
**Holat:** frozen v1 (orkestr tasdiqlagan, 2026-10-21), qo'shimchalar v1.1–v1.11 (fake va real servis shartnoma testlari bir xil) · **Versiya:** 1.11 · **Egasi:** pipeline-engineer

- v1.12: `FakeCallLogService` (`_fake_calls.py`): namunaviy provayder jurnali; shartnoma testlari fake va real uchun bir xil (`tests/services/calls_contract.py`).
- v1.11: `FakeDataBrowserService` (`_fake_browser.py`): sinov dunyosi "bazada saqlangandek" ko'rinadi (har kvotaga bitta ertalabki narx), 30 daqiqalik yangilash oralig'i va `admin_ids` real servisdagidek.
- `create_fake_services(*, clock: Clock = system_clock, admin_ids: frozenset[int] | None = None) -> Services` — barcha protokollarning bitta umumiy xotira holati ustidagi soxta amalga oshirilishi (`FakeAnalysisService`, `FakeSignalService`, `FakeCatalogService`, `FakeSettingsService`, `FakeTemplateService`, `FakeWatchlistService`, `FakeBetService`, `FakeResultService`, `FakeReportService`, `FakeSystemStatusService`). `Clock = Callable[[], datetime]` (UTC).
- Ma'lumot: hash asosida **deterministik** (tasodif yo'q): APL, La Liga, Seriya A, Bundesliga (futbol) va KHL (xokkey, `experimental=True`), soat bo'yicha "bugun" dan 7 kun oldin — 14 kun keyin, har kuni har ligada bitta o'yin; bozorlar 1X2, ikki natija, Total 2.5, BTTS, Fora −0.5 (xokkey: 1X2, Total 5.5). Tugagan o'yinlarning ~15% ida natija "API dan kelmagan" (`/natijalar` uchun).
- Byudjet: kunlik limit 100, avtomatik sarf 40, qo'lda zaxira 30 so'rov; 10 daqiqalik kesh. Cooldown, progress, idempotentlik va xatolar shartnomadagidek.
- Dunyo injeksiya qilingan soatga ergashadi: mahalliy kun o'zgarsa qayta yaratiladi (bugundan −7..+14 kun), avval ko'rilgan o'yinlar esa saqlanib qoladi (eski signallar va batafsil karta ishlayveradi). Xokkey `list_sports` da `experimental=True`, qolgan (futbol/xokkeydan boshqa) sportlar `available=False`.
- I/O, tarmoq, `sleep` yo'q. `RecalcVerdict.CANCELLED` va `failed_fixtures > 0` holatlarini soxta servis hosil qilmaydi (real servisda: o'yin qoldirilgan / tarkib, qisman nosozlik).
- Qatlam himoyasi: `import-linter` qoidasi "production code never depends on the fake services" — `app.pipeline`, `app.scheduler`, `app.services.interfaces`, `app.services.errors`, `app.services.report_math`, `app.services.real` fake modullarni import qila olmaydi.
- v1.2: `FakeNotifier`, `recalc_window`/`recalc_signals`, `get_signals`, `BusyError`, `client_token` va retro oynasi chegarasi fake da ham amalga oshirilgan; shartnoma testlari real servisda ham o'tadi (`tests/services/test_real_contract.py`).

## 6. Model API si — `src/app/models/__init__.py`
**Holat:** frozen v1 (orkestr tasdiqlagan, 2026-10-21), v1.1 qo'shimchalari (T3 fix, orkestr ko'rib chiqadi), v1.2 qo'shimchalari (T5, o'rganish halqasi), v1.3 qo'shimchalari (T5 fix, takroriy tekshiruv va rejimlar) · **Versiya:** 1.3 · **Egasi:** quant-modeler · Qarorlar: [ADR 0003](adr/0003-dixon-coles-and-devig.md), [ADR 0005](adr/0005-backtest-methodology.md), [ADR 0013](adr/0013-learning-loop-math.md) · **Import:** `from app.models import ...`

Barcha funksiyalar **sinxron va sof** (I/O yo'q, faqat `app.domain`/`app.errors` import qilinadi); pipeline ularni `asyncio.to_thread` orqali chaqiradi. Xatolar: `ModelInputError` (`DataError`; kiritma yaroqsiz → o'yinni o'tkazib yubor, logla), `UnknownTeamError` (`ModelInputError`; jamoa modelda yo'q → o'tkazib yubor), `ModelFitError` (`AppError`; fit yaqinlashmadi). Ehtimollik/koeffitsient — `float`; pul — `Decimal`.

**Konvensiyalar:** jamoa kaliti `team_key(team) = str(team.ref)`; o'yin sanasi `match_date(fixture)` = kickoff ning UTC sanasi; `fit_dixon_coles(..., as_of)` da `as_of` **eksklyuziv** (unda yoki undan keyin o'ynalgan o'yin → xato). Fit **har liga uchun alohida**.

| Qadam | API | Izoh |
|---|---|---|
| Fit kiritmasi | `match_records(fixtures, results) -> list[MatchRecord]`, `match_record(fixture, result)` | faqat `finished` natijalar |
| Fit | `fit_dixon_coles(matches, as_of, *, xi=DEFAULT_XI_PER_DAY) -> DixonColesModel` | `xi` backtestda tanlanadi |
| Bashorat | `predict_markets(model, home_key, away_key, markets) -> dict[Market, MarketProbabilities]` | bitta hisob matritsasi; `UnknownTeamError` |
| Narxlar | `complete_quotes(snapshots, kickoffs=None) -> list[MarketQuote]` | (o'yin, bukmeker, kind, bozor) bo'yicha to'liq to'plamlar, eng yangi narx; `kickoffs` (`{fixture_ref: kickoff}`) berilsa, kickoffda yoki undan keyin olingan qaror vaqti narxlari (`closing` dan boshqa `kind`) tashlanadi — pipeline doim beradi (v1.1) |
| Bozor modeli | `fair_markets(quotes_of_one_fixture, *, kind, config=DEFAULT_MARKET_MODEL, references=model_probs) -> dict[Market, FairMarket]` | Pinnacle → `market_avg` zaxira (`wide_uncertainty`); Osiyo butun/chorak chiziqlari uchun `references` (DC) kerak, bo'lmasa bozor tashlanadi; DC 1X2 dan hosil qilinadi (`derived`) |
| Ansambl | `ensemble_markets(model_probs, fair, weight) -> dict[Market, MarketProbabilities]`, `ensemble(model, market, weight)` | `p = w·p_DC + (1−w)·p_bozor`; bozor bahosi yo'q bozor qaytmaydi |
| Ehtimollik | `MarketProbabilities.outcomes[selection]` (`AsianOutcomeProbabilities`), `.win_probability(selection)` (`win + half_win`) | |
| EV | `asian_expected_value(outcomes, odds)`, `asian_fair_odds(outcomes)`; ikkilik: `expected_value`, `fair_odds` | `0.05` = +5% |
| Stavka | `capped_fraction(asian_kelly_fraction(outcomes, odds))` → `stake_amount(bank, fraction) -> Decimal`; `flat_stake(ev, unit)` | Kelly × 0.25, maks 2%, sentgacha pastga |
| Ishonch | `assess_confidence(ConfidenceInputs(...)) -> ConfidenceAssessment(level, reasons, sanity_flag, disagreement)`; `lower_level(level, steps)` | `ConfidenceLevel` (HIGH/MEDIUM/LOW) servis enumiga **nom bo'yicha** o'tkaziladi; `sanity_flag` = farq > 15 p.p. → "value" emas, tekshiruv. Chegaralar qat'iy, farq 1e-9 gacha yaxlitlanadi (aynan 15 p.p. — sanity emas). xG yo'q bo'lsa tarkib e'lon qilinmaguncha maksimal daraja O'RTA |
| Natija | `settle_selection(market, selection, home_goals, away_goals) -> Settlement` | win/half_win/push/half_loss/loss; `Outcome` ga pipeline o'tkazadi |

`ConfidenceInputs`: `min_team_matches` (fit oynasidagi ikki jamoadan kichigi), `xg_available`, `lineups_known`, `model_probability`, `market_probability | None`, `days_to_kickoff` (manfiy = retro), `market_wide_uncertainty`, `calibration_bias | None`. Chegaralar nomlangan konstantalar: `MIN_TEAM_MATCHES=6`, `GOOD_TEAM_MATCHES=15`, `MODERATE_DISAGREEMENT=0.08`, `SANITY_DISAGREEMENT=0.15`, `PRELIMINARY_DAYS=3`, `MAX_CALIBRATION_BIAS=0.05`.

**Backtest kutubxonasi** `app.models.backtest` (skript va testlar uchun, pipeline emas): `build_dataset(HistoryBatch)`, `walk_forward(...)`, `run_experiment(dataset, ExperimentConfig) -> ExperimentResult`, `render_report(result, RunMetadata)`, `generate_history(SyntheticConfig)`. Skript: `scripts/backtest.py [--synthetic]`.

**v1.2 (T5, faqat qo'shimcha; [ADR 0013](adr/0013-learning-loop-math.md)):** yangi sof modullar (importi `from app.models.segments import ...`, `from app.models.calibration import ...`; `app.models` paketining eksport ro'yxati o'zgarmagan).
- `app.models.segments`: `SegmentKey(sport, league, market, odds_bucket)`, `SegmentObservation(key, unit_return, hit, p_model, block, clv)`, `odds_bucket(odds)`, `compute_segment_stats(observations, policy) -> dict[SegmentKey, SegmentStats]` (`n`, `blocks`, `roi`/`clv`/`calibration_bias` — har biri `Estimate(n, raw, shrunk, low, high)`: empirik Bayes shrinkage `k=30` + seeded blok-bootstrap 95% CI), `shrink(...)`, `segment_action(stats, policy, *, status, now) -> SegmentDecision(action, state, ev_floor, reason, detail)` (`SegmentAction`: keep / raise_threshold / disable / reenable; `SegmentState`: active / raised / disabled; `SegmentReason` kodlari). Konstantalar: `MIN_SEGMENT_SAMPLE=50`, `PRIOR_STRENGTH=30`, `RAISED_EV_FLOOR=0.08`, `DISABLE_SHRUNK_ROI=-0.05`, `MIN_HOLD_DAYS=14`, `DISABLE_COOLDOWN_DAYS=60`.
- `app.models.calibration`: `CalibrationSample(probability, hit, settled_at)`, `fit_calibration(samples, *, cutoff, version, policy) -> CalibrationFit(reason, version, gain, gain_low, n_train, n_holdout, .activate)` (faqat `settled_at < cutoff`; 70/30 vaqt bo'yicha bo'lish; darvoza: Brier yutug'i >= 0.001 va bir tomonlama 95% bootstrap quyi chegarasi > 0), `CalibrationVersion(version, method, params, n_samples, brier_before, brier_after, created_at, passed_gate)`, `apply_calibration`, `calibrate_complementary` (yig'indi 1 ga qayta normallanadi), `rescale_outcomes`, `validate_params`, `select_rollback`. Konstantalar: `MIN_CALIBRATION_SAMPLES=200`, `MIN_HOLDOUT_SAMPLES=60`, `MIN_BRIER_GAIN=0.001`.
- Pipeline (shartnoma emas, ma'lumot uchun): `Candidate.raw_probability`, `ValuationInput.calibration`, `PipelineContext.learning` (`LearningSource`: `NoLearning` standart, `DbLearningSource` ishlab chiqarishda), `run_learning(ctx)` haftalik job (`scheduler.JOB_LEARNING`, dushanba 09:00), `rollback_learning_change(ctx, change_id)`. Baza: Alembic `0008` — `signals.p_raw`, `segment_stats.state_since/hold_until`, `calibration_versions.passed_gate`, jadval `learning_changes`; repo: `settled_learning_signals`, `list_segment_rows`, `save_segment_row`, `list_calibration_versions`, `active_calibration`, `add_calibration_version`, `set_active_calibration`, `add_learning_change`, `list_learning_changes`, `get_learning_change`, `mark_change_rolled_back` (`app.db.repo`).

**v1.3 (T5 fix, faqat qo'shimcha; mavjud nomlar o'zgarmagan; [ADR 0013 qo'shimchasi](adr/0013-learning-loop-math.md)):**
- `app.models.segments`: `Estimate.p_value`; `SegmentStatus.flagged` / `SegmentDecision.flagged`; `SegmentReason.AWAITING_CONFIRMATION`; `compute_segment_stats(..., *, since_block=None)`; `segment_action(..., fdr_ok=True)`; yangi `fdr_significant(stats, policy)` (Benjamini–Hochberg), `review_segments(observations, statuses, *, now, policy) -> SegmentReview(stats, decisions)` (haftalik tekshiruvning yagona kirish nuqtasi). Standartlar qat'iylashdi: `CI_LEVEL=0.995`, `BOOTSTRAP_RESAMPLES=2000`, `FDR_LEVEL=0.05`; tightening ROI **va** CLV (AND) + ketma-ket ikki run talab qiladi; RAISED segment faqat sinov davri ma'lumoti bilan baholanadi.
- `app.models.calibration`: `confirmed(fit, *, previous_pass_at)`, `MAX_CONFIRMATION_GAP`; `ONE_SIDED_CONFIDENCE=0.995`, `BOOTSTRAP_RESAMPLES=2000`.
- Konfiguratsiya (`config.py`, qo'shimcha): `LEARNING_MODE` (`off|shadow|apply`, standart `shadow`), `LEARNING_MANUAL_HOLD_DAYS` (standart 30).
- Pipeline: `app.pipeline.learning_settings` (`LearningMode`, `LearningSettings(mode, manual_hold)`, `DEFAULT_MANUAL_HOLD`), `PipelineContext.learning_settings`; `overlay.load_overlay(source)` (istalgan xatoda xom model); `learning.RollbackResult`, `rollback_change(ctx, id)`, `can_push(market)`; `rollback_learning_change` saqlangan (`bool`); `learning.MANUAL_HOLD` konstantasi `learning_settings.DEFAULT_MANUAL_HOLD` ga ko'chdi. Retro kartalar overlay olmaydi.
- Baza: Alembic `0009` — `learning_changes.applied` (default true; shadow = false), `segment_stats.flagged` (default false). Repo: `LearningChange.applied`, `SegmentRecord.flagged`, `list_learning_changes(..., applied=None)`, `add_learning_change(..., applied=True)`, `latest_change_of_kind(session, kind)`; `active_calibration` endi `passed_gate` ni talab qiladi.

## 7. LLM API si — `src/app/llm/service.py`
**Holat:** frozen (v1 taklifi T4 da yozilgan), qo'shimchalar v1.1 (T4 fix), v1.2 (T5 pipeline) · **Versiya:** 1.2 · **Egasi:** llm-engineer · Qaror: [ADR 0009](adr/0009-llm-integration.md) · **Import:** `from app.llm.service import LlmService, create_llm_service`

LLM qadami **hech qachon istisno ko'tarmaydi** va pipeline'ni to'xtatmaydi: o'chirilgan, kvotasi tugagan, ishlamayotgan yoki yaroqsiz javob bergan LLM `ReviewStatus.FALLBACK` (post-mortem uchun `None`) beradi, signal izohsiz (faqat raqamlar bilan) yuboriladi. LLM ehtimollik, koeffitsient yoki EV'ni qaytarmaydi: sxemada bunday maydon yo'q.

| Element | API | Izoh |
|---|---|---|
| Servis | `LlmService(backend: JsonGenerator \| None, *, budget=None, input_token_budget=6000)`; `create_llm_service(settings, budget=None) -> LlmService` | `backend=None` yoki `GEMINI_API_KEY` yo'q → LLM o'chiq (hamma natija fallback, sabab `llm_disabled`) |
| Tekshiruv | `await service.review_candidates(candidates: Sequence[CandidateInput], context: ExperienceContext \| None = None) -> tuple[LlmReview, ...]` | Natija kirish bilan **bir-bir va bir xil tartibda**. Bitta so'rov (≤ 10 nomzod; ortig'i `omitted_by_token_budget` fallback). Bo'sh kirish → `()`, chaqiruv yo'q |
| Post-mortem | `await service.postmortem(case: PostmortemInput) -> PostmortemResult \| None` | `category` ∈ {`omadsizlik`, `ma'lumot yetishmadi`, `model xatosi`} + `reason`; `None` = hozir javob yo'q, keyinroq qayta urining (F8) |
| Hisob | `service.budget` → `LlmBudget`: `snapshot()` (bugungi so'rov/token), `journal()` (oxirgi 500 so'rov: vaqt, maqsad, prompt versiyasi, model, tokenlar, natija; prompt matni yo'q) | UTC kun; kunlik limit: `LlmLimits(max_requests_per_day=100, max_tokens_per_day=250_000)`. v1.1: `snapshot().requests` — HTTP urinishlar (klient ichidagi qayta urinishlar ham), `snapshot().logical_requests` — servis chaqiruvlari; `JournalEntry.attempts`, `JournalEntry.estimated` (provayder usage bermaganda tokenlar belgidan baholangan). `reserve()/settle()/release()` parallel chaqiruvlarda limitdan oshishni oldini oladi |

**Kirish DTO lari** (`app.llm.inputs`, frozen pydantic, `extra="forbid"`; pipeline to'ldiradi): `CandidateInput(candidate_id, league, home_team, away_team, kickoff (UTC), market, selection, line, odds, p_model, p_market, ev, confidence_level, confidence_reasons, lineup_known, forms: tuple[TeamForm], absences: tuple[Absence])`; `TeamForm(team, results "WDLWW", goals_for, goals_against)`; `Absence(team, player, status out|doubtful|suspended, reason)`; `ExperienceContext(segment_stats, lessons, user_notes)` (F8 to'ldiradi; ro'yxatlar muhimlik tartibida); `PostmortemInput(candidate, final_score "1-2", xg_summary, match_events)`. `candidate_id` — pipeline bergan noyob kalit (masalan `"<fixture_ref>:<market>:<selection>"`; bir o'yinda bir nechta tanlov bo'lishi mumkin, shuning uchun rejadagi `fixture_id` o'rniga).

**Chiqish** (`app.llm.schemas`): `LlmReview(candidate_id, status: ok|note_dropped|fallback, verdict: Verdict|None, risk_flags: tuple[RiskFlag, ...], note: str|None, flags, fallback_reason, prompt_version, model, input_tokens, output_tokens)`. `Verdict`: `tasdiq | ehtiyot | rad`. `RiskFlag`: `jarohat, diskvalifikatsiya, rotatsiya, motivatsiya, derbi, tarkib_nomalum, malumot_yetarli_emas, ziddiyatli_malumot`. Tokenlar butun so'rovning narxi bo'lib **birinchi** `LlmReview` ga yoziladi (yig'indi to'g'ri chiqishi uchun). `prompt_version` (`context_check.v1`) va `model` har bir natija bilan saqlanadi (pipeline signalga yozadi).

**Fallback sabablari** (`fallback_reason`): `llm_disabled`, `budget_exhausted`, `llm_unavailable` (429/5xx/timeout qayta urinishlardan keyin), `invalid_output` (2 urinishdan keyin ham sxemaga mos emas), `missing_in_response`, `omitted_by_token_budget`, `invalid_input` (masalan, takroriy `candidate_id`), `internal_error`. `note_dropped`: verdict saqlanadi, izoh tashlanadi (`flags`: `unknown_name_in_note` — kirishda yo'q jamoa/o'yinchi nomi; `note_too_long` — > 400 belgi; `note_suspicious` — izohda HTML/teg yoki `<<<`/`>>>` delimitr). Izoh 2 gapgacha qisqartiriladi. Gap boshidagi kirishda yo'q nom ham `unknown_name_in_note` beradi (kichik o'zbekcha gap boshlovchilar ro'yxati bundan mustasno).

**Pipeline qoidalari:** (1) `verdict=rad` bo'lsa ham signalni o'chirish/ko'rsatish siyosati pipeline'niki; LLM maslahatchi. (2) `status=fallback` → "LLM izohi yo'q" deb belgilab yuboring; xato deb hisoblamang. (3) Chaqiruv ~1 so'rov/kun; qayta hisoblashlarda har safar chaqirmang (kvota). (4) `Settings` dan `create_llm_service(get_settings())` bilan yasang; jarayon bo'yicha bitta `LlmService` (budget holati shunda saqlanadi). (5) **Izoh (`LlmReview.note`) va `PostmortemResult.reason` — model matni, ishonchsiz:** bot/pipeline uni Telegram'ga chiqarishda **HTML-escape** qilishi shart (kod faqat markup/delimitrni rad etadi, tilni emas). (6) `PostmortemResult.reason` bo'sh (`""`) bo'lishi mumkin: qo'riq uni rad etgan; bunday `reason` saboq sifatida saqlanmasin.

**Klient** (`app.clients.gemini`): `GeminiClient.generate_json(system_instruction, contents, response_schema) -> GeminiResponse(text, model, usage, attempts)`, `create_gemini_client(settings)`, `GeminiError` (`ExternalServiceError`; v1.1: `attempts` — HTTP urinishlar soni, `usage` — bo'sh javobda sarflangan tokenlar), `JsonGenerator` protokoli. Transport xatolari (`aiohttp`/`httpx`/OS/timeout) tuzilma bo'yicha tranzient deb qayta urinadi, qolgan istisnolar `GeminiError` ga o'raladi (xom istisno chiqmaydi). Model — `GEMINI_MODEL` (standart `gemini-flash-latest`), `temperature=0.2`, timeout 30 s, 429/5xx/timeout/transport xatosida 3 urinishgacha eksponensial kutish.

**v1.2 (T5 pipeline, faqat qo'shimcha):** `PostmortemInput.original_verdict` (`tasdiq`/`ehtiyot`/`rad`) va `.original_note` (o'yindan oldingi sharh; model matni, ma'lumot sifatida); prompt `postmortem.v2` (`v1` saqlanadi, `POSTMORTEM_PROMPT` endi v2). Post-mortem, saboq va tajriba konteksti to'ldirilishi (pipeline): [ADR 0015](adr/0015-feedback-loop-pipeline.md). `ExperienceContext` ni pipeline har sharh paketi uchun yig'adi: `segment_stats` ≤ 4, `lessons` ≤ 5, `user_notes` ≤ 3; `CandidateInput.forms` to'ldiriladi, `absences` hozircha bo'sh (jarohat bazada saqlanmaydi).

**Baholash:** `tests/llm/golden/*.json` (27 holat, 22 tasi jonli), `scripts/llm_eval.py` (faqat qo'lda, kalit bilan; kalitsiz exit 2), natija `docs/llm_eval/<sana>.md`.

---

## O'zgarishlar jurnali
| Sana | Shartnoma | O'zgarish | Kim | Ruxsat |
|---|---|---|---|---|
| 2026-10-21 | 1. Domen, 2. `SportAdapter` | v1 yozildi (T2, F2.4) | data-engineer | orkestr ko'rib chiqadi |
| 2026-10-21 | 3. Baza sxemasi | v1 yozildi, Alembic `0001` (T2, F2.1) | data-engineer | orkestr ko'rib chiqadi |
| 2026-10-21 | 3. Baza sxemasi | v1.1 (qo'shimcha): Alembic `0002` — `user_settings.daily_loss_limit`/`weekly_loss_limit`, `ix_user_bets_signal_id`, opening/closing uchun qisman unique indeks; football-data o'yin `external_id` = `liga/mavsum/uy/mehmon` (sanasiz) | data-engineer | orkestr (T2 fix round) |
| 2026-10-21 | 4. Servis shartnomasi, 5. Soxta servis | v1 taklif yozildi (T2): DTO lar, 10 ta protokol, `Services` to'plami, xatolar ierarxiyasi, deterministik fake, qayta ishlatiladigan shartnoma testlari ([ADR 0004](adr/0004-service-contract.md)) | pipeline-engineer | orkestr ko'rib chiqadi |
| 2026-10-21 | 4. Servis shartnomasi, 5. Soxta servis | v1.1 (T3, faqat qo'shish): `SignalSource`, `SignalView.source`/`recommended_at`, `SportOption` + `CatalogService.list_sports`, `DateWindow.utc_bounds`/`contains`, `horizon_end`, `ReportRequest.recent`/`all_time`/`resolve`/`recent_days`, `MAX_ENTITY_ID_BYTES`/`is_valid_entity_id`; tavsiya, retro, yashirish, sevimlilar, `scope=all` va horizon semantikasi aniqlashtirildi | pipeline-engineer | orkestr (sharh tuzatishlari) |
| 2026-10-21 | 1. Domen, 3. Baza sxemasi | v1.2 (T3, faqat qo'shimcha): `BOOKMAKER_1XBET`; Alembic `0003` — `signals.recommended_at`, `min_prob`, `min_ev`, `odds_min`, `odds_max`, `(user_id, recommended_at)` indeksi; `SignalSource` + `analysis`, `retro`; `api_usage.reserved_manual` ma'nosi aniqlandi; §3.1 repository API, §3.2 klient va adapter ([ADR 0007](adr/0007-api-client-and-repositories.md)) | data-engineer | orkestr ko'rib chiqadi |
| 2026-10-21 | 6. Model API | v1 taklif yozildi (T3): domen adapterlari, `predict_markets`, bozor modeli (`fair_markets`), ansambl, `assess_confidence`, `asian_fair_odds`, `settle_selection`; backtest kutubxonasi ([ADR 0005](adr/0005-backtest-methodology.md)) | quant-modeler | orkestr (tasdiqlangan) |
| 2026-10-21 | 6. Model API | v1.1 (T3 fix, faqat qo'shish/aniqlashtirish): `complete_quotes(..., kickoffs=None)` — kickoffdan keyingi qaror vaqti narxlari tashlanadi (QA F-3); ishonch farqi chegarasi qat'iy va 1e-9 gacha yaxlitlangan (QA F-2); backtest: `complete_seasons`, `season_end`, `GateStatus`, `gate_passed`, darvoza qaror #11 bo'yicha | quant-modeler | orkestr (tasdiqlangan) |
| 2026-10-21 | 4. Servis shartnomasi | v1.1 (qo'shimcha, T3 fix round): `MAX_RETRO_LOOKBACK_DAYS = 90` — retro oynaning orqaga qarash chegarasi (qaror #12); retro oyna uzunligi uchun mavjud `MAX_WINDOW_DAYS` (31) qayta ishlatiladi. Bot kalendari va matnli oraliq shuni ishlatadi; servis tomonida majburlash pipeline-engineer ishi | bot-engineer | orkestr (T3 fix round topshirig'i) |
| 2026-10-21 | 7. LLM API | v1 taklif yozildi (T4): `LlmService.review_candidates` / `postmortem`, kirish DTO lari, `LlmReview`, fallback sabablari, kunlik limit, golden baholash ([ADR 0009](adr/0009-llm-integration.md)) | llm-engineer | orkestr ko'rib chiqadi |
| 2026-10-22 | 3. Baza sxemasi (3.1), 3.2 | v1.3 (T3 fix round, faqat qo'shimcha): `complete_recalc`/`release_recalc` `claimed_at`, `settle_bet`/`set_result_note` `user_id` parametrlari (chaqiruvchilar yangilanadi); `finished_fixtures` qayta o'yinlarni saqlaydi; `latest_odds` turi guruh bo'yicha; kvota `reconcile(key, ...)`, `shared_guard`; xG cheklovi; `HANDICAP_CONVENTION` ([ADR 0007](adr/0007-api-client-and-repositories.md) §12) | data-engineer | orkestr (T3 fix round topshirig'i) |
| 2026-10-22 | 3. Baza sxemasi (3.1) | v1.4 (T4 data round): Alembic `0004` `pipeline_ledger`, `0005` `saved_queries` tuzilgan jadval/bayroqlar (`schedule_cron` olib tashlandi), `0006` `user_bets.client_token` + `signal_notes`; repo: `catalog`, `notes`, `get_bet_by_token`, idempotent `create_bet` ([ADR 0011](adr/0011-t4-data-round.md)) | data-engineer | orkestr (pipeline-engineer so'rovi, ADR 0008) |
| 2026-10-22 | 3. Baza sxemasi, 4. Servis shartnomasi, 5. Soxta servis | v1.5 (T4 fix round, faqat qo'shimcha): Alembic `0007` (`signals.recommended_odds`/`fair_odds`/`quoted_at`, `pipeline_ledger.attempts`/`retry_after`); `SignalView.recommended_odds`/`zero_ev_odds`/`profit_odds`, `RecalcVerdict.NO_PRICE`, `AnalysisResult.skipped_no_price`/`skipped_stale_price`/`data_stale`, `ApiQuota.manual_reserve`/`manual_used`, `SystemStatus.llm` (`LlmQuota`); `reschedule_recalc`, `open_bets_of_settled_signals`, `touch_signal_quote_time`, `latest_odds(taken_before=)`; `place_bet` qoidalari; qaror #17 (foyda va CLV birinchi tavsiya koeffitsientida) ([ADR 0008](adr/0008-pipeline-and-real-services.md) qo'shimchasi) | pipeline-engineer | orkestr (T4 fix round topshirig'i; bot-engineer so'rovi: `SystemStatus.llm`, `ApiQuota` bo'linishi) |
| 2026-10-22 | 4. Servis shartnomasi, 5. Soxta servis | v1.6 (T5 bot, faqat qo'shish): `UndeliverableAware` porti va `UNDELIVERABLE_REFERENCE`, `ExpressService`/`ExpressView`/`ExpressLeg`/`MIN_EXPRESS_LEGS`/`MAX_EXPRESS_LEGS`/`Services.express`, `ReportService.export_bets`; fake va real servislar, `express_math`, shartnoma testlari ([ADR 0012](adr/0012-t5-bot-features.md)) | bot-engineer | orkestr (T5 topshirig'i) |
| 2026-10-22 | 3. Baza sxemasi (3.1), 4. Servis shartnomasi, 7. LLM API | v1.7 (T5 pipeline, faqat qo'shish): `SignalView.home_team_id/away_team_id/league_id`, `LimitPeriod`/`LossLimitNotice`, `DailyDigest.template_name/loss_limit`, `AnalysisResult.loss_limit`; `FixtureRecord` katalog id lari, `db.repo.feedback`, `bets_settled_between`; `PostmortemInput.original_*`, prompt `postmortem.v2`; zarar limiti va jadvalli shablon qoidalari ([ADR 0015](adr/0015-feedback-loop-pipeline.md)) | pipeline-engineer | orkestr (T5 pipeline topshirig'i) |
| 2026-10-23 | 4. Servis shartnomasi, 5. Soxta servis | v1.8 (T5 bot fix, faqat qo'shish): cashout = to'langan pul summasi (`ResultMarkRequest.cashout_bet_id/cashout_payout`, qaror #18), `BetService.get_bet`, `BankTotals`/`BankView.totals`, ekspress EV `prod(1+ev)-1` + `ExpressLeg.fair_odds`, `LearningService`/`Services.learning`/`LearningChangeView`/`LearningPage`/`LearningSummary`/`LearningMode`/`RollbackOutcome`, `Notifier.send_learning_summary`, `SystemStatus.learning_mode/learning_missed`; `report_math.safe_cell` + CSV BOM ([ADR 0016](adr/0016-t5-bot-fix-and-f8-ui.md)) | bot-engineer | orkestr ko'rib chiqadi |
| 2026-10-23 | 3. Baza (repo), 4. Servis shartnomasi, 5. Soxta servis | v1.9 (T6 fix A, faqat qo'shish; v1.8 dagi cashout "boshqa stavkalar" qoidasi #22 bilan almashtirildi): `MAX_MONEY`, cashout chegaralari, ochiq stavkali signal settlement (#19), aylanma = xavf ostidagi stavka (#21), Kelly balansdan, `db.repo.get_bet`/`signal_has_bets`/`settled_profit_total`, kesh chegaralari va `PayloadShape`, bukmeker filtri, `retention`, `promote_shadow_state`, `redaction` ([ADR 0018](adr/0018-t6-fix-round-a.md)) | pipeline-engineer | orkestr qarorlari #19–#23 |
| 2026-10-23 | 4. Servis shartnomasi, 5. Soxta servis | v1.10 (T6 fix C, faqat qo'shish): `SystemStatus.build`, `DigestResume`/`DigestProgress`/`DailyDigest.progress` (kartalar bo'yicha yetkazish jarayoni), kartani doimiy rad etish/vaqtincha xato qoidasi, yetkazish kalitlari uchun 6 soatlik qayta urinish jadvali, `Ledger.complete_many`, `db.repo.get_signals_by_ids`, sharh vaqt chegarasi paket boshiga, `retention` job, `INGEST_EXTRA_BOOKMAKERS` ([ADR 0019](adr/0019-t6-fix-round-c.md)) | bot-engineer / pipeline-engineer | orkestr ko'rib chiqadi |
| 2026-10-22 | 6. Model API, 3. Baza sxemasi | v1.2 (T5, faqat qo'shimcha): `models.segments` (shrinkage, bootstrap CI, `segment_action`), `models.calibration` (isotonic, walk-forward, darvoza, versiya/rollback); Alembic `0008` (`signals.p_raw`, `segment_stats.state_since/hold_until`, `calibration_versions.passed_gate`, `learning_changes`); repo `learning`; pipeline `learning`/`overlay`, `JOB_LEARNING` ([ADR 0013](adr/0013-learning-loop-math.md)) | quant-modeler | orkestr (T5 topshirig'i) |
| 2026-10-22 | 4. Servis shartnomasi, 5. Soxta servis | v1.2 (T4, faqat qo'shish): `Notifier` porti va DTO lari, `DeliveryKey`, `ServiceUnavailableError`, `BusyError`, `recalc_window`/`recalc_signals`, `get_signals`, `BetDraft.client_token`, bank semantikasi, `DateWindow.ensure_allowed`/`ensure_within_lookback`, `FakeNotifier`; `_fake_metrics` → `report_math` ([ADR 0008](adr/0008-pipeline-and-real-services.md)) | pipeline-engineer | orkestr ko'rib chiqadi |
| 2026-10-22 | 7. LLM API | v1.1 (T4 fix round, faqat qo'shimcha): `note_suspicious` bayrog'i; `GeminiError.attempts/usage`; `BudgetSnapshot.logical_requests`, `JournalEntry.attempts/estimated`, `LlmBudget.reserve/settle/release`; `inputs.MAX_NOTE_CHARS` -> `MAX_USER_NOTE_CHARS`; izoh/`reason` HTML-escape qoidasi; gap boshidagi nom tekshiruvi; golden 27 holat ([ADR 0009](adr/0009-llm-integration.md), tuzatish raundi) | llm-engineer | orkestr (T4 fix round topshirig'i) |
| 2026-10-23 | 4. Servis shartnomasi, 5. Soxta servis | v1.8 (T5 bot fix, faqat qo'shish): cashout = to'langan pul summasi (`ResultMarkRequest.cashout_bet_id/cashout_payout`, qaror #18), `BetService.get_bet`, `BankTotals`/`BankView.totals`, ekspress EV `prod(1+ev)-1` + `ExpressLeg.fair_odds`, `LearningService`/`Services.learning`/`LearningChangeView`/`LearningPage`/`LearningSummary`/`LearningMode`/`RollbackOutcome`, `Notifier.send_learning_summary`, `SystemStatus.learning_mode/learning_missed`; `report_math.safe_cell` + CSV BOM ([ADR 0016](adr/0016-t5-bot-fix-and-f8-ui.md)) | bot-engineer | orkestr ko'rib chiqadi |
| 2026-10-23 | 6. Model API, 3. Baza sxemasi | v1.3 (T5 fix, faqat qo'shimcha): takroriy tekshiruvga qarshi himoya (99.5% oraliq, BH FDR, ketma-ket ikki run), `LEARNING_MODE` (`shadow` standart), Alembic `0009` (`learning_changes.applied`, `segment_stats.flagged`), `review_segments`, `confirmed`, `rollback_change`/`RollbackResult`, `load_overlay` ([ADR 0013 qo'shimchasi](adr/0013-learning-loop-math.md)) | quant-modeler | orkestr ko'rib chiqadi |
| 2026-10-26 | 3. Baza sxemasi | v1.11 (T7-A, faqat qo'shimcha): Alembic `0010` (`provider_calls`), repo `record_call`/`list_calls`/`count_calls`/`get_call`/`purge_calls_older_than`/`call_stats`, `SqlCallRecorder`, klientlarga ixtiyoriy `recorder`, `CALL_LOG_RETENTION_DAYS` ([ADR 0020](adr/0020-provider-call-log.md)) | data-engineer | orkestr ko'rib chiqadi |
| 2026-10-26 | 3. Baza (repo), 4. Servis shartnomasi, 5. Soxta servis | v1.11 (T7-B, faqat qo'shish): tahlil faqat bazadan (qaror #25): `DataStaleError`, `NotAuthorizedError`, `CostEstimate.data_stale/last_refresh_at`, `DataBrowserService` + `Services.browser` (baza ko'rish va adminning "Bazani yangilash" i), `db.repo.browse`, `pipeline.freshness`, `refresh_manual`, kunlik refresh mahalliy kunni to'liq qamraydi, `REFRESH_AT`/`MANUAL_REFRESH_MIN_MINUTES`, `max_quote_age` 30 soat ([ADR 0021](adr/0021-db-only-analysis.md)) | pipeline-engineer | orkestr ko'rib chiqadi |
| 2026-10-26 | 4. Servis shartnomasi, 5. Soxta servis, 7. LLM API | v1.12 (T7-C, faqat qo'shish): `CallLogService` + `Services.calls` (majburiy maydon), `CallLogEntry/Request/Page/Detail/Stat`, `MAX_CALL_STATS_DAYS`; `generate_json(..., operation=None)`, `LlmService` vazifa nomlarini uzatadi ([ADR 0020](adr/0020-provider-call-log.md)) | pipeline-engineer | orkestr ko'rib chiqadi |
| 2026-10-29 | 1. Domen, 4. Servis shartnomasi, 5. Soxta servis | v1.12 (jonli o'yinlar, faqat qo'shish): `Provider.FOOTBALL_DATA`; `MatchesService` + `Services.matches` (majburiy maydon), `MatchItem`/`MatchSheet`/`SideLineup`/`LineupPlayer`/`MatchState`, `MAX_MATCH_DAYS`, `check_match_days`; `FakeMatchesService`; platforma: `ProviderConfig.log_provider`, `ApiClient(quota=None)` (kunlik kvotasiz provayder) ([ADR 0023](adr/0023-football-data-org-client.md), [ADR 0024](adr/0024-live-matches-from-football-data.md)) | data-engineer / pipeline-engineer / bot-engineer | orkestr ko'rib chiqadi |
| 2026-10-29 | 2. `SportAdapter` (amalga oshirish), platforma | API-Football olib tashlandi (buzuvchi, foydalanuvchi qarori): `FootballProvider`, `FootballAdapter`, `football_mapping`, `ApiFootballClient`, `Settings.api_football_key`/`handicap_convention`/`require_explicit_handicap_convention`, `Runtime.http`; `ProviderConfig.log_provider` majburiy; Football-Data.org API servisi (`football_data_org_requests`, kengaytirilgan `football_data_org_models`, `FootballDataClient` protokoli barcha endpointlar bilan; `competition_matches(code, filters)` imzosi o'zgardi), `FOOTBALL_DATA_REQUESTS_PER_MINUTE` ([ADR 0025](adr/0025-football-data-only.md)) | data-engineer / pipeline-engineer | foydalanuvchi talabi; orkestr ko'rib chiqadi |
| 2026-10-29 | 2. `SportAdapter` (amalga oshirish), 3. Baza (manba nomi) | Football-Data.org provayderi (faqat qo'shish): `sports.football_data_org` (`FootballDataAdapter`, `SOURCE = football-data-org`, `map_fixture`/`map_result`/`map_odds`), `pipeline.provider.FootballDataProvider`, `TrackedLeague.provider_code`/`provider_ref`, `LeagueCatalog.by_provider_code`, `FOOTBALL_DATA_ORG_SOURCE`, `usage_counter`, `HttpFootballDataClient.matches(..., fresh=)`, `Score.regular_time`/`ninety_minutes`; `PipelineConfig.history_sources` boshiga `football-data-org`; runtime `bet_bookmaker = market_avg` ([ADR 0025](adr/0025-football-data-only.md)) | data-engineer / pipeline-engineer | foydalanuvchi talabi; orkestr ko'rib chiqadi |
