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


---

## Qo'shimcha (T5 fix round, 2026-10-23): takroriy tekshiruv, rejimlar va rollback qoidalari

**Kontekst.** T5 QA ([docs/qa/T5-learning.md](../qa/T5-learning.md)) topdi: haftalik tekshiruv 52 marta takrorlanadi, shuning uchun 95% oraliq nol-ustunlikli segmentlarning 9–23% ini yiliga bir marta "tuzatadi", mukammal modelga ~25% ehtimol bilan soxta kalibratsiya beradi (T5-QA-1/3); push natijalar kalibratsiyani buzadi (T5-QA-4); sinov davri eski tarix bilan baholanadi (T5-QA-2); qo'lda rollback keyingi haftada bekor bo'ladi yoki yangi holatni o'chiradi (T5-QA-5/6); buzilgan `params` tahlilni yiqitadi (T5-QA-7). Quyidagi qarorlar ularni yopadi. Eslatma: yuqoridagi asosiy matndagi "95%" va "bir tomonlama 95%" endi **99.5%** (pastga qarang).

### D1. O'rganish rejimi `LEARNING_MODE` = `off | shadow | apply` (standart `shadow`)
- `off`: job hech narsa qilmaydi (ledger'da hafta band qilinmaydi).
- `shadow` (standart): job hamma narsani hisoblaydi va yozadi (`segment_stats`, `calibration_versions`), har bir o'zgarishni `learning_changes` ga **`applied=False`** ("shunday qilgan bo'lardi") bilan yozadi, lekin tahlil yo'li `NoLearning` oladi: EV chegarasi ko'tarilmaydi, segment o'chirilmaydi, kalibratsiya qo'llanmaydi.
- `apply`: avvalgi xatti-harakat (`applied=True`), pastdagi tuzatishlar bilan.
- Ulash: `runtime.open_runtime` faqat `apply` da `DbLearningSource` beradi; `PipelineContext.learning_settings = LearningSettings(mode, manual_hold)`. Alembic `0009`: `learning_changes.applied` (eski qatorlar `true`), `segment_stats.flagged`. Repo: `list_learning_changes(..., applied=None|True|False)`, `LearningChange.applied`. `/ozgarishlar` (keyingi vazifa) shadow yozuvlarni alohida ko'rsatishi mumkin.
- **`shadow` → `apply` o'tish — odam qarori**, paper trading (F10) davomida shadow jurnalini ko'rib chiqqandan keyin. Eslatma: shadow rejimda holat mashinasi "o'ylab" yuradi (segment qatorlari yangilanadi), shuning uchun `apply` ga o'tganda shu holat darhol kuchga kiradi; jurnaldagi eski yozuvlar `applied=False` bo'lib qoladi. Bu ataylab: odam aynan ko'rgan holatni yoqadi.

### D2. Takroriy tekshiruvga qarshi himoya (segmentlar, T5-QA-1)
Uch qatlam (hammasi `models/segments.py`; yagona kirish nuqtasi `review_segments`, job va simulyatsiyalar bir xil kodni ishlatadi):
1. **99.5% ikki tomonlama oraliq** (`CI_LEVEL=0.995`, 2000 bootstrap) — ROI uchun va (yetarli bo'lsa) CLV uchun; `RAISE` ham, `DISABLE` ham ROI **va** CLV ning **VA** (AND) sharti bilan (avval RAISE da `OR` edi: CLV yolg'iz o'zi segmentni qattiqlashtirmaydi).
2. **Benjamini–Hochberg (FDR)** shu hafta ko'rilgan barcha segmentlar (n ≥ 50, o'chirilmagan) bo'yicha, `FDR_LEVEL=0.05`. p-qiymat: `Φ(shrunk / se)`, `se` — bootstrap standart xatosi (shrinkage vazni bilan); qiymat `Estimate.p_value`. Bitta segment bo'lsa BH oddiy 5% testga tushadi (99.5% oraliq qattiqroq); yuzlab segmentda esa chegara `0.05·k/m` ga tushadi.
3. **Ketma-ket ikki haftalik run.** Dalil birinchi marta `segment_stats.flagged=True` bilan belgilanadi (`reason=awaiting_confirmation`, holat o'zgarmaydi); keyingi hafta ham dalil bo'lsa — harakat. Dalil yo'qolsa bayroq tushadi.

**Simulyatsiya** (koeffitsient 2.0, 52 hafta, ROI haqiqatda `2p−1`, har dunyoda 20 segment; nol-ustunlik = `p=0.5`):

| Stsenariy | Signal/hafta | Segmentlar | Oldin: kamida bitta RAISE/DISABLE | Keyin | Oldin: 3+ holat o'zgargan | Keyin | Holat o'zgarishi/segment/yil (keyin) |
|---|---|---|---|---|---|---|---|
| ROI = 0 | 3 | 600 | 8.7% | **1.17%** | 6.7% | 0.17% | 0.025 |
| ROI = 0 | 8 | 400 | 12.5% | **1.00%** | 9.6% | 0.25% | 0.023 |
| ROI = −20% (p=0.40) | 3 | 200 | 92% (median 17-hafta) | 68% (median 23) | — | 14% | 1.43 |
| ROI = −20% | 8 | 200 | o'lchanmagan | 95.5% (median 14) | — | 65.5% | 3.3 |
| ROI = −10% (p=0.45) | 8 | 200 | 81% | 40.5% (median 26) | — | 10% | 0.91 |

Talqin: soxta harakat ~9–12% dan ~1% ga tushdi (maqsad ≤ ~1%). Narxi — quvvat: −20% ROI da 3 signal/hafta bo'lgan segmentning ~2/3 qismi bir yilda topiladi (avval ~9/10), −10% da yarmidan kamrog'i (avval 4/5) — 50–150 signalli namunada 99.5% oraliq nolni ajratishi uchun haqiqatan ~200+ signal kerak. Bu ataylab tanlangan: tizim "yetarli dalil yo'q" deydi; yo'qolgan foyda (yomon segmentni bir necha oy kech o'chirish) soxta o'chirishdan (yaxshi segmentni yo'qotish) arzonroq. Haqiqatan yomon segmentlarda 3+ o'zgarish (o'chirish → 60 kun → sinov → yana o'chirish) kutilgan davriy hodisa, nol-ustunlikda esa yo'q.

### D3. Sinov davri va gisterezis (T5-QA-2)
- `RAISED` (sinov, shu jumladan o'chirishdan qaytganda) segment statistikasi **faqat `state_since` dan keyingi** o'yinlar (kickoff kuni ≥ `state_since` sanasi) bo'yicha hisoblanadi (`compute_segment_stats(..., since_block=)`, `review_segments` avtomatik beradi). Global o'rtacha (shrinkage nishoni) hamma ma'lumotdan. Sinov davrida 50 yangi signal bo'lmaguncha `too_few_signals` — holat saqlanadi (oldingi zaharli tarix qaror qilmaydi: o'chirish → 60 kun → 7 kun sinov → yana o'chirish sikli yo'q).
- `_from_raised` da `_held` (14 kun) **birinchi** tekshiriladi: hold davrida `DISABLE` ham bo'lmaydi (avval "kuchli dalil hold ni buzadi" edi).
- Simulyatsiya (nol-ustunlik, yuqoridagi jadval): holat o'zgarishi segment-yilga 0.023–0.025, 3+ o'zgarish ≤ 0.25% (avval 6.7–9.6%).

### D4. Kalibratsiya darvozasi (T5-QA-3)
`ONE_SIDED_CONFIDENCE=0.995`, 2000 bootstrap, va **ketma-ket ikki haftada** darvozadan o'tish (`calibration.confirmed(fit, previous_pass_at=)`: oldingi run versiyasi ham `passed_gate`, oraliq ≤ 10 kun — o'tkazib yuborilgan hafta zanjirni uzadi). Faol xarita bor bo'lsa, yangisi faqat `n_samples ≥ 1.25 ×` faolning n_samples bo'lganda almashtiradi (haftalik chayqalish va jurnal shovqini yo'q; `REFRESH_GROWTH`).

Simulyatsiya (p ~ U(0.45; 0.75), hit-rate = p − og'ish, haftalik refit, 52 hafta, `confirmed` bilan):

| Og'ish | Signal/hafta | Dunyolar | Oldin (95%, bir martalik) | Keyin (99.5% + ketma-ket 2) | Median hafta (keyin) |
|---|---|---|---|---|---|
| 0 (mukammal) | 20 | 40 | 22.5% | **0%** (0/40; 0.99 da 0/100) | — |
| 0 (mukammal) | 50 | 100 | 27.5% | **0%** (0/100) | — |
| 0.05 | 50 | 20 | 70% | 5% | 27 |
| 0.08 | 20 | 20 | 85% | 10% | 39 |
| 0.08 | 50 | 20 | 100% | 35% | 37 |
| 0.12 | 50 | 20 | — | 95% | 31 |
| 0.15 | 50 | 20 | — | 100% | 18 |
| 0.15 | 20 | 20 | — | 80% | 28 |

Soxta faollashuv ≤ 3% maqsadiga erishildi (kuzatilgan 0/140; 95% yuqori chegara ≈ 2–3%). Narxi: kichik og'ish (8 p.p.) 50 signal/hafta da bir yilda 35% ehtimol bilan topiladi (avval 100%), katta og'ish (12–15 p.p.) hali ishonchli topiladi. Taqqos uchun muqobil: 0.99 + ketma-ket 2 → 0.08/50 da 45%, mukammal modelda 0/100; ADR talabi (0.995) saqlandi, kerak bo'lsa `CalibrationPolicy(confidence=0.99)` bilan yumshatiladi.

### D5. Push (VOID) va kalibratsiya (T5-QA-4)
Butun chiziqli bozorlar (Total 2.0, handikap 0 / −1: `pipeline.learning.can_push`) push massasiga ega; push `void` bo'lib saqlanadi va o'rganishga kirmaydi, shuning uchun kuzatilgan "hit" chastotasi `P(win | no push)` bo'lib qoladi, model esa `P(win)` deydi. Tanlov: **bunday bozorlar kalibratsiya ma'lumotidan chiqariladi** (yarim va chorak chiziqlar — push yo'q — qoladi). Muqobil (`p/(1−p_push)` bilan solishtirish) modelning push ehtimolini har signal uchun saqlashni talab qiladi — hozir bunday maydon yo'q, shuning uchun rad etildi. Test: mukammal Total 2.0 modeli (P(over)=0.4, P(push)=0.2) — `TOO_FEW_SAMPLES`, hech qanday xarita. **Ma'lum cheklov:** segment ROI da push (0 daromad) hali ham tashlanadi, shuning uchun push ko'p bozorlarda |ROI| `1/(1−P(push))` marta oshib ko'rinadi; ahamiyati kichik (qarorlar 99.5% oraliq va −5% chegarasi bilan), lekin keyingi ishda `void`-push ni 0 daromad bilan qo'shish mumkin (`settlement_returns` `PUSH` ni biladi).

### D6. Rollback qoidalari (T5-QA-5/6) va buzilgan ma'lumot (T5-QA-7)
- Kalibratsiyani qo'lda qaytarish (`calibration_rolled_back`) `LEARNING_MANUAL_HOLD_DAYS` (standart 30) davomida yangi faollashtirishni muzlatadi (yangi versiya baribir saqlanadi). Segment rollback uchun ham xuddi shu sozlama.
- Rollback faqat o'zgarish yaratgan holat hali joriy bo'lsa ishlaydi: kalibratsiya uchun `after.version == joriy faol`, segment uchun `(state, ev_threshold, enabled) == after`; aks holda `RollbackResult.STALE` (log, hech narsa yozilmaydi, yozuv `rolled_back` bo'lmaydi). Yangi API: `rollback_change(ctx, id) -> RollbackResult`; `rollback_learning_change` — uning `bool` ko'rinishi.
- `overlay.version_of`: `params` dict emas → `ModelInputError` → `DbLearningSource` bo'sh overlay qaytaradi (log). Tahlil, qayta hisoblash va detal yo'li `overlay.load_overlay(source)` orqali o'qiydi: istalgan istisno → xom model (log). O'rganish tahlilni hech qachon yiqitmaydi.

### D7. Boshqa
- `active_calibration` `passed_gate=True` ni talab qiladi (darvozadan o'tmagan versiya faol belgilangan bo'lsa ham ishlatilmaydi).
- Retro kartalar bugungi overlay'ni olmaydi (xom model): retro "o'sha paytda model nima derdi" ko'rsatkichi, bugungi kalibratsiya/qoidalar kelajak bilimi bo'lardi. Eng sodda va izchil variant (bayroq emas).
- Haftalik job muvaffaqiyatsiz bo'lsa va qayta urinish haftadan o'tib ketsa, hafta yo'qoladi — qolgan eslatma (QA); `/holat` da ko'rsatish vazifasi bot tomonida.
