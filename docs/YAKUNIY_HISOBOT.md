# Yakuniy hisobot — AI Sport Tahlilchisi (v1.0.0-rc1)

> Sana: 2026-10-26. Holat: **kod bo'yicha tayyor**; haqiqiy kalitlar, jonli sinov va paper trading sizning ishlaringiz.

## 1. Nima qurildi
Telegram maslahatchi bot (avtomatik stavka qo'ymaydi): statistik model (Dixon-Coles + bozor modeli, Pinnacle de-vig) value stavkalarni topadi, Gemini sifat kontekstini qo'shadi, stavkani siz qo'lda qo'yasiz.

- Sport tanlash, o'yinlar soni, sana oralig'i, jamoa/o'yin tanlash, ishonch/EV chegaralari, kunlik avtomatik tahlil va qayta hisoblash.
- Stavkalarni belgilash (o'tdi/o'tmadi/cashout), bank, zarar limitlari, hisobot, grafiklar, CSV, retro-tahlil, ekspress.
- O'rganish halqasi: post-mortem, saboqlar, segment statistikasi, kalibratsiya (`LEARNING_MODE=shadow` standart).
- Operatsion qism: Docker, CI/CD (GHCR → SSH → zaxira → healthcheck → rollback), zaxira/tiklash, qorovul, RUNBOOK.

## 2. Qanday ishga tushiriladi
- **Lokal sinov** (kalitsiz, soxta ma'lumot): `.env` da `TELEGRAM_BOT_TOKEN` va `ADMIN_IDS`, so'ng `docker compose -f docker-compose.local.yml up --build`.
- **Server**: `docker-compose.yml` + [RUNBOOK.md](../RUNBOOK.md) (`deploy/bootstrap.sh`).
- Bot long polling bilan ishlaydi (webhook yo'q — ataylab).

## 3. Tekshiruv natijalari
- Testlar: 4324 (4161 tezkor + 163 sekin), 0 xfail; qamrov ≈ 97.9% (`models` 99.8%, `pipeline` 97.2%).
- ruff, `mypy --strict`, import-linter (6 shartnoma), deptry: yashil.
- Mustaqil QA ([qa/T6-acceptance.md](qa/T6-acceptance.md)): 0 ta 🔴, 🟠/🟡 yopilgan.
- fix-C ustidan alohida qayta sharh **o'tkazilmadi** (sharhlovchi agent sessiya uzilishida to'xtagan); uning o'rniga mustaqil QA va to'liq test to'plami xizmat qildi.

## 4. Ochiq xavflar (halol ro'yxat)
1. API-Football, Gemini va Telegram bilan **jonli ishlab ko'rilmagan** (sintetik fixture va soxta serverlar).
2. Osiyo fora konvensiyasi (`HANDICAP_CONVENTION`) haqiqiy narxlarga qarshi tasdiqlanmagan.
3. F3 darvozasi (model bozordan yaxshimi) haqiqiy tarixiy ma'lumotda **ishga tushirilmagan** (football-data.co.uk bu tarmoqda bloklangan).
4. `deploy/bootstrap.sh` va `deploy.yml` serverda sinalmagan, faqat statik/test bilan.
5. Kunlik xabar: jarayon o'lsa bitta xabar takrorlanishi mumkin (yo'qotishdan afzal; ADR 0019).
6. Baza hajmi, kesh, Gemini kechikishi — taxminiy.
7. Model ustunligi isbotlanmagan: darvozadan o'tmasa, faqat bozor modeli rejimi.

## 5. Sizning qadamlaringiz (tartib bilan)
1. `.env`: `TELEGRAM_BOT_TOKEN`, `ADMIN_IDS`, `API_FOOTBALL_KEY`, `GEMINI_API_KEY`.
2. `python scripts/probe_api.py` → natijaga qarab `HANDICAP_CONVENTION=home_line|own_line`.
3. Telegram ochiq tarmoqda `docs/test_scenarios.md` bo'yicha jonli sinov (avval `APP_SERVICES=fake`, keyin `real`).
4. Filtrsiz tarmoq/serverda `import_history.py` → `backtest.py` (F3 darvozasi).
5. VPS, GitHub secret'lari, birinchi deploy, tiklash mashqi (RUNBOOK 3-bo'lim).
6. F10 paper trading 4–6 hafta, pulsiz. Real pul (F11) va `LEARNING_MODE=apply` — faqat sizning qaroringiz.

Batafsil: [HOLAT.md](HOLAT.md), [decisions_log.md](decisions_log.md), [ochiq_masalalar.md](ochiq_masalalar.md).
