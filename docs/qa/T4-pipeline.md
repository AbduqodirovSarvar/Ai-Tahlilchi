# QA: T4 — pipeline, real servislar, rejalashtiruvchi (F4.1–F4.8)

- **Tekshiruvchi:** test-engineer (mustaqil QA)
- **Sana:** 2026-10-22
- **Branch:** `wave/T4-qa-pipeline` (`main` `c09f659` dan)
- **Tekshirilgan kod:** `src/app/pipeline/`, `src/app/services/real/`, `src/app/scheduler.py`, `python -m app.pipeline.refresh` ([ADR 0008](../adr/0008-pipeline-and-real-services.md), [shartnoma §3.1, §4–7](../contracts.md))
- **Muhit:** Windows 11, Python 3.13, `python -m uv run`, tarmoq yo'q (HTTP `respx` bilan, Gemini mock, vaqtinchalik SQLite). Ishlab chiqarish kodi (`src/`, `scripts/`) **o'zgartirilmadi**; xatolar `xfail(strict=True)` testlar bilan qotirilgan.

## Yakuniy xulosa

### Darvozadan o'tadi — 3 ta 🟠 shart bilan (🔴 yo'q)

Pul va ehtimollik hisobi mustaqil oracle bilan **tasdiqlandi**: qo'lda yozilgan Dixon-Coles ligasida (`scipy.stats.poisson` + darslikdagi tau tuzatishi) ansambl ehtimolligi, adolatli narx, EV, flat va ¼-Kelly (2% chegara bilan) stavka, shu jumladan chorak chiziqli Osiyo fora, kod bilan **to'liq mos keldi** (1e-9 aniqlikda). 18 ta natija turi (yarim yutuq/yutqazish, push, chorak total, AH ±0.25/±0.75/±1) va foydalanuvchi bankining `Decimal` yig'indisi aniq (`==`) mos keldi. Look-ahead yo'q (nazorat testi bilan), 1xBet narxi hech qachon almashtirilmaydi, foydalanuvchilar izolyatsiyasi buzilmagan (17 chaqiruv), vaqt mintaqalari va DST bo'yicha har kuni aynan bitta xabar.

Lekin F10 (paper trading) dan **oldin** tuzatilishi kerak bo'lgan uchta 🟠 bor:

1. **F-1** — to'xtatilgan (abandoned) o'yin qisman hisob bilan oddiy natija sifatida yopiladi (bukmekerlar bunday stavkani qaytaradi). Noto'g'ri foyda birligi.
2. **F-2** — jarayon 08:00 da o'chiq bo'lsa, `settle`/`refresh` keyingi ishga tushirishda **bajarilmaydi** (F4.7 "o'tkazib yuborilgan vazifalarni bajarish" talabi buzilgan): natijalar bir sutkaga kechikadi.
3. **F-3** — natija xabari yuborilmay qolsa (Telegram xatosi), keyingi ishga tushirishda **qayta urinilmaydi**: foydalanuvchi natijani hech qachon xabar sifatida olmaydi (hisobotda bor).

Qolganlari 🟡 (qayd etilgan, bloklamaydi).

## Mezonlar

| # | Mezon | Holat | Dalil |
|---|---|---|---|
| 1 | Contract suite **real** servislarda o'tadi | ✅ | `tests/services/test_real_contract.py` 62 test (fake: 62) — `pytest tests/services/test_real_contract.py tests/services/test_fake_contract.py` → 124 yig'ildi, hammasi yashil (to'liq to'plamda) |
| 2 | "Soxta kun" e2e raqamlari qo'lda tekshirilgan | ✅ | Mavjud `test_day_e2e` foydani servisning o'z koeffitsientidan qayta hisoblaydi; **qo'shildi** `test_t4_fake_day.py`: har signalning koeffitsienti provayder narx ro'yxatidagi 1xBet narxiga teng, `ev = p·odds − 1`, natijalar dunyoning yashirin hisobidan mustaqil oracle bilan, hisobot foydasi `Decimal` bilan teng |
| 3 | Idempotent qayta ishga tushirish | ✅ (⚠ F-3, F-4) | refresh 2-marta 0 so'rov; kunlik xabar: qayta tick'da yuborilmaydi, signal versiyasi 1 qoladi; ikkita bir vaqtdagi tick — 1 xabar; settle 2-marta 0 |
| 4 | Rejalashtiruvchi restartda joblarni saqlaydi | ✅ | 3 ketma-ket restart: 4 job, dublikat yo'q (`test_a_restart_never_duplicates_or_loses_a_job`) |
| 4b | …va o'tkazib yuborilgan jobni bajaradi | ❌ | **F-2**: kuzatuv testi (nazorat bilan) `xfail` |
| 5 | `python -m app.pipeline.refresh` chiqish kodlari 0/1/2, kalit yo'q xabari aniq | ✅ | haqiqiy subprocess: kalitsiz `exit=2`, xabar: `refresh_not_configured reason='API_FOOTBALL_KEY is not set; add it to .env (see .env.example)'`, `Traceback` yo'q; 0/1 kodlari mavjud testlar bilan (`test_misc`) |
| 6 | 10 liga yangilanishi ≤ 40 so'rov (haqiqiy pipeline yo'li, respx sanog'i) | ✅ (⚠ F-7, F-12) | `refresh()` haqiqiy `FootballProvider` + `ApiClient` + kvota ustida: **32 so'rov** = 3 (kunlar) + 1 (20 natija) + 8 (xG) + 20 (liga×kun koeffitsientlari); kvota hisobi 32 ga teng, `manual_used = 0`. Koeffitsientlar 2 sahifali bo'lsa 52 (> 40), lekin avtomatik kvota (70) ichida, `manual_used = 0` |
| 7 | (1) qo'lda hisoblangan liga: p, adolatli narx, EV, stavka | ✅ | `test_t4_hand_league.py` 23 test (shundan 2 xfail: F-6) |
| 8 | (2) barcha natija turlari, bank, CLV | ✅ (❌ F-1, ⚠ F-5, F-9, F-11) | `test_t4_settlement.py` 18 test (shundan 3 xfail: F-1, F-5, F-9) |
| 9 | (3) look-ahead | ✅ | `test_t4_resilience.py` (4 test, nazorat bilan) |
| 10 | (4) 1xBet narxi yo'q → almashtirilmaydi | ✅ (⚠ F-10) | 4 test |
| 11 | (5) qulash in'ektsiyasi | ✅ (❌ F-3, F-4) | 7 test (shundan 2 xfail) |
| 12 | (6) bir vaqtdalik | ✅ | `BusyError`, cooldown 9:59/10:00, 2 tick, lease |
| 13 | (7) vaqt mintaqalari, tinch soatlar | ✅ | `test_t4_time_zones.py` 20 test |
| 14 | (8) foydalanuvchi izolyatsiyasi | ✅ | `test_t4_isolation.py` 17 test |
| 15 | (9) byudjet / uzilish | ✅ | `test_t4_acceptance.py` |
| 16 | (10) LLM | ✅ | `test_t4_resilience.py` |
| 17 | (11) hisobotlar, sevimlilar, `scope=all` | ✅ | `test_t4_reports.py` 3 test |

### Tafsilotlar

**(1) Qo'lda hisoblangan liga.** Ikki jamoa, qat'iy reytinglar (`intercept 0.1`, uy ustunligi `0.25`, hujum/mudofaa `0.30/0.05` va `−0.20/0.10`, `rho −0.06`): λ = e^0.55 = 1.7333, μ = e^−0.15 = 0.8607; DC: uy 57.38%, durang 24.87%, mehmon 17.75% (qo'lda bir marta tekshirilgan). Pinnacle marjasiz 2.0/4.0/4.0 (adolatli ehtimollik de-vig usulidan qat'i nazar 50/25/25%), ansambl `0.5·DC + 0.5·bozor`: uy 53.688%, durang 24.936%, mehmon 21.376%. 1xBet 2.10: EV = +12.75%, adolatli narx 1.8626; to'liq Kelly 11.59%, ¼ = 2.90% → **2% chegara** → 20.00 → **2.0 birlik**. 1xBet 1.95: EV +4.69%, ¼-Kelly 1.2347% → 12.34 → **1.2 birlik** (chegara ishlamaydi). Chorak chiziqli AH −0.25 (uy): yutuq 57.38% / yarim yutqazish 24.87% / yutqazish 17.75%, narx 1.95 → EV +24.32%, adolatli narx 1.5261, stavka 2.0 (chegara); narx 1.58 → EV +3.09%, to'liq Kelly `minimize_scalar` bilan 7.012%, ¼ = 1.753% → 17.53 → **1.7 birlik**. Flat rejimida musbat EV ga 1 birlik, manfiyga 0. Model faqat `as_of = ertaga` (eksklyuziv) bilan so'raladi. Ehtimolligi 30% dan past tanlov (durang 24.9%) ko'rsatiladi, lekin hech qachon stavka qilinmaydi (`no_value`).

**(2) Natijalar.** 18 holat: 1X2, ikkilangan imkoniyat, BTTS, total 2.0/2.25/2.5/2.75, AH 0/−0.25/−0.75/−1.0/−1.5/+0.5 (uy va mehmon), natijalar `win/half_win/void(push)/half_loss/loss` — oracle har bir chiziqni ikki yarim stavkaga bo'ladi, `settle_selection` ni chaqirmaydi; shu oracle `hypothesis` bilan 400 tasodifiy (chiziq, hisob) uchun `settle_selection` bilan solishtirildi. Signal foydasi va foydalanuvchining o'z koeffitsienti/summasi bilan stavkasi `stake · (Decimal(str(odds)) − 1) · ulush` ga **aniq** teng; bank balansi `1000 + Σ stavka foydasi` ga aniq teng; ikkinchi `run_settlement` hech narsani o'zgartirmaydi. Kechiktirilgan (postponed) → kutadi, bekor qilingan → void/0, ball bilan to'xtatilgan → ❌ F-1. CLV: so'nggi `prematch` Pinnacle snapshot'i erta snapshot'dan ustun (ln(2.0·0.625) = 0.2231, erta bo'lsa 0), `closing` `prematch` dan ustun, yomon narxda manfiy, Pinnacle yo'q bo'lsa `None`. AET/PEN: haqiqiy `FootballProvider` + respx orqali, `fulltime 1–1 / goals 2–1` (va `0–0 / 2–2`) → uy yutqazadi, durang yutadi, total 2.5 OVER yutqazadi.

**(3) Look-ahead.** Ertangi kunga (va undan keyingi) tegishli 12 ta "yashirin" natija qo'shildi, hamda kickoff paytidan va undan keyingi soatda olingan absurd narxlar: oldingi signal raqamlari (koeffitsient, ehtimollik, EV, ishonch) **aynan** bir xil. Nazorat: bugun ertalabki natijalar va kickoff'dan oldingi narx signalni **o'zgartiradi** (test "tishli").

**(4) 1xBet narxi.** Faqat Pinnacle narxi bo'lsa — signal yo'q (hatto `include_no_value` bilan ham), nomukammal 1xBet to'plami Pinnacle bilan to'ldirilmaydi, ikkala bukmeker bo'lsa har bir signal 1xBet narxida va Pinnacle narxidan farq qiladi, signal yaratilgandan keyin 1xBet narxi yo'qolsa qayta hisoblash **eski 1xBet narxini** saqlaydi (hukm `below_threshold`), Pinnacle'nikini hech qachon olmaydi. Foydalanuvchiga ko'rinadigan belgi yo'q → F-10.

**(5) Qulash.** Kunlik xabar: yuborish xato bersa signallar saqlangan, qayta urinish 1 marta yetkazadi, versiya 1; jarayon xabar yuborilgandan keyin "o'ldirilsa" (BaseException) — lease (15 daq) ichida qayta yuborilmaydi, undan keyin **bir xil `DeliveryKey`** bilan qayta yuboriladi (kamida bir marta; notifier kalit bo'yicha takrorni tashlay oladi — shartnomaga muvofiq). Pre-match: xato → vazifa bo'shatiladi → qayta urinishda 1 xabar. 24 soat: ❌ F-4. Natija: ❌ F-3 (lekin ikki marta yopilmaydi va natija yo'qolmaydi).

**(6) Bir vaqtdalik.** Bir foydalanuvchining ikkita bir vaqtdagi `analyze` → aynan bitta `BusyError`, qulf ochiladi; boshqa foydalanuvchilar bir-birini to'smaydi; xatodan keyin qulf bo'shaydi. Cooldown: 9:59 → `CooldownActiveError(retry_after=1s)`, 10:00 → ruxsat. Bir vaqtdagi ikkita `recalc_signal` → bittasi cooldown oladi (6 ta ishga tushirishda barqaror). Ikkita parallel `run_due_recalcs` → har vazifa aynan bir marta (2 vazifa, 1 xabar); o'lgan ishchining egallashi 14 daq da qayta olinmaydi, 16 daq da bir marta olinadi; ikkita parallel kunlik tick → 1 xabar, 1 signal.

**(7) Vaqt mintaqalari.** Yarim soatlik tick'larda har kuni aynan bitta xabar, kutilgan UTC lahzalari qo'lda yozilgan: Toshkent 04:00 UTC; Nyu-York 13:00 → 14:00 UTC (2026-11-01 DST tugashi) va 14:00 → 13:00 UTC (2027-03-14); Oklend 20:00 UTC (kun chegarasi: mahalliy 09:00 = oldingi UTC kun) va 21:00 UTC (2027-04-04 DST tugashi); mavjud bo'lmagan 02:30 (2027-03-14) — o'sha kuni bir marta (03:30 EDT); ikki marta bo'ladigan 01:30 (2026-11-01) — bir marta. Tinch soatlar (qaror #15): 22:00–08:00 (yarim tundan o'tadi) va 07:00 yuborish vaqti → 08:00 da (oxiri istisno) bitta xabar, 07:00–07:55 "held"; kechikish 11 va 12 soat → yuboriladi, 13 soat → o'tkazib yuboriladi (`daily_catch_up = 12h`). Pre-match 19:00 da 6 xil oyna: `22:00–08:00` yuboradi, `19:00–03:00` (boshi kiritilgan) tashlaydi, `08:00–19:00` (oxiri istisno) yuboradi, `20:00–19:30` tashlaydi, `18:59–19:01` tashlaydi, oynasiz yuboradi; **har holda qayta hisob bajariladi** (koeffitsient 2.30, versiya 2). Tinch soatda kelgan natija saqlanadi, xabar tashlanadi (qaror #15); admin ogohlantirishi tinch soatda ham ketadi. `QuietWindow.contains` ga `hypothesis` oracle (daqiqa-sutka) qo'shildi.

**(8) Izolyatsiya.** B foydalanuvchi A ning identifikatorlari bilan 10 ta metodni chaqiradi (`get_signal`, `hide_signal`, `recalc_signal`, `recalc_query`, `place_bet` (token bilan va tokensiz), `mark_result`, `add_note`, `delete_template`, `set_notifications`) → hammasi `NotFoundError`; `get_signals`, `recalc_signals`, `remove_watch` → jim o'tkazib yuboriladi; ro'yxatlar, hisobot, eksport, bank, sozlamalar, kun yakuni faqat B ning o'z qatorlarini beradi; har safar A ning to'liq holati (signal, ro'yxat, stavkalar, bank, shablonlar, sevimlilar, sozlamalar, hisobot, eksport) oldin/keyin **bir xil**. Begona `recalc_signal` egasining cooldown'ini yoqmaydi; bir xil `client_token` ikki foydalanuvchida ikki mustaqil stavka beradi; begona va mavjud bo'lmagan id xabari bir xil.

**(9) Byudjet va uzilish.** Avtomatik kvota (`daily_limit=20` → 14) o'rtada tugasa: kunlar (60 o'yin) va natijalar (20) saqlanadi, faqat koeffitsientlar qadami o'tkazib yuboriladi (`skipped=['odds']`, 14 so'rov sarflangan) va **yarim yozilmaydi** (0 narx), `manual_used = 0`, qo'lda zaxira (6) butun, `/holat` da `refresh` ❌ va `skipped=` tafsiloti. Zaxiradan ko'p so'ragan qo'lda tahlil `BudgetExceededError(needed > available)` va **0 so'rov** sarflaydi. Provayder 503 → qo'lda tahlil `ServiceUnavailableError(component="api-football")`; qayta hisoblash raqamlarni o'zgartirmaydi va `data_as_of` = ishlatilgan narx vaqti. API kaliti loglarda, ledger'da va `/holat` da yo'q.

**(10) LLM.** Yo'q / `LlmService(None)` / ichki xato bergan backend / istisno / sekin (timeout) → signallar raqamma-raqam bir xil, verdiktsiz. `rad`: tavsiya olib tashlanadi (oddiy tahlilda yo'q, maxsus tahlilda `no_value`), lekin koeffitsient/ehtimollik/EV o'zgarmaydi; `ehtiyot`: tavsiya qoladi, raqamlar bir xil, verdikt saqlanadi.

**(11) Hisobotlar.** Hisobot/eksport faqat tavsiya qilingan signallarni sanaydi (to'plam = tavsiya qilingan har qanday ishga tushirishdan), retro va `no_value` hech qachon; yashirilganlar ham sanaladi (hisobot o'zgarmaydi, ro'yxatdan yo'qoladi, `include_hidden=True` da qaytadi); retro `day_summary`/`pending_results` ga kirmaydi. Sevimli o'yin `max_signals` kesimidan tashqarida qo'shiladi va `matched` ga kirmaydi; `scope=all` sozlamadagi ligalar bilan cheklanadi, sevimli jamoa o'sha ligadan tashqarida bo'lsa ham qo'shiladi, aniq `scope=leagues` ustun.

## Natijalar

Darvoza (to'liq, `main` + 132 yangi test), 2026-10-22:

| Buyruq | Natija |
|---|---|
| `uv run pytest --cov=app` | **2508 passed, 10 xfailed** (strict), 380 s; umumiy qamrov **98%** (baza: 2386 passed, 98.23%) |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 301 files already formatted |
| `uv run mypy src` | Success: no issues found in 146 source files |

Qamrov (branch bilan): `app/pipeline` **96%** (2026 satr/55 o'tkazib yuborilgan; talab ≥ 90%), `app/services/real` **94%**, `app/scheduler.py` 99%. Eng past: `settle.py` 91% (qo'lda natija/cashout tarmoqlari, 253–293), `real/notifier.py` 86% (`LogOnlyNotifier` log tarmoqlari), `real/account.py` 92% (shablon jadvali dekodlash). Yangi testlar `prematch.py` qamrovini 86% → 95%, `settle.py` 87% → 91% ga oshirdi.

Barqarorlik: bir vaqtdalik/lease testlari 8 marta ketma-ket (CPU yuki ostida) o'tdi; vaqtga bog'liq yagona testlar (rejalashtiruvchi) 3 s / 10 s kutish bilan.

## Topilmalar

### 🟠 F-1. To'xtatilgan (abandoned) o'yin qisman hisob bilan oddiy natija sifatida yopiladi
- **Joy:** `src/app/pipeline/settle.py:94-102` (`settlement_of` holatga qaramaydi) va `:184-196` (`_settle_one` faqat hisob **yo'q** bo'lganda void qiladi).
- **Takrorlash:** `FixtureResult(status=ABANDONED, home_score=2, away_score=0)` + `1X2 HOME @2.0, 1 birlik` → `outcome = WIN`, foyda `+1.0` (kutiladi: `VOID`, `0`). `map_result` shunday natija hosil qiladi (`ABD` → `goals` zaxirasi). Modul docstringi va ADR 0008 §6: "bekor/to'xtatilgan — void".
- **Test:** `tests/integration/test_t4_settlement.py::test_an_abandoned_match_with_a_partial_score_is_void` (xfail). Ballsiz `ABANDONED`, `CANCELLED` — void (✅).
- **Tuzatish:** `settlement_of` va `_settle_one` da avval `fixture.status in _VOIDING` tekshirilsin (hisobdan oldin).

### 🟠 F-2. Jarayon o'chiq bo'lganda o'tkazib yuborilgan `settle`/`refresh` bajarilmaydi
- **Joy:** `src/app/scheduler.py:151-157` (`replace_existing=True` har startda `next_run_time` ni qayta hisoblaydi), `:14-16` (docstring "misfire_grace_time … missed during downtime run when the process is back").
- **Takrorlash:** jadval saqlanadi, `settle` ning `next_run_time` i 1 soat o'tgan qilinadi, jarayon qayta ishga tushadi → job **ishlamaydi** (5 s kutildi). Nazorat: xuddi shu saqlangan jobni qayta ro'yxatdan o'tkazmasdan yuklasa, APScheduler uni **ishga tushiradi** (test o'tadi). Natija: 08:00 da o'chiq bo'lsa natijalar keyingi kun 08:00 gacha yopilmaydi (kunlik xabar `daily-tick` + `daily_catch_up` bilan to'g'ri, `refresh` ni esa tahlilning o'z yuklashi qoplaydi, lekin `settle` ni hech narsa qoplamaydi).
- **Test:** `tests/integration/test_t4_scheduler.py::test_a_daily_job_missed_while_down_runs_after_the_restart[settle|refresh]` (xfail, `slow`).
- **Tuzatish:** mavjud jobni almashtirmang (`replace_existing=False` + `ConflictingIdError` ushlash yoki faqat trigger o'zgarganda `reschedule_job`), yoki startda `settle`/`refresh` ni, agar oxirgi muvaffaqiyatli ishga tushish > 1 sutka bo'lsa, bir marta ishga tushiring (ledger `last_runs` bor).

### 🟠 F-3. Natija xabari yuborilmasa, qayta urinilmaydi (foydalanuvchi hech qachon bilmaydi)
- **Joy:** `src/app/pipeline/settle.py:148-154` (`r.result is None` filtri, natija allaqachon yozilgan) va `:284-296` (`_announce` xatoni `raise` qiladi → butun `run_settlement` to'xtaydi).
- **Takrorlash:** 2 ta signal, `result_settled` ning 1-chaqiruvi `ConnectionError` → `run_settlement` istisno bilan tugaydi (2-signal shu ishga tushirishda yopilmaydi); 2-ishga tushirish 2-signalni yopadi va xabar beradi, **1-signal uchun xabar hech qachon yuborilmaydi** (jami 1 xabar, kutilgan 2). Natijalar to'g'ri saqlanadi, ikki marta yopilmaydi (✅ alohida test).
- **Test:** `tests/integration/test_t4_resilience.py::test_a_failed_result_message_is_not_lost` (xfail).
- **Tuzatish:** natijani xabarsiz ham yopish (xatoni ushlab `fail` qoldirish, davom etish) va xabarni alohida o'tishda (`result` bor, lekin `settled` kaliti `done` emas) qayta urinish.

### 🟡 F-4. 24 soatlik yangilanish xabari yuborilmasa yo'qoladi
- **Joy:** `src/app/pipeline/prematch.py:79-95, 117-122` — qayta hisob yangi raqamlarni saqlaydi, keyin xabar xato beradi, vazifa bo'shatiladi; qayta urinishda "oldin" allaqachon yangi, o'zgarish (≥ 10%) ko'rinmaydi, xabar yuborilmaydi.
- **Test:** `test_a_failed_24h_message_is_not_lost` (xfail); pre-match uchun (har doim yuboriladi) qayta urinish ✅.
- **Tuzatish:** o'zgarishni oxirgi **xabar qilingan** raqamlarga nisbatan hisoblash (masalan, ledger'da xabar bergan koeffitsientni saqlash).

### 🟡 F-5. CLV "yopilish"i kickoff'dan keyingi snapshot bo'lishi mumkin
- **Joy:** `src/app/pipeline/settle.py:121` — `complete_quotes(snapshots)` kickoff xaritasisiz chaqiriladi (shartnoma 6: "pipeline doim beradi", QA F-3 tuzatishi).
- **Takrorlash:** 09:30 da adolatli, 11:00 (kickoff 10:00 dan keyin) da absurd `prematch` Pinnacle snapshot → CLV 0.6176 (kutiladi 0.2231).
- **Test:** `test_a_price_captured_after_kickoff_is_never_the_close` (xfail). Real xavf past (kickoff'dan keyin `prematch` olinmaydi: `_live` tekshiradi), lekin himoya yo'q.

### 🟡 F-6. `SignalView.fair_odds` chorak chiziqli Osiyo uchun nol-EV narx emas
- **Joy:** `src/app/services/interfaces.py:741-743` (`1 / probability`; `probability` yarim yutuqni hisoblaydi).
- **Takrorlash:** AH −0.25 (uy): `fair_odds = 1.743`, haqiqiy nol-EV narx `1.5261`. Bot buni "adolatli koeffitsient" deb ko'rsatsa noto'g'ri.
- **Test:** `test_fair_price_of_a_quarter_line_is_the_zero_ev_price` (xfail). Pul hisobiga ta'sir yo'q (EV to'g'ri).

### 🟡 F-7. `PipelineConfig.xg_per_refresh = 10` ishlamaydi (haqiqatda 8)
- **Joy:** `src/app/pipeline/ingest.py:140, 157` → `provider.py:192-195` `with_expected_goals(results)` `max_requests` siz (standart 8).
- **Test:** `test_the_configured_xg_budget_is_the_one_spent` (xfail). Byudjetga zararsiz (kamroq), lekin sozlama aldaydi.

### 🟡 F-8. Qadamlari o'tkazib yuborilgan refresh kun uchun "bajarildi" deb belgilanadi
- **Joy:** `src/app/pipeline/refresh.py:82` (`complete` `summary.skipped` dan qat'i nazar). 08:30 da provayder ishlamasa, keyingi `refresh()` `already_done`; faqat `force` yoki tahlilning o'z yuklashi tuzatadi. Hujjatlashtirilgan xatti-harakat sifatida testda qotirilgan (`test_a_refresh_that_lost_its_provider_is_marked_done_for_the_day_and_not_retried`). `/holat` da ❌ ko'rinadi. Tavsiya: qisman natijani `fail` qilib, keyingi tick'da qayta urinish.

### 🟡 F-9. `profit_of` float shovqinini Decimal'ga olib kiradi
- **Joy:** `src/app/pipeline/settle.py:108-110` (`Decimal(repr(odds − 1.0))`). Koeffitsient 1.01 → `0.0100000000000000090`. 4 xonali ustun (`MONEY_SCALE = 4`) buni yashiradi (18 holatdagi aniq tenglik o'tadi), faqat aniq yarim-xona tengliklarida 1 kvant (0.0001) farq beradi (`hypothesis`: `stake=0.01, odds=1.01, half_win`).
- **Test:** `test_profit_of_is_exact_for_two_decimal_odds` (xfail), `test_stored_profit_is_the_exact_decimal_within_one_quantum` (✅). Tuzatish: `Decimal(str(odds)) − 1`.

### 🟡 F-10. 1xBet narxi yo'q o'yin jim tushib qoladi (belgi yo'q)
- **Joy:** `src/app/pipeline/valuation.py:126-128` (`SKIP_NO_PRICE`), `analyze.py:253` (faqat `debug` log). Qaror #10: "signal 'narx yo'q' deb belgilanadi"; ADR 0008 §1: "tanlov chiqmaydi". Almashtirilmaydi (✅), lekin foydalanuvchi "nega bu o'yin yo'q" ni bilmaydi va `AnalysisResult` da skip hisobi yo'q (`fixtures_analyzed` ularni ham sanaydi). Tavsiya: `AnalysisResult` ga `skipped_no_price` hisobi yoki `/holat` da.

### 🟡 F-11. `results.closing_odds` qaysi bukmekerniki ekani tasodifiy
- **Joy:** `src/app/pipeline/settle.py:124` (`own[0]` — `complete_quotes` tartibidagi birinchi bukmeker, alfavit bo'yicha `1xbet` < `pinnacle`). Ikkala bukmeker bo'lsa `closing_odds = 1.5` (1xBet), CLV esa Pinnacle ning de-vig'langan narxiga nisbatan. Faqat Pinnacle bo'lsa — Pinnacle narxi. Maydon ma'nosi aniq emas; tavsiya: har doim CLV hisoblangan manba narxi (Pinnacle), yoki ikkalasini alohida saqlash.

### 🟡 F-12. Haqiqiy koeffitsientlar sahifalansa refresh 40 so'rovdan oshadi
- Reja §9 byudjeti (≤ 40) 1 sahifali koeffitsientni nazarda tutadi: har liga×kun 2 sahifa bo'lsa 52 so'rov (`test_busy_leagues_with_paged_odds_...`). Kvota nazorati (avtomatik 70) uni baribir cheklaydi, qadam o'tkazib yuboriladi. Haqiqiy ligalarda (≤ 10 o'yin/kun) odatda 1 sahifa; jonli kalit bilan F10 da o'lchang.

## Qo'shilgan testlar

| Fayl | Nima | Jami test (shundan xfail) |
|---|---|---|
| `tests/integration/test_t4_hand_league.py` | (1) qo'lda hisoblangan liga, flat va ¼-Kelly | 23 (2) |
| `tests/integration/test_t4_settlement.py` | (2) natijalar, bank, CLV, hypothesis | 18 (3) |
| `tests/integration/test_t4_resilience.py` | (3)(4)(5)(6)(10), uzilishda eskirgan belgi | 32 (2) |
| `tests/integration/test_t4_time_zones.py` | (7) mintaqalar, DST, tinch soatlar | 20 |
| `tests/integration/test_t4_isolation.py` | (8) izolyatsiya | 17 |
| `tests/integration/test_t4_reports.py` | (11) | 3 |
| `tests/integration/test_t4_acceptance.py` | so'rovlar soni, byudjet, uzilish, CLI, AET/PEN, kalit gigienasi | 13 (1) |
| `tests/integration/test_t4_scheduler.py` | restart, o'tkazib yuborilgan job | 5 (2, `slow`) |
| `tests/integration/test_t4_fake_day.py` | "soxta kun" mustaqil narx/natija tekshiruvi | 1 |
| `tests/integration/t4_support.py`, `t4_api.py` | quruvchilar, respx API-Football | — |

Sekin testlar (`@pytest.mark.slow`): rejalashtiruvchining o'tkazib yuborilgan joblari (2 × 5 s) va vaqt mintaqasi senariylari (yarim soatlik tick'lar, ~1 s har biri).

## Xavflar / eslatmalar

- Test kodi `src/` ga tegmadi. Barcha xatolar `xfail(strict=True)`: tuzatilganda XPASS bo'lib yiqiladi, belgini olib tashlash kerak.
- Cooldown (`_claim_cooldown`) "o'qi → yoz" ikki bosqichli; ikkita bir vaqtdagi `/qayta` bitta jarayonda (aiosqlite) takrorlanmadi (6 marta barqaror), lekin kod atomik emas. Bitta jarayon bo'lgani uchun xavf past.
- Osiyo fora uchun Pinnacle anchor'li ansambl (Pinnacle ikki yoqlama narxidan Osiyo natijalari taqsimoti) oracle bilan tekshirilmadi: oracle "faqat model" yo'lini (maxsus tahlil, anchor yo'q) qamraydi; anchor'li yo'l `app.models` T3 QA tomonidan tekshirilgan.
- Haqiqiy API kaliti bilan sinov (F0.7) kechiktirilgan (qaror #1): so'rovlar soni, sahifalash va `AWD/WO` holatlari jonli ma'lumotda tasdiqlanmagan.
- `closing` snapshot'ni provayder bermaydi (ADR 0008 §6); CLV "so'nggi pre-match" ga nisbatan — haqiqiy yopilishdan biroz yuqori baholangan bo'lishi mumkin.
