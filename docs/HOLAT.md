# Loyiha holati — bajarilgan va qolgan ishlar

> Holat sanasi: **2026-10-23** · So'nggi teg: `v0.6.0` · `pyproject` versiyasi: `0.6.1` (keyingi teg) · Joriy to'lqin: **T6** (yakuniy integratsiya, sharh tuzatishlari)
> Batafsil vazifalar ro'yxati: [progress.md](progress.md) · Qarorlar: [decisions_log.md](decisions_log.md) · Ochiq masalalar: [ochiq_masalalar.md](ochiq_masalalar.md) · Server: [RUNBOOK.md](../RUNBOOK.md)

## Qisqacha

| Ko'rsatkich | Qiymat |
|---|---|
| To'lqinlar | T1 ✅ · T2 ✅ · T3 ✅ · T4 ✅ · T5 ✅ · **T6 jarayonda** (tuzatish raundlari va mustaqil QA) |
| Teglar | `v0.1.0` … `v0.6.0` (har to'lqin yopilganda) |
| Testlar | ≈ **3813** (`main`), 0 xfail; `slow` belgisi bilan ≈ 94 tasi ajratilgan (CI da alohida ish) |
| Sifat darvozalari | ruff, `mypy --strict`, import-linter (6 shartnoma), deptry, pip-audit, gitleaks, qamrov (umumiy ≥ 85%, `models`/`pipeline` ≥ 90%): CI da majburiy |
| Kod | `src/app/` (src layout), Python 3.13, aiogram 3, SQLAlchemy 2 (async) + SQLite |
| Docker image | ≈ 768 MB (pandas va scikit-learn runtime'dan olib tashlangan), non-root, health probe, kod `3` bilan o'z-o'zini qayta ishga tushirish |
| Agentlar modeli | Sonnet 5.5 (qaror #13) |
| Serverda ishga tushirilganmi | **Yo'q.** Hamma narsa skript, workflow va testlar bilan tayyor; birinchi deploy foydalanuvchida ([RUNBOOK.md](../RUNBOOK.md)) |

---

## ✅ Bajarilgan ishlar

### T1 — Loyiha skeleti (`v0.1.0`)
- `uv`, `pyproject.toml`, Docker (multi-stage, non-root), docker-compose, GitHub Actions CI, pre-commit, kod sifati darvozalari.
- Konfiguratsiya kalitlarsiz ishga tushadi; kalit kerak bo'lgan komponent aniq xato beradi. Loglar JSON, maxfiy qiymatlar yashiriladi.
- Minimal bot: whitelist, faqat shaxsiy chat.

### T2 — Shartnomalar va ma'lumotlar (`v0.2.0`)
- Domen modellari, baza sxemasi (Alembic), tarixiy import (football-data.co.uk, idempotent), model yadrosi (de-vig, Dixon-Coles, bozorlar, EV, Kelly), servis shartnomasi + soxta servis.

### T3 — Model, backtest, API klient, bot MVP (`v0.3.0`)
- Bozor modeli + ansambl + ishonch darajasi; walk-forward backtest (leakage mustaqil tajribalar bilan tasdiqlangan), kuchaytirilgan F3 darvozasi (CLV > 0, 95% CI).
- API-Football klienti (limit, retry, kesh, kvota), futbol adapteri, jamoalarni moslashtirish, lotin/kirill qidiruv.
- Bot MVP: menyu, sozlamalar, tahlil wizardi, `/tahlil`, signal kartalari, sahifalash.

### T4 — Haqiqiy servislar (`v0.4.0`)
- Pipeline: kunlik yangilash, tahlil, qayta hisoblash (24 soat / pre-match), natijalarni hisoblash (settle, CLV), APScheduler, `Notifier` porti, `/holat`.
- Gemini integratsiyasi: promptlar, javob sxemasi, fallback, gallyutsinatsiyadan himoya, etalon eval to'plami, kunlik limit.

### T5 — Kengaytmalar va deploy (`v0.5.0`)
- F7: maxsus tahlil, batafsil karta, sevimlilar, shablonlar (jadvalli), stavkalar va bank (zarar limitlari), hisobot, grafiklar, CSV eksport, retro-tahlil, ekspress.
- F8: natijalarni qo'lda belgilash, kecha yakuni, post-mortem, saboqlar, segment statistikasi, kalibratsiya, o'rganish halqasi (`shadow` standart), `/ozgarishlar`.
- F9: server tayyorlash skripti, GHCR + SSH deploy, avtomatik rollback, zaxira/tiklash (avtomatik sinalgan), healthcheck, healthchecks.io, log rotatsiyasi, `RUNBOOK.md`, FSM uchun persistent storage.
- Korporativ TLS-proksi qo'llab-quvvatlashi (`v0.6.0`, [ADR 0017](adr/0017-tls-trust-configuration.md)).

### T6 — Yakuniy integratsiya (jarayonda)
- Butun kod bazasi sharhi (xavfsizlik va arxitektura/ops) o'tkazildi; topilmalar ikki parallel raundda tuzatilmoqda:
  - **fix-a** (pipeline/servislar): qarorlar #19–#23 (ochiq stavka, shadow sofligi, void aylanma, cashout, shadow → apply).
  - **fix-b** (infratuzilma, [ADR 0018](adr/0018-t6-ops-hardening.md)): sirlar (`HEALTHCHECKS_URL`), deploy/CI qattiqlashtirish, zaxira xavfsizligi, alohida `jobs.db`, rejalashtiruvchi bot ulangandan keyin start, ichki qorovul, versiya (`app_started`), `HANDICAP_CONVENTION` majburiy tasdig'i, bog'liqliklarni tozalash, Dependabot va haftalik `pip-audit`.
- Mustaqil QA va yakuniy hisobot (`docs/YAKUNIY_HISOBOT.md`) tuzatishlar birlashgandan keyin.

---

## ⏳ Qolgan ishlar

### Loyiha tomonida (T6 yopilishi uchun)
- [ ] fix-a va fix-b ni birlashtirish, mustaqil QA (test-engineer) va qayta sharh (code-reviewer).
- [ ] `/holat` da build versiyasini ko'rsatish (bot/servis qatlami `app.ops.version.get_version()` ni chaqiradi; [ADR 0018](adr/0018-t6-ops-hardening.md) §4).
- [ ] `pipeline/retention.py` job'ini rejalashtiruvchiga ulash (pipeline-engineer yozgach).
- [ ] `v0.6.x` tegi va `docs/YAKUNIY_HISOBOT.md`.

### Sizdan kerak bo'ladigan ishlar (foydalanuvchiga bog'liq, men bajara olmayman)
1. **Kalitlar:** `TELEGRAM_BOT_TOKEN`, `ADMIN_IDS`, `API_FOOTBALL_KEY`, `GEMINI_API_KEY` (`.env` ga; hech qachon gitga emas).
2. **F0 probe:** `scripts/probe_api.py` — API imkoniyatlari, liga ID lari va **Osiyo fora yozilishi**. Natijaga qarab `HANDICAP_CONVENTION=home_line|own_line` ni `.env` ga yozing: usiz `APP_SERVICES=real` ishga tushmaydi, birinchi koeffitsientdan keyin o'zgartirib bo'lmaydi.
3. **Haqiqiy backtest (F3 darvozasi)** — korporativ filtrsiz tarmoqda yoki serverda: `scripts/import_history.py` → `scripts/backtest.py` (qaror #7). Darvozadan o'tmasa real rejimga o'tilmaydi.
4. **Jonli Telegram sinovi** — [test_scenarios.md](test_scenarios.md) bo'yicha (bu mashinada Telegram firewall bilan bloklangan).
5. **Server:** VPS (≈ 2 GB RAM), `deploy/bootstrap.sh`, GitHub secret'lari (`SSH_HOST`, `SSH_USER`, `SSH_KNOWN_HOSTS`, `production` environment da `SSH_KEY`), birinchi deploy, reboot va tiklash mashqi ([RUNBOOK.md](../RUNBOOK.md) 3-bo'lim).
6. **Masofaviy zaxira:** `rclone crypt` remote sozlash ([RUNBOOK.md](../RUNBOOK.md) 8-bo'lim), healthchecks.io check yaratish.
7. **F10 paper trading** (4–6 hafta, real pulsiz) → F10 darvozasi → F11 production (`v1.0.0`): **faqat sizning qaroringiz bilan**. `LEARNING_MODE=apply` ga o'tish ham odam qarori.

---

## ⚠️ Muhim cheklovlar va xavflar

| # | Masala | Ta'siri | Yechim |
|---|---|---|---|
| 1 | Korporativ veb-filtr football-data.co.uk ni, firewall Telegram'ni bloklaydi | Haqiqiy backtest va jonli bot bu kompyuterda ishlamaydi | Serverda / boshqa tarmoqda ishga tushirish |
| 2 | API kalitlari yo'q | API-Football test ma'lumotlari sxema asosida sintetik; jonli sinov qilinmagan | F0 da `probe_api.py` bilan qayta yozib olish |
| 3 | Model bozordan yaxshiroq ekani isbotlanmagan | Haqiqiy ustunlik faqat haqiqiy ma'lumotda ko'rinadi | F3 darvozasi → F10 paper trading; o'tmasa — bozor modeli rejimi |
| 4 | Osiyo forasi konvensiyasi tasdiqlanmagan | Noto'g'ri bo'lsa AH koeffitsientlari teskari saqlanadi | `HANDICAP_CONVENTION` majburiy tasdiqlanadi; F0 probe birinchi narx saqlanishidan oldin |
| 5 | Deploy serverda hali sinalmagan | Skriptlar va workflow faqat testlar bilan tasdiqlangan | Birinchi deploy'ni kuzatib turish, RUNBOOK 3.6 tekshiruvlari |
| 6 | Test to'plami katta (to'liq ≈ 6–17 daqiqa) | Lokal sikl sekin | `pytest -m "not slow"` tezkor; CI da tezkor va sekin ishlar alohida |

---

## Asosiy qarorlar (to'liq ro'yxat: [decisions_log.md](decisions_log.md))
- Bot faqat shaxsiy chatda ishlaydi (#4); standart sozlamalar "Balanslangan" profil (#8).
- Stavka narxi = 1xBet, adolatli ehtimollik = Pinnacle (#10); F3 darvozasi statistik kuchaytirilgan (#11).
- Retro: oyna ≤ 31 kun, orqaga ≤ 90 kun (#12); agentlar Sonnet 5.5 da (#13).
- Foyda va CLV birinchi tavsiya koeffitsientida (#17); cashout = to'langan pul summasi (#18).
- T6: ochiq stavkali signal doim yopiladi (#19), `shadow` rejimi sof (#20), void aylanmaga kirmaydi (#21), cashout bitta stavkaga tegishli (#22), `shadow → apply` o'tishi jurnalga yoziladi (#23).
