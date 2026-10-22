# QA hisoboti: T3 — Ma'lumotlar qatlami (F2.2, F2.3, F2.5, F2.6, F2.8, F0.7)

- **Tekshiruvchi:** test-engineer (mustaqil QA)
- **Sana:** 2026-10-21
- **Branch:** `wave/T3-qa-data` (`main` `5855316` dan)
- **Muhit:** Windows 11, Python 3.13, uv. Tarmoq yo'q: barcha HTTP `respx` bilan; API-Football fixture'lari **sintetik, sxema asosida** ([README](../../tests/fixtures/api_football/README.md)). Haqiqiy kalit bilan tasdiqlash F0 da.
- **Ishlab chiqarish kodi (`src/`) o'zgartirilmadi.** Faqat `tests/integration/` va shu hisobot.

## Yakuniy xulosa

### ✅ Darvozadan o'tadi (shartli)

🔴 topilma yo'q: pul, ehtimollik yoki kvota hisobida noto'g'ri natija topilmadi. Kvota (zaxira 30%), qayta urinish, kesh, sirlar, xaritalash (AET/PEN, AH ishorasi), qayta hisob navbati, hisobot o'qishlari va `Decimal` pul mustaqil testlarda tasdiqlandi.

Shartlar:
1. **F2 qabul mezonining birinchi yarmi bajarilmagan:** `python -m app.pipeline.refresh` mavjud emas (`src/app/pipeline/` bo'sh). Bu pipeline to'lqinining vazifasi; "≤ 40 so'rov" esa adapter darajasida o'lchandi va tasdiqlandi (pastda). Mezon pipeline'da to'liq yopiladi.
2. 2 ta 🟠 va 5 ta 🟡 topilma strict `xfail` testlari bilan qayd etilgan (tuzatilganda XPASS bo'lib yiqiladi, belgini olib tashlash kerak).

## F2 qabul mezonlari

| Mezon | Holat | Dalil |
|---|---|---|
| Bitta to'liq yangilash (10 liga) ≤ 40 so'rov | ✅ | `estimate_requests(FetchPlan(league_seasons=10, days=1, odds_league_days=10, fixtures_by_id=20, injury_fixtures=5))` = **27**; `respx` bilan sanalgan simulyatsiya (adapter → klient → HTTP) = **27**; koeffitsientlar 2 sahifali bo'lsa 37 (ikkalasi ham mos). Kvota hisoblagichi ham 27, `manual_used=0`, avtomatik zaxira 70−27. `test_a_full_refresh_of_ten_leagues_stays_within_forty_requests` |
| Kesh: 10 daqiqa ichida takroriy yangilash 0 so'rov | ✅ | `test_an_immediate_second_refresh_is_served_from_cache` (601 s dan keyin yana 27) |
| Bepul tarif tezligi ≤ 10/daq | ✅ | Ishlab chiqarish konfigi (10/daq, burst 1): so'rovlar orasi ≥ 6 s, istalgan 60 s oynada ≤ 10; 27 so'rovlik yangilash ≈ 156 s fon vaqti |
| `python -m app.pipeline.refresh` buyrug'i | ❌ (pipeline to'lqini) | modul yo'q; `test_the_refresh_entry_point_..._exists` strict xfail (QA-T3-A1) |
| Testlarda tarmoq yo'q | ✅ | barcha yangi testlar `respx`/SQLite; probe subprocess testi o'lik proksi (`127.0.0.1:9`) bilan: chiqish kodi 2 (1 emas), demak ulanishga urinilmagan |
| `probe_api.py` kalitsiz: exit 2, so'rov yo'q | ✅ | kalit yo'q / `""` / `"   "` → exit 2, hisobot fayli yaratilmaydi; in-process: 0 so'rov |
| Probe xarajati | ✅ | 10 liga: ≤ 1 + 2×10 + 4 = 25 so'rov, faqat `v3.football.api-sports.io`; kalit faqat sarlavhada; hisobot, yozib olingan JSON, stdout/stderr da kalit va shaxsiy maydonlar (`email`, `firstname`, `lastname`) yo'q |
| Alembic `0003` | ✅ | CLI: `upgrade head` → `check` ("No new upgrade operations detected") → `downgrade 0002` → `upgrade head` → `0003 (head)`; mavjud qatorlar saqlanadi (`test_0003_...keeps_rows`); QA testlari bazani **haqiqiy migratsiya** bilan quradi |

## Risk asosidagi tekshiruvlar (yangi testlar)

| Xavf | Natija | Test |
|---|---|---|
| Avtomatik so'rov zaxiraga tegmaydi, 400 ta bir vaqtdagi `gather` | ✅ aynan 70 ta o'tadi, 330 rad, `requests_used=70` | `test_concurrent_automatic_charges_never_enter_the_reserve` |
| Qo'lda so'rov zaxirani ishlatadi | ✅ keyingi 30 ta o'tadi (jami 100) | `..._manual_can_still_use_the_whole_reserve...` |
| Aralash (2 avtomatik : 1 qo'lda) invariant | ✅ har bir muvaffaqiyatli avtomatik zaryaddan keyin `used ≤ limit − max(reserve − manual, 0)` | `test_interleaved_purposes_keep_the_reserve_invariant` |
| Bir xil, lekin **SQL** do'kon bilan (haqiqiy `await` nuqtalari) | ✅ | `test_sql_store_keeps_the_reserve_under_concurrency` |
| HTTP darajasi: 150 parallel so'rov | ✅ 70 ta yuboriladi, rad etilganlar yuborilmaydi (`route.call_count==70`) | `test_http_level_concurrency_...` |
| Kesh urishi pul olmaydi | ✅ | `test_cache_hits_are_not_charged` |
| Qayta urinishlar to'g'ri sanaladi | ✅ 500,502,200 → 3 ta so'rov va 3 zaryad; 429×4 → aynan `max_attempts`=4, oxirgisidan keyin kutish yo'q | `test_each_retry_attempt_...`, `test_exhausted_retries_...` |
| 429 + `Retry-After` | ✅ `7`→7 s; `9999`→120 s (chegara); `0/-5/nan`→oddiy backoff 0.5 s; `inf`→120 s | `test_429_retry_after_is_honoured_and_capped` |
| 401/403/404 qayta urinilmaydi va keshlanmaydi | ✅ har chaqiruv = 1 so'rov, kutish yo'q | parametrlangan test |
| Kvota yo'lda tugasa | ✅ yuborilmaydi | `test_quota_exhaustion_mid_retry_...` |
| Provayder sarlavhalari xato javobda ham kvotani oshiradi | ✅ | `test_provider_headers_reconcile_even_on_failed_responses` |
| `errors` to'la HTTP 200 keshlanmaydi | ✅ (`error_token`, `error_plan`); `errors: []` va `{}` muvaffaqiyat | `test_error_envelopes_with_http_200_are_never_cached` |
| Sirlar: loglar (8 stsenariy: 200, 500, 429, 401, JSON emas, `errors`, timeout, ulanish xatosi), kesh kalitlari, istisno zanjiri | ✅ kalit yo'q; uchinchi tomon istisno matni (testda ataylab kalit bilan yozilgan `ReadTimeout`) loyiha istisnosi va loglarga o'tmaydi | `test_secret_never_reaches_logs_cache_keys_or_exceptions` |
| Xaritalash: AET (90' = 1–1, goals 2–1) va PEN (0–0) regulyar vaqt hisobi; `fulltime` yo'q bo'lsa `goals` ishlatilmaydi | ✅ | `test_extra_time_...`, hypothesis `test_result_mapping_is_total_and_consistent` |
| PST/CANC/AWD/WO hisobsiz (provayder hisob yuborsa ham); ABD `finished` emas; UTC o'tkazish | ✅ | mapping testlari |
| Osiyo forasi ishorasi: `HOME_LINE` (`Away -1.5` → uy chizig'i −1.5) va `OWN_LINE` (`Away +1.5` → −1.5); ikki konvensiya faqat Away ishorasida farq qiladi; `Home 0` ishorasiz | ✅ | AH testlari |
| Noma'lum bozorlar (`Exact Score`, `Asian Handicap First Half`, `Draw No Bet`, `Home/Away`, bo'sh nom) e'tiborsiz va hisobotga yoziladi; `nan/inf/1e999/0/1.0/-2/1,85` narxlar snapshot bo'lmaydi | ✅ + hypothesis (istalgan matn yiqilmaydi, narx doim chekli va > 1) | |
| Jamoa moslash: alias urishlari (Man City/United, Betis, Sociedad, PEC Zwolle) | ✅ barchasi `ALIAS`, noaniq/mos kelmagan yo'q | `test_alias_hits_...` |
| Jamoa moslash: 3 ta to'liq jadval (EPL 20, La Liga 20, Serie A/NL 10) alias bilan va **faqat fuzzy** rejimda | ✅ noto'g'ri juftlik 0; alias bilan 100% to'g'ri | `test_full_league_tables_never_cross_merge` |
| Manchester City↛United, Real Madrid/Betis/Sociedad, PSV↔PEC, `Internazionale` ↔ `Internacional` | ✅ haqiqiy klub yo'q bo'lsa — mos kelmagan; `Internazionale` — noaniq (birlashtirilmaydi) | |
| `Inter` ↔ `Internacional` (haqiqiy `Inter` tarixda yo'q) | ❌ **birlashtiriladi** | QA-T3-M1 |
| Qidiruv: kirill → lotin (16 ta so'rov), lotin → kirill, registr/diakritika/apostrof | ✅ | |
| Qidiruv: `Аякс`→Ajax, `Бавария`→Bayern | ❌ bo'sh natija | QA-T3-S1 |
| Qidiruv hypothesis: istalgan so'rov va nomlarda yiqilmaydi, ball 60..100, kamayish tartibida | ✅ | |
| `claim_due_recalcs`: 8 parallel egallovchi (alohida sessiyalar, fayl bazasi), 24 vazifa | ✅ takror yo'q, xato yo'q, qolgani keyin olinadi, yo'qolgan yo'q | `test_concurrent_claimers_...` |
| Qulagan ishchi (rollback) vazifani yo'qotmaydi; lease chegarasi (aynan 15:00 — hali amal qiladi, +1 s — qayta olinadi); bajarilgan vazifa qayta olinmaydi | ✅ | |
| `upsert_signal`: 8 xil kalit (bozor/chiziq/tanlov/foydalanuvchi, AH ±0.5) alohida; qayta tahlil — shu qator, versiyalar 1,2,3 | ✅ | `test_signal_identity_...` |
| Hisobot o'qishlari 8 kombinatsiya (manba × holat × tavsiya): `recommended_at` bor, retro emas, **yashirilgan kiradi**; ro'yxat yashirin/retroni chiqarmaydi; retro `unsettled`/`signals_for_fixtures` da yo'q; boshqa foydalanuvchi aralashmaydi | ✅ | `test_report_reads_...` |
| Pul `Decimal` oxirigacha: 10×`0.1` = `1.0000` aniq; bazada `typeof = text`; float/int/str/NaN/Infinity rad (`DataError`) | ✅ | `test_money_...` |
| Bir o'yin tarix + jonli manbadan: bitta qator | ✅ (`IdentityMap` bo'lsa); xaritalanmagan jamoa — ikki qator (jim birlashtirilmaydi); ABD hisob bilan ham modelga kirmaydi | `test_a_match_known_from_history_and_live_is_counted_once` |

## Topilmalar

### 🟠 Yuqori

**QA-T3-R1 — `finished_fixtures` bir mavsumda ikki marta o'ynalgan juftlikni yo'qotadi**
`src/app/db/repo/fixtures.py:169-172`. Takrorlashni olib tashlash kaliti `(liga, mavsum, uy, mehmon)`. Belgiya Pro League (liga 144, kuzatiladigan 10 liganing biri) da oddiy mavsum va play-off bosqichida bir xil uy–mehmon juftligi bir mavsumda ikki marta o'ynaladi; keyingisi **jim tashlanadi** → model ma'lumotni yo'qotadi (ehtimollik hisobiga ta'sir). Manbalar orasidagi dublikat ajratilishi kerak (turli `source`), bir manba ichidagi ikki o'yin emas.
Qayta ishlab chiqarish: `tests/integration/test_t3_data_repo.py::test_a_pairing_played_twice_in_one_season_is_not_collapsed` (strict xfail). Tavsiya: kalitga `source` qo'shish yoki faqat `|kickoff` farqi < ~2 kun bo'lganda birlashtirish. Egasi: data-engineer.

**QA-T3-B1 — xG so'rovlari F2 byudjetiga sig'maydi**
`src/app/sports/football.py` `with_expected_goals` (har bir tugagan o'yin = 1 so'rov, `cost.py` da `statistics_fixtures`). Reja §9 jadvalida xG qatori yo'q. Bitta kunda 20 ta yangi tugagan o'yin: 27 + 20 = **47 > 40**, 30 ta bo'lsa 57. Adapter xatosi emas, byudjet teshigi: pipeline xG ni avtomatik kvotaning qolgan qismi bilan cheklashi va `estimate_requests` bilan oldindan hisoblashi shart.
Dalil: `test_expected_goals_for_every_new_result_would_break_the_budget`, `test_expected_goals_are_one_request_per_finished_fixture` (ikkalasi yashil — xulq hujjatlashtirilgan). Egasi: pipeline-engineer + rejani yangilash.

### 🟡 O'rta

**QA-T3-M1 — prefiks nomlar 90 balldan o'tib birlashadi.** `src/app/sports/team_matching.py:200-202`. `WRatio` bir nom ikkinchisining prefiksi bo'lsa ball **aynan 90** (0.9 koeffitsient) beradi va chegara `>= 90`. `Inter` tarixda yo'q bo'lsa `Internacional` ga birlashadi (va teskarisi). Cheklovchi omillar: liga bo'yicha alohida moslash, alias, aniq moslik oldin. Tavsiya: fuzzy uchun chegarani 90 dan yuqori qilish yoki prefiks holatini "noaniq" deb belgilash. Test: `test_prefix_names_are_not_merged_when_the_real_club_is_absent` (strict xfail).

**QA-T3-S1 — kirill so'rovlar ekzonimlarni topmaydi.** `src/app/db/search.py` (`rank`): `Аякс`→`ayaks`≠`Ajax`, `Бавария`→`bavariya`≠`Bayern`. Natija bo'sh. Qo'shimcha kuzatuv: `ПСЖ` → PSV Eindhoven (72 ball, noto'g'ri), `МАН СИТИ` → Roma (60). Tavsiya: kichik ekzonim/qisqartma jadvali (alias jadvali bilan bir joyda). Test: `test_cyrillic_exonyms_find_the_latin_name` (strict xfail).

**QA-T3-D1 — bir xil parallel so'rovlar bitta so'rovga birlashmaydi (single-flight yo'q).** `src/app/clients/http.py:189-195`. Kesh javob kelgandan keyingina to'ladi; 20 ta bir vaqtdagi bir xil `get_json` = 20 ta so'rov va 20 zaryad (sekin tarmoq bilan). Foydalanuvchi ikki marta bosganda yoki pipeline parallel chaqirganda kvota behuda ketadi. Test: `test_concurrent_identical_requests_cost_one_request` (strict xfail).

**QA-T3-D2 — o'qib bo'lmaydigan 200 javob 10 daqiqa keshda qoladi.** `src/app/clients/api_football.py:201-208`: `invalidate` faqat to'g'ri `errors` da chaqiriladi; `Envelope` validatsiyasi yiqilsa (masalan `errors: {"rateLimit": 429}` — qiymat satr emas) buzuq tana keshda qoladi va qayta o'ynaladi, qayta urinish mumkin emas. Test: `test_unparsable_envelope_is_not_cached` (strict xfail).

**QA-T3-R2 — bir vaqtdagi ikki birinchi tahlil: yutqazgan `IntegrityError` oladi.** `src/app/db/repo/signals.py:52-63`. Dublikat signal paydo bo'lmaydi (UNIQUE himoya qiladi), lekin ikkinchi sessiya versiya 2 bo'lish o'rniga xom `IntegrityError` bilan yiqiladi (bot ikki marta bosilganda xato xabari). Deterministik takror: `test_two_simultaneous_first_analyses_become_one_signal_with_two_versions` (strict xfail, SQLAlchemy hodisasi bilan navbatlash). Tavsiya: `ON CONFLICT DO UPDATE` yoki bitta qayta urinish.

### Kuzatuvlar (xfail emas)

- **Tezlik cheklovchi navbati FIFO:** qo'lda (foydalanuvchi) so'rov fon yangilanishining butun navbatidan keyin turadi (bepul tarifda ≈ 6 s × navbat, yangilanish davomida ~2.5 daqiqagacha). Kvotada qo'lda zaxira bor, lekin tezlikda ustuvorlik yo'q.
- **`errors.rateLimit` (HTTP 200) qayta urinilmaydi:** `ApiFootballError` darhol ko'tariladi (xavfsiz, lekin yangilanish qisman to'xtaydi). Token bucket buni kamaytiradi.
- **`complete_recalc`/`release_recalc` egasini tekshirmaydi:** lease tugab boshqa ishchi egallagan vazifani sekin birinchi ishchi ham yopa oladi (`operations.py:95,105`). Idempotent ishlar uchun zararsiz.
- **Probe sarfi `api_usage` ga yozilmaydi** (`InMemoryUsageStore`, `scripts/probe_api.py:362`): ilovaning birinchi so'rovida sarlavhalar orqali tuzatiladi (faqat yuqoriga).
- **Osiyo forasi konvensiyasi hamon tasdiqlanmagan** (ADR 0007): testlar ikkala konvensiyani oracle bilan qamraydi; to'g'risi F0 da haqiqiy narxlar bilan aniqlanadi.
- `python -m app.pipeline.refresh` yo'q (QA-T3-A1, yuqorida).

## Test natijalari

| | Oldin (`main`) | QA dan keyin |
|---|---|---|
| Testlar | 1438 passed, 4 xfailed | **1597 passed, 13 xfailed** (shundan 9 ta yangi strict xfail; 159 yangi test) |
| Qamrov | 98.91% | **98.98%** (9144 stmt, branch bilan) |
| `coverage_gate.py` | — | OK (`models` 99.73%, `pipeline` hali bo'sh: SKIP) |
| ruff / format / mypy src / lint-imports / deptry | — | ✅ `All checks passed!`, `202 files already formatted`, `no issues found in 101 source files`, `Contracts: 6 kept, 0 broken`, deptry `Success` |

Yangi fayllar (`tests/integration/`): `test_t3_data_client.py`, `test_t3_data_mapping.py`, `test_t3_data_matching_search.py`, `test_t3_data_repo.py`, `test_t3_data_budget.py` — 159 test (+ 9 strict xfail), ≈ 25 s (`slow` kerak emas). `conftest.py` ga `tests/clients` va `tests/db` fixture'lari qayta eksport qilindi.

**Barqarorlik:** yangi 5 fayl ketma-ket 3 marta (tarmoq yuklamasi tufayli sekin) 159 passed/9 xfailed; repo fayli alohida 5 marta; parallel-egallash va ikki-sessiya testlari deterministik (hodisa bilan navbatlanadi, `sleep` yo'q). 20 marta takror emas, 12 ga mo'ljallangan sikl mashina yuklamasi sababli to'xtatildi — ochiq aytiladi.

**Mutatsion o'ylash:** kvotadagi `asyncio.Lock` olib tashlansa, faqat SQL-do'konli parallel test yiqiladi (in-memory do'kon `await` da bo'shatmaydi) — shuning uchun ikkalasi ham bor. Zaxira formulasi `reserve_left` buzilsa aralash invariant testi yiqiladi.
