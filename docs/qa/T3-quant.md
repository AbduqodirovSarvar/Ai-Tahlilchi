# QA hisoboti: T3 — Bozor modeli, ansambl, ishonch darajasi, walk-forward backtest (F3.3–F3.9)

- **Tekshiruvchi:** test-engineer (mustaqil QA)
- **Sana:** 2026-10-21
- **Branch:** `wave/T3-qa-quant` (`main` `dce179e` dan)
- **QA commitlari:** `572999d` (oracle testlar), `630f641` (backtest yaxlitligi), `ab1abd6` (skript e2e), shu hisobot
- **Muhit:** Windows 11, Python 3.13.15, `python -m uv run --no-config`. Tarmoq yo'q. Ishlab chiqarish kodi (`src/`, `scripts/`) o'zgartirilmadi.

## Yakuniy xulosa

### ✅ Darvozadan o'tadi (T3 vositasi sifatida), 1 ta 🟠 topilma bilan

🔴 topilma yo'q: hisob-kitob (settlement), EV, ROI, CLV, ansambl, bozor modeli va ishonch darajasi mustaqil oracle'lar bilan to'g'ri chiqdi; sizib chiqish (leakage) yo'q, hold-out bir marta baholanadi, natija seed bo'yicha aniq takrorlanadi. Commit qilingan sintetik hisobot bayt-bayt qayta olindi.

**Lekin** 🟠 F-1 haqiqiy ma'lumotdagi F3 darvozasidan **oldin** yopilishi kerak: football-data importidan keyin standart ishga tushirishda hali tugamagan joriy mavsum (2026) hold-out bo'lib qoladi. Vaqtinchalik yo'l: serverda `scripts/backtest.py --holdout-season 2025`. 3 ta 🟡 topilma va 2 ta metodologik eslatma pastda. Har bir haqiqiy xato `xfail(strict=True)` test bilan qotirilgan: tuzatilganda test XPASS bo'lib yiqiladi va belgini olib tashlash kerak bo'ladi.

Eslatma: bu xulosa **vositani** tasdiqlaydi. F3 darvozasining o'zi (modelning bozordan yaxshiligi) faqat haqiqiy tarixda tekshiriladi (qarorlar jurnali #7) va hali kutilmoqda.

## quant-modeler da'volarining tekshiruvi

| Da'vo | Natija | Dalil |
|---|---|---|
| `src/app/models` qamrovi ≥ 90% | ✅ | QA dan oldin: `908 passed`, umumiy 99.64%; models fayllari 98–100% (`devig.py` 98%, `adapters.py`, `outcomes.py`, `metrics.py`, `report.py` 99%, qolganlari 100%). `coverage_gate.py`: pastda |
| Sintetik samarali bozorda ustunlik yo'q, CLV < 0 | ✅ | Mavjud test (seed 11) + QA mustaqil ravishda **yangi 5 seed** (21–25) × `w ∈ {1, 0.5}` da ishga tushirdi: 10/10 da CLV −4.3% … −5.5%, yopilishni yutish ulushi 0.000, ROI CI kutilgan EV ni 10/10 da qamraydi, CI ning pastki chegarasi doim < 0. Seedlar bo'yicha o'rtacha ROI (w = 1) −5.7%, kutilgan −4.6% |
| Sizib chiqish testlari bor va ma'noli | ✅ | Mavjud: har bir fit instrumentlangan (`newest < as_of`), kelajak hisoblari o'zgartirilsa o'tmish bashorati bir xil, closing narxlar qarorga ta'sir qilmaydi. QA kuchaytirdi: **kutubxona chegarasida** (HistoryBatch → `run_experiment`) natija **va narxlar** o'zgartiriladi, stavkalar to'liq solishtiriladi; mutatsiya bilan tekshirildi (pastda) |
| Bo'sh baza → exit 2 + aniq xabar | ✅ | Haqiqiy **CLI jarayoni** (`subprocess`, Alembic bilan yaratilgan bo'sh baza): `returncode == 2`, chiqishda `no history in DB - run scripts/import_history.py first`, hisobot papkasi yaratilmaydi |
| Hisobotda seed, ma'lumot izi, commit | ✅ | Skript orqali sintetik hisobot: `Git commit` 40 belgili hash (`-dirty` ixtiyoriy), `Ma'lumot izi (SHA-256)` mustaqil hisoblangan `dataset_fingerprint(generate_history(...))` ga teng, `Seed (bootstrap)` va `synthetic.seed` berilgan seed. Iz tartibga bog'liq emas va bitta narx 0.01 ga o'zgarsa o'zgaradi |
| Commit qilingan sintetik hisobot qayta olinadi | ✅ | `scripts/backtest.py --synthetic` (standart parametrlar, 23 s) qayta ishga tushirildi: `docs/backtest/2026-10-21-synthetic.md` bilan farq **faqat** `Sana` va `Git commit` qatorlarida (`diff --strip-trailing-cr`); barcha raqamlar, 144 konfiguratsiya, tanlov va hold-out bir xil |

## F3 qabul mezonlari (shu to'lqin doirasi)

| ID | Mezon | Holat | Dalil |
|---|---|---|---|
| F3.3 | Bozor modeli: Pinnacle de-vig → adolatli ehtimollik, zaxira | ✅ | Pinnacle ustun; faqat `market_avg` bo'lsa u ishlatiladi va `wide_uncertainty=True`; Pinnacle to'plami to'liq bo'lmasa (bitta narx yo'q) tashlanadi va zaxiraga o'tiladi; `bet365`/`market_max`/boshqa `kind` → bozor bahosi yo'q. Proporsional de-vig qo'lda hisoblangan (0.482759 / 0.275862 / 0.241379) bilan 1e-12 da mos; ikki tomonli Shin = additiv yopiq formula |
| F3.4 | Ansambl `p = w·p_DC + (1−w)·p_bozor` | ✅ | hypothesis (200 misol × 2): ehtimolliklar [0, 1] da, 1X2 yig'indisi 1 (1e-12), butun/chorak Osiyo chiziqlarida ikkinchi tomon = birinchisining ko'zgusi, natija tanlovma-tanlov chiziqli aralashma bilan bir xil; `w ∉ [0, 1]` va `NaN` rad etiladi |
| F3.5 | EV, Kelly, flat (T2 da qabul qilingan) | ✅ | Qo'lda hisoblangan 5 stavkali daftar (pastda) |
| F3.6 | Ishonch darajasi | ✅ (F-2 bilan) | Barcha chegaralar aniq chegarada: 6/5 va 15/14 o'yin, 3.0 / 3.0001 kun, kalibratsiya 0.05 / 0.0501, farq 0.08 va 0.15 atrofida. Float chegarasida nomuvofiqlik: F-2 |
| F3.7 | Walk-forward, grid, liga va bozor kesimlari | ✅ (F-1, F-3 bilan) | Hold-out bir marta (instrumentlangan), kelajak perturbatsiyasi, determinizm — pastda |
| F3.8 | Hisobot: ROI, yield, signallar, Brier/log-loss, kalibratsiya, drawdown | ✅ | Hisobot qayta olinadi; ROI/drawdown/hit-rate/CLV qo'lda hisoblangan daftar bilan mos |
| F3.9 | Unit testlar: yig'indi = 1, ma'lum misollar, EV | ✅ | Mavjud testlar + QA oracle testlari |
| F3 darvozasi (haqiqiy ma'lumot) | Kalibratsiya, ≥ 500 signal ROI ≥ 0, EV ≥ 5% da ROI > 0, ≥ 3 liga | — kutilmoqda | Bu mashinada haqiqiy tarix yo'q (#7). Sintetikda ❌ — kutilgan natija (samarali bozor). Serverda ishga tushirishdan oldin F-1 ga qarang |

## QA qo'shgan testlar (risk asosida, mustaqil oracle)

Hammasi `tests/integration/` da, tez (jami ≈ 10 s), shuning uchun `slow` belgisi kerak bo'lmadi.

**`test_t3_quant_oracles.py`** (102 test + 2 xfail)
- **Settlement haqiqat jadvali:** 70 qator qo'lda yozilgan (1X2, ikki natija, BTTS, total 0.5/2.0/2.25/2.5/2.75/3.0, fora 0, ±0.25, ±0.75, −1.0, −1.25, −1.75, +1.5, −2.0, ikkala tomon). Qo'shimcha: mustaqil "yarim stavka har qo'shni chiziqqa" oracle'i (butun sonli chorak gollarda) bilan hypothesis: hisob 0–9, fora −3.5…+3.5 va total 0.25…7.5 ning **barcha** chorak qadamlari.
- **EV/ROI/CLV daftari** (`candidate_bets` → `summarize_bets` orqali, flat 1 birlik):

| # | Bozor | Natija | Koeff. | EV (qo'lda) | Hisob | Foyda | Yopilish adolatli koeff. | CLV |
|---|---|---|---|---|---|---|---|---|
| 1 | 1X2 uy | 2–1 | 2.50 | +0.25 | win | +1.50 | 2.5 | 0 |
| 2 | AH 0.0 uy | 1–1 | 1.90 | +0.105 | push (void) | 0 | 1.875 | ln(1.90/1.875) |
| 3 | Total 2.25 over | 1–1 | 2.00 | +0.05 | half loss | −0.50 | 2.125 | ln(2.00/2.125) |
| 4 | AH −0.75 uy | 2–1 | 2.20 | −0.02 | half win | +0.60 | 2.25 | ln(2.20/2.25) |
| 5 | Total 2.5 under | 2–2 | 1.80 | +0.08 | loss | −1.00 | 1/0.55 | ln(0.99) |

  Kutilgan yig'ma: aylanma 5, foyda +0.6, **ROI +12%**, hit rate 40%, maks. pasayish 1.0 (1.6 → 0.6), yopilishni yutish 20% (faqat #2), yopilish narxidagi ROI +6%. Barchasi mos. Kelly foydasi = Kelly stavkasi × o'sha birlik qaytimi, stavka ≤ 2% bank.
- **Ikki tomonli Osiyo de-vig** (hypothesis): `asian_from_two_way` natijasida ikkala tomon o'z adolatli koeffitsienti `1/p` da EV = 0 (EV qo'lda yig'ilgan, `asian_expected_value` ishlatilmagan).
- **Bozor modeli** zaxira tartibi va to'liq bo'lmagan to'plamlar (yuqorida).
- **Ishonch darajasi** aniq chegaralari (17 holat) + 2 strict xfail (F-2).

**`test_t3_backtest_integrity.py`** (5 test + 1 xfail)
- **Hold-out bir marta:** `walk_forward`, `_grid_rows`, `_choose`, `evaluate` instrumentlandi. Hold-out mavsumi aynan **bitta** `walk_forward` va **bitta** `evaluate` chaqiruvida, ikkalasi ham `_choose` dan **keyin**; grid faqat validatsiya bashoratlarini ko'radi; validatsiya yurishlari soni = `len(xis)`.
- **Determinizm:** bir xil ma'lumot va seed → `rows`, `chosen`, hold-out stavkalari, xulosa va **render qilingan hisobot matni** bir xil. Bootstrap CI faqat seedga bog'liq (boshqa seed → boshqa CI, ROI o'zgarmaydi).
- **Kelajak perturbatsiyasi (kutubxona chegarasida):** hold-out mavsumining 1/3 qismidagi kesish nuqtasidan boshlab barcha natijalar va `taken_at ≥ kesish` bo'lgan barcha narxlar o'zgartirildi. Natija: grid qatorlari, tanlov va validatsiya stavkalari bir xil; kesishdan oldingi hold-out stavkalari **barcha maydonlari bilan** (settlement, CLV, yopilish foydasi ham) bir xil; kesish kunidagi stavkalar qarori (bozor, tanlov, koeff., p, EV, stavka) bir xil; keyingi stavkalar haqiqatan o'zgargan (perturbatsiya ishlagan).
- **Mutatsiya tekshiruvi:** fit oynasi `bisect_right` + `as_of + 1 kun` ga o'zgartirilsa (bir kunlik sizib chiqish), test **yiqiladi** (kesish kunidagi qarorlar farq qiladi). Shu sababli test `refit_days=7` bilan (har o'yin kuni refit). Faqat `bisect_right` mutatsiyasi esa DC fitterning o'zi tomonidan to'xtatiladi (`as_of` dagi o'yin → xato) — ADR 0003 dagi himoya haqiqatan ishlaydi.

**`test_t3_backtest_script.py`** (3 test + 1 xfail)
- Bo'sh baza: haqiqiy CLI jarayoni, exit 2 va xabar (yuqorida).
- **Commit qilingan football-data fixture'lari:** Alembic baza → `scripts/import_history.py --offline` (E0, SP1, D1; 2021–2024; 9 o'yin) → `scripts/backtest.py`. To'g'ri natija: **exit 2**, `no validation predictions could be scored; check data coverage`, hisobot yozilmaydi. Sabab: uchta mavsum bor (SP1 2021, D1 2023, E0 2024), lekin har bir ligada bitta mavsum, demak validatsiya o'yinlaridan oldin hech bir ligada tarix yo'q. Hisobot "hech narsa" asosida qurilmaydi — to'g'ri xatti-harakat.
- Sintetik hisobot ikki marta: seed/iz/commit qatorlari va `Sana` dan boshqa butun matn bir xil.

## Topilmalar

### 🟠 F-1. Hali tugamagan joriy mavsum standart hold-out bo'lib qoladi
- **Joy:** `scripts/backtest.py:164-177` (`_to_batch`): mavsum faqat bazada `FINISHED` bo'lmagan o'yini bo'lsa "tugamagan" deb hisoblanadi.
- **Muammo:** football-data fayllarida faqat **o'ynalgan** o'yinlar bor (o'ynalmaganlar umuman qator emas; importer hisobsiz qatorni tashlaydi). `import_history.py` standart diapazoni `2021..joriy mavsum` (bugun 2026). Natijada bazada 2026 mavsumining bir necha turi bor, ularning hammasi `FINISHED` → 2026 "tugagan" hisoblanadi va **hold-out bo'ladi**, 2025 esa validatsiyaga ketadi. Skript docstringi ("unplayed fixtures are never used as the hold-out") va ADR 0005 ("hold-out = oxirgi **tugagan** mavsum") bilan zid. Shuningdek, `complete` bo'sh bo'lsa (177-qator) `holdout=None` → `split_seasons` oxirgi (tugamagan bo'lishi mumkin) mavsumni oladi.
- **Ta'sir:** haqiqiy F3 darvozasi bir necha haftalik qisqa mavsumda baholanadi (≥ 500 signal deyarli mumkin emas, natija shovqinli), to'liq oxirgi mavsum esa ko'p sinovli tanlovga sarflanadi. Darvoza qarori noto'g'ri asosda qabul qilinishi mumkin.
- **Takrorlash:** `tests/integration/test_t3_backtest_script.py::test_a_season_still_in_progress_is_not_the_default_holdout` (strict xfail): 2023–2026 sintetik mavsumlar, 2026 ning faqat birinchi 1/3 qismi bazada (hammasi tugagan) → hisobotda `hold-out 2026` (10 bashorat), kutilgan `hold-out 2025`.
- **Tavsiya (quant-modeler):** mavsumni to'liq deb faqat aniq belgi bilan hisoblash (masalan, o'yinlar soni `n·(n−1)` ga teng, yoki bugungi sana tushgan mavsum hold-out emas), aks holda aniq xabar bilan `--holdout-season` talab qilish. Hozircha serverda `--holdout-season 2025` ni aniq bering.

### 🟡 F-2. 8 va 15 p.p. chegaralari float ayirmada solishtiriladi
- **Joy:** `src/app/models/confidence.py:166-168` (`disagreement > SANITY_DISAGREEMENT`, `> MODERATE_DISAGREEMENT`), farq `abs(p_model − p_market)` xom float.
- **Muammo:** aynan 15 p.p. farq juftlikka qarab turlicha baholanadi: ikki xonali 86 juftlikdan (×2 yo'nalish) 102 holatda `LOW + sanity_flag`, 70 holatda `MEDIUM`. Masalan `(0.35, 0.50)` → sanity, `(0.10, 0.25)` → yo'q. 8 p.p. da ham xuddi shunday.
- **Ta'sir:** past: real ehtimolliklar ixtiyoriy float, aniq chegaraga tushish ehtimoli juda kichik. Lekin shartnoma "farq > 15 p.p." deydi va chegaradagi test natijasi tasodifiy.
- **Takrorlash:** `test_t3_quant_oracles.py::test_an_exact_fifteen_point_gap_is_judged_the_same_for_every_pair` va `..._eight_point_...` (strict xfail).
- **Tavsiya:** farqni solishtirishdan oldin yaxlitlash (masalan, `round(d, 12)`) yoki kichik tolerans.

### 🟡 F-3. `opening` deb belgilangan, lekin kickoff dan keyin olingan narx stavka qaroriga kiradi
- **Joy:** `src/app/models/adapters.py:133` (`complete_quotes`: eng yangi `taken_at` yutadi), `src/app/models/backtest/walk_forward.py:168` (faqat `kind` ga ishonadi).
- **Muammo:** bashorat narxlari faqat `kind` belgisi bilan tanlanadi, `taken_at < kickoff` tekshirilmaydi. Bitta o'yinga kickoff dan 30 daqiqa keyin qayta yuborilgan `opening` narx kelsa, u "eng yangi" sifatida oldingi narxni almashtiradi va qaror kelajak narxida qabul qilinadi.
- **Ta'sir:** football-data importida `opening` = kickoff − 1 kun, shuning uchun hozir amalda yuz bermaydi. Bu himoya qatlami (defense-in-depth) yetishmasligi: kelajakdagi manbalar (F4 pipeline, API) yoki import xatosi jim sizib chiqishga olib keladi.
- **Takrorlash:** `test_t3_backtest_integrity.py::test_an_opening_price_taken_after_kickoff_is_not_used_for_the_bet` (strict xfail).
- **Tavsiya:** `build_dataset`/`walk_forward` da bashorat narxlarini `taken_at < kickoff` bilan cheklash (yoki shunday narxni xato deb loglash).

### 🟡 F-4 (eslatma). Void stavka aylanmaga kiradi — konvensiya hujjatlanmagan
- **Joy:** `src/app/models/backtest/metrics.py` (`turnover = Σ flat_stake`), ADR 0005 §6.
- Push (void) stavka aylanmada qoladi: daftarda ROI = 0.6 / 5 = **12%**; void chiqarilsa 0.6 / 4 = 15% bo'lardi. Ikkalasi ham ishlatiladi; tanlov xato emas, lekin ADR da aniq yozilishi kerak (paper trading F10 va bot statistikasi bir xil konvensiyani ishlatishi uchun). QA testi joriy konvensiyani qotiradi.

### 🟡 F-5 (eslatma). "EV ≥ 5% filtrida ROI musbat" nuqta bahosi bo'yicha
- **Joy:** `src/app/models/backtest/report.py:87` (`_gated_ev_check`).
- Samarali (ta'rifiga ko'ra yutib bo'lmaydigan) sintetik bozorda bu mezon **✅** chiqdi: ROI +7.48%, CI [−15.14%; +29.26%], 89 signal. Mezon reja bo'yicha yozilgan, lekin haqiqiy ma'lumotdagi qarorda faqat nuqta bahosiga tayanmaslik, CI va signallar sonini (≥ 500 mezoni kabi) birga ko'rish tavsiya etiladi. Qaror birgalikda qabul qilinadi (reja F3).

## Test natijalari va to'liq darvoza

| | QA dan oldin | QA dan keyin |
|---|---|---|
| Testlar | 908 passed | **1018 passed, 4 xfailed** (strict), 116 s |
| Branch coverage (umumiy) | 99.64% | 99.64% (5058 stmt, 760 branch; QA testlari faqat `tests/` da) |
| `scripts/ops/coverage_gate.py` (models/pipeline ≥ 90%) | — | `OK */app/models/*: 99.73% of 1854 statements`; pipeline: SKIP (hali bo'sh) |
| `ruff check .` | — | `All checks passed!` |
| `ruff format --check .` | — | `136 files already formatted` |
| `mypy src` | — | `Success: no issues found in 63 source files` |
| `lint-imports` / `deptry .` | — | `Contracts: 6 kept, 0 broken.` / `No dependency issues found` |

**Barqarorlik:** uchta yangi modul 20 marta ketma-ket ishga tushirildi: 20/20 `110 passed, 4 xfailed` (har biri ≈ 10 s), yiqilish yo'q. Barcha tasodif seed bilan, vaqt va tarmoq ishlatilmaydi.

**Ishlash:** sintetik standart backtest 23 s (ADR: ≈ 50 s). Mavjud sanity testi bilan bir xil o'lchamdagi ishga tushirish alohida ≈ 2 s; to'liq to'plamda u 41 s o'lchandi — shu mashinadagi parallel yuklama (boshqa agentlar), isbotlanmagan.
