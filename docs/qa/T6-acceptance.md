# QA hisoboti: T6 — yakuniy qabul tekshiruvi (F1–F9, asosiy reja §6.1–6.14)

- **Tekshiruvchi:** test-engineer (mustaqil QA; parallel ishlagan ikki sharhlovchidan alohida)
- **Sana:** 2026-10-23
- **Branch:** `wave/T6-qa` (`main` `a888baa` dan)
- **Muhit:** Windows 11, Python 3.13, `python -m uv run --no-config`, `UV_SYSTEM_CERTS=1`. Docker 29.6.1 (korporativ CA `CA_BUNDLE_FILE` bilan). Tarmoq: Telegram korporativ firewall tomonidan bloklangan (HTML "blok sahifa" qaytaradi), football-data.co.uk bloklangan, API kalitlari yo'q. Haqiqiy tashqi API ga **hech qanday** murojaat qilinmadi.
- **Qoida:** ishlab chiqarish kodi (`src/`, `scripts/`) **o'zgartirilmadi**. Topilgan xatolar `xfail(strict=True)` testlar bilan qotirilgan (tuzatilganda XPASS bo'lib yiqiladi).
- **Oldingi hisobotlarga ishonilmadi:** ularda ✅ deyilgan joylarning ko'pi qayta ishga tushirildi; mos kelgan va kelmagan joylar quyida.

## 1. Yakuniy xulosa

### Birinchi **bir foydalanuvchili paper-trading** relizi uchun: ✅ DARVOZADAN O'TADI (shartli)

🔴 topilma **yo'q**. Pul hisobi (bank, foyda, hisobot, CSV), natijalarni hisoblash (AET, chorak chiziqli yarim yutuq/yarim yutqazish, qo'lda belgi ustuvorligi), foydalanuvchilar izolyatsiyasi, takroriy ishga tushirish xavfsizligi va tizim qulashidan tiklanish **mustaqil qo'lda hisob** va 188 ta o'ldirish nuqtasida tekshirildi.

| Soha | Xulosa | Asos |
|---|---|---|
| Pul, bank, hisobot | ✅ | 1 haftalik senariy: har bir stavka foydasi, bank, hisobot birliklari, "Kecha" bloki `Fraction` asosidagi mustaqil oracle bilan **aniq** mos (§4.1) |
| Natijalarni hisoblash | ✅ | AET (90 daqiqa), `AH −0.75` yarim yutuq, `AH −0.25` yarim yutqazish, qo'lda belgi API dan ustun, qayta ishga tushirishda o'zgarmaydi |
| Pipeline va rejalashtiruvchi | ✅ | 188/188 o'ldirish nuqtasida signal yo'qolmadi va takrorlanmadi; 145 ta aynan bir marta, 43 tasi hujjatlashtirilgan "kamida bir marta" |
| Bot (haqiqiy dispatcher + haqiqiy notifier) | ✅ | hafta senariysi, 2 foydalanuvchi izolyatsiyasi, restart, eskirgan tugmalar, 263 ta robustlik testi |
| Model o'rganish halqasi | ✅ | `off` = `shadow` (bit-ma-bit bir xil tavsiyalar); `apply` aynan "o'lik" segmentni o'chiradi |
| Zaxira / migratsiya / healthcheck | ✅ | tiklash mashqi baytma-bayt bir xil raqamlar; 2 revizion pastga-yuqoriga ma'lumot yo'qotmasdan |
| Samaradorlik va xotira | ✅ | 30 nomzod 0.85 s (sovuq), 5 000 qatorli eksport 0.54 s, 200 kunda xotira o'sishi 0.8 MB |
| Tashqi xizmatlar bilan haqiqiy ishlash | 🔒 | Telegram, API-Football, Gemini, football-data, VPS: bu mashinada tekshirib bo'lmaydi |
| **F3 darvozasi (model bozordan yaxshimi)** | 🔒 / kutilmoqda | haqiqiy tarixiy ma'lumot yo'q (qaror #7). Bu relizning **asosiy** noma'lumligi |

**Shartlar (paper-trading boshlanishidan oldin, birinchi navbatda):**
1. **F3 darvozasini serverda bajarish** (`import_history.py` + `backtest.py`). O'tmasa, F10 boshlanmaydi (qaror #7).
2. **T6-F-1** (🟠): bitta rad etilgan karta qolgan kartalarni yutib yuboradi. Kichik tuzatish, lekin rad etilgan kartadan keyingilari (eng yomon holda 10 tadan 9 tasi) jim yo'qoladi.
3. Jonli Telegram qo'lda sinovi (`docs/test_scenarios.md`): token kelgach.

## 2. Qabul matritsasi: F0–F11

Belgilar: ✅ tasdiqlandi · ⚠️ qisman / shart bilan · ❌ bajarilmagan · 🔒 tekshirib bo'lmaydi (kalit / server / Telegram kerak).

| # | Mezon (reja) | Holat | Dalil |
|---|---|---|---|
| F0.7 | `probe_api.py` kalitsiz aniq xabar bilan chiqadi, hech narsa so'ramaydi | ✅ | `python scripts/probe_api.py` → `probe_not_run … Nothing was requested.`, **exit 2** |
| F0 | Joriy mavsum va koeffitsientlar bepul tarifda ochiqmi (darvoza) | 🔒 | API kaliti yo'q (qaror #1); skript tayyor va testlangan (`tests/clients/test_probe_script.py`) |
| F1 | `docker compose up` bilan bot ishga tushadi | ⚠️ | `docker compose -p t6qa -f docker-compose.yml -f docker-compose.proxy.yml build` → **exit 0**. `run`: `alembic upgrade head` 0001→**0009** bajarildi (`migrations_done`), tokensiz `startup_failed … TELEGRAM_BOT_TOKEN is not set`, **exit 2** (entrypoint 1 s kutdi). Soxta token bilan Telegram ulanishi 🔒 (firewall HTML qaytardi → `ClientDecodeError`, exit 1: T6-F-5) |
| F1 | Konteyner healthcheck | ✅ | konteyner ichida `python -m app.ops.healthcheck` → `{"healthy": true, config/database/disk ok}` |
| F1 | Bot `/start` ga javob beradi | 🔒 / ✅ offlayn | jonli: token + Telegram kerak. Offlayn: haqiqiy dispatcher orqali `/start`, whitelist, shaxsiy chat (`tests/integration/test_whitelist.py`, `tests/e2e/test_bot_access_e2e.py`, T6 `test_6_12_a_stranger_gets_no_answer_at_all`) |
| F1 | CI yashil | 🔒 / ✅ lokal | GitHub Actions bu yerda ishlamaydi. Lokal ekvivalent: §6 (pytest, ruff, format, mypy, lint-imports, deptry). `pip-audit` lokal 🔒 (korporativ CA Python 3.13 qat'iy tekshiruvidan o'tmaydi), `gitleaks` 🔒 (image yo'q); `tests/integration/test_secret_hygiene.py` ✅ |
| F2 | `python -m app.pipeline.refresh` tanlangan ligalarni bazaga yozadi | ✅ | haqiqiy `FootballProvider` + `ApiClient` + respx: `fixtures=60 results=20 odds=240` (`tests/integration/test_t4_acceptance.py::test_a_daily_refresh_of_ten_leagues_stays_within_forty_requests`). CLI kalitsiz: `refresh_not_configured`, **exit 2**, traceback yo'q |
| F2 | Bitta to'liq yangilash ≤ 40 so'rov | ⚠️ ✅ | respx sanog'i: **34** so'rov = 3 (kunlar) + 1 (20 natija) + 10 (xG) + 20 (liga×kun koeffitsienti); kvota hisobi 34, `manual_used = 0`. ⚠️ Har bir liga-kun koeffitsientlari **2 sahifali** bo'lsa 54 (> 40), lekin avtomatik kvota (70) ichida (`test_busy_leagues_with_paged_odds…`) |
| F2 | Alembic: upgrade → downgrade → upgrade, **ma'lumot bilan** | ✅ | `tests/integration/test_t6_acceptance.py::test_two_revisions_down_and_up_again_loses_no_signal_bet_or_result`: 1 haftalik bazada `0009 → 0006 → head`; fixtures/odds/signals/versions/results/user_bets/users soni va `sum(profit)` o'zgarmadi; `alembic check` toza. To'liq `base` aylanishi: `tests/db/test_migrations.py` (+ ma'lumotli variant) ✅ |
| F2 | Testlar o'tadi | ✅ | §6 (4138 passed) |
| F3.1–F3.9 | Model, EV, staking, ishonch, walk-forward backtest vositasi | ✅ | `tests/models/**` (qamrov §6); sintetik backtest **qayta ishlab chiqiladi**: `scripts/backtest.py --synthetic` ikki marta → hisobotlar va PNG baytma-bayt bir xil, ma'lumot izi SHA-256 `64a643c1…c5c9` = `docs/backtest/2026-10-21-synthetic.md` dagi (46 s) |
| F3 darvoza | Kalibratsiya ≤ 5 p.p.; ≥ 500 signal; **CLV 95% CI > 0**; ROI CI ≥ −2%; ≥ 3 liga CLV > 0; bozor modeliga nisbatan | 🔒 | haqiqiy football-data tarixi yo'q (qaror #7). Sintetik (samarali) bozorda `w = 0` tanlandi, stavka yo'q, CLV n/a: kutilgan sanity natijasi. **Darvoza hali bajarilmagan** |
| F4 | Tizim 3 kun lokal o'zi ishlaydi, signal va natijalar bazada to'g'ri | ✅ (simulyatsiya) / 🔒 real vaqt | 1 haftalik senariy (§4.1) va 200 kunlik sikl (§5) injeksiyalangan soat bilan; real 3 kunlik devor-soat ishga tushirish bajarilmadi |
| F4.7 | Rejalashtiruvchi restartdan keyin saqlanadi, takrorlanmaydi | ✅ | `tests/integration/test_t4_scheduler.py`, `tests/pipeline/test_scheduler*.py` (304 ta test bilan birga qayta ishga tushirildi, hammasi yashil) |
| F4 | Idempotentlik (qulash/qayta ishga tushirish) | ✅ | o'ldirish mashqi: 188/188 (§4.3) |
| F5 | 20 nomzodli tahlil ≤ 15 s | ⚠️ | mock backend bilan ✅ (`tests/llm/test_golden.py::test_twenty_candidates_of_review_finish_well_under_fifteen_seconds`; LLMsiz pipeline 30 nomzodni 0.85 s da tahlil qiladi, §5). ⚠️ Haqiqiy Gemini kechikishi o'lchanmagan; kodda sharh vaqt chegarasi **30 s** va hamma paketlarga umumiy (T6-F-6) |
| F5 | Gemini o'chiq bo'lganda ham signal yuboriladi | ✅ | `test_gemini_down_the_signals_still_arrive_without_notes` (10 ta kartaning hammasi, izohsiz, ish xatosiz); LLM kunlik limiti tugaganda ham (`test_llm_budget_exhausted_midweek…`) |
| F6 | §6.2, 6.4, 6.5, 6.6, 6.12 oqimlari | ✅ avtomat / 🔒 qo'lda | §3 jadvali; `docs/test_scenarios.md` 1–10-bo'limlar jonli Telegramda bajarilmagan |
| F7 | Barcha menyu bo'limlari ishlaydi | ✅ avtomat / 🔒 qo'lda | `tests/e2e/test_f7*_e2e.py`, `test_qa_t5_*`; 11–19-bo'limlar jonli bajarilmagan |
| F8 | Sun'iy natijalar bilan kalibratsiya va segment filtri to'g'ri; kichik namunada hech narsa o'zgarmaydi | ✅ | `tests/e2e/test_t6_learning_e2e.py`: `off` va `shadow` da tavsiyalar bit-ma-bit bir xil; `apply` da faqat "o'lik" segment (liga × bozor × koeffitsient oralig'i) o'chadi; kichik namuna: `tests/pipeline/test_learning*.py` (T5) |
| F9 | Zaxira va **tiklash sinovi** | ✅ | `test_a_backup_taken_while_the_bot_runs_restores_to_the_same_numbers`: ochiq (WAL) bazadan onlayn zaxira → `verify` → boshqa joyga tiklash → bank, hisobot va `/eksport` baytma-bayt bir xil; buzilgan bazani almashtirishda xavfsizlik nusxasi; kesilgan zaxira rad etiladi, baza o'zgarmaydi |
| F9 | Healthcheck holatlari | ✅ | `test_the_health_check_states_on_a_lived_database` (subprocess): sog'lom → exit 0; baza yo'q → 1; disk < chegara → 1; buzilgan fayl → 1; `real` rejimda rejalashtiruvchi yo'q → 1 |
| F9 | Server qayta yuklangach o'zi tiklanadi; bitta merge bilan deploy | 🔒 | VPS yo'q. Statik: `restart: unless-stopped` + healthcheck, `deploy.yml`, `remote-deploy.sh`, entrypoint (`tests/ops/**`, 8 test o'tkazib yuborildi: shellcheck/POSIX signallar yo'q) |
| F9 | `rclone` masofaviy zaxira, healthchecks.io jonli | 🔒 | kalit/hisob yo'q |
| F10 | ≥ 150–200 yakunlangan signal, CLV > 0, 14 kun aralashuvsiz | — | boshlanmagan (F3 darvozasidan keyin) |
| F11 | Reliz `v1.0.0` | — | boshlanmagan |

## 3. Asosiy reja §6.1–6.14: har bir imkoniyat

Dalil ustunidagi fayllar to'liq to'plamda qayta ishga tushirildi (§6). "T6" belgisi shu to'lqinda qo'shilgan, **haqiqiy servislar** ustidagi test.

| § | Imkoniyat | Holat | Dalil |
|---|---|---|---|
| 6.1 | Doimiy menyu (9 tugma), `/start`, `/yordam`, `/bekor` | ✅ | `tests/bot/test_dispatcher.py`, `tests/e2e/test_bot_buttons_e2e.py` |
| 6.2 | Sana oralig'i: tugmalar, kalendar, matn (`25.10 - 31.10`, `25.10.2026-31.10.2026`) | ✅ | `tests/e2e/test_bot_wizard_e2e.py`, `tests/bot/test_parsing.py` (hypothesis) |
| 6.2 | Kelajak ≤ 14 kun (`horizon_end`), orqaga ≤ 90, oyna ≤ 31 | ✅ | T6 `test_6_2_the_future_is_limited_to_the_horizon` (haqiqiy servis; rad etilgan so'rovda provayderga murojaat yo'q) |
| 6.2 | Sport, ehtimollik, EV, qamrov (liga / jamoa / aniq o'yin, sahifalash), soni, bozorlar, saralash, tasdiq + taxminiy API sarfi | ✅ | `tests/e2e/test_bot_wizard_e2e.py`, `tests/bot/test_analysis_flow.py` |
| 6.2 | `/tahlil 25.10-31.10 futbol 60% Arsenal,Inter` tezkor buyruq | ✅ | `tests/bot/test_parsing.py`, `test_quick_and_cards.py` |
| 6.2 | 3 kundan uzoq o'yinlar: ishonch bir pog'ona past, `⏳ Dastlabki tahlil` | ✅ | T6 `test_6_2_matches_more_than_three_days_away_are_marked_preliminary` (haqiqiy pipeline: ≤3 kun — belgisiz, >3 kun — hamma kartada belgi) |
| 6.2 | Oraliq signallari uchun avtomatik qayta hisoblash (24 soat, N daqiqa) | ✅ | `tests/pipeline/test_day_e2e.py`, T6 hafta senariysi (har signalga yangilash, ikki marta emas), `test_a_scheduled_update_failure_is_retried_and_never_doubled` |
| 6.2 | Retro-tahlil (o'tgan oraliq, ✅/❌, API sarfi ~0) | ✅ | `tests/e2e/test_f77_retro_e2e.py` |
| 6.2 | 💾 Shablonlar + jadval (har kuni/hafta kuni, vaqt) | ✅ | T6 `test_a_scheduled_template_runs_once_on_its_day_and_titles_the_digest` (kuniga bir marta, sarlavha shablon nomi bilan, soatlar o'tgach ham 1 ta) |
| 6.2 | Karta: tanlov, ehtimollik, adolatli koef., EV, ishonch, tavsiya, izoh, 4 tugma | ✅ | T6 hafta senariysi (haqiqiy kartalar, Gemini izohi bilan), `tests/bot/test_formatters.py` |
| 6.2 | Mos o'yin yetmasa ochiq aytiladi, ro'yxat to'ldirilmaydi | ✅ | T6 `test_6_2_a_short_result_is_stated_not_padded` ("so'ragansiz", kartalar < so'ralgan) |
| 6.3 | Maxsus tahlil: jamoa qidiruvi (lotin/kirill, noaniq), `/tahlil A - B`, checkbox, EV filtrisiz "Value topilmadi" | ✅ | `tests/e2e/test_f71_custom_e2e.py` (22), `tests/db/test_search.py` |
| 6.4 | 🔄 bitta signal: yangi koef., farq xabari (`Koeff.: 2.04 → 1.84  EV: +3.8% → −6.5%`, holat) | ✅ | T6 `test_day_one_…_recalculation_diff` |
| 6.4 | Himoya: o'sha o'yin uchun 10 daqiqada 1 marta | ✅ | T6 `test_6_4_a_second_recalculation_inside_ten_minutes_is_refused_then_allowed` (9 daq → rad, 10 daq → ruxsat) |
| 6.4 | 🔄 Hammasini yangilash, `/qayta`; `signal_versions` | ✅ | `tests/e2e/test_bot_recalc_e2e.py`, `tests/pipeline/test_flows.py` |
| 6.4 | Zaxira tugasa keshlangan raqamlar "11:00 holatiga" belgisi bilan | ✅ | T6 `test_api_quota_exhausted_gives_clear_notices_and_nothing_crashes` ("📦 Yangi ma'lumot olinmadi: raqamlar 11:00 holatiga", tahlil: "⚠️ So'rov API limitidan oshadi: 44 ta … zaxirada 0") |
| 6.5 | Sozlamalar: 13 maydon, standartlar (45% / EV 5% / 1.50–3.00 / 10 / 09:00 / 60 daq / tinch soat 00–08 / Asia/Tashkent), risk profillari | ✅ | T6 `test_6_5_a_new_user_gets_the_balanced_defaults_of_the_plan`; `tests/bot/test_settings_flow.py`. Eslatma: reja jadvalida standart 50% / 1.40–3.00, qaror #8 uni "Balanslangan"ga o'zgartirgan (45% / 1.50–3.00) |
| 6.6 | Ehtimollik va ishonch darajasi aralashtirilmaydi; ≥ 80–90% ogohlantirishi **bir marta** | ✅ | T6 `test_6_6_the_high_probability_warning_is_shown_once`; `EV < 0` → "❗ Qiymatsiz" (`tests/bot/test_formatters.py`) |
| 6.7 | ⭐ Sevimlilar: jamoa/liga, `/kuzat`; sevimli o'yin kunlik xabarga **har doim** kiradi; bildirishnomalar | ✅ | T6 `test_6_7_a_followed_team_is_always_in_the_digest_even_without_value` (hech qanday signal chegaradan o'tmaganda ham), `tests/e2e/test_f73_watchlist_e2e.py` |
| 6.8 | 📋 Batafsil karta: forma, H2H, top-3 hisob, bozorlar jadvali, koef. harakati | ✅ | `tests/e2e/test_f71*.py`, `tests/bot/test_detail.py`. ⚠️ jarohatlar bazada yo'q (`absences` bo'sh, ADR 0015) |
| 6.9 | ✅ Stavka qildim: haqiqiy koef. va summa; ikki marta bosish = 1 stavka | ✅ | T6 hafta senariysi (bir xil callback ikki marta, restartdan keyin ham), `tests/e2e/test_bot_bets_e2e.py` (parallel bosish ham) |
| 6.9 | Natija avtomatik hisoblanadi: foyda/zarar, bank | ✅ | T6 `test_day_two_every_number_equals_the_hand_computation` (13 stavka, har biri `Fraction` oracle bilan; bank = 1000 + Σ) |
| 6.9 | `/bank`: balans, oylik P/L, grafik PNG | ✅ | `tests/e2e/test_f75_bank_e2e.py`, `tests/bot/test_charts.py` |
| 6.9 | Kunlik/haftalik zarar limiti: ogohlantirish va o'sha kun yangi signal yuborilmaydi | ✅ | T6 `test_loss_limit_stops_new_signals_and_says_so_once_while_others_are_unaffected` (raqam: zarar 150 ≥ 30; kunlik xabar va shablon bloklandi, **bitta** xabar; nazorat foydalanuvchi signal oldi; qo'lda tahlil bannerli) |
| 6.10 | Hisobot: davrlar, kesimlar, ko'rsatkichlar, kalibratsiya jadvali, grafik, `/eksport` (2 fayl) | ✅ | T6 `test_6_10_…` (`/hisobot` HTML to'g'ri, `/eksport` 2 hujjat), `tests/e2e/test_f76_report_e2e.py`, `test_qa_t5_report_export_e2e.py` |
| 6.11 | 🧩 Ekspress: 2–4 tanlov, ehtimollik/EV, bog'liq bozorlar birlashmaydi, 34% ogohlantirishi | ✅ | `tests/e2e/test_f78_express_e2e.py`, `tests/services/test_express_math.py` (hypothesis) |
| 6.12 | `/holat`: API limitlari, oxirgi yangilanish, xatolar, LLM, o'rganish rejimi | ✅ | T6 `test_the_status_screen_after_a_working_week_lists_the_jobs_and_no_errors` (real rejim, "Oxirgi xatolar: yo'q", sirlar yo'q) |
| 6.12 | Whitelist: begonaga javob yo'q (guruh/kanal ham) | ✅ | T6 `test_6_12_a_stranger_gets_no_answer_at_all` (0 ta Telegram so'rovi), `tests/e2e/test_bot_access_e2e.py` |
| 6.12 | Tizim xatolari adminga avtomatik | ✅ | `test_the_pipeline_gives_up_after_five_failures_and_tells_the_admin_not_crashes`; `alert_throttled` (30 daq) |
| 6.13A | O'yin tugagach natija avtomatik; `[✅][❌][↩️][💸][💬]`; `/natijalar` | ✅ | T6 (qo'lda belgi `/natijalar` orqali, API natijasiga **teskari**, keyingi `settle` uni o'zgartirmadi); `tests/e2e/test_f81_results_e2e.py` |
| 6.13A | Ertalabki "Kecha: N signal · W ✅ · L ❌ …" | ✅ | T6: `📆 Kecha: 13 ta signal · W ✅ · L ❌` satri oracle bilan teng |
| 6.13A | `💬 Izoh` keyingi tahlilda kontekst bo'ladi | ✅ | T6 `test_6_13_a_users_note_reaches_the_next_language_model_prompt` (izoh matni keyingi Gemini so'rovida) |
| 6.13B | 1-qatlam kalibratsiya, 2-qatlam segment filtri | ✅ | T6 `test_shadow_changes_nothing_apply_changes_exactly_the_condemned_segment` |
| 6.13B | 3-qatlam tajriba konteksti (≤ 5 saboq, izohlar) | ✅ | yuqoridagi test + `tests/pipeline/test_experience.py` |
| 6.13B | 4-qatlam post-mortem va saboqlar (≥ 2 marta, 90 kun) | ✅ | `tests/pipeline/test_postmortem.py`, `test_lessons.py` (T5). ⚠️ T6 hafta senariysida post-mortem ishga tushirilmadi (`/holat`: "hali ishlamagan") |
| 6.13C | Barcha signallar hisobda (selection bias yo'q); kichik namunada o'zgarish yo'q; har o'zgarish jurnalda, `/ozgarishlar`, orqaga qaytarish | ✅ | T6 learning testi (`/ozgarishlar` da shadow yozuvi, undo tugmasi yo'q; apply da bor), `tests/e2e/test_f88_learning_e2e.py` |
| 6.13C | Haftalik "model nimani o'rgandi" xabari | ✅ | `tests/e2e/test_f88_learning_e2e.py`, `tests/pipeline/test_scheduler_learning.py` |
| 6.14 | Keyingi bosqich g'oyalari (live, function calling, infografika) | — | rejalashtirilmagan (F12); tekshirilmadi |
| §5 | Ko'p sport arxitekturasi (`SportAdapter`) | ✅ | `tests/sports/test_base.py`, shartnoma testlari; faqat futbol adapteri bor (F12) |
| §9 | API so'rovlari byudjeti (~25–43 avtomatik, zaxira 30%) | ✅ ⚠️ | F2 qatoriga qarang |

## 4. T6 da qo'shilgan testlar va ularning natijalari

Yangi fayllar (hammasi `tests/` ichida; ishlab chiqarish kodiga tegilmagan):

| Fayl | Nima | Soni |
|---|---|---|
| `tests/e2e/t6_world.py`, `t6_oracle.py` | Harness (haqiqiy dispatcher + haqiqiy `TelegramNotifier` + haqiqiy servislar + skriptli API/Gemini) va **mustaqil** hisob oracle'i (`Fraction`, `app.models` import qilinmaydi) | — |
| `tests/e2e/test_t6_week_e2e.py`, `test_t6_week_more_e2e.py` | Bir hafta: 1-kun, 2-kun hisob-kitob, zarar limiti, shablon, tinch soat, Gemini/API/LLM limiti, restart, 2 foydalanuvchi | 14 |
| `tests/e2e/test_t6_learning_e2e.py` | `off` / `shadow` / `apply` | 2 |
| `tests/e2e/test_t6_plan_conformance_e2e.py` | Reja §6 bandlari haqiqiy servislarda | 10 |
| `tests/e2e/test_t6_kill_drill_e2e.py` | Kunlik jobni **har bir** chegarada o'ldirish | 1 (189 ishga tushirish) |
| `tests/e2e/test_t6_notifier_faults_e2e.py` | 429 / 403 / 400 / 5xx / tarmoq xatosi har bir xabar yo'lida | 27 (1 xfail) |
| `tests/e2e/test_t6_robustness_e2e.py` | Yaroqsiz callback, buzilgan FSM, juda katta kirish, qulflangan baza | 263 (1 xfail) |
| `tests/integration/test_t6_acceptance.py` | Zaxira/tiklash, migratsiya, healthcheck holatlari | 6 |
| `tests/integration/test_t6_scale.py`, `t6_scale_world.py` | Ishlab chiqarish hajmidagi baza, samaradorlik va xotira (`slow`) | 3 |
| `tests/integration/test_t6_findings.py` | Qat'iy xfail: N+1 (ikkita), begona qulflari | 4 (3 xfail) |

### 4.1 Bir haftalik senariy: mustaqil qo'lda hisob bilan mos kelish

Dunyo: 2 liga, 12 jamoadan, 3 mavsum tarixi; skriptli API-Football, skriptli Gemini; **haqiqiy** pipeline, ledger, `TelegramNotifier` (bir xil yozuvchi Telegram sessiyasiga), `SqliteStorage` FSM. Soat — ko'chma (`MutableClock`), ticklar rejalashtiruvchining o'zi chaqiradigan job nomlari bilan (`PipelineJobs.run`).

1. **1-kun:** 08:30 refresh → kunlik xabar (1 ta, qayta tick'larda ham 1 ta; Gemini 1 so'rov, 10 kartaning hammasida izoh) → karta orqali stavka, tasdiq tugmasi **ikki marta** bosildi → 1 stavka → koeffitsient 10% tushdi → 🔄: `Koeff.: 2.04 → 1.84 EV: +3.8% → −6.5%`.
2. **2-kun:** 13 signal (10 tahlil + 3 qo'lda: AET uchun `1X2 uy @2.30`, `AH −0.75 @1.90`, `AH −0.25 @2.10`) va har biriga o'z koeffitsienti/summasi bilan stavka. Foydalanuvchi bir o'yinni qo'lda **API'ga teskari** belgiladi. 03:00 UTC `settle` (ikki marta ishga tushirildi).
3. **Oracle** (`t6_oracle.py`): chorak chiziq = ikkita yarim stavka; foyda = `stake × (wins·(odds−1) − losses)` `Fraction` da.

Natijalar (hammasi ✅): har signalning belgisi (`win/loss/half_win/half_loss/void`) oracle bilan teng; AET o'yinida (90 daqiqada 1–1) uy tanlovi **yutqazadi**; `AH −0.75` 1–0 → `HALF_WON`; `AH −0.25` 1–1 → `HALF_LOST`; **har bir stavka foydasi** 0.01 aniqlikda teng; bank = 1000 + Σ stavka foydasi (±0.05); `bank.totals.settled == 13`, `open_bets == 0`; hisobot `settled/won/lost` va `profit_units` (qo'lda belgi ±1 unit koeffitsientda) teng; ikkinchi `settle` natija xabarlarini ko'paytirmadi; 2-kunlik raqam: `Kecha: 13 ta signal · W ✅ · L ❌`. **Mutatsiya tekshiruvi:** oracle natijasiga +1 qo'shilsa test yiqiladi (`Decimal('7.90') == Decimal('8.90')`).

Qo'shimcha hafta senariylari (hammasi ✅):
- **Zarar limiti** (30): yutqazgan stavkalar 24-kuni `settle` dan keyin zarar 150; kunlik xabar o'rniga **bitta** "🛑 Zarar limitiga yetildi", shu kunning shablon xabari ham bloklandi (ikkinchi xabar yo'q); nazorat foydalanuvchi o'sha ertalab 10 ta signal oldi; qo'lda tahlilda banner.
- **Tinch soatlar** (00:00–08:00, yuborish 01:00): 02:00–07:30 mahalliy xabar yo'q, 08:00 da **bitta**.
- **Shablon**: kuniga bir marta, sarlavha shablon nomi.
- **Gemini ishlamaydi**: 10 kartaning hammasi izohsiz yetdi, job xatosiz. **LLM kunlik limiti** (1 so'rov): ikkinchi so'rov backendga bormadi, ertasi kuni yangilandi.
- **API kvotasi tugadi**: 🔄 "11:00 holatiga", tahlil "API limitidan oshadi … zaxirada 0", `/holat` javob beradi.
- **Restart** (yangi dvigatel, kontekst, notifier, dispatcher; bir xil `week.db` va `fsm.db`): kunlik xabar va kartalar takrorlanmadi, yarim qolgan wizard saqlandi va davom etdi, `/bekor` dan keyin eski tugma "⌛ Bu tugma eskirgan", qayta yuborilgan tasdiq tugmasi yangi stavka yaratmadi.
- **Ikki foydalanuvchi**: bir xil o'yinlarga turli summa/koeffitsient; har birining banki o'z oracle'iga teng (1000 va 5000 bazasidan); begona callback (`hide/bet/recalc/detail`) boshqaning signaliga ta'sir qilmadi.

### 4.2 O'rganish halqasi: `off` ≡ `shadow`, `apply` aynan nimani o'zgartiradi

150 ta (hit-rate 25%, CLV −0.10) tarixiy signal `football-data:E0 · 1X2 · 2.20–3.00` segmentiga yozildi; ikki haftalik ishga tushirish (14 va 21 okt).

| Rejim | Journal | Tavsiyalar |
|---|---|---|
| `off` | bo'sh, `segment_stats` bo'sh | asosiy (10 ta) |
| `shadow` | 1 ta `segment_disabled`, `applied=False`; `/ozgarishlar` da "kuzatuv", **↩️ yo'q** | asosiy bilan **bit-ma-bit bir xil** (nom, bozor, koef., ehtimollik 1e-9, EV, birlik, ishonch) |
| `apply` | 1 ta `segment_disabled`, `applied=True`, ↩️ bor | segmentdagi tavsiya yo'q; boshqa tavsiyalarning hammasi o'zgarishsiz; faqat shu o'yinlarda "keyingi eng yaxshi" nomzod chiqishi mumkin |

Muhim kuzatuv (xato emas): bir marta tavsiya qilingan signal `recommended_at` o'zgarmasligi sababli keyingi o'rganishdan keyin ham ro'yxatda qoladi (shartnoma v1.1). Shu sababli "apply" faqat **keyingi** tahlillarga ta'sir qiladi.

### 4.3 O'ldirish mashqi (chaos): kunlik xabarning har bir chegarasi

Chegara = SQL operatori, `commit`, Gemini so'rovi, Telegram so'rovi. Jarayon `BaseException` (`except Exception` ushlamaydi) bilan n-chi chegarada "o'ladi" va keyingi **har** chegarada ham (tozalash kodi ishlamaydi, `kill -9` kabi). Qayta ishga tushirish: yangi dvigatel/kontekst/notifier/dispatcher; 6 tick, har biri +20 daqiqa (ijara 15 daq).

- Chegaralar: **188**. Ishga tushirishlar: 189 (1 nazorat + 188).
- **Yo'qotish yo'q:** 188/188 da kamida 1 ta kunlik xabar va barcha 10 karta yetdi.
- **Signal takrorlanishi yo'q:** 188/188 da saqlangan signallar to'plami nazorat bilan bir xil, hammasi `version = 1`.
- **Aynan bir marta:** 145 ta. **Kamida bir marta (xabar ikki nusxa):** 43 ta (o'ldirish #143–#185, ya'ni birinchi Telegram xabaridan keyin); eng ko'pi 2 nusxa (11 xabar × 2).
- Yakuniy holat: har holatda `(daily, kun, foydalanuvchi, digest)` kaliti `done`.

## 5. Samaradorlik va hajm (ishlab chiqarish o'lchamidagi baza)

Baza: 10 liga × 20 jamoa × 5 mavsum = **16 230 o'yin** (16 200 natijali), **48 300** koeffitsient qatori (so'nggi 6 oy: opening/morning/prematch/closing, Pinnacle va 1xBet, 1X2 + Total 2.5), 30 kelgusi nomzod; fayl 17.3 MB (qurish 9.5 s). Mashina band bo'lmaganda o'lchandi (band holatda ~2×).

| O'lchov | Natija | Chegara (xavfsizlik) |
|---|---|---|
| Kunlik tahlil, 30 nomzod, sovuq (10 liga modeli qayta fit) | **0.85 s** (band mashinada 1.8 s) | 90 s |
| Xuddi shu, issiq | 0.72 s | 15 s |
| Sekin SQL (≥ 0.25 s) tahlil, hisobot, eksport, 200 kunlik siklda | **0 ta** | — |
| `/hisobot` (hammasi, 5 000 signal) / kesim bilan | 0.53 s / 0.52 s | 10 s |
| `/eksport` signals.csv, 5 001 qator (734 KB) | 0.54 s | 15 s |
| `/eksport` bets.csv, 1 000 stavka (90 KB) | **1.96 s** (N+1, T6-F-3) | — |
| Refresh yozuvi (240 koef. qatori, 60 o'yin) | respx testi < 1 s (provayder xotirada) | — |
| 200 kunlik sikl (har kun yangi o'yin + koef., kunlik xabar, shablon, qayta hisob, settle; LLM yoqilgan) | 97 s | — |
| Jarayon ichidagi xotira (`tracemalloc`) 20-kun → 200-kun | 3.7 → 4.6 MB (**+0.8 MB**) | +20 MB |
| `ModelCache` hajmi | 29 → 64 (chegarasi `CACHE_CAPACITY = 64`) | ≤ 64 |
| LLM jurnali | 100 yozuv (chegara 500) | ≤ 500 |

Xulosa: xotira va keshlar chegaralangan; "chiqarish chegarasi" shaklidagi sekin so'rov faqat bitta: stavkalar ro'yxati (N+1).

## 6. Darvoza (to'liq, oxirgi holat)

| Buyruq | Natija |
|---|---|
| `python -m uv run --no-config pytest` (to'liq, `--cov`) | **4138 passed, 8 skipped, 4 xfailed**, 28 min 07 s. Umumiy qamrov **98.28%** (chegara 85%) |
| `python scripts/ops/coverage_gate.py` | `app/models` **99.75%** (2354 operator, min 90%), `app/pipeline` **97.25%** (3452, min 90%), exit 0 |
| `ruff check .` / `ruff format --check .` | toza / toza (452 fayl) |
| `mypy src` | exit 0 (strict) |
| `lint-imports` | exit 0 |
| `deptry .` | "No dependency issues found", exit 0 |

Izohlar: 8 skip: shellcheck o'rnatilmagan (6) va POSIX signallari (2), bu Windows cheklovi. To'liq yugurishdan keyin yana bitta strict xfail (begona qulflari, T6-F-8) va bir nechta tozalash tahrirlari qo'shildi; ular tegadigan 8 ta T6 fayl alohida qayta yurgizildi: **321 passed, 5 xfailed** (barcha 5 ta T6 xfail), shundan so'ng ruff va format toza. Baseline (T6 testlarsiz, `main`): 3813 passed, 8 skipped (14 min 45 s). Yangi: 330 ta test (5 ta strict xfail), shundan sekin (`slow`) belgisi bilan: hafta, o'rganish, chaos, miqyos va qabul fayllari. Tez yurgizish uchun: `-m "not slow"`.

Barqarorlik: T6 e2e fayllari ketma-ket bir necha marta yurgizildi (to'liq yugurish + 2 ta alohida), natijalar bir xil; vaqtga bog'liq tasdiqlar faqat keng chegaralar (90 s / 15 s / 20 MB), qolgani injeksiyalangan soat va `hash` asosidagi deterministik ma'lumot.

Qayta takrorlash: `python -m uv run --no-config pytest tests/e2e/test_t6_*.py tests/integration/test_t6_*.py -s` (o'lchamlar `T6-PERF` / `T6-CHAOS` qatorlari bilan chop etiladi).

## 7. Topilmalar

> **Holat (T6 fix C, 2026-10-23, [ADR 0019](../adr/0019-t6-fix-round-c.md)):** F-1…F-8 tuzatildi, tegishli strict xfail'lar olib tashlandi va oddiy regressiya testlariga aylandi. F-4: aniq bir marta 77% (145/188) dan 88.5% (170/192) ga, qolgan chegaralar har biri bitta xabar takrori (95% ga erishilmadi, sababi ADR 0019 §2). F-3: `get_bet` fix A da, ro'yxat/eksport N+1 si fix C da. Quyida topilmalarning asl matni (tuzatishdan oldingi holat) saqlangan.

### 🟠 T6-F-1. Bitta rad etilgan karta kunlik xabarning qolgan kartalarini yutib yuboradi
- **Joy:** `src/app/bot/notifier.py:198-201` (`_daily`: `if await self._send(...) is None: break`), qaytariladigan havola — sarlavha xabarniki.
- **Ssenariy:** Telegram 12 ta xabarni ketma-ket qabul qilishi kerak (sarlavha + kun sarlavhasi + 10 karta). Biror kartani Telegram rad etsa (400, yoki ikkinchi urinishda ham 429), tsikl to'xtaydi; xabar "yetkazildi" deb hisoblanadi (ledger kaliti `done`), qayta urinish ham, admin ogohlantirishi ham yo'q (faqat `notify_rejected` ogohlantirish logi). Natija: shu kunning qolgan kartalari (2-karta rad etilsa 8 ta) foydalanuvchiga **hech qachon** yetmaydi, sarlavhada esa "10 ta signal" deyiladi.
- **Dalil:** `tests/e2e/test_t6_notifier_faults_e2e.py::test_the_remaining_cards_are_still_sent_when_one_card_is_rejected` (strict xfail; hozir faqat 1 karta yetadi).
- **Tuzatish:** `break` o'rniga davom etish va rad etilganlarni sanash; sarlavhada "N dan M tasi yuborilmadi" yoki ledger kalitini muvaffaqiyatsiz deb belgilab qayta urinish. Foydalanuvchi "⚡ Bugungi signallar" orqali ko'ra oladi (shuning uchun 🟠, 🔴 emas).

### 🟡 T6-F-2. Baza qulflanganda `PipelineJobs.run` istisno ko'taradi
- **Joy:** `src/app/scheduler.py:151-170`: xatoni yozish (`record_run`, `record_error`, `alert_throttled`) o'sha qulflangan bazaga yoziladi, `OperationalError` tashqariga chiqadi ("hech qachon ko'tarmaydi" va docstring'ga zid). `run_due_digests` (`daily.py:_digest_for → fail_key`) ham: bitta foydalanuvchining xatosi o'sha tick'dagi qolgan foydalanuvchilarni to'xtatadi.
- **Ta'sir:** APScheduler istisnoni logga yozadi va keyingi tick (1 daq) tiklanadi (tekshirildi: qulf yechilgach aynan 1 ta xabar, 10 signal). Lekin `/holat` da xato ko'rinmaydi va admin ogohlantirishi ketmaydi. Bitta foydalanuvchida jiddiy emas.
- **Dalil:** `tests/e2e/test_t6_robustness_e2e.py::test_a_job_wrapper_never_raises_even_when_the_database_is_locked` (strict xfail); tiklanish: `test_a_locked_database_fails_the_job_cleanly_and_the_next_tick_recovers` ✅.
- **Tuzatish:** xatolarni yozishni o'z `try/except` ichiga olish (log bilan), yoki alohida qisqa-timeout dvigatel (`create_usage_engine` kabi).

### 🟡 T6-F-3. Stavkalar ro'yxati va `get_bet` da N+1 so'rov
- **Joy:** `src/app/services/real/signals.py:282-297` (`_bets`: har bir stavka uchun `data.get_signal`), `:299-304` (`get_bet` hamma stavkani yuklab chiziqli qidiradi), `export_bets` (`:394`).
- **O'lchov:** ≈ 2–3.6 ms/stavka: 1 000 stavkada bitta stavkani ochish/belgilash ~2–3.6 s, `bets.csv` 1.96 s. F10 (≤ 200 stavka) da ~0.5 s: qabul qilsa bo'ladi; F11 dan oldin tuzatilsin.
- **Dalil:** `tests/integration/test_t6_findings.py` (ikkita strict xfail: so'rovlar soni stavkalar soniga proporsional).
- **Tuzatish:** `signals` ni `IN (...)` bilan bir so'rovda; `get_bet` ni ID bo'yicha.

### 🟡 T6-F-4. "Kamida bir marta" oynasi 188 chegaradan 43 tasini egallaydi
- **Joy:** `src/app/pipeline/digest_store.py:34-43` (`remember_announcement`: har karta uchun alohida ledger tranzaksiyasi, 3 chegara × 10) `daily.py:122` da yuborishdan keyin, `_finish` dan oldin chaqiriladi.
- **Ta'sir:** shu oraliqda o'lsa, qayta ishga tushirishdan keyin butun xabar (11 xabar) ikkinchi marta yuboriladi. Ma'lumot yo'qolmaydi, signal takrorlanmaydi.
- **Tuzatish:** hamma kartani bitta tranzaksiyada yozish yoki `daily` kalitini oldin yopish; oyna 43 dan ~2 chegaragacha qisqaradi.

### 🟡 T6-F-5. Telegram o'rniga HTML sahifa kelganda (firewall) log 38 KB va exit 1
- **Joy:** `src/app/main.py:connect_bot` faqat `TelegramUnauthorizedError` ni ushlaydi; `ClientDecodeError` butun HTML tanasi bilan `fatal_error` logiga yoziladi (bitta qator ~37 KB), Docker restart siklida har safar takrorlanadi. Token logda **yo'q** (soxta token 0 marta uchradi: ✅).
- **Tuzatish:** tarmoq/dekodlash xatolarini qisqa xabar bilan o'rash (`ConfigurationError` emas, lekin kesilgan matn).

### 🟡 T6-F-6. LLM sharh vaqt chegarasi yagona 30 s va butun tahlilga umumiy
- **Joy:** `src/app/pipeline/review.py:225-240` (`review_safely` `asyncio.timeout(30)` butun `reviewer.review` atrofida). 20 nomzod = 2 paket ketma-ket; ikkinchisi cho'zilsa, birinchi paketning kelgan javoblari ham tashlanadi (hamma kartada izoh yo'q). Raqamlar (ehtimollik/EV) ta'sirlanmaydi. Reja F5 "20 nomzod ≤ 15 s" mock bilan tasdiqlangan; haqiqiy Gemini bilan o'lchanmagan.
- **Tuzatish:** paket boshiga alohida vaqt chegarasi.

### 🟡 T6-F-7. Provayder xatosidan keyin 5 urinish ~15 daqiqada tugaydi
- **Joy:** `src/app/pipeline/ledger.py:46-49,102-105` (`MAX_ATTEMPTS = 5`, kechikish 1·2·4·8 daq). Telegram 15 daqiqadan uzoq ishlamasa, o'sha kunning xabari "tugatilgan" (`exhausted`) bo'ladi, admin ogohlantiriladi, signallar saqlanadi (⚡ orqali ko'rinadi). Hujjatlashtirilgan dizayn; maslahat: urinishlar oralig'ini uzaytirish (masalan, 1–2 soatgacha) yoki ertalab 2 marta urinish.
- **Dalil:** `test_the_pipeline_gives_up_after_five_failures_and_tells_the_admin_not_crashes`.

### 🟡 T6-F-8. Begona foydalanuvchi har biri jarayon xotirasida abadiy qulf qoldiradi
- **Joy:** `src/app/bot/dispatcher.py:build_dispatcher`: aiogram'ning FSM middleware'i `Dispatcher.__init__` da **oldin** ro'yxatdan o'tadi, whitelist esa undan keyin. Shu sababli har bir yangi begona `(user, chat)` kaliti `SimpleEventIsolation._locks` ga yoziladi va hech qachon o'chmaydi (60 ta begona → 60 ta qulf; javob yo'q, `fsm.db` ga yozuv yo'q, ya'ni foydalanuvchiga ko'rinadigan oqibat yo'q).
- **Ta'sir:** bot nomini bilgan har kim xotirani sekin o'stira oladi (bitta qulf ~ yuzlab bayt). Bir foydalanuvchili tizim uchun xavfsizlik emas, "chegaralangan kesh" tamoyilining buzilishi.
- **Dalil:** `tests/integration/test_t6_findings.py::test_strangers_leave_nothing_behind_in_the_process` (strict xfail).
- **Tuzatish:** begona update FSM middleware'ga yetmasligi uchun whitelist'ni undan oldin bajarish, yoki chegaralangan (LRU) izolyatsiya obyekti.

### Oldingi hisobotlardagi tasdiqlanmagan / yopilgan narsalar
- T5 QA F-1…F-10 va T4 F-1…F-12: tuzatilgan va qayta ishlamayotgan xfail yo'q (to'liq to'plamda 0 xfail eskidan; T6 faqat o'zining 5 ta strict xfail'ini qo'shadi).
- ⚠️ T5-QA-1/3 (segment / kalibratsiya darvozalarining "ko'p tekshiruv" xavfi) 99.5% interval + ketma-ket 2 ishga tushirish bilan tuzatilgan; T6 `apply` mashqida bitta segment ikki ketma-ket haftadan keyingina o'chdi (mos).

## 8. Tekshirib bo'lmaydiganlar (aniq)

1. Jonli Telegram: token + firewall'siz tarmoq. Soxta token bilan ulanish urinishi firewall HTML sahifasini oldi (F1).
2. API-Football: kalit yo'q → haqiqiy javob shakli, Osiyo fora yozilishi (`HANDICAP_CONVENTION`), liga ID lari, bepul tarif joriy mavsumi.
3. Gemini: haqiqiy kechikish, javob sifati, kvota.
4. F3 darvozasi: haqiqiy tarix (football-data bloklangan).
5. VPS: qayta yuklashdan keyin tiklanish, GHCR/SSH deploy, `rclone`, healthchecks.io.
6. `pip-audit` (korporativ CA), `gitleaks` (image yo'q), GitHub Actions: CI da bajariladi.
7. Real vaqtda 3 kunlik ishga tushirish va 14 kunlik aralashuvsiz ishlash (F10 mezoni).
