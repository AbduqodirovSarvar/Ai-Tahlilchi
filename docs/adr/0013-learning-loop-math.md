# ADR 0013: O'rganish halqasi (feedback loop) matematikasi — segmentlar, shrinkage, isotonic kalibratsiya

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-22
- **Muallif:** quant-modeler
- **Kod:** `src/app/models/{segments,calibration}.py`, `src/app/pipeline/{learning,overlay}.py`, `src/app/db/repo/learning.py`, Alembic `0008`
- **Bog'liq:** asosiy reja §6.13 (B va C qismlari), qarorlar #11 (CI bilan darvoza) va #17 (foyda/CLV birinchi tavsiya koeffitsientida), [ADR 0005](0005-backtest-methodology.md)

## Kontekst
Reja §6.13 ikki avtomatik qatlamni talab qiladi: (1) kalibratsiya ("model 60% desa-yu, haqiqatda 52% o'tsa, to'g'rilash") va (2) segment filtri (doimiy manfiy liga × bozor × koeffitsient kesimlarida EV chegarasini oshirish yoki o'chirish). Xavf: kichik namunada tasodifiy tebranishga moslashib, yaxshi segmentni o'chirish yoki mavjud modelni buzish. Shuning uchun har bir harakat **namuna hajmi, ishonch oralig'i va gisterezis** bilan himoyalangan.

## Qarorlar

### 1. Qaysi signallar hisobga olinadi
Tavsiya qilingan (`recommended_at` bor), `retro` bo'lmagan, natijasi `win/half_win/half_loss/loss` bo'lgan **barcha** signallar, **yashirinlari ham** (yashirish — ko'rinish tanlovi, tanlov xatosi emas), foydalanuvchi stavka qilgan-qilmaganidan qat'i nazar. Bir o'yin-bozor-tanlov bir nechta foydalanuvchiga yetkazilgan bo'lsa, **bitta hodisa** (eng kichik `signal_id`). `void` va `cashout` modelning to'g'riligi haqida ma'lumot bermaydi va tashlanadi. ROI va CLV birinchi tavsiya koeffitsientida (qaror #17); har bir signal uchun ROI = 1 birlik tikilgan daromad (`settlement_returns`), stavka hajmiga bog'liq emas.

### 2. Segment statistikasi (`models/segments.py`)
Segment = `sport × league × market turi × koeffitsient oralig'i` (`<1.50, 1.50–1.80, 1.80–2.20, 2.20–3.00, 3.00+`; chiziq — masalan Total 2.5 va 3.5 — bitta `market` ustuniga tushadi, jadvalda `line` yo'q). Metrikalar: `n`, ROI, CLV (log), kalibratsiya og'ishi (o'rtacha bashorat − kuzatilgan chastota).

**Empirik Bayes (normal-normal) shrinkage.** Segmentning xom o'rtachasi `m` (`n` ta signal), global o'rtacha `g` (barcha signallar):

`shrunk = (n·m + k·g) / (n + k)`,  `k = PRIOR_STRENGTH = 30`.

`k` — "oldingi bilim" 30 ta soxta signalga teng: 50 signalli segment o'z o'rtachasining 62.5% ini saqlaydi, 300 signalli — 91% ini. `k` ataylab **qat'iy konstanta**, ma'lumotdan baholanmaydi: bir necha segmentda segmentlararo dispersiyaning o'zi beqaror baholanadi, ko'rinadigan qat'iy oldingi bilimni tekshirish osonroq. Qo'l bilan hisob (testda): `n=50, m=−0.10, g=0.02, k=30 → 0.625·(−0.10)+0.375·0.02 = −0.055`.

**Ishonch oralig'i.** Seeded blok-bootstrap (`BOOTSTRAP_RESAMPLES=1000`, 95%): bloklar — o'yin kunlari (bir kundagi natijalar korrelyatsiyalangan), shuning uchun kun to'liq qayta tanlanadi. Bootstrap taqsimoti xuddi shu shrinkage xaritasidan o'tkaziladi (`g` qat'iy), shuning uchun nuqtaviy baho va oraliq har doim mos. Seed = `(policy.seed, crc32(segment kaliti))`: natija kiritish tartibiga bog'liq emas va takrorlanadi (testlangan).

### 3. Qaror qoidalari (`segment_action`)
Barcha chegaralar `SegmentPolicy` ning nomlangan konstantalari.

1. **Qo'lda to'xtatish** (`hold_until`) hamma narsani muzlatadi.
2. **O'chirilgan segment** yangi signal bermaydi, demak yangi ma'lumot kelmaydi: u `DISABLE_COOLDOWN_DAYS=60` dan keyin **sinov holatida** (`RAISED`, oshirilgan EV chegarasi) qaytadi.
3. **`n < MIN_SEGMENT_SAMPLE=50`** bo'lsa hech narsa o'zgarmaydi.
4. **Qattiqlashtirish faqat ahamiyatli manfiy dalil bilan:** ROI oralig'ining *yuqori* chegarasi < 0 (yoki CLV da ≥ 50 qiymat bo'lsa, CLV oralig'ining yuqori chegarasi < 0). Ketma-ket yo'qotishlar o'z-o'zidan hech narsa qilmaydi (testlangan: yaxshi segmentda 8 ta ketma-ket mag'lubiyat → o'zgarish yo'q).
   - **`RAISE_THRESHOLD`:** EV chegarasi `RAISED_EV_FLOOR=0.08` ga ko'tariladi (foydalanuvchining o'z chegarasidan past bo'lmasligi uchun: effektiv chegara — ikkalasining kattasi).
   - **`DISABLE`:** shrunk ROI ≤ −5% **va** ROI oralig'i < 0 **va** (CLV yetarli bo'lsa) CLV oralig'i ham < 0.
5. **Yengillashtirish (gisterezis):** `RAISED` → `ACTIVE` faqat `MIN_HOLD_DAYS=14` o'tgach va **ijobiy nuqtaviy dalil** bilan (shrunk ROI > 0 va CLV ≥ 0) hamda ahamiyatli manfiy emas. "Ahamiyatli yomon" va "aniq yaxshi" orasidagi o'lik zona holatni saqlaydi, shuning uchun haftama-hafta chayqalish yo'q (testlangan). Kuchli dalil (`DISABLE`) hold davrida ham ishlaydi.

Natija — `SegmentDecision(action, state, ev_floor, reason, detail)`; `reason` — barqaror kod (`SegmentReason`), foydalanuvchi matni `bot/texts.py` da.

### 4. Kalibratsiya (`models/calibration.py`)
- **Usul:** isotonic regressiya (PAV, o'z amalga oshirishimiz: ~20 qator, deterministik, parametrlari JSON `{"x": [...], "y": [...]}`; oraliqda chiziqli interpolyatsiya). Chiqish `[0.01, 0.99]` ga qisiladi (bosqich 0 yoki 1 ishonch emas).
- **Sizib chiqishsiz (walk-forward):** faqat `settled_at < cutoff` bo'lgan namunalar; ular `(settled_at, p, hit)` bo'yicha tartiblanadi; **eski 70% o'qitish, yangi 30% hold-out** (fit hold-outni ko'rmaydi). Kelajak natijalarni o'zgartirish avvalgi kalibratsiyani o'zgartirmasligi testlangan.
- **Minimal namuna:** 200 (hold-out ≥ 60). Kamroq bo'lsa fit qaytarilmaydi.
- **Faollashtirish darvozasi:** hold-out'da signal bo'yicha Brier yutug'i `(p−y)² − (f(p)−y)²` ning o'rtachasi ≥ `MIN_BRIER_GAIN=0.001` **va** uning bir tomonlama 95% bootstrap quyi chegarasi > 0 (qaror #11 ruhida: nuqtaviy baho emas, CI). Shuning uchun yaxshi kalibrlangan model xaritani **hech qachon** olmaydi (yutuq — shovqin), kichik namuna ham (quyi chegara < 0). `brier_before/after` — hold-out raqamlari (halol dalil); yuborilayotgan xarita barcha mos namunada qayta fit qilinadi (ko'proq ma'lumot; ADR da ochiq aytilgan kichik nomuvofiqlik: sinalgan xarita 70% da fit).
- **Qo'llash:** `apply_calibration`. Bir bozorning to'liq bo'linadigan tanlovlari (yig'indi 1: 1X2, Over/Under butun-bo'lmagan chiziq) **birga** kalibrlanadi va qayta normallanadi (`calibrate_complementary`); qolganlari (Osiyo chizig'i push bilan, qisman) alohida. Osiyo natija taqsimoti `rescale_outcomes` bilan yutuq tomoni va yutqazish tomoni proporsional masshtablanadi, EV qayta hisoblanadi.
- **Versiyalash va orqaga qaytarish:** `CalibrationVersion(version, method, params, n_samples, brier_before, brier_after, created_at, passed_gate)`. Faqat `passed_gate` versiya faol bo'la oladi; `select_rollback` — oldingi o'tgan versiya (yo'q bo'lsa — xom model).
- **Tanlov xatosi haqida:** signallar faqat yuqori EV tanlangan nomzodlar, kalibratsiya shu tanlangan namunada o'rganilib, barcha nomzodlarga qo'llanadi. Bu rejadagi maqsad ("model 60% desa...") bilan mos; kengroq namunada o'rgatish kelajak ishi.

### 5. O'z-o'zini o'qitishdan saqlanish (`p_raw`)
Kalibratsiya o'zining chiqishida o'qitilsa, har hafta xarita ustiga xarita qo'yilar edi. Shuning uchun `signals.p_raw` (Alembic `0008`) kalibratsiyagacha bo'lgan ehtimollikni saqlaydi; o'rganish `p_raw ?? p_model` dan o'qiydi. Kalibratsiya faol bo'lmaganda `p_raw = NULL` (p_model xom).

### 6. Pipeline integratsiyasi (kichik, qo'shimcha)
- **`app/pipeline/learning.py`** — haftalik job (dushanba 09:00, `settle` dan keyin), ledger kaliti `(learning, haftaning dushanbasi)`: bir haftada ikki marta chaqirilsa — hech narsa. Bitta tranzaksiya: o'rtada to'xtasa oldingi holat qoladi. Bir xil ma'lumotda qayta ishga tushirish bir xil qatorlarni yozadi, jurnalga yozuv qo'shmaydi va bir xil kalibratsiyani (xuddi shu `n_samples` + `params`) ikki marta saqlamaydi.
- **`app/pipeline/overlay.py`** — tahlil yo'li uchun o'zgarmas `LearningOverlay(calibration, rules)`; `DbLearningSource` o'qiy olmasa yoki parametrlar buzilgan bo'lsa **xom modelga qaytadi** (loglanadi) — o'rganish muammosi tahlilni to'xtatmasligi kerak.
- **Hook:** `analyze.py` (`_value` boshida bitta o'qish; `_value_one` da filtr), `recalc.py` va `detail.py` (faqat kalibratsiya), `valuation.value_fixture` (`calibration=`), `persist`/`recalc` (`p_raw`). Segment filtri faqat tavsiyalarga qo'llanadi: "hammasini ko'rsat" (custom) va kuzatuvdagi o'yinlar filtrlanmaydi; mavjud signal qayta hisoblashda filtrlanmaydi.
- **Jurnal** `learning_changes` (Alembic `0008`): `kind`, `subject`, `before`, `after`, `reason`, `rolled_back`. `rollback_learning_change` kalibratsiyani oldingi versiyaga qaytaradi yoki segmentni oldingi holatga qaytarib `MANUAL_HOLD=30` kunga muzlatadi (keyingi hafta hukm odam qarorini bekor qilmasin).

## Nosozlik rejimlari
| Holat | Natija |
|---|---|
| Job o'rtada to'xtadi | tranzaksiya qaytadi; ledger lease tugagach keyingi chaqiruv qayta urinadi |
| Job ikki marta | ikkinchisi `skipped` |
| Bo'sh/kam tarix | `TOO_FEW_SAMPLES`, hech narsa yozilmaydi |
| Saqlangan xarita buzilgan | `DbLearningSource` xom modelga qaytadi, log |
| Baza o'qilmadi (migratsiya yo'q) | xuddi shunday |
| Rollbackdan keyin dalil hanuz yomon | 30 kun muzlatish |

## Cheklovlar (halol)
- Kam signalli real tizimda (kuniga bir necha signal) kalibratsiya darvozasi uzoq vaqt o'tmaydi: kichik og'ish (~8 p.p.) uchun ishonchli aniqlash ~ minglab signal talab qiladi. Bu xususiyat: tizim "yetarli dalil yo'q" deydi, tasodifga moslashmaydi.
- Faol kalibratsiya, yangi fit yaxshilanmasa ham, qo'lda qaytarilmaguncha qoladi (avtomatik o'chirish yo'q).
- Hold-out bootstrap mustaqil namunalarni nazarda tutadi (vaqt korrelyatsiyasi hisobga olinmagan).
- Segment `market` kesimi chiziqsiz; ko'p chiziqli bozorlar bitta segmentga birlashadi.
