# ADR 0003: Dixon-Coles modeli, de-vig, EV va staking matematikasi

- **Holat:** qabul qilingan
- **Sana:** 2026-10-21
- **Muallif:** quant-modeler (T2)
- **Kod:** `src/app/models/` (`devig.py`, `poisson.py`, `score_markets.py`, `asian.py`, `ev.py`, `staking.py`)

## Kontekst
3-qadam (asosiy reja, 4-bo'lim) har bir o'yin uchun `p_model`, `p_bozor`, EV va stavka hajmini beradi. Bu formulalar butun tizim qarorlarining asosi: xato bo'lsa, signal ham, backtest ham noto'g'ri bo'ladi. Shuning uchun matematika, sonli barqarorlik va tanlangan variantlar shu yerda qayd etiladi. T2 da modul faqat primitivlar va o'z frozen dataclass'lari bilan ishlaydi; domen turlariga adapterlar T3 da qo'shiladi.

## 1. De-vig (marjani olib tashlash)
Bitta **to'liq** bozor (barcha o'zaro istisno natijalar: 1X2, bitta chiziqning over/under'i) narxlari `o_i > 1`. Implied ehtimollik `π_i = 1/o_i`, `Π = Σπ_i`, marja (overround) `Π − 1`.

| Usul | Formula | Xususiyati |
|---|---|---|
| Proporsional | `p_i = π_i / Π` | Oddiy; marjani teng taqsimlaydi, autsayderlarni oshirib baholaydi (favourite-longshot bias) |
| **Shin (standart)** | `p_i(z) = (√(z² + 4(1−z)π_i²/Π) − z) / (2(1−z))`, `z` shunday tanlanadiki `Σp_i(z) = 1` | Insayder ulushi `z` modeli; marjani asosan autsayderlarga yuklaydi, empirik jihatdan eng yaxshi kalibrlangan |
| Power | `p_i = π_i^k`, `Σp_i = 1` | Shin'ga o'xshash siljish, bitta parametr |

**Shin'ni yechish.** `f(z) = Σp_i(z) − 1`. `f(0) = √Π − 1 > 0` (Π > 1 da) va `z → 1` da `Σp_i → Σπ_i²/Π < 1` (barcha `π_i < 1`), demak `[0, 1)` oralig'ida ildiz doim bor va `brentq` (xtol 1e-14) kafolatli topadi. Marja yo'q yoki manfiy bo'lsa (`Π ≤ 1`, masalan turli bukmekerlarning eng yaxshi narxlari) Shin modeli ma'nosiz: `z = 0` va proporsional normallash qaytariladi. Yaxlitlash sababli `f(0) ≤ 0` bo'lib qolgan `Π ≈ 1 + 1e-16` holati ham shu yo'lga tushadi (hypothesis topgan holat, regressiya testi bor).

**Mustaqil tekshiruvlar (testlarda):** ikki natijali bozorda Shin = additiv usul (`p_i = π_i − (Π−1)/2`) va `z = (Π−1)(d²−Π) / (Π(d²−1))`, `d = π_1 − π_2`; teskari yo'nalish: haqiqiy `p` va `z` dan Shin narxlari `π_i = β·√(z p_i + (1−z) p_i²)` quriladi va `devig_shin` ularni 1e-8 aniqlikda tiklaydi. Power uchun ham xuddi shunday aylanma test.

**Power yechimi.** `g(k) = log Σ exp(k·log π_i)` log-fazoda (`logaddexp`) hisoblanadi, `k` bo'yicha qat'iy kamayadi. Yuqori chegara analitik: `g(k) ≤ log n + k·max log π_i`, shuning uchun `k_max = 2·log n / (−max log π_i)` da `g < 0` kafolatlangan (tsikl yo'q).

## 2. Dixon-Coles modeli
```
log λ = intercept + home_advantage + attack[home] − defence[away]
log μ = intercept + attack[away] − defence[home]
P(X = x, Y = y) = τ(x, y; λ, μ, ρ) · Pois(x; λ) · Pois(y; μ)
τ(0,0) = 1 − λμρ,  τ(0,1) = 1 + λρ,  τ(1,0) = 1 + μρ,  τ(1,1) = 1 − ρ,  boshqalarda 1
```
`attack` katta: ko'p gol uradi; `defence` katta: kam gol o'tkazadi. `τ` kichik hisoblar (0-0, 1-1) orasidagi bog'liqlikni tuzatadi va to'rt katakning umumiy massasini saqlaydi (`−λμρ + λμρ + λμρ − λμρ = 0` ko'paytuvchilar bilan).

**Likelihood (log-fazoda).** Har o'yin uchun `ℓ_k = log τ + x·log λ − λ + y·log μ − μ − log x! − log y!`. Maqsad funksiya: `−Σ w_k ℓ_k / Σ w_k` (og'irlik birligiga normallangan, shuning uchun optimizator toleranslari ma'lumot hajmiga bog'liq emas). Faktoriallar optimumni o'zgartirmaydi va faqat hisobot uchun qo'shiladi (`log_likelihood`, og'irlikli).

**Vaqt bo'yicha og'irlik.** `w_k = exp(−ξ · age_days)`, yosh `as_of` ga nisbatan. Og'irliklar eng yangi o'yinga nisbatan hisoblanadi (`exp(−ξ(age − min age))`): optimum umumiy ko'paytuvchiga invariant, lekin juda eski ma'lumotda barcha og'irliklar nolga tushib ketmaydi. Standart `ξ = 0.0019/kun` (Dixon-Coles'ning yarim haftaga 0.0065 ga teng; yarim yemirilish ≈ 1 yil); aniq qiymat walk-forward backtestda (F3.7) tanlanadi. Test: `ξ = ln2/Δ` bilan fit, yangi o'yinlar ikki marta takrorlangan `ξ = 0` fit bilan 1e-5 aniqlikda bir xil (og'irlikli MLE orakuli).

**Identifikatsiya.** `Σ attack = Σ defence = 0` parametrlash orqali **aniq** bajariladi: optimizator `n−1` erkin parametrni ko'radi, oxirgisi `−Σ(qolganlar)`. Umumiy daraja `intercept` da. Bundan tashqari jamoalar grafi bog'liq bo'lishi kerak: bir-biri bilan hech o'ynamagan guruhlar (masalan, ikki liga aralashtirilgan) nisbiy kuchini aniqlab bo'lmaydi, bu `ModelInputError` beradi. Model **har liga uchun alohida** fit qilinadi. Kuzatuvlar soni (`2 × o'yinlar`) parametrlar sonidan kam bo'lsa ham xato.

**Optimizatsiya.** L-BFGS-B, **analitik gradient** (`∂ℓ/∂log λ = x − λ + (∂τ/∂log λ)/τ`, so'ng `bincount` bilan jamoalarga zanjir qoidasi; test: `check_grad` xatosi < 1e-5), chegaralar: kuchlar `±4` (log), `intercept ∈ [−5, 3]`, `home_advantage ∈ ±1.5`, `ρ ∈ ±0.5`. Chegaralar real ligalarda faol bo'lmaydi, lekin degenerat jamoani (masalan, hech gol urmagan) `−∞` ga ketishdan saqlaydi (test bor). Chiziqli qidiruv `τ ≤ 0` nuqtaga tushsa, `τ` `1e-12` ga qisiladi (log to'siq), optimumda bu holat bo'lmaydi. `result.success` yolg'on yoki parametrlar cheksiz bo'lsa, `ModelFitError` (sabab va iteratsiyalar soni bilan). 20 jamoa × 8 mavsum (3040 o'yin) ≈ 20 ms.

**Ma'lumot sizib chiqishi.** `as_of` **eksklyuziv**: `match_date >= as_of` bo'lgan o'yin tashlab yuborilmaydi, balki xato beradi. Walk-forward'da bunday o'yin look-ahead xatosidir va jim o'tib ketmasligi kerak. Sanalar `datetime.date` (`datetime` emas: aralashtirish kun arifmetikasini buzadi).

**Noma'lum jamoa.** `UnknownTeamError`. Liga o'rtachasi bilan almashtirish **tanlanmadi**: noma'lum jamoalar asosan yuqori ligaga ko'tarilganlar va o'rtacha reyting ularni muntazam oshirib baholaydi (soxta "value"). Pipeline bunday o'yinni o'tkazib yuboradi (asosiy reja: < 6 o'yin bo'lsa o'tkaziladi).

**ρ ning amal qilish sohasi.** Fit `τ > 0` ni faqat **kuzatilgan** juftliklarda ta'minlaydi. Ko'rilmagan juftlikda (masalan λ = 4.6) `1 + μρ` manfiy bo'lishi mumkin (testda haqiqatan topildi). Shuning uchun bashoratda `ρ` shu o'yin uchun ruxsat etilgan oraliqqa qisiladi: `−1/max(λ, μ) < ρ < min(1, 1/(λμ))` (`feasible_rho`). Bevosita `score_matrix(..., rho)` chaqiruvi esa noto'g'ri `ρ` da xato beradi (yashirin tuzatish yo'q).

## 3. Hisob matritsasi va bozorlar
- Matritsa `m[i, j]`, `i, j ≤ N`. `N` avtomatik: kamida 10, Poisson dumi `P(X > N) < 1e-10` bo'lguncha oshiriladi (maksimum 50). So'ng DC tuzatish va **normallash** (`m / Σm`): bozor ehtimolliklari doim 1 ga teng bo'ladi, kesilgan dum yo'qolmaydi. Poisson log-pmf `gammaln` orqali (katta λ da ham barqaror).
- 1X2: diagonal ostida, ustida va diagonal; ikki natija (1X, 12, X2); BTTS `1 − P(x=0 yoki y=0)`; eng ehtimolli N ta hisob (teng bo'lsa kam gol birinchi).
- **Osiyo chiziqlari** (total va fora): margin = `jami − chiziq` (over), `chiziq − jami` (under), `(uy − mehmon) + fora` (uy). Chiziq 0.25 ga karrali. Butun/yarim chiziq: `> 0` yutuq, `= 0` qaytarish (push), `< 0` yutqazish. Chorak chiziq (2.25, −0.75) stavkaning yarmi qo'shni ikki chiziqqa bo'linadi: margin `+0.25` → yarim yutuq, `−0.25` → yarim yutqazish. Natija besh ehtimollik: `win, half_win, push, half_loss, loss`. Tekshiruv (property test): chorak chiziq EV = ikki yarim chiziq EV larining o'rtachasi.
- Mustaqil orakullar: ρ = 0 da `scipy.stats.skellam` (1X2, fora), `poisson(λ+μ)` (totallar), `(1−e^−λ)(1−e^−μ)` (BTTS).

## 4. EV va staking
- `EV = p · o − 1`; Osiyo bozori uchun `EV = Σ p_k · r_k`, `r = (o−1, (o−1)/2, 0, −0.5, −1)`. Oddiy bozor bu besh natijaning ikkilik holati (formula bitta joyda).
- Adolatli koeffitsient `1/p`.
- To'liq Kelly: ikkilik stavka uchun `f* = EV/(o − 1)` (EV ≤ 0 da 0). Osiyo stavkasi uchun yopiq formula yo'q: `G(f) = Σ p_k log(1 + f r_k)` botiq, `G'(0) = EV`, ildiz `brentq` bilan topiladi, `f ≤ 1` (leverage yo'q). Test: ikkilik holatda sonli yechim yopiq formulaga teng.
- Tavsiya: `min(0.25 · f*, 0.02)` (asosiy reja), summa `Decimal`, sentgacha **pastga** yaxlitlanadi. Flat rejim: faqat EV > 0 bo'lsa 1 birlik. Stavka hech qachon manfiy emas.

## Ko'rib chiqilgan muqobillar
- **Oddiy mustaqil Poisson (ρ = 0):** soddaroq, lekin 0-0 va 1-1 ni kam baholaydi (durang bozori uchun muhim). DC bitta parametr bilan buni tuzatadi.
- **Bivariate Poisson (Karlis-Ntzoufras) / Bayes ierarxik modeli (shrinkage):** kuchliroq, lekin murakkab va sekin. Kam ma'lumotli jamoalar uchun shrinkage keyinroq (F8.5) yoki L2 jarima sifatida qo'shilishi mumkin; hozir backtest natijasini kutadi.
- **Raqamli gradient:** 41 parametrda ~40 marta ko'p hisob va aniqlik past. Analitik gradient tanlandi va testlangan.
- **Kesilgan dumni oxirgi qator/ustunga yig'ish:** ba'zi bozorlarda aniq, lekin "10+" katagi aniq hisob emas bo'lib qoladi. Adaptiv `N` + normallash tanlandi (dum < 1e-10, ta'siri sezilmaydi).

## Oqibatlar
- `ξ`, ansambl `w` va EV chegarasi backtestda tanlanadi (F3.7); bu ADR faqat formulalarni qotiradi.
- `scipy` uchun tip stub'lari (`scipy-stubs`) lock-faylda yo'q: scipy importlari `# type: ignore[import-untyped]` bilan belgilangan va qaytgan qiymatlar `float(...)` bilan aniq tiplanadi. infra-engineer'ga `scipy-stubs` ni dev bog'liqlikka qo'shish taklif qilingan.
- Model funksiyalari sinxron va sof; pipeline ularni `asyncio.to_thread` orqali chaqiradi.
