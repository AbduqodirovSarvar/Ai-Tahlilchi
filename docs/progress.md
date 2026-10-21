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
- [~] F0.7 `scripts/probe_api.py` tekshiruv skripti (data-engineer; kalit kerak)
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
- [x] F2.1 SQLAlchemy modellari (18 jadval) + Alembic `0001` (upgrade/downgrade testlangan), engine (WAL, busy_timeout, foreign_keys), domen ⇄ ORM mapperlar, idempotent upsert (data-engineer)
- [x] F2.4 `SportAdapter` protokoli va domen modellari: `docs/contracts.md` 1–3 frozen v1 (orkestr tasdig'i kutilmoqda) (data-engineer)
- [~] F2.7 Tarixiy CSV importi: kod va testlar tayyor (`scripts/import_history.py`, offline/kesh, idempotent). Haqiqiy ma'lumot bilan ishga tushirish **bu mashinada bloklangan**: korporativ veb-filtr football-data.co.uk ni "Gambling" toifasida to'sadi. CSV'larni boshqa tarmoqdan `data/history/<mavsum>/<liga>.csv` ga qo'yib `--offline` bilan yoki serverda ishga tushirish kerak (data-engineer)
- [ ] `services/interfaces.py` va `services/fake.py` (pipeline-engineer)
- [ ] Shartnomalarni qotirish (`docs/contracts.md` v1) (orkestr)

## T3 — Asosiy qurilish
- [ ] F2.2 Repository qatlami
- [ ] F2.3 Bazaviy API klient (rate limit, retry, limit himoyasi, kesh)
- [ ] F2.5 Futbol adapteri
- [ ] F2.6 Jamoa nomlarini normallashtirish va qidiruv
- [ ] F2.8 Ma'lumotlar qatlami testlari
- [ ] F3.1 De-vig (proporsional, Shin)
- [ ] F3.2 Dixon-Coles modeli va bozorlar
- [ ] F3.3 Bozor modeli
- [ ] F3.4 Ansambl
- [ ] F3.5 EV va staking
- [ ] F3.6 Ishonch darajasi
- [ ] F3.7 Walk-forward backtest
- [ ] F3.8 Backtest hisoboti
- [ ] F3.9 Model unit testlari
- [ ] 🚦 F3 darvozasi
- [ ] F6.1 Bot karkasi: routerlar, middleware
- [ ] F6.2 Asosiy menyu, `/start`, `/yordam`
- [ ] F6.3 Sozlamalar
- [ ] F6.4 Tahlil oqimi (FSM)
- [ ] F6.5 `/tahlil` tezkor buyrug'i parseri
- [ ] F6.6 Signal kartasi formatteri
- [ ] F6.10 Telegram cheklovlari

## T4 — Tizimni birlashtirish
- [ ] F4.1 `pipeline/refresh.py`
- [ ] F4.2 `pipeline/analyze.py`
- [ ] F4.3 `pipeline/daily.py`
- [ ] F4.4 `pipeline/recalc.py`
- [ ] F4.5 `pipeline/prematch.py`, `scheduled_recalcs`
- [ ] F4.6 `pipeline/settle.py`
- [ ] F4.7 APScheduler (persistent, idempotent)
- [ ] F4.8 "Soxta kun" integratsion testi
- [ ] F5.1 `clients/gemini.py`
- [ ] F5.2 Prompt shablonlari
- [ ] F5.3 Kirish JSON yig'uvchi
- [ ] F5.4 Validatsiya, qayta urinish, fallback
- [ ] F5.5 Bepul tarif limitlari nazorati
- [ ] F5.6 Gemini testlari
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
