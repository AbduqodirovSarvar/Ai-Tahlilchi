# Interfeys shartnomalari

> Qoidalar: [agentlar_rejasi.md, 3-bo'lim](../agentlar_rejasi.md). Qotirilgan shartnomaga **faqat yangi narsa qo'shiladi**; buzuvchi o'zgarish orkestr ruxsati bilan va barcha foydalanuvchi agentlar yangilangandan keyin.
> Har bir o'zgarish pastdagi "O'zgarishlar jurnali"ga yoziladi.

**Umumiy holat:** 1–3 (domen, `SportAdapter`, baza sxemasi) — **frozen v1, orkestr tomonidan tasdiqlangan (2026-10-21)**. 4–5 (servis shartnomasi va soxta servis) — **frozen v1 (orkestr tasdiqlagan, 2026-10-21)** (T2, orkestr ko'rib chiqadi). 6–7 keyingi to'lqinlarda.

| # | Shartnoma | Fayl | Egasi | Foydalanuvchilar | Holat |
|---|---|---|---|---|---|
| 1 | Domen modellari | `src/app/domain/*.py` | data-engineer | hamma | qotirilgan (v1) |
| 2 | Sport adapteri | `src/app/sports/base.py` (`SportAdapter`) | data-engineer | pipeline, quant | qotirilgan (v1) |
| 3 | Baza sxemasi | `src/app/db/schema.py` | data-engineer | pipeline, bot (DTO orqali) | qotirilgan (v1) |
| 4 | Servis shartnomasi | `src/app/services/interfaces.py`, `src/app/services/errors.py` | pipeline-engineer | bot | frozen v1 (orkestr tasdiqlagan, 2026-10-21) (orkestr ko'rib chiqadi) |
| 5 | Soxta servis | `src/app/services/fake.py` (`create_fake_services`, `FakeAnalysisService`, ...) | pipeline-engineer | bot | frozen v1 (orkestr tasdiqlagan, 2026-10-21) (orkestr ko'rib chiqadi) |
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

## 4. Servis shartnomasi — `src/app/services/interfaces.py`
**Holat:** frozen v1 (orkestr tasdiqlagan, 2026-10-21) (orkestr ko'rib chiqadi) · **Versiya:** 1 · **Egasi:** pipeline-engineer · Qarorlar: [ADR 0004](adr/0004-service-contract.md) · **Import:** `from app.services.interfaces import ...`, `from app.services.errors import ...`

Bot tizim bilan **faqat** shu modul orqali gaplashadi (domen qiymat turlari `Sport`, `Market`, `MarketType`, `Selection` ham ishlatiladi). Barcha DTO lar `@dataclass(frozen=True, slots=True)`; noto'g'ri kiritma konstruktorda `ValidationFailedError` beradi (noto'g'ri so'rov obyekti umuman yaratilmaydi). Barcha metodlar `async`.

**Konvensiyalar**
- Lahzalar (`kickoff`, `recalculated_at`, `data_as_of`) — tz-aware **UTC** `datetime`. Foydalanuvchi tanlaydigan **sanalar** (`DateSpec`, `DateWindow`, `ReportRequest`, `day_summary(day)`) — foydalanuvchi vaqt mintaqasidagi (`UserSettingsView.timezone`, standart `Asia/Tashkent`) kalendar sanalari; UTC ga servis o'tkazadi.
- Ehtimollik, koeffitsient, EV — `float` (`ev=0.05` = +5%); pul va unit — `Decimal`.
- ID lar shaffof emas: `SignalId`, `BetId`, `TemplateId` — `int`; liga/jamoa/o'yin/so'rov — `EntityId = str` (≤ 64 bayt, `callback_data` ga sig'adi).
- Takroriy tahlil bir xil signal ID larini qaytaradi (kalit: foydalanuvchi + o'yin + bozor + tanlov).

**Nomlangan chegaralar (bot shulardan foydalanadi, raqamni takrorlamaydi)**

| Konstanta | Qiymat | Ma'nosi |
|---|---|---|
| `MIN_PROBABILITY_LOWER` / `_UPPER` | 0.30 / 0.95 | minimal ehtimollik oralig'i |
| `HIGH_PROBABILITY_WARNING` | 0.80 | §6.6 ogohlantirishi chegarasi |
| `MIN_EV_LOWER` / `_UPPER` | 0.0 / 0.20 | minimal EV oralig'i |
| `MAX_FUTURE_DAYS` | 14 | oyna bugundan ko'pi bilan 14 kun oldinga |
| `MAX_WINDOW_DAYS` | 31 | oyna uzunligi (retro uchun ham) |
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
- `include_no_value=True` (maxsus tahlil, §6.3): filtrlar o'chadi, tanlangan bozorlarning barcha tanlovlari qaytadi, chegaradan pastdagilari `no_value=True`, `stake_units=0`.
- Sevimli jamoa/liga o'yini (`scope=all`) shartga mos kelmasa ham eng yuqori EV li tanlovi bilan qo'shiladi (`watchlist=True`, `no_value=True`).
- `kickoff` dan 3 kundan uzoq — `preliminary=True`, ishonch bir pog'ona past. O'tgan oyna — `retro=True`, `result` to'ldirilgan, API sarfi yo'q. O'yin davom etayotgan (in-play) o'yinlar tahlil va ro'yxatdan chiqariladi.
- Byudjet: `estimate_cost` hech narsa sarflamaydi; `analyze` qo'lda so'rovlar zaxirasidan oshsa `BudgetExceededError(needed, available)`. 10 daqiqa ichida olingan ma'lumot qayta so'ralmaydi.
- Qayta hisob: bitta o'yin uchun `RECALC_COOLDOWN` da bir marta (`CooldownActiveError(retry_after)`), boshlangan o'yin — `ValidationFailedError`. Zaxira tugagan bo'lsa raqamlar o'zgarmaydi va `data_as_of` ("09:00 holatiga") qo'yiladi. `recalc_query` cooldown dagi va boshlangan signallarni o'tkazib yuboradi (`skipped_*`), bitta o'yindagi bir nechta signalni bitta hisob bilan yangilaydi; bitta xato butun to'plamni yiqitmaydi (`failed`).
- Egalik: boshqa foydalanuvchining signali, so'rovi yoki shabloni `NotFoundError` (mavjud emasdek). Noma'lum foydalanuvchi standart sozlamalarni oladi.
- `update_settings` to'liq almashtiradi va `risk_profile` ni chegaralardan qayta chiqaradi (preset bilan aynan mos → shu profil, aks holda `custom`).

**Xatolar — `app.services.errors`:** `ServiceError(AppError)` → `ValidationFailedError(field, message)` (+ `ValueError`), `NotFoundError(entity, entity_id)` (+ `LookupError`), `CooldownActiveError(retry_after: timedelta)`, `BudgetExceededError(needed, available)`.

## 5. Soxta servis — `src/app/services/fake.py`
**Holat:** frozen v1 (orkestr tasdiqlagan, 2026-10-21) (orkestr ko'rib chiqadi) · **Versiya:** 1 · **Egasi:** pipeline-engineer

- `create_fake_services(*, clock: Clock = system_clock) -> Services` — barcha protokollarning bitta umumiy xotira holati ustidagi soxta amalga oshirilishi (`FakeAnalysisService`, `FakeSignalService`, `FakeCatalogService`, `FakeSettingsService`, `FakeTemplateService`, `FakeWatchlistService`, `FakeBetService`, `FakeResultService`, `FakeReportService`, `FakeSystemStatusService`). `Clock = Callable[[], datetime]` (UTC).
- Ma'lumot: hash asosida **deterministik** (tasodif yo'q): APL, La Liga, Seriya A, Bundesliga (futbol) va KHL (xokkey, `experimental=True`), soat bo'yicha "bugun" dan 7 kun oldin — 14 kun keyin, har kuni har ligada bitta o'yin; bozorlar 1X2, ikki natija, Total 2.5, BTTS, Fora −0.5 (xokkey: 1X2, Total 5.5). Tugagan o'yinlarning ~15% ida natija "API dan kelmagan" (`/natijalar` uchun).
- Byudjet: kunlik limit 100, avtomatik sarf 40, qo'lda zaxira 30 so'rov; 10 daqiqalik kesh. Cooldown, progress, idempotentlik va xatolar shartnomadagidek.
- I/O, tarmoq, `sleep` yo'q. `RecalcVerdict.CANCELLED` va `failed_fixtures > 0` holatlarini soxta servis hosil qilmaydi (real servisda: o'yin qoldirilgan / tarkib, qisman nosozlik).

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
| 2026-10-21 | 4. Servis shartnomasi, 5. Soxta servis | v1 taklif yozildi (T2): DTO lar, 10 ta protokol, `Services` to'plami, xatolar ierarxiyasi, deterministik fake, qayta ishlatiladigan shartnoma testlari ([ADR 0004](adr/0004-service-contract.md)) | pipeline-engineer | orkestr ko'rib chiqadi |
