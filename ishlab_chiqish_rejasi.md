# AI Sport Tahlilchisi — 0 dan Production gacha Ishlab Chiqish Rejasi

> Funksional talablar va arxitektura: [ai_sport_tahlilchisi_va_telegram_bot_loyihasi_rejasi.md](ai_sport_tahlilchisi_va_telegram_bot_loyihasi_rejasi.md).
> Agentlar orqali bajarish tartibi (to'lqinlar, egalik): [agentlar_rejasi.md](agentlar_rejasi.md).
> Bu hujjat **qanday va qaysi tartibda** qurishni belgilaydi. Har bir vazifaning ID si bor (masalan, `F2.3`), ish shu ID lar bo'yicha boshlanadi va kuzatiladi.

---

## 0. Umumiy Ko'rinish

| Faza | Nomi | Davomiyligi* | Natija | Darvoza (Go/No-go) |
|---|---|---|---|---|
| F0 | Tayyorgarlik va ma'lumot manbalarini tekshirish | 1–2 kun | Kalitlar, tekshirilgan API imkoniyatlari | ✅ API joriy mavsum va koeffitsientlarni beradi |
| F1 | Loyiha skeleti va instrumentlar | 2 kun | Ishlaydigan bo'sh ilova, CI | — |
| F2 | Ma'lumotlar qatlami | 4–5 kun | Baza, API klientlar, futbol adapteri | — |
| F3 | Statistik model va backtest | 5–7 kun | Model, EV, backtest hisoboti | ✅ Backtest qabul mezonlari |
| F4 | Pipeline va rejalashtiruvchi | 3 kun | Avtomatik kunlik sikl | — |
| F5 | Gemini integratsiyasi | 2–3 kun | Kontekst tekshiruvi va izoh | — |
| F6 | Telegram bot: MVP | 5–7 kun | Tahlil oqimi, sozlamalar, qayta hisob | — |
| F7 | Telegram bot: kengaytmalar | 5–6 kun | Maxsus tahlil, sevimlilar, bank, hisobot | — |
| F8 | Feedback loop | 3–4 kun | Natijalar, kalibratsiya, saboqlar | — |
| F9 | Deploy va infratuzilma | 2 kun | VPS da ishlayotgan tizim | — |
| F10 | Paper trading | 4–6 hafta | ≥ 150–200 signal statistikasi | ✅ Production mezonlari |
| F11 | Production (v1.0) | 1 kun | Reliz, runbook | — |
| F12 | Kengaytirish | doimiy | Yangi sportlar | Har sport uchun backtest |

\* Kuniga ≈ 4–5 soat ish hisobida. **Jami ishlab chiqish: ≈ 7–9 hafta + 4–6 hafta paper trading.**

```
F0 ─► F1 ─► F2 ─► F3 ─┬─► F4 ─► F5 ─► F6 ─► F7 ─► F8 ─► F9 ─► F10 ─► F11
                      │                                 ▲
                      └── (F3 darvozasidan o'tmasa: model qayta ishlanadi)
F9 (deploy) ni F6 dan keyin erta qilish ham mumkin, shunda bot serverda sinovdan o'tadi.
```

---

## 1. Texnik Qarorlar (bir marta belgilanadi)

| Soha | Tanlov | Sabab |
|---|---|---|
| Til | Python 3.13 | Statistika, AI SDK, bot kutubxonalari |
| Paket boshqaruvi | `uv` | Tez, lock-fayl, virtual muhit |
| Bot | `aiogram 3.x` | Async, FSM (bosqichli oqimlar), router tizimi |
| HTTP | `httpx` (async) + `tenacity` (retry) | Limitlar va qayta urinish nazorati |
| Baza | SQLite → `SQLAlchemy 2.x` (async) + `aiosqlite`, migratsiya: `Alembic` | Oddiy, PostgreSQL ga o'tish oson |
| Sozlamalar | `pydantic-settings` + `.env` | Tiplangan konfiguratsiya |
| Rejalashtiruvchi | `APScheduler` (AsyncIOScheduler, SQLAlchemy jobstore) | Restartdan keyin ham vazifalar saqlanadi |
| Model | `numpy`, `scipy`, `pandas`, `scikit-learn` (kalibratsiya) | Poisson / Dixon-Coles, isotonic |
| AI | `google-genai` SDK, Gemini Flash | Structured output (`response_schema`) |
| Qidiruv | `rapidfuzz` | Jamoa nomlarini noaniq qidirish |
| Grafik | `matplotlib` | Bank va hisobot grafiklari |
| Loglar | `structlog` (JSON) | Serverda tahlil qilish oson |
| Sifat | `ruff` (lint + format), `mypy`, `pytest`, `pytest-asyncio`, `respx` | Avtomatik tekshiruv |
| CI/CD | GitHub Actions | Test → Docker image → VPS ga deploy |
| Konteyner | Docker (bitta servis) + volume (`/data`) | Oddiy deploy |
| Vaqt | Bazada **UTC**, ko'rsatishda `Asia/Tashkent` | Vaqt mintaqasi xatolarining oldi olinadi |

**Git qoidalari:** `main` doim ishlaydigan holatda bo'ladi. Har bir faza alohida `feature/F2-data-layer` kabi branchda bajariladi. Commitlar `feat: ...`, `fix: ...` formatida yoziladi. Har faza oxirida teg qo'yiladi: `v0.2.0`.

---

## F0. Tayyorgarlik va Ma'lumot Manbalarini Tekshirish

**Maqsad:** loyihaning eng katta xavfi bo'lgan ma'lumot manbalarini kod yozishdan oldin tekshirish.

| ID | Vazifa | Kim |
|---|---|---|
| F0.1 | Telegram: @BotFather orqali bot yaratish, **token** olish; o'z `user_id` ingizni olish (@userinfobot) | Siz |
| F0.2 | API-Football (api-sports.io) ro'yxatdan o'tish, **API kalit** olish | Siz |
| F0.3 | Google AI Studio: **Gemini API kalit** olish | Siz |
| F0.4 | *(Ixtiyoriy)* The Odds API kaliti | Siz |
| F0.5 | Private GitHub repozitoriy yaratish | Siz |
| F0.6 | Lokal muhit: Python 3.13, `uv`, Git, Docker Desktop | Siz / birga |
| F0.7 | **Tekshiruv skripti** (`scripts/probe_api.py`): bepul tarifda joriy mavsum ochiqmi; kerakli ligalar uchun `odds` bormi, qaysi bukmekerlar (1xBet, Pinnacle) mavjud; `injuries`, `lineups` va `fixtures/statistics` da xG bormi; bitta so'rov qancha limit sarflaydi | Men |
| F0.8 | Kuzatiladigan **ligalar ro'yxati**ni kelishish (boshlanishda 8–10 ta) | Birga |
| F0.9 | Backtest uchun tarixiy ma'lumot: **football-data.co.uk** CSV (natijalar va yopilish koeffitsientlari, jumladan Pinnacle), so'nggi 3–5 mavsum | Men |

**Darvoza F0:**
- ✅ Joriy mavsum + koeffitsientlar bepul tarifda ishlaydi → bepul tarif bilan davom etiladi.
- ⚠️ Joriy mavsum yopiq → ishlab chiqish va backtest bepul ma'lumot bilan davom etadi, F10 dan oldin pullik tarifga (≈ $19/oy) o'tiladi.
- ❌ Kerakli ligalar uchun koeffitsient umuman yo'q → The Odds API asosiy koeffitsient manbaiga aylanadi.

---

## F1. Loyiha Skeleti va Instrumentlar

| ID | Vazifa |
|---|---|
| F1.1 | Papka tuzilmasi (asosiy rejaning 8-bo'limi), `pyproject.toml`, `uv.lock` |
| F1.2 | `src/app/config.py`: `pydantic-settings`, `.env.example` (barcha kalitlar, `ADMIN_IDS`, `TZ`, `DB_PATH`) |
| F1.3 | Loglash (`structlog`, JSON), global xato ushlagich |
| F1.4 | Kod sifati darvozalari (`CLAUDE.md` → "Kod sifati standarti"): `ruff` (kengaytirilgan qoidalar), `mypy --strict`, `import-linter`, `pytest-cov` chegaralari, `pip-audit`, `deptry`, `gitleaks`; `pre-commit` hook va CI |
| F1.5 | `Dockerfile` (multi-stage, non-root user), `docker-compose.yml` (lokal ishga tushirish uchun) |
| F1.6 | GitHub Actions: `lint` + `test` har push da |
| F1.7 | `README.md`: ishga tushirish yo'riqnomasi |
| F1.8 | Minimal bot: `/start` → "Ishlayapti ✅" (whitelist bilan). Butun zanjir ishlashini tekshiradi |

**Qabul mezoni:** `docker compose up` bilan bot ishga tushadi va `/start` ga javob beradi; CI yashil.

---

## F2. Ma'lumotlar Qatlami

| ID | Vazifa |
|---|---|
| F2.1 | SQLAlchemy modellari: asosiy rejaning 7-bo'limidagi barcha jadvallar. Alembic boshlang'ich migratsiyasi |
| F2.2 | Repository qatlami (`db/repo.py`): CRUD va tez-tez ishlatiladigan so'rovlar |
| F2.3 | Bazaviy API klient: rate limiter, retry (`tenacity`), `api_usage` hisoblagichi, kunlik limit himoyasi (zaxira 30%), kesh (TTL) |
| F2.4 | `SportAdapter` protokoli va domen modellari (`Fixture`, `OddsSnapshot`, `Market`, `Selection`) |
| F2.5 | Futbol adapteri: ligalar, mavsum o'yinlarini sinxronlash (`from/to` inkremental), bugungi o'yinlar, koeffitsientlar (sahifalash), jarohatlar, tarkiblar, natijalar (`ids=` paketli) |
| F2.6 | Jamoa nomlarini normallashtirish va qidiruv indeksi (`rapidfuzz`, lotin/kirill transliteratsiya) |
| F2.7 | Tarixiy ma'lumot importi: football-data.co.uk CSV → `fixtures` + `odds_snapshots` (closing) |
| F2.8 | Testlar: yozib olingan API javoblari (`respx` fixtures), limit va kesh testlari |

**Qabul mezoni:** `python -m app.pipeline.refresh` buyrug'i tanlangan ligalar ma'lumotini bazaga yozadi; bitta to'liq yangilash ≤ 40 so'rov sarflaydi; testlar o'tadi.

---

## F3. Statistik Model va Backtest

| ID | Vazifa |
|---|---|
| F3.1 | `models/devig.py`: marjani olib tashlash (proporsional va Shin usuli) |
| F3.2 | `models/poisson.py`: Dixon-Coles modeli, vaqt bo'yicha og'irlik (eski o'yinlar kamroq ta'sir qiladi), uy ustunligi; hisob matritsasidan bozorlar: 1X2, Ikki natija, Total 1.5/2.5/3.5, BTTS, Osiyo forasi |
| F3.3 | `models/market.py`: bozor modeli (Pinnacle de-vig → "adolatli" ehtimollik) |
| F3.4 | Ansambl: `p = w × p_poisson + (1 − w) × p_bozor` (`w` backtestda tanlanadi) |
| F3.5 | `models/ev.py` va `models/staking.py`: EV, Kelly 1/4, maksimal 2% cheklov, flat rejim |
| F3.6 | `models/confidence.py`: ishonch darajasi (ma'lumot sifati, model va bozor farqi, kun uzoqligi) |
| F3.7 | `scripts/backtest.py`: **walk-forward** (faqat o'yindan oldingi ma'lumot bilan), EV chegarasi va ehtimollik chegarasi bo'yicha grid, liga va bozor kesimlari |
| F3.8 | Backtest hisoboti: ROI, Yield, signallar soni, Brier/log-loss (model va bozor), kalibratsiya grafigi, maksimal pasayish (drawdown) |
| F3.9 | Unit testlar: ehtimolliklar yig'indisi = 1, ma'lum misollarda to'g'ri natija, EV formulasi |

**Darvoza F3 (backtest qabul mezonlari):**
Barcha mezonlar **faqat hold-out** (oxirgi **to'liq tugagan** mavsum, bir marta baholanadi) bo'yicha va statistik ishonch bilan tekshiriladi (qarorlar jurnali #11):
- Kalibratsiya: har bir 10% li oraliqda (n ≥ 30) og'ish ≤ 5 p.p.
- ≥ 500 signal; **CLV (yopilish de-vig narxiga nisbatan) o'rtachasi > 0 va 95% CI pastki chegarasi > 0** — asosiy mezon.
- ROI: 95% CI pastki chegarasi ≥ −2% (nuqta bahosi ≥ 0%). Nuqta bahosi bajarilib, CI bajarilmasa — "ahamiyatsiz" deb alohida ko'rsatiladi, ✅ hisoblanmaydi.
- Natija bitta ligaga bog'liq emas: n ≥ 100 signal bo'lgan kamida 3 ta ligada CLV > 0.
- Ma'lumot: sof bozor (`w = 0`) bazaviy varianti bilan solishtiriladi; model undan yomon bo'lsa, bozor modeli rejimi tanlanadi.

Mezonlar bajarilmasa, model yaxshilanadi (parametrlar, bozor modeli ulushi, ligalar ro'yxati) yoki faqat **bozor modeli** (Pinnacle vs boshqa bukmekerlar) bilan davom etiladi. Qaror birgalikda qabul qilinadi.

---

## F4. Pipeline va Rejalashtiruvchi

| ID | Vazifa |
|---|---|
| F4.1 | `pipeline/refresh.py`: kunlik kesh yangilash (08:30) |
| F4.2 | `pipeline/analyze.py`: umumiy tahlil funksiyasi `analyze(query)`. Kunlik, sana oralig'i, maxsus, shablon va sevimlilar shu funksiyadan foydalanadi |
| F4.3 | `pipeline/daily.py`: kunlik tahlil va yuborish (foydalanuvchi sozlamasidagi vaqtda) |
| F4.4 | `pipeline/recalc.py`: qayta hisoblash, `signal_versions`, farq hisoblash |
| F4.5 | `pipeline/prematch.py` va `scheduled_recalcs`: 24 soat va N daqiqa oldin yangilash |
| F4.6 | `pipeline/settle.py`: natijalarni tekshirish (08:00), `results`, CLV |
| F4.7 | APScheduler: persistent jobstore, takroriy ishga tushishdan himoya (idempotentlik), o'tkazib yuborilgan vazifalarni bajarish |
| F4.8 | Integratsion test: "soxta kun" ma'lumotlari bilan to'liq sikl |

**Qabul mezoni:** tizim 3 kun davomida lokal ravishda o'zi ishlaydi, signallar va natijalar bazada to'g'ri yoziladi.

---

## F5. Gemini Integratsiyasi

| ID | Vazifa |
|---|---|
| F5.1 | `clients/gemini.py`: `response_schema`, `temperature`, timeout, retry, token hisobi |
| F5.2 | Prompt shablonlari (`prompts/*.md`, versiyalangan): kontekst tekshiruvi, izoh, post-mortem |
| F5.3 | Kirish JSON yig'uvchi: nomzodlar, jarohatlar, forma va tajriba konteksti (F8 da to'ldiriladi) |
| F5.4 | `pydantic` validatsiya, 1 marta qayta urinish, **fallback**: LLM ishlamasa signal izohsiz yuboriladi |
| F5.5 | Bepul tarif limitlari nazorati, so'rovlarni jurnalga yozish |
| F5.6 | Testlar: soxta (mock) javoblar, buzilgan JSON, timeout |

**Qabul mezoni:** 20 ta nomzodli tahlil ≤ 15 soniyada bajariladi; Gemini o'chirilganda ham tizim signal yuboradi.

---

## F6. Telegram Bot — MVP

| ID | Vazifa |
|---|---|
| F6.1 | Bot karkasi: routerlar, middleware (whitelist, rate limit, xatolarni ushlash, `user` yuklash) |
| F6.2 | Asosiy menyu (reply keyboard), `/start`, `/yordam` |
| F6.3 | ⚙️ Sozlamalar: barcha parametrlar, risk profillari, 80%+ ogohlantirishi |
| F6.4 | 📊 **Tahlil oqimi (FSM):** sana oralig'i (tugmalar + inline kalendar + matn), sport, ehtimollik, EV, qamrov (ligalar / jamoalar / aniq o'yinlar, sahifalangan checkbox), son, bozorlar, saralash, tasdiqlash va taxminiy API sarfi |
| F6.5 | `/tahlil` tezkor buyrug'i parseri (`25.10-31.10 futbol 60% Arsenal,Inter`) + testlar |
| F6.6 | Signal kartasi formatteri, kunlar bo'yicha guruhlash, sahifalash, "Value topilmadi" holati |
| F6.7 | 🔄 Qayta hisoblash (bitta signal / butun so'rov), farq xabari, cooldown |
| F6.8 | ⚡ Bugungi signallar va kunlik avtomatik xabar |
| F6.9 | `/holat`: API limitlari, oxirgi yangilanish, xatolar |
| F6.10 | Telegram cheklovlari: xabar uzunligi (4096), flood control, `callback_data` ≤ 64 bayt (ID lar orqali) |

**Qabul mezoni:** asosiy rejaning 6.2, 6.4, 6.5, 6.6 va 6.12-bo'limlaridagi oqimlar qo'lda sinovdan o'tadi (test ssenariylari ro'yxati bo'yicha).

---

## F7. Telegram Bot — Kengaytmalar

| ID | Vazifa |
|---|---|
| F7.1 | 🔍 Maxsus tahlil: jamoa qidiruvi, `/tahlil Arsenal - Chelsea`, EV filtrisiz to'liq natija |
| F7.2 | 📋 Batafsil karta: forma, H2H, eng ehtimolli 3 hisob, barcha bozorlar jadvali, koeffitsient harakati |
| F7.3 | ⭐ Sevimlilar va bildirishnomalar (tarkib, koeffitsient o'zgarishi, natija) |
| F7.4 | 💾 Shablonlar: saqlash, ishga tushirish, jadval biriktirish |
| F7.5 | 🎟 Mening stavkalarim: "Stavka qildim", haqiqiy koeffitsient va summa, bank, kunlik zarar limiti |
| F7.6 | 📈 Hisobot: davrlar, kesimlar, kalibratsiya jadvali, grafik (PNG), `/eksport` CSV |
| F7.7 | Retro-tahlil (o'tgan sana oralig'i) |
| F7.8 | 🧩 Ekspress tuzuvchi (ixtiyoriy) |

**Qabul mezoni:** barcha menyu bo'limlari ishlaydi, test ssenariylari o'tadi.

---

## F8. Feedback Loop

| ID | Vazifa |
|---|---|
| F8.1 | Natijalarni qo'lda belgilash tugmalari, `/natijalar`, `💬 Izoh` |
| F8.2 | Ertalabki "kecha yakuni" bloki |
| F8.3 | Post-mortem: har bir ❌ uchun Gemini toifalashi → `postmortems` |
| F8.4 | Saboqlar: teglash, tasdiqlash (≥ 2 marta), eskirish (90 kun) → `lessons` |
| F8.5 | `segment_stats`: haftalik hisob, shrinkage, avtomatik chegara o'zgartirish |
| F8.6 | Kalibratsiya: isotonic regressiya, versiyalash, Brier oldin va keyin; yaxshilanmasa faollashtirilmaydi |
| F8.7 | Promptga "Tajriba konteksti" blokini qo'shish (F5.3 bilan) |
| F8.8 | `/ozgarishlar` jurnali, orqaga qaytarish, haftalik "model nimani o'rgandi" hisoboti |

**Qabul mezoni:** sun'iy (simulyatsiya) natijalar bilan kalibratsiya va segment filtri to'g'ri ishlaydi; kichik namunada hech narsa o'zgarmaydi.

---

## F9. Deploy va Infratuzilma

| ID | Vazifa |
|---|---|
| F9.1 | VPS (2 GB RAM, Ubuntu 24.04, Yevropa); SSH faqat kalit bilan, `ufw`, `fail2ban`, `unattended-upgrades` |
| F9.2 | Docker o'rnatish, `/opt/ai-tahlilchi` papkasi, `.env` (faqat serverda, `chmod 600`) |
| F9.3 | GitHub Actions: `main` ga merge → Docker image (GHCR) → SSH orqali `docker pull && up -d` → Alembic migratsiya |
| F9.4 | Zaxira: har kuni SQLite `.backup` → siqish → tashqi saqlash (`rclone`), 14 kunlik saqlash; **tiklashni sinab ko'rish** |
| F9.5 | Monitoring: konteyner `restart: unless-stopped` + healthcheck; healthchecks.io "dead man's switch" (kunlik sikl ishlamasa ogohlantiradi); xatolar admin Telegramiga |
| F9.6 | Log rotatsiyasi (Docker `max-size`), disk joyini kuzatish |
| F9.7 | `RUNBOOK.md`: restart, zaxiradan tiklash, kalitni almashtirish, versiyani orqaga qaytarish |

**Qabul mezoni:** serverni qayta yuklashdan keyin tizim o'zi tiklanadi; zaxiradan tiklash sinovdan o'tgan; deploy bitta merge bilan bajariladi.

---

## F10. Paper Trading (Sinov davri)

| ID | Vazifa |
|---|---|
| F10.1 | Tizim serverda to'liq rejimda ishlaydi, stavkalar **faqat qog'ozda** (real pulsiz) |
| F10.2 | Haftalik ko'rib chiqish: ROI, CLV, kalibratsiya, xatolar, API sarfi; topilgan xatolarni tuzatish |
| F10.3 | Bot UX bo'yicha o'z tajribangiz asosida tuzatishlar |
| F10.4 | Model parametrlarini faqat **haftalik** va jurnal bilan o'zgartirish (har kuni emas) |

**Darvoza F10 (production mezonlari):**
- ≥ 150–200 yakunlangan signal.
- **CLV o'rtachasi musbat** (asosiy mezon), ROI ≥ −2% (kichik namunada tebranish bo'lishi mumkin).
- Tizim 14 kun davomida qo'lda aralashuvsiz ishlagan, kritik xatolar yo'q.
- API va Gemini limitlari hech qachon tugamagan.

Mezonlar bajarilmasa, paper trading davom etadi yoki model qayta ishlanadi. Real pul bilan foydalanishga o'tilmaydi.

---

## F11. Production (v1.0)

| ID | Vazifa |
|---|---|
| F11.1 | Reliz `v1.0.0`, `CHANGELOG.md` |
| F11.2 | Bank va unit sozlamalari, kunlik/haftalik zarar limitlari yoqilgan |
| F11.3 | Birinchi oy minimal stavkalar (bankning 0.5–1% i) |
| F11.4 | Oylik ko'rib chiqish: CLV, ROI, segmentlar, xarajatlar |

---

## F12. Kengaytirish (v1.x)

Har bir yangi sport alohida mini-loyiha sifatida qilinadi: **adapter → model → backtest (F3 darvozasi) → test rejimi → paper trading → yoqish.**
1. 🏒 Xokkey (Poisson, futbolga eng yaqin)
2. 🏀 Basketbol (ochkolarning normal taqsimoti)
3. 🏐 Voleybol, 🎾 Tennis (alohida ma'lumot manbai)
4. Live tahlil, Gemini function calling orqali erkin savollar, infografika

---

## 2. Test Strategiyasi

| Daraja | Nima | Vosita |
|---|---|---|
| Unit | Model, EV, de-vig, parser, formatterlar | `pytest` |
| Integratsiya | API klientlar (yozib olingan javoblar), baza, pipeline | `pytest` + `respx` + vaqtinchalik SQLite |
| End-to-end | "Soxta kun" ssenariysi: refresh → tahlil → signal → natija | `pytest` |
| Qo'lda | Bot oqimlari bo'yicha ssenariylar ro'yxati (`docs/test_scenarios.md`) | Telegram |
| Model | Backtest regressiyasi: yangi versiya oldingisidan yomon bo'lmasin | `scripts/backtest.py` |

Minimal qamrov: `models/` va `pipeline/` uchun ≥ 80%.

---

## 3. Xavflar va Ularni Boshqarish

| Xavf | Ehtimol | Ta'sir | Choralar |
|---|---|---|---|
| Bepul API joriy mavsumni bermaydi | O'rta | Yuqori | F0 da tekshiriladi; pullik tarif ≈ $19/oy |
| Kerakli bukmeker koeffitsientlari yo'q | O'rta | Yuqori | The Odds API muqobil manba |
| Model bozordan yaxshiroq emas | **Yuqori** | Yuqori | F3 va F10 darvozalari, bozor modeli, ligalarni qayta tanlash |
| Gemini bepul tarifi yoki modeli o'zgaradi | O'rta | Past | Fallback (LLM siz ishlash), model nomi konfiguratsiyada |
| API limitlari tugaydi | O'rta | O'rta | Kesh, zaxira 30%, taxminiy sarfni oldindan ko'rsatish |
| Bukmeker akkauntga limit qo'yadi | O'rta | O'rta | Bir nechta bukmeker, koeffitsientni solishtirish |
| Ma'lumot yo'qolishi | Past | Yuqori | Kunlik zaxira va tiklash sinovi |
| Kalitlar oshkor bo'lishi | Past | Yuqori | `.env` gitda emas, GitHub Secrets, kalitni almashtirish runbookda |

---

## 4. Ishni Boshlash Uchun Kerakli Narsalar

**Sizdan (F0):**
1. Telegram bot tokeni va `user_id` ingiz
2. API-Football kaliti
3. Gemini API kaliti
4. *(Ixtiyoriy)* The Odds API kaliti
5. Kuzatiladigan ligalar ro'yxati (yoki men taklif qilgan standart Top-10)
6. GitHub repozitoriy (yoki hozircha faqat lokal git)

> 🔐 Kalitlarni chatga yozmang. Ular faqat lokal `.env` fayliga yoziladi (F1.2 da `.env.example` tayyorlanadi).

**Kalitlarsiz darhol boshlash mumkin bo'lgan ishlar:** F1 (skelet), F2.1–F2.4 (baza va interfeyslar), F2.7 + F3 (tarixiy CSV bilan model va backtest).

**Boshlash buyrug'i namunalari:**
- `F0 ni boshla`: tekshiruv skripti (kalitlar tayyor bo'lsa)
- `F1 ni boshla`: loyiha skeleti
- `F1–F3 ni ketma-ket bajar`: bir necha fazani birdan bajarish

Har bir faza oxirida: bajarilgan vazifalar ro'yxati, test natijalari va keyingi faza uchun savollar beriladi.
