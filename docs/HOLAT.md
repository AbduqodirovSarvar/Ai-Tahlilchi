# Loyiha holati — bajarilgan va qolgan ishlar

> Holat sanasi: **2026-10-21** · `main`: `e378c40` · So'nggi teg: `v0.2.0` (`v0.3.0` T3 yopilganda qo'yiladi)
> Batafsil vazifalar ro'yxati: [progress.md](progress.md) · Qarorlar: [decisions_log.md](decisions_log.md) · Ochiq masalalar: [ochiq_masalalar.md](ochiq_masalalar.md)

## Qisqacha

| Ko'rsatkich | Qiymat |
|---|---|
| To'lqinlar | T1 ✅ · T2 ✅ · T3 ≈ 90% (data tuzatishlari qoldi) · T4 boshlangan · T5–T6 kutilmoqda |
| Testlar (`main`) | **1687 passed**, 0 xfail |
| Test qamrovi | ≈ 99% (umumiy), `app/models` ≈ 99.8% |
| Sifat darvozalari | ruff, `mypy --strict`, import-linter (6 shartnoma), deptry, pip-audit, gitleaks — yashil |
| Kod | `src/app/` (src layout), Python 3.13 |
| Agentlar modeli | Sonnet 5.5 (qaror #13) |

---

## ✅ Bajarilgan ishlar

### T1 — Loyiha skeleti (`v0.1.0`)
- Papka tuzilmasi, `uv`, `pyproject.toml`, Docker (non-root, multi-stage), docker-compose, GitHub Actions CI.
- Konfiguratsiya: kalitlarsiz ishga tushadi, kalit kerak bo'lgan komponent aniq xato beradi.
- Loglar: JSON, **maxfiy qiymatlar avtomatik yashiriladi** (token sizishi topilib tuzatilgan).
- Minimal bot: whitelist, faqat shaxsiy chat.

### T2 — Asosiy shartnomalar va ma'lumotlar (`v0.2.0`)
- **Domen modellari** (o'yin, jamoa, bozor, koeffitsient) — qotirilgan shartnoma.
- **Baza sxemasi**: 18 jadval, Alembic migratsiyalari (0001–0003).
- **Tarixiy import**: football-data.co.uk CSV (10 liga × 6 mavsum), idempotent.
- **Model yadrosi**: de-vig (proporsional/Shin/power), Dixon-Coles, barcha bozorlar (1X2, ikki natija, total, BTTS, Osiyo forasi — chorak chiziqlar bilan), EV, 1/4 Kelly.
- **Servis shartnomasi** (bot ↔ tizim) + soxta (fake) servis.

### T3 — Model, backtest, API klient, bot MVP
- **Statistik model**: bozor modeli (Pinnacle), ansambl, ishonch darajasi (Yuqori/O'rta/Past).
- **Walk-forward backtest**: kelajak ma'lumotidan foydalanmaslik (leakage) mustaqil tajribalar bilan tasdiqlangan; samarali bozorda soxta ustunlik ko'rsatmaydi.
- **F3 darvozasi kuchaytirildi** (qaror #11): CLV > 0 (95% CI bilan), faqat to'liq tugagan hold-out mavsum.
- **API-Football klienti**: limit, retry, kesh, kunlik kvota (30% qo'lda zaxira) — 10 liga yangilash ≈ 27 so'rov (chegara 40).
- **Futbol adapteri**, jamoalarni manbalararo moslashtirish, lotin/kirill qidiruv, repository qatlami.
- **Telegram bot MVP** (soxta servis ustida):
  - asosiy menyu, `/start`, `/yordam`, `/bekor`;
  - ⚙️ Sozlamalar — 16 maydon, risk profillari, 80%+ ogohlantirishi;
  - 📊 Tahlil wizardi — sana oralig'i (tugmalar + kalendar + matn), sport, ehtimollik, EV, qamrov (liga/jamoa/o'yin), son, bozorlar, saralash, API sarfi tasdig'i;
  - `/tahlil 25.10-31.10 futbol 60% Arsenal,Inter` tezkor buyrug'i;
  - signal kartalari, kunlar bo'yicha guruhlash, sahifalash, ⭐ Sevimlilar bloki;
  - bir vaqtdagi bosishlardan himoya, tahlil fonda, eskirgan tugmalar to'g'ri ishlanadi.
- Har bir qism: **kod sharhi + mustaqil QA + tuzatish raundi** o'tkazilgan.

---

## 🔄 Hozir bajarilmoqda

| Agent | Vazifa |
|---|---|
| data-engineer | T3 data tuzatishlari (20 band): yarim tunda kvota, SQLite qulfi, Belgiya play-off o'yinlari, xG byudjeti, jamoa moslashtirish chegarasi, kirill nomlar jadvali va boshqalar |
| pipeline-engineer | **T4 / F4**: haqiqiy servislar — kunlik yangilash, tahlil, qayta hisoblash, pre-match, natijalarni hisoblash (settle), scheduler, `Notifier` porti |
| llm-engineer | **T4 / F5**: Gemini integratsiyasi — promptlar, javob sxemasi, fallback, gallyutsinatsiyadan himoya, 20 ta etalon holatli eval to'plami |

---

## ⏳ Qilinishi kerak bo'lgan ishlar

### T4 (davomi)
- [ ] Pipeline va LLM natijalarini sharh + QA dan o'tkazish, tuzatish, birlashtirish.
- [ ] **F6.7–F6.9**: botni haqiqiy servisga ulash — 🔄 qayta hisoblash, ⚡ bugungi signallar, `/holat`.
- [ ] T3 data tuzatishlarini birlashtirish → **`v0.3.0`**; T4 yakunida **`v0.4.0`**.

### T5 — Kengaytmalar va deploy
- [ ] **F7**: 🔍 maxsus tahlil, 📋 batafsil karta, ⭐ sevimlilar va bildirishnomalar, 💾 shablonlar + jadval, 🎟 stavkalar va bank (zarar limitlari), 📈 hisobot + grafiklar + CSV eksport, retro-tahlil, 🧩 ekspress.
- [ ] **F8**: feedback loop — natijalarni belgilash, kecha yakuni, post-mortem, saboqlar, segment statistikasi, kalibratsiya, `/ozgarishlar`.
- [ ] **F9**: VPS, CI/CD deploy, zaxira va tiklash sinovi, monitoring, `RUNBOOK.md`, FSM uchun persistent storage.

### T6 — Yakuniy integratsiya
- [ ] To'liq e2e, barcha qabul mezonlarini qayta tekshirish, butun kod bazasi sharhi.
- [ ] `docs/YAKUNIY_HISOBOT.md`.

### Sizdan kerak bo'ladigan ishlar (loyiha tayyor bo'lgach)
1. `.env` ga kalitlar: `TELEGRAM_BOT_TOKEN`, `ADMIN_IDS`, `API_FOOTBALL_KEY`, `GEMINI_API_KEY`.
2. **F0 tekshiruvi**: `python -m uv run python scripts/probe_api.py` — API imkoniyatlari, Osiyo forasi yozilishi, liga ID lari tasdiqlanadi.
3. **Haqiqiy backtest (F3 darvozasi)** — korporativ filtrsiz tarmoqda yoki serverda:
   `scripts/import_history.py` → `scripts/backtest.py` (qaror #7).
4. Jonli Telegram sinovi — [test_scenarios.md](test_scenarios.md) bo'yicha.
5. VPS (≈ 2 GB RAM) va deploy ruxsati.
6. **F10 paper trading** (4–6 hafta, real pulsiz) → F11 production — **faqat sizning qaroringiz bilan**.

---

## ⚠️ Muhim cheklovlar va xavflar

| # | Masala | Ta'siri | Yechim |
|---|---|---|---|
| 1 | Korporativ veb-filtr football-data.co.uk ni bloklaydi | Haqiqiy tarixiy backtest bu kompyuterda ishlamaydi | Serverda / boshqa tarmoqda ishga tushirish |
| 2 | API kalitlari yo'q | API-Football test ma'lumotlari sxema asosida sintetik; jonli bot sinovi qilinmagan | F0 da `probe_api.py` bilan qayta yozib olish |
| 3 | Model bozordan yaxshiroq ekani isbotlanmagan | Haqiqiy ustunlik faqat haqiqiy ma'lumotda ko'rinadi | F3 darvozasi → F10 paper trading; o'tmasa — bozor modeli rejimi |
| 4 | Osiyo forasi yozilish konvensiyasi tasdiqlanmagan | Noto'g'ri bo'lsa AH koeffitsientlari teskari saqlanadi | F0 probe birinchi narx saqlanishidan oldin |
| 5 | Test to'plami katta (≈ 3–6 daqiqa) | CI sekinroq | `slow` belgisi bilan ajratish mumkin |

---

## Asosiy qarorlar (to'liq ro'yxat: [decisions_log.md](decisions_log.md))
- Bot faqat shaxsiy chatda ishlaydi (#4).
- Standart sozlamalar = "Balanslangan" profil: 45% / EV 5% / 1.50–3.00 (#8).
- Stavka narxi = 1xBet, adolatli ehtimollik = Pinnacle (#10).
- F3 darvozasi statistik jihatdan kuchaytirilgan (#11).
- Retro: oyna ≤ 31 kun, orqaga ≤ 90 kun (#12).
- Agentlar Sonnet 5.5 da (#13).
