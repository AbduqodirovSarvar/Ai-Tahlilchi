# AI Tahlilchi — Agentlar Orqali Implementatsiya Rejasi

> Bog'liq hujjatlar: [asosiy reja](ai_sport_tahlilchisi_va_telegram_bot_loyihasi_rejasi.md) · [ishlab chiqish rejasi (F0–F12)](ishlab_chiqish_rejasi.md) · [CLAUDE.md](CLAUDE.md)
> Agent ta'riflari: [.claude/agents/](.claude/agents/)

---

## 1. Rollar

| Rol | Kim | Vazifasi |
|---|---|---|
| **Mahsulot egasi** | Siz | To'lqinni boshlash buyrug'i, darvoza qarorlari (F0, F3, F10), kalitlar, savollarga javob |
| **Orkestr (Tech lead)** | Asosiy Claude sessiyasi | To'lqinni rejalash, agentlarga vazifa berish, natijalarni birlashtirish (merge), ziddiyatlarni hal qilish, `docs/progress.md` ni yuritish, sizga hisobot berish |
| **Ijrochi agentlar** | 6 ta mutaxassis | O'z sohasidagi vazifalarni bajarish |
| **Nazoratchi agentlar** | 2 ta | Mustaqil tekshiruv (QA) va kod sharhi |

---

## 2. Agentlar va Egalik

| Agent | Fazalar / vazifalar | Egalik qiladigan papkalar |
|---|---|---|
| 🏗 `infra-engineer` | F1, F9 | `pyproject.toml`, `Dockerfile`, `.github/`, `src/app/config.py`, `src/app/logging.py`, `src/app/main.py`, `deploy/`, `scripts/ops/`, `README.md`, `RUNBOOK.md` |
| 🗄 `data-engineer` | F0.7, F2 | `src/app/domain/`, `src/app/db/`, `src/app/clients/` (gemini'dan tashqari), `src/app/sports/`, `alembic/`, `scripts/probe_api.py`, `scripts/import_history.py` |
| 📐 `quant-modeler` | F3, F8.5–F8.6 | `src/app/models/`, `scripts/backtest.py`, `docs/backtest/` |
| ⚙️ `pipeline-engineer` | F4, servis shartnomasi | `src/app/services/`, `src/app/pipeline/`, `src/app/scheduler.py` |
| 🤖 `llm-engineer` | F5, F8.3–F8.4, F8.7 | `src/app/clients/gemini.py`, `src/app/llm/` |
| 💬 `bot-engineer` | F6, F7, F8.1–F8.2, F8.8 | `src/app/bot/`, `docs/test_scenarios.md` |
| 🧪 `test-engineer` | Har to'lqin oxirida QA | `tests/integration/`, `tests/e2e/`, `tests/conftest.py`, `docs/qa/` |
| 🔍 `code-reviewer` | Har to'lqin oxirida sharh (faqat o'qiydi) | — |

Har bir ijrochi agent o'z modullari uchun unit testlarni `tests/<modul>/` da o'zi yozadi.

**Egalik qoidasi:** agent faqat o'z papkalarini o'zgartiradi. Boshqa papkada o'zgarish kerak bo'lsa, hisobotda **so'rov** sifatida yozadi va orkestr uni tegishli agentga beradi. Bu parallel ishda ziddiyatlarning oldini oladi.

---

## 3. Shartnomalar Birinchi (Contract-first)

Agentlar bir-birini kutmasdan parallel ishlashi uchun avval **interfeyslar** yoziladi va qotiriladi:

| Shartnoma | Fayl | Egasi | Kim foydalanadi |
|---|---|---|---|
| Domen modellari | `src/app/domain/*.py` | data-engineer | hamma |
| Sport adapteri | `src/app/sports/base.py` (`SportAdapter`) | data-engineer | pipeline, quant |
| Baza sxemasi | `src/app/db/schema.py` | data-engineer | pipeline, bot (DTO orqali) |
| Tahlil servisi | `src/app/services/interfaces.py` (`AnalysisQuery`, `AnalysisService`, `SignalView`, `RecalcDiff`) | pipeline-engineer | bot |
| Soxta servis | `src/app/services/fake.py` (`FakeAnalysisService`) | pipeline-engineer | bot (servis tayyor bo'lguncha) |
| Model API si | `src/app/models/__init__.py` (`predict_markets`, `compute_ev`, `stake`, `confidence`) | quant-modeler | pipeline |
| LLM API si | `src/app/llm/service.py` (`review_candidates`, `postmortem`) | llm-engineer | pipeline |

Barcha shartnomalar `docs/contracts.md` da ro'yxatga olinadi. Qotirilgandan keyin ularga **faqat yangi narsa qo'shiladi**. Buzuvchi o'zgarish uchun orkestr ruxsati va barcha foydalanuvchi agentlarni yangilash kerak.

---

## 4. To'lqinlar (Waves)

Har bir to'lqin bir nechta agentning **parallel** ishidan iborat. To'lqin 6-bo'limdagi sikl bilan yakunlanadi.

```
T0  F0 tekshiruv ─────────────── (kalitlar bo'lganda, T1–T3 bilan parallel bo'lishi mumkin)
T1  [infra] F1 skelet
T2  [data] shartnoma + sxema + CSV import  ║  [pipeline] servis shartnomasi + Fake
T3  [data] F2 qolgani  ║  [quant] F3 model + backtest  ║  [bot] F6 (Fake servis bilan)
        ── 🚦 F3 darvozasi (sizning qaroringiz) ──
T4  [pipeline] F4  ║  [llm] F5  ║  [bot] F6 yakuni + haqiqiy servisga ulash
T5  [bot] F7  ║  [quant] F8.5–8.6  ║  [llm] F8.3–8.4, 8.7  ║  [infra] F9
T6  [bot] F8.1–8.2, 8.8  →  to'liq integratsiya, e2e, deploy
        ── 🚀 F10 paper trading → 🚦 F10 darvozasi → F11 production ──
```

### T0 — Ma'lumot manbalarini tekshirish (F0)
| Agent | Vazifalar |
|---|---|
| data-engineer | F0.7 `probe_api.py` → `docs/api_probe_report.md` |
| Siz | F0.1–F0.6, F0.8 (kalitlar `.env` ga, ligalar ro'yxati) |
**Natija:** 🚦 F0 darvozasi. Bepul tarif, pullik tarif yoki The Odds API: qaror sizniki.

### T1 — Skelet (F1)
| Agent | Vazifalar |
|---|---|
| infra-engineer | F1.1–F1.8, `docs/progress.md` va `docs/contracts.md` shablonlari |
**Nima uchun yolg'iz:** keyingi barcha agentlar shu tuzilmaga tayanadi.

### T2 — Shartnomalar (≈ 1 kun)
| Agent | Vazifalar |
|---|---|
| data-engineer | F2.1 (sxema + Alembic), F2.4 (domen + `SportAdapter`), F2.7 (tarixiy CSV importi: quant uchun kerak) |
| pipeline-engineer | `services/interfaces.py`, `services/fake.py` |
**Natija:** orkestr shartnomalarni ko'rib chiqadi va qotiradi.

### T3 — Asosiy qurilish (≈ 1–1.5 hafta)
| Agent | Vazifalar | Bog'liqlik |
|---|---|---|
| data-engineer | F2.2, F2.3, F2.5, F2.6, F2.8 | T2 |
| quant-modeler | F3.1–F3.9 (tarixiy CSV ma'lumotlarida) | T2 (F2.7) |
| bot-engineer | F6.1–F6.6, F6.10 (`FakeAnalysisService` bilan) | T2 |
**Natija:** 🚦 **F3 darvozasi.** Orkestr backtest hisobotini sizga taqdim etadi, davom etish yoki model ustida ishlash qarori sizniki.

### T4 — Tizimni birlashtirish (≈ 1 hafta)
| Agent | Vazifalar | Bog'liqlik |
|---|---|---|
| pipeline-engineer | F4.1–F4.8 | T3 (data, quant) |
| llm-engineer | F5.1–F5.6 | T2 |
| bot-engineer | F6.7–F6.9, Fake → haqiqiy servisga o'tish | pipeline F4.2 |
**Natija:** lokal tizim 3 kun o'zi ishlaydi (F4 mezoni).

### T5 — Kengaytmalar va deploy (≈ 1–1.5 hafta)
| Agent | Vazifalar |
|---|---|
| bot-engineer | F7.1–F7.8 |
| quant-modeler | F8.5, F8.6 |
| llm-engineer | F8.3, F8.4, F8.7 |
| infra-engineer | F9.1–F9.7 (skriptlar va workflow; serverda bajarish siz bilan birga) |

### T6 — Yakuniy integratsiya (≈ 3–4 kun)
| Agent | Vazifalar |
|---|---|
| bot-engineer | F8.1, F8.2, F8.8 |
| test-engineer | To'liq e2e, barcha fazalar qabul mezonlarini qayta tekshirish |
| code-reviewer | Butun kod bazasi sharhi |
| infra-engineer | Production deploy, zaxiradan tiklash sinovi |
**Natija:** 🚀 F10 paper trading boshlanadi.

### F10 davomida (haftalik)
| Agent | Vazifa |
|---|---|
| quant-modeler | Haftalik CLV, ROI va kalibratsiya tahlili → `docs/backtest/weekly-<sana>.md` |
| test-engineer | Loglar va xatolar tahlili |
| Tegishli agent | Topilgan xatolarni tuzatish (har biri kichik to'lqin sifatida) |

---

## 5. Agent Hisoboti Formati

Har bir agent ish oxirida quyidagini qaytaradi:

```markdown
## Hisobot: <agent> · <to'lqin> · <sana>
**Bajarilgan vazifalar:** F2.1 ✅, F2.4 ✅, F2.7 ⚠️ (qisman: ...)
**O'zgargan fayllar:** (qisqa ro'yxat)
**Tekshiruvlar:** pytest 48/48 ✅ · ruff ✅ · mypy ✅ · qamrov 84%
**Shartnoma o'zgarishlari:** yo'q / bor (nima qo'shildi)
**Boshqa agentlarga so'rovlar:** (masalan, "pipeline: refresh entrypoint nomi kerak")
**Ochiq savollar (mahsulot egasiga):** ...
**Xavflar / eslatmalar:** ...
```

---

## 6. To'lqin Sikli (har bir to'lqin uchun)

```
1. Rejalash      Orkestr: to'lqin vazifalari, agentlar uchun aniq topshiriqlar (ID lar, kirish, chiqish)
2. Ijro          Ijrochi agentlar PARALLEL, har biri alohida git worktree va branchda
3. Birlashtirish Orkestr: branchlarni merge qiladi, ziddiyatlarni hal qiladi
4. Nazorat       test-engineer ║ code-reviewer: PARALLEL tekshiruv
5. Tuzatish      🔴 va 🟠 topilmalar tegishli agentga qaytariladi (maks. 2 aylanish)
6. Yopish        progress.md yangilanadi, git teg (v0.X.0), sizga qisqa hisobot
7. Darvoza       (agar bo'lsa) qaror sizdan so'raladi
```

**Agentga topshiriq shabloni** (orkestr yozadi):
```
Vazifalar: F2.3, F2.5
Kontekst: docs/contracts.md (v1), docs/api_probe_report.md
Kirish: src/app/domain/, src/app/db/schema.py (qotirilgan)
Chiqish: src/app/clients/api_sports.py, src/app/sports/football.py + testlar
Cheklovlar: egalik jadvali; haqiqiy API ga murojaat yo'q
Bajarildi mezoni: ishlab_chiqish_rejasi.md F2 qabul mezoni
```

---

## 7. Git va Izolyatsiya

- Parallel agentlar `isolation: worktree` rejimida ishlaydi: har biri o'z nusxasi va branchida (`wave/T3-data`, `wave/T3-quant`, `wave/T3-bot`), shuning uchun bir-birining fayllarini buzmaydi.
- Merge tartibi: avval shartnoma egalari (data → pipeline), keyin foydalanuvchilar (quant, llm, bot).
- Har merge'dan keyin `uv run pytest` butun loyihada ishga tushiriladi.
- `main` doim ishlaydigan holatda turadi. To'lqin teglari: `v0.1.0` (T1) … `v0.6.0` (T6), `v1.0.0` (F11).

---

## 8. Muammolar va Eskalatsiya

| Holat | Harakat |
|---|---|
| Agent bloklandi (ma'lumot, kalit, noaniq talab) | Ishni to'xtatadi, hisobotda savol qaytaradi → orkestr sizdan so'raydi |
| Shartnoma o'zgarishi kerak | Agent taklif yozadi → orkestr qaror qiladi → shartnoma egasi o'zgartiradi → foydalanuvchi agentlar yangilanadi |
| 2 tuzatish aylanishidan keyin ham 🔴 qolsa | Orkestr muammoni o'zi tahlil qiladi yoki sizga variantlar bilan murojaat qiladi |
| Darvoza mezoni bajarilmadi (F3, F10) | Keyingi to'lqin boshlanmaydi. Variantlar va raqamlar sizga taqdim etiladi |
| Tashqi API o'zgardi yoki ishlamayapti | data-engineer adapterni moslashtiradi. Vaqtincha kesh va fallback bilan ishlanadi |

---

## 9. Resurslar va Samaradorlik

- Bir vaqtda **2–4 ta** agent ishlaydi. Bundan ko'pi ziddiyatlarni va token sarfini oshiradi.
- Nazoratchi agentlar (QA, sharh) har to'lqinda **bir marta** ishga tushiriladi, har bir kichik o'zgarishda emas.
- Kichik tuzatishlar (1–2 fayl) agentsiz, orkestrning o'zi tomonidan bajariladi.
- Har bir to'lqin oxirida siz qisqa xulosa olasiz: nima bajarildi, nima qoldi, qarorlar.

---

## 10. Avtonom Rejim (bitta buyruq bilan oxirigacha)

Siz **"Ishni boshlang (avtonom)"** deysiz. Orkestr T1 dan T6 gacha barcha to'lqinlarni sizdan ruxsat so'ramasdan ketma-ket bajaradi va oxirida yakuniy hisobot beradi.

### 10.1. Boshlashdan oldin (bir martalik, ≈ 15 daqiqa)
| # | Tayyorgarlik | Majburiymi | Bo'lmasa nima bo'ladi |
|---|---|---|---|
| 1 | `.env` faylida kalitlar: Telegram token, `ADMIN_IDS`, API-Football, Gemini | Tavsiya | Kalitsiz qismlar soxta ma'lumot va tarixiy CSV bilan quriladi; jonli sinov (F0, jonli bot) "kechiktirilgan" deb belgilanadi |
| 2 | `.claude/settings.json` ruxsatlari (tayyorlangan) | Ha | Har bir buyruqda ruxsat so'raladi, avtonomlik buziladi |
| 3 | Kompyuter yoqilgan, VS Code va Claude Code sessiyasi ochiq | Ha | Sessiya to'xtaydi; keyin `davom et` buyrug'i bilan tiklanadi |
| 4 | Docker Desktop o'rnatilgan | Yo'q | Docker image build tekshiruvi CI'ga qoldiriladi |
| 5 | GitHub repo va push ruxsati | Yo'q | Hammasi lokal git'da qoladi |
| 6 | VPS va SSH kirish | Yo'q | Deploy paketi va yo'riqnoma tayyorlanadi, serverga joylash sizga qoldiriladi |

### 10.2. Avtomatik qaror qoidalari (darvozalar sizsiz qanday o'tadi)
| Holat | Orkestrning qarori |
|---|---|
| **F0:** kalit yo'q | F0 o'tkazib yuboriladi, `docs/decisions_log.md` ga "kechiktirilgan" deb yoziladi |
| **F0:** joriy mavsum bepul tarifda yopiq | Tarixiy ma'lumot bilan davom etiladi; yakuniy hisobotda "pullik tarif kerak" deb ko'rsatiladi |
| **F3:** backtest mezonlari bajarilmadi | quant-modeler **2 marta** yaxshilashga urinadi. Natija bo'lmasa, standart rejim **bozor modeli** (Pinnacle va boshqa bukmekerlar farqi) qilib belgilanadi va davom etiladi. Bu yakuniy hisobotda birinchi o'rinda ko'rsatiladi |
| Noaniq talab | Rejalardagi standart variant tanlanadi va ADR + `docs/decisions_log.md` ga yoziladi |
| Shartnoma o'zgarishi | Orkestr o'zi qaror qiladi va barcha bog'liq agentlarni yangilaydi |
| 2 tuzatish aylanishidan keyin ham 🟠 yoki 🟡 topilma | `docs/ochiq_masalalar.md` ga yoziladi, ish davom etadi |
| 🔴 topilma (noto'g'ri ehtimollik yoki pul hisobi, xavfsizlik, ma'lumot yo'qolishi) | **Hech qachon chetlab o'tilmaydi.** Tuzatilmaguncha keyingi to'lqinga o'tilmaydi. Hal bo'lmasa, o'sha funksiya feature-flag bilan o'chiriladi va yakuniy hisobotda ko'rsatiladi |
| Kod sifati darvozasi qizil | Tuzatilmaguncha merge qilinmaydi (istisnosiz) |

### 10.3. Avtonom rejim chegaralari
Bu narsalar xavfsizlik yoki jismoniy sabablarga ko'ra avtomatlashtirilmaydi:
- **Real pul bilan ishlashga o'tish (F11)** har doim sizning qaroringiz.
- **F10 paper trading** real vaqtda 4–6 hafta davom etadi. Avtonom rejim tizimni qurib, ishga tushirib va paper tradingni boshlab tugaydi. Haftalik tahlillar keyin rejalashtirilgan vazifa sifatida bajarilishi mumkin.
- **Pul sarflash** (pullik tarif, VPS sotib olish) va loyihadan tashqaridagi fayllarni o'zgartirish yo'q.
- **Tashqariga chiqarish** (GitHub'ga push, serverga deploy) faqat 10.1-jadvalda oldindan ruxsat berilgan bo'lsa bajariladi.

### 10.4. Sizni faqat qachon bezovta qilamiz
Faqat **chetlab o'tib bo'lmaydigan to'siqda**: masalan, kalit noto'g'ri va kalitsiz davom etib bo'lmaydigan qadamga yetildi. Bunday holatda ham orkestr avval boshqa bajarilishi mumkin bo'lgan ishlarni tugatadi, keyin bitta xabarda barcha savollarni birga beradi.

### 10.5. Kuzatish va tiklash
- Har bir to'lqin oxirida `docs/progress.md` yangilanadi. Uni istalgan vaqtda ochib, holatni ko'rishingiz mumkin (aralashmasdan).
- Sessiya uzilsa (kompyuter o'chdi, kontekst tugadi): **`davom et`** buyrug'i bilan orkestr `progress.md` dan oxirgi holatni o'qib, ishni davom ettiradi.
- Yakunda: `docs/YAKUNIY_HISOBOT.md`. Unda nima qurildi, qanday ishga tushiriladi, backtest natijalari, qabul qilingan qarorlar, ochiq masalalar va sizdan kerak bo'lgan keyingi qadamlar bo'ladi.

---

## 11. Boshlash Buyruqlari

| Buyruq | Nima bo'ladi |
|---|---|
| **`Ishni boshlang (avtonom)`** | **T1 → T6 to'liq, sizsiz (10-bo'lim)** |
| `davom et` | Uzilgan avtonom ishni `progress.md` dan davom ettirish |
| `T1 ni boshla` | infra-engineer skeletni quradi → QA + sharh → hisobot |
| `T1 va T2 ni bajar` | Ketma-ket ikki to'lqin, oraliq hisobot bilan |
| `T0 ni boshla` | Probe skripti (kalitlar `.env` da bo'lishi kerak) |
| `Holat` | `docs/progress.md` bo'yicha joriy holat |
| `T3 ni boshla, bot-engineer'siz` | To'lqinni qisman bajarish |

> Kalitlarsiz **T1, T2 va T3** (bot va backtest qismlari) darhol boshlanishi mumkin. T0 ni kalitlar tayyor bo'lganda istalgan vaqtda qo'shish mumkin.
