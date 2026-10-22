# Holat kuzatuvchisi (progress)

> Vazifa ID lari: [ishlab_chiqish_rejasi.md](../ishlab_chiqish_rejasi.md). To'lqinlar va egalik: [agentlar_rejasi.md](../agentlar_rejasi.md).
> Belgilar: `[x]` bajarildi · `[ ]` ochiq · `[~]` kechiktirilgan / qisman (izoh bilan).
> Vazifa faqat barcha darvozalar yashil bo'lganda (`pytest`, `ruff`, `mypy`, `lint-imports`) belgilanadi.

**Joriy to'lqin:** T1 (skelet): QA va sharh topilmalari tuzatildi (fix round 1), qayta tekshiruv kutilmoqda.

## T0 — Ma'lumot manbalarini tekshirish (F0)
> Kechiktirilgan: API kalitlari hali yo'q (qarang: [decisions_log.md](decisions_log.md)).

- [~] F0.1 Telegram bot tokeni va `user_id` (foydalanuvchi)
- [~] F0.2 API-Football kaliti (foydalanuvchi)
- [~] F0.3 Gemini API kaliti (foydalanuvchi)
- [~] F0.4 *(ixtiyoriy)* The Odds API kaliti (foydalanuvchi)
- [x] F0.5 GitHub repozitoriy (`origin` sozlangan)
- [x] F0.6 Lokal muhit: Python 3.13, `uv`, Git, Docker Desktop
- [~] F0.7 `scripts/probe_api.py` tekshiruv skripti: **kod tayyor va testlangan** (T3, data-engineer); kalitsiz aniq xabar bilan chiqadi (kod 2), hech narsa so'ramaydi. Haqiqiy ishga tushirish kalit qo'shilgach: `docs/api_probe_report.md` + `tests/fixtures/api_football/recorded/`
- [ ] F0.8 Ligalar ro'yxatini kelishish
- [~] F0.9 Tarixiy ma'lumot: football-data.co.uk CSV: import tayyor, yuklab olish korporativ tarmoqda bloklangan (F2.7 ga qarang)

## T1 — Skelet (F1) · infra-engineer
- [x] F1.1 Papka tuzilmasi (src layout), `pyproject.toml`, `uv.lock`
- [x] F1.2 `src/app/config.py` (pydantic-settings, `DISPLAY_TZ`), `.env.example`
- [x] F1.3 Loglash (`structlog`, JSON/dev, maxfiy qiymatlarni tozalash), xatolar ierarxiyasi, global xato ushlagich, chiqish kodlari 0/1/2
- [x] F1.4 Kod sifati darvozalari: ruff, mypy --strict, import-linter, pytest-cov, pip-audit, deptry, gitleaks, pre-commit
- [x] F1.5 `Dockerfile` (multi-stage, non-root), `docker-compose.yml`
- [x] F1.6 GitHub Actions CI (lint + test + security + docker)
- [x] F1.7 `README.md`
- [x] F1.8 Minimal bot `/start` → "Ishlayapti ✅" (whitelist + faqat shaxsiy chat): offline testlar bilan tasdiqlangan. Jonli Telegram sinovi token qo'shilgandan keyin (F0 kechiktirilgan)

## T2 — Shartnomalar
- [x] F2.1 SQLAlchemy modellari (18 jadval) + Alembic `0001` (upgrade/downgrade testlangan), engine (WAL, busy_timeout, foreign_keys), domen ⇄ ORM mapperlar, idempotent upsert (data-engineer). Fix round: Alembic `0002` (opening/closing yagona, loss limitlar, `user_bets.signal_id` indeksi), `merge_outcome` holat/hisob qoidalari
- [x] F2.4 `SportAdapter` protokoli va domen modellari: `docs/contracts.md` 1–3 frozen v1 (orkestr tasdig'i kutilmoqda) (data-engineer)
- [~] F2.7 Tarixiy CSV importi: kod va testlar tayyor (`scripts/import_history.py`, offline/kesh, idempotent). Haqiqiy ma'lumot bilan ishga tushirish **bu mashinada bloklangan**: korporativ veb-filtr football-data.co.uk ni "Gambling" toifasida to'sadi. CSV'larni boshqa tarmoqdan `data/history/<mavsum>/<liga>.csv` ga qo'yib `--offline` bilan yoki serverda ishga tushirish kerak (data-engineer). Fix round: QA F-1..F-5 va sharh R1, R3–R6 tuzatildi, strict xfail lar olib tashlandi
- [x] `services/interfaces.py` (DTO lar, 10 ta protokol, `Services`), `services/errors.py`, `services/fake.py` (deterministik fake) va qayta ishlatiladigan shartnoma testlari `tests/services/contract_suite.py`; `docs/contracts.md` §4–5 frozen v1 taklif, [ADR 0004](adr/0004-service-contract.md) (pipeline-engineer)
- [ ] Shartnomalarni qotirish (`docs/contracts.md` v1) (orkestr)

## T3 — Asosiy qurilish
- [x] Servis shartnomasi sharh tuzatishlari, v1.1 (faqat qo'shish): tavsiya/retro/yashirish qoidalari, `scope=all` → `settings.league_ids`, sevimlilar kesimdan tashqarida, `list_sports`, `utc_bounds`, horizon, `ReportRequest.recent`, ID ≤ 40 bayt, fake tuzatishlari, fake uchun `import-linter` qoidasi (pipeline-engineer)
- [x] F2.2 Repository qatlami: `app.db.repo` (o'yinlar, koeffitsientlar, signallar + versiyalar, natijalar, sozlamalar, sevimlilar, shablonlar, lease asosidagi qayta hisob navbati, stavkalar), `SqlUsageStore`; Alembic `0003` (`signals.recommended_at` + chegaralar, servis v1.1); [ADR 0007](adr/0007-api-client-and-repositories.md) (data-engineer)
- [x] F2.3 Bazaviy API klient: token bucket, `tenacity` retry (`Retry-After`), TTL kesh (10 daq / 6 soat), kunlik kvota + 30% qo'lda zaxira, sarlavhalar bilan moslash, `estimate_requests` (data-engineer)
- [~] F2.5 Futbol adapteri: `FootballAdapter` (`SportAdapter`), xaritalash, 10 liga ID si, jamoa moslash (alias + rapidfuzz). Kod va sintetik javoblardagi testlar tayyor; haqiqiy API bilan tekshiruv (AH yozilishi, liga ID lari) F0.7 ga bog'liq (data-engineer)
- [x] F2.6 Jamoa qidiruvi: `app.db.search` (o'zbek lotin/kirill transliteratsiya, rapidfuzz, prefiks ustunligi, hypothesis testlari) (data-engineer)
- [x] T3 fix round (data-engineer, 2026-10-22): kod sharhi va QA topilmalari (20 band) tuzatildi, strict xfail lar olib tashlandi (A1 pipeline to'lqini uchun qoldi): yarim tun reconcile, SQLite yozuvchi, play-off qayta o'yinlari, xG byudjeti, fuzzy moslash, ekzonimlar, single-flight, lease egaligi, probe/AH konvensiyasi ([ADR 0007](adr/0007-api-client-and-repositories.md) §12)
- [x] F2.8 Ma'lumotlar qatlami testlari: respx (har bir endpoint, sahifalash, retry, 429 + Retry-After, kvota zaxirasi, kesh TTL), xaritalash chegaraviy holatlari, adapter mosligi, repository (vaqtinchalik SQLite). Javoblar sintetik — F0 da qayta yoziladi (data-engineer)
- [x] F3.1 De-vig (proporsional, Shin, power): `models/devig.py` (T2, quant-modeler; [ADR 0003](adr/0003-dixon-coles-and-devig.md))
- [x] F3.2 Dixon-Coles modeli va bozorlar: `models/poisson.py`, `models/score_markets.py`, `models/asian.py` (T2); domen adapterlari `models/adapters.py`, `models/outcomes.py` (T3)
- [x] F3.3 Bozor modeli: `models/market.py` (Pinnacle de-vig, `market_avg` zaxira + noaniqlik bayrog'i, Osiyo butun/chorak chiziqlari, DC 1X2 dan) (T3; [ADR 0005](adr/0005-backtest-methodology.md))
- [x] F3.4 Ansambl: `models/ensemble.py` (to'liq natija taqsimotlari bo'yicha, `w` backtestda tanlanadi) (T3)
- [x] F3.5 EV va staking: `models/ev.py`, `models/staking.py` (Kelly 1/4, maksimal 2%, flat; Osiyo chiziqlari uchun ham); `asian_fair_odds` (T3)
- [x] F3.6 Ishonch darajasi: `models/confidence.py` (Yuqori/O'rta/Past, sabab kodlari, > 15 p.p. sanity bayrog'i) (T3)
- [x] F3.7 Walk-forward backtest: `app.models.backtest` (sof) + `scripts/backtest.py` (DB yoki `--synthetic`) (T3)
- [~] F3.8 Backtest hisoboti: sintetik samarali bozorda, qaror #11 darvozasi bilan: [standart](backtest/2026-10-21-synthetic.md) (w = 0 tanlandi, stavka yo'q) va [diagnostika, w > 0](backtest/2026-10-21-synthetic-model-only.md) (ROI ahamiyatsiz, CLV −3.2% ❌). T3 fix: QA F-1…F-5 va sharh topilmalari tuzatildi. **Haqiqiy ma'lumotdagi hisobot serverda** (qarorlar jurnali #7)
- [x] F3.9 Model unit testlari: adapterlar, bozor modeli, ansambl, ishonch, backtest (instrumentlangan leakage testlari, samarali bozor sanity testi, hypothesis); `src/app/models` qamrovi 99.7% (T3)
- [ ] 🚦 F3 darvozasi: **kutilmoqda** — haqiqiy football-data tarixi bilan serverda `scripts/import_history.py` + `scripts/backtest.py` (#7)
- [x] F6.1 Bot karkasi: routerlar (factory), middleware (whitelist, user konteksti, token-bucket rate limit, servis xatolari → o'zbekcha xabar), `Services` DI, `APP_SERVICES=fake` ([ADR 0006](adr/0006-bot-architecture.md)) (bot-engineer)
- [x] F6.2 Asosiy menyu (§6.1), `/start`, `/yordam`, `/bekor`; keyingi to'lqin bandlari "tez orada" bilan ulangan
- [x] F6.3 Sozlamalar: §6.5 dagi barcha maydonlar, risk profillari, 80%+ ogohlantirishi (jarayon xotirasida "bir marta")
- [x] F6.4 Tahlil oqimi (FSM): sana tugmalari + inline kalendar + matn, sport (`list_sports`), ehtimollik/EV, qamrov (ligalar/jamoalar/sahifalangan o'yinlar), son, bozorlar, saralash, tasdiqlash + `estimate_cost`, progress, shablon saqlash. FSM: MemoryStorage (persistent storage seam tayyor, eskirgan tugmalar ishlangan)
- [x] F6.5 `/tahlil` parseri (sof funksiya, unit + hypothesis testlar) → tasdiqlash ekrani
- [x] F6.6 Signal kartasi (§6.2, HTML escape, ⏳/❗/🧪/🕰 retro), kunlar bo'yicha guruhlash, ⭐ Sevimlilar bloki, sahifalash, shortfall va "Value topilmadi"; 🙈 Yashirish ishlaydi, 🔄/📋/✅ F6.7/F7 da
- [x] F6.10 Telegram cheklovlari: 4096 bo'lish, `callback_data` ≤ 64 (indekslar, property test), flood control (bir marta qayta urinish), har callbackga `answer()`. Qo'lda sinov: [test_scenarios.md](test_scenarios.md) (jonli sinov token qo'shilgandan keyin)

## T4 — Tizimni birlashtirish
- [x] F4.1 `pipeline/refresh.py`: fixtures/natijalar/koeffitsientlar (avtomatik kvota, qadam bo'yicha o'tkazib yuborish), `python -m app.pipeline.refresh` (kodlar 0/1/2, kalit yo'q xabari) ([ADR 0008](adr/0008-pipeline-and-real-services.md))
- [x] F4.2 `pipeline/analyze.py` + `valuation`/`selection`/`persist`/`modeling`: yagona tahlil yo'li (DC keshi, Pinnacle de-vig, 1xBet narxi, ishonch, flat/Kelly, LLM kancha: `rad` → tavsiya emas), real servislar `app.services.real`
- [x] F4.3 `pipeline/daily.py`: foydalanuvchi vaqtida, tinch soat, idempotent kalit, catch-up
- [x] F4.4 `pipeline/recalc.py`: diff + `signal_versions`, yaratilish chegaralari bo'yicha verdikt, zaxira tugasa `data_as_of`, bekor qilish
- [x] F4.5 `pipeline/prematch.py`, `scheduled_recalcs`: 24 soat va N daqiqa oldin, lease egasi bilan yakunlash, sevimlilar (tarkib, koeffitsient harakati)
- [x] F4.6 `pipeline/settle.py`: natija (Osiyo yarim natijalari), foyda, CLV, foydalanuvchi stavkalari, qo'lda natija ustuvor
- [x] F4.7 `app/scheduler.py`: APScheduler + SQLAlchemy jobstore, settle 08:00, refresh 08:30, daqiqalik tick'lar; restartdan keyin saqlanadi; `main` da bot bilan birga (`APP_SERVICES=real`)
- [x] F4.8 "Soxta kun" e2e (`tests/pipeline/test_day_e2e.py`: refresh → kunlik xabar → pre-match → natija → hisobot, qo'lda tekshirilgan foyda), contract suite real servislarda, idempotentlik, byudjet, LLM yo'q/`rad`/`ehtiyot`, rejalashtiruvchi restarti
- [x] F5.1 `clients/gemini.py` (async `google-genai` o'rami, `response_schema`, retry, token metrikasi; ADR 0009)
- [x] F5.2 Prompt shablonlari (`context_check.v1`, `postmortem.v1`, paket ma'lumoti, keshlangan yuklovchi)
- [x] F5.3 Kirish JSON yig'uvchi (delimitlangan ma'lumot bloki, deterministik token byudjeti, "Tajriba konteksti")
- [x] F5.4 Validatsiya, qayta urinish, fallback (`LlmService`; gallyutsinatsiya qo'rig'i; `postmortem` minimal)
- [x] F5.5 Bepul tarif limitlari nazorati (kunlik so'rov/token byudjeti, xotiradagi jurnal)
- [x] F5.6 Gemini testlari (mock; 26 golden holat, `scripts/llm_eval.py` jonli faqat qo'lda; jonli baholash kalit kelgach bajariladi)
- [ ] F6.7 Qayta hisoblash
- [ ] F6.8 Bugungi signallar va kunlik xabar
- [ ] F6.9 `/holat`

## T5 — Kengaytmalar va deploy
- [ ] F7.1 Maxsus tahlil
- [ ] F7.2 Batafsil karta
- [ ] F7.3 Sevimlilar va bildirishnomalar
- [ ] F7.4 Shablonlar
- [ ] F7.5 Mening stavkalarim va bank
- [ ] F7.6 Hisobot va eksport
- [ ] F7.7 Retro-tahlil
- [ ] F7.8 Ekspress tuzuvchi (ixtiyoriy)
- [ ] F8.3 Post-mortem
- [ ] F8.4 Saboqlar
- [ ] F8.5 `segment_stats`
- [ ] F8.6 Kalibratsiya
- [ ] F8.7 Promptga "Tajriba konteksti"
- [ ] F9.1 VPS xavfsizligi
- [ ] F9.2 Docker va `/opt/ai-tahlilchi`
- [ ] F9.3 Deploy workflow (GHCR + SSH + Alembic)
- [ ] F9.4 Zaxira va tiklash sinovi
- [ ] F9.5 Monitoring (healthcheck, healthchecks.io, admin ogohlantirish)
- [ ] F9.6 Log rotatsiyasi, disk kuzatuvi
- [ ] F9.7 `RUNBOOK.md`

## T6 — Yakuniy integratsiya
- [ ] F8.1 Natijalarni qo'lda belgilash
- [ ] F8.2 "Kecha yakuni" bloki
- [ ] F8.8 `/ozgarishlar` jurnali
- [ ] To'liq e2e va qabul mezonlarini qayta tekshirish (test-engineer)
- [ ] Butun kod bazasi sharhi (code-reviewer)
- [ ] Production deploy va zaxiradan tiklash sinovi (infra-engineer)

## F10 — Paper trading
- [ ] F10.1 Serverda to'liq rejim (faqat qog'ozda)
- [ ] F10.2 Haftalik ko'rib chiqish
- [ ] F10.3 UX tuzatishlari
- [ ] F10.4 Parametrlarni faqat haftalik o'zgartirish
- [ ] 🚦 F10 darvozasi

## F11 — Production (v1.0)
- [ ] F11.1 Reliz `v1.0.0`, `CHANGELOG.md`
- [ ] F11.2 Bank va zarar limitlari
- [ ] F11.3 Birinchi oy minimal stavkalar
- [ ] F11.4 Oylik ko'rib chiqish
