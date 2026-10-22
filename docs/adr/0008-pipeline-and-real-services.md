# ADR 0008: Pipeline, real servislar va rejalashtiruvchi

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-22
- **Muallif:** pipeline-engineer (T4: F4.1–F4.8)
- **Kod:** `src/app/pipeline/`, `src/app/services/real/`, `src/app/scheduler.py`
- **Bog'liq:** [ADR 0004](0004-service-contract.md) (§11, v1.2), [ADR 0007](0007-api-client-and-repositories.md) (so'rov hayot yo'li, lease), qarorlar jurnali #10 (stavka narxi), #14 (LLM verdikti)

## Kontekst
Bot fake servislar ustida qurilgan. T4 da haqiqiy tizim kerak: ma'lumotni yangilaydi, bitta yo'ldan tahlil qiladi, signalni saqlaydi, qayta hisoblaydi, natijani yopadi va hammasini jadval bo'yicha o'zi bajaradi. Cheklovlar: API kaliti hali yo'q (testlar `respx` / skriptlangan provayder bilan), bitta jarayon, SQLite, `models` — sof, `services` faqat `pipeline` orqali bazaga chiqadi.

## Qarorlar

### 1. Bitta tahlil yo'li: `AnalysisEngine.run(AnalysisRequest)`
Kunlik, sana oralig'i, maxsus, shablon va sevimlilar tahlili bir xil zanjir: oynadagi o'yinlar → qamrov → (byudjet ichida) yetishmagan ma'lumotni olish → liga bo'yicha DC (kesh) → `predict_markets` → Pinnacle de-vig (`fair_markets`) → ansambl → **foydalanuvchi bukmekeri narxi bo'yicha EV** (qaror #10, standart 1xBet; narx yo'q → tanlov chiqmaydi) → ishonch (`assess_confidence`; xG ham tarkib ham yo'q → eng yuqori daraja O'RTA; farq > 15 p.p. → `sanity_flag` → "value" emas) → chegaralar → har o'yindan eng yaxshi bitta → saralash → kesim → LLM → saqlash. Servis faqat **so'rov qurish** (mahalliy sana → UTC, id lar, sozlamalar) va **foydalanuvchi qoidalari** (horizon, bir vaqtda bitta tahlil, cooldown, zaxira) bilan shug'ullanadi. Formulalar faqat `app.models` da; pipeline orkestratsiya.
- Faqat boshlanmagan o'yinlar haqiqiy signal; tugaganlari `retro`; o'yin ichidagi, qoldirilgan, bekor qilingan o'yinlar o'tkazib yuboriladi. Bitta o'yindagi xato (`fixture_failed`) loglanadi va sanaladi, qolganlari ishlanadi.
- Model `(liga, as_of, o'yinlar soni)` bo'yicha keshlanadi (`asyncio.to_thread`, bitta qulf: parallel so'rovlar bitta fit ni bo'lishadi); yangi natija saqlansa kalit o'zgaradi — eskirgan model qolmaydi.

### 2. Ma'lumot olish: o'qish → tarmoq → qisqa yozish
ADR 0007 qoidasi: tarmoq chaqiruvi vaqtida sessiya ochiq emas; har yozish alohida tranzaksiyada commit. Qamrov (qaysi kunlarning o'yinlari/koeffitsientlari qachon olingan) ledger'da: restart ham, ikkinchi foydalanuvchi ham qayta so'ramaydi; `estimate_cost` shu qamrovni hisobga oladi (sovuq keshda koeffitsientlar uchun liga×kun yuqori baho). Avtomatik ishlar faqat avtomatik kvotadan, foydalanuvchi harakatlari butun qolgandan foydalanadi; byudjet yetmasa qo'lda so'rov `BudgetExceededError`, avtomatik qadam o'tkazib yuboriladi (qisman yozilmaydi).
Provayder jamoalari tarix identifikatsiyasiga o'tkaziladi (`IdentityMap`): faqat **alias/aniq** moslashuv qo'llanadi; `fuzzy` moslashuvlar loglanadi ("ko'rib chiqish kerak"), **jim bog'lanmaydi** (data-engineer qoidasi, ADR 0007 §12).

### 3. Idempotentlik: ledger (`pipeline_ledger`)
Kalit `(job, kun, foydalanuvchi, mavzu)` — shartnomadagi `DeliveryKey`. `claim` (lease bilan, qisqa tranzaksiya, **tarmoqdan oldin**) → ish → `complete`/`fail`. Tugagan kalit hech qachon qayta berilmaydi (takroriy kunlik ishga tushirish dublikat signal va xabar yaratmaydi), qulagan ishchining egallashi lease'dan keyin qayta olinadi, xato kalit darhol qayta uriniladi. Xuddi shu jadval to'rt narsani saqlaydi: kalitlar; "qachon bo'lgan" tamg'alari (cooldown, qamrov); har job'ning oxirgi natijasi (`/holat`); oxirgi 100 xato. Xabarga havola (`chat:message`) ham shu yerda: keyingi yangilanish unga javob bo'ladi.
- **So'rov id lari** (`recalc_query`) va **`place_bet` tokenlari** ham ledger'da (restartdan keyin ishlaydi). Eslatmalar (`add_note`) ham — chunki `set_result_note` faqat yopilgan signalga yozadi, shartnoma esa kelajak signalga ham izoh ruxsat beradi.
- Jadval hozir `CREATE TABLE IF NOT EXISTS` bilan yaratiladi (`Ledger.ensure_schema`); **data-engineer Alembic migratsiyasini qabul qilsin** (so'rov pastda).

### 4. Rejalashtiruvchi
APScheduler 3 `AsyncIOScheduler` + `SQLAlchemyJobStore` (shu baza fayli): jadval restartdan keyin saqlanadi, `replace_existing` ro'yxatdan o'tishni idempotent qiladi. Joblar: `settle` 08:00, `refresh` 08:30 (ko'rsatish vaqt mintaqasida), `daily-tick` va `recalc-tick` har daqiqa. **Foydalanuvchi vaqti — tick'da hal qilinadi**, har foydalanuvchiga alohida job emas: sozlama o'zgarsa darhol amal qiladi, job'larni qayta ro'yxatdan o'tkazish kerak emas, restartdan keyingi "o'tkazib yuborilgan" xabar `daily_catch_up` (12 soat) ichida yuboriladi. `misfire_grace_time` (kunlik joblar 6 soat, tick 60 s), `coalesce`, `max_instances=1`. Job xatosi ushlanadi, ledger'ga yoziladi, adminga ogohlantirish ketadi; rejalashtiruvchi yiqilmaydi. `stop()` ishlayotgan joblarni kutadi. APScheduler job'ni pickle qiladi, shuning uchun job — modul darajasidagi `execute_job(name)`, jonli obyektlar o'rnatilgan `PipelineJobs` dan olinadi.

### 5. Qayta hisoblash va pre-match
`recalc_signals`: o'yin bo'yicha guruhlab bitta olish + bitta model; yangi versiya (`signal_versions`), verdikt **yaratilish chegaralari** bo'yicha; zaxira tugasa hech narsa olinmaydi, saqlangan raqamlar qayta baholanadi va `data_as_of` qo'yiladi; qoldirilgan/bekor/to'xtatilgan o'yin signalni `cancelled` qiladi. `scheduled_recalcs` (24 soat va N daqiqa oldin) tavsiya qilinganda navbatga qo'yiladi; `claim_due_recalcs` lease egasi `claimed_at` bilan `complete/release` qiladi (data-engineer v1.3). 24 soatlik yangilanish faqat o'zgarish bo'lsa (verdikt yoki koeffitsient ≥ 10%) xabar beradi; pre-match har doim. Tinch soatda xabar tashlanadi, qayta hisob bajariladi.

### 6. Natija, CLV, stavkalar
`settle_selection` (Osiyo yarim natijalari ham) → `Outcome`; foyda = `stake_units × settlement_returns`; CLV = `ln(olingan / de-vig yopilish adolatli)`, yopilish — `closing` snapshot, bo'lmasa oxirgi `prematch` (provayder yopilishni bermaydi; eng yaqin narsa). Osiyo chiziqlari reference'siz → CLV yo'q. Foydalanuvchi stavkalari o'z koeffitsienti va summasi bilan yopiladi; qo'lda natija (`manual`) avtomatik natija bilan almashtirilmaydi, stavkalar unga ergashadi (cashout — birliklarni stavkaga proporsional). Tugagan, lekin natijasiz o'yin keyingi ishga qadar kutadi; bekor/to'xtatilgan — void.

### 7. Real servislar va LLM
`app.services.real`: har protokol pipeline + `app.pipeline.data` (servislar uchun yagona, ochiq ro'yxatli saqlash eshigi; import-linter `services → pipeline` qoidasi saqlanadi) ustida. "Bugun"ni servis injeksiya qilingan `Clock` dan oladi. Hisobot arifmetikasi `report_math` da fake bilan **umumiy**. `NotifierDelivery` pipeline'ning ichki `Delivery` ni shartnomadagi `Notifier` ga o'giradi; bot `Notifier` ni bermaguncha `LogOnlyNotifier` faqat loglaydi.
LLM: `CandidateReviewer` protokoli, haqiqiy amalga oshirish `LlmCandidateReviewer(LlmService)` (≤ 10 ta nomzod/so'rov, `fallback` = fikr yo'q). Timeout/xato → signal verdiktsiz ketadi. Qaror #14: `rad` → tavsiya qilinmaydi (saqlanadi, `no_value`, faqat maxsus tahlil/sevimlilarda ❗), `ehtiyot` → tavsiya qilinadi, verdikt va izoh saqlanadi, `tasdiq` → oddiy.

### 8. Ulash
`APP_SERVICES=real` (`config.py` Literal ga "real" qo'shildi, standart `fake`). `main.run_real`: `open_runtime` (engine + provayder, kalit yo'q bo'lsa faqat-baza rejimi) → `create_real_services` → rejalashtiruvchi → bot; chiqishda rejalashtiruvchi to'xtatiladi, baza yopiladi. `python -m app.pipeline.refresh`: chiqish kodlari 0/1/2, kalit yo'q bo'lsa aniq xabar.

## Ko'rib chiqilgan muqobillar
- **Har foydalanuvchi uchun alohida cron job:** sozlama o'zgarganda qayta ro'yxatdan o'tkazish va farqlarni boshqarish kerak; tick + ledger oddiyroq va restartga chidamli.
- **Idempotentlikni APScheduler'ga topshirish:** u faqat "bir marta ishga tushirish"ni biladi, "bu xabar yuborildi"ni emas.
- **Pipeline `Notifier` ni to'g'ridan-to'g'ri ishlatishi:** `pipeline → services` teskari import (taqiqlangan); shuning uchun ichki `Delivery` + adapter.
- **Ledger o'rniga yangi jadvallar har biri uchun:** migratsiya va sxema bo'yicha kelishuv talab qilardi; bitta umumiy jadval hozircha yetarli.

## Oqibatlar
- `contract_suite` real servislarda ham o'tadi (62 test, seed qilingan SQLite, injeksiya qilingan soat, vaqt o'tganda natijalar ochiladigan "natija tasmasi").
- Hisobotlar `recommended_at` bor, retro bo'lmagan signallardan (yashirilganlar ham) hisoblanadi; ROI — foydalanuvchining shu davr signallariga qilgan barcha yopilgan stavkalari.
- **Cheklovlar:** jarohat/tarkib faqat `lineups_confirmed` bayrog'i (jarohatlar kartaga ulanmagan: `absences=()`); xG faqat yangi tugaganlar uchun va byudjet bilan cheklangan; shablonlarning jadvali saqlanadi, lekin avtomatik ishga tushirilmaydi; bot `Notifier` amalga oshirilmaguncha kunlik xabarlar faqat loglanadi.

## Boshqa agentlarga so'rovlar
- **data-engineer:** (1) `pipeline_ledger` jadvali uchun Alembic migratsiyasi (`app/pipeline/ledger.py` dagi `ledger_table`); (2) `saved_queries` ga `per_day`, `include_no_value` va tuzilgan jadval ustunlari (hozir `schedule_cron` matniga `# per_day no_value` sifatida yoziladi); (3) `signals`/`results` uchun eslatmalar jadvali va `user_bets.client_token` (hozir ledger'da); (4) `app.pipeline.queries` dagi o'qishlarni (`list_leagues`, `get_team`, `get_league`, `league_refs`, `team_refs`, `league_teams`, `user_ids`) `app.db.repo` ga ko'chirish.
- **bot-engineer:** `Notifier` ni Telegram'da amalga oshirish (`send_*` + `MessageRef`, `reply_to`, bloklangan foydalanuvchi → `None`), `BusyError` / `ServiceUnavailableError` xabarlari, `place_bet` ga `client_token=callback.id`, `recalc_window` bilan `/qayta`, `get_signals` bilan ro'yxatlar, `BankView` semantikasi.
- **llm-engineer:** `ExperienceContext` hozircha `None` (F8 to'ldiradi); `CandidateInput.forms/absences` pipeline tomonidan hali to'ldirilmaydi — jarohat/forma ma'lumotini pipeline'ga ulash F5/F8 da kelishiladi.


## Qo'shimcha (T4 fix round, 2026-10-22): kod sharhi va QA topilmalari

Sharh (R1-R7) va QA (F-1...F-12) natijasida qabul qilingan qarorlar. Ular yuqoridagi qarorlarni almashtirmaydi, aniqlashtiradi.

1. **Retro kartalar haqiqiy signallarni siqib chiqarmaydi (R1).** Kunlik so'rov `skip_retro=True` (boshlangan/tugagan o'yinlar umuman tahlil qilinmaydi). Qo'lda so'rovda retro tanlovlar `max_signals` kesimidan alohida byudjet oladi, `matched` va `fixtures_analyzed` ga kirmaydi; faqat butun oyna retro bo'lsa (o'tgan kunlarga qarash) ular natija hisoblanadi.
2. **Stavka qayta hisobda yo'qolmaydi (R2).** Tavsiya qilingan stavka har qayta hisobda yangi raqamlardan o'sha staking qoidasi (`stake_units`: flat/Kelly) bilan qayta hisoblanadi; EV <= 0 bo'lsa oldingi tavsiya qilingan stavka saqlanadi (bir marta nolga tushib, abadiy 0 bo'lib qolmaydi). Kelly uchun sun'iy `max(units, 0.1)` olib tashlandi; staking rejimi `STAKING_MODE` sozlamasidan (`flat`/`kelly`).
3. **Yopilgan signalga stavka yo'q (R3).** `place_bet` yopilgan, bekor qilingan yoki boshlangan o'yin signaliga `ValidationFailedError`; takroriy `client_token` birinchi stavkani qaytaradi. Settlement qo'shimcha ravishda natijasi bor signallarning ochiq stavkalarini yopadi (belt and braces).
4. **Yetkazish kamida bir marta, kalit bo'yicha dedupe (R4).** `Delivery` `None` qaytarsa kalit `done` bo'lmaydi. `Ledger.fail` urinishlarni sanaydi, `retry_after = min(1 daq * 2^(n-1), 30 daq)`, 5-urinishdan keyin `exhausted` (qayta berilmaydi) va admin ogohlantirishi: oddiy xato va "voz kechdi" alohida throttle kalitlarida (30 daq), shuning uchun yakuniy ogohlantirish oldingisi bilan yutilmaydi. Kunlik xabar tahlili birinchi urinishda saqlanadi (`daily.outcome`: signal id lari va hisoblagichlar), qayta urinish LLM/tahlilni takrorlamaydi. Pre-match vazifasi darhol qayta egallanmaydi: `run_at` backoff bilan suriladi (`reschedule_recalc`), o'zgarmagan raqamlar yangi `signal_versions` qatorini yozmaydi; 24 soatlik yangilanish foydalanuvchi *oxirgi ko'rgan* raqamlarga (ledger `notified`) nisbatan baholanadi, shuning uchun muvaffaqiyatsiz yuborish o'zgarishni yutib yubormaydi. Natija xabari ham shu mexanizmda (qayta urinish `due_retries` orqali, qolgan yopishlarni to'xtatmaydi). `claim` endi compare-and-set; `purge_expired` eski kalitlarni (35 kun) va `query:`/`msg:` yozuvlarini tozalaydi (har kuni settle bilan).
5. **Rejalashtiruvchi to'xtashi va o'tkazib yuborilgan vazifalar (R5, F-2).** `stop()` avval tikni to'xtatadi (`pause`), ishlayotgan joblarni o'zi kuzatadi (`JobTracker`, `shield`) va timeout (60 s) gacha kutadi, keyin `shutdown`. Joblar faqat mavjud bo'lmaganda (yoki trigger o'zgarganda) qo'shiladi: `replace_existing` har startda `next_run_time` ni qayta hisoblab o'tkazib yuborilgan `settle`/`refresh` ni yo'qotardi; endi `start(paused=True)` -> ro'yxatga olish -> `resume()`.
6. **Eskirgan narx (R6).** Qaror 1xBet narxi `max_quote_age` (6 soat = 2 x `odds_ttl`) dan eski bo'lsa o'yin o'tkazib yuboriladi (`skipped_stale_price`); anchor (Pinnacle) narxi bet narxidan `max_quote_skew` (3 soat) dan ko'p farq qilsa ishlatilmaydi (bozor anchorsiz). Retro kartalar uchun yoshi tekshirilmaydi. Kartalarda `data_as_of` = bukmeker narxi vaqti (`signals.quoted_at`).
7. **Foyda/CLV birinchi tavsiya qilingan koeffitsientda (R7, qaror #17).** `signals.recommended_odds` o'zgarmas; `signals.odds` qayta hisoblar bilan yangilanadi. Signal foydasi va CLV `first_odds` da; foydalanuvchi stavkasi `odds_taken` da. `closing_odds` — pick bukmekerining o'z yopilish narxi (alifbo tartibi emas), CLV — Pinnacle de-vig yopilishiga nisbatan; kickoffdan keyingi narxlar hisobga olinmaydi (`latest_odds(taken_before=kickoff)`).
8. **Pul arifmetikasi.** `profit_of` `Decimal(str(odds))` bilan (`float` shovqinisiz). To'xtatilgan/bekor qilingan o'yin hisobi bo'lsa ham `VOID` (F-1). Qoldirilgan o'yin kutadi.
9. **Qayta hisobda narx yo'qolsa `NO_PRICE`**; faqat anchor bilan tasdiqlangan nomzod `still_valid` bo'la oladi.
10. **Refresh.** O'tkazib yuborilgan qadam bo'lsa kun `done` emas: kalit `fail` (backoff), `recalc-tick` `refresh(retry_only=True)` bilan qayta uradi; kutilmagan istisno `fail()` qiladi; `xg_per_refresh` endi haqiqatan ishlatiladi (`max_requests`); CLI noto'g'ri sozlamada 2 kod bilan chiqadi.

**Kechiktirilgan (kod yo'q):** jobstore sinxron SQLite bloklashi (MemoryJobStore + ro'yxatga olish varianti), foydalanuvchi bo'yicha cooldown, batafsil kartadagi `as_of` nomuvofiqligi. **Deploy eslatmasi:** `uv run alembic upgrade head` ilovadan oldin bajarilishi shart (`pipeline_ledger` yangi ustunlari); `Ledger.ensure_schema` faqat migratsiyadan o'tmagan bo'sh baza uchun.
