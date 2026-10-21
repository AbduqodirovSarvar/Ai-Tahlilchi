# ADR 0007: API klient, futbol adapteri, repository va qidiruv

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-21
- **Muallif:** data-engineer (T3: F2.2, F2.3, F2.5, F2.6, F2.8, F0.7)

## Kontekst
API-Football bepul tarifi kuniga 100 so'rov va daqiqasiga ~10 so'rov beradi (asosiy reja §9). API kaliti hali yo'q (qarorlar #1), ishlab chiqish mashinasidan tarmoqqa chiqilmaydi (#7): barcha kod sintetik, API v3 sxemasiga mos javoblar bilan quriladi va testlanadi. Pipeline va servislar uchun repository funksiyalari kerak (servis shartnomasi v1.1: `recommended_at`, retro, yashirish qoidalari).

## Qarorlar

### 1. Qatlamlar
`app.clients` (eng past I/O qatlam): `rate_limit` (token bucket), `cache` (TTL), `quota` (kunlik limit, `UsageStore` protokoli), `http` (`ApiClient`), `cost` (`estimate_requests`), `api_football` (endpoint klient), `api_football_models` (pydantic). `app.sports` klientdan foydalanadi, `app.db` esa `UsageStore` ni SQL da amalga oshiradi (`SqlUsageStore`). `sports` va `db` bir-birini import qilmaydi; mavjud `import-linter` qatlamlari o'zgarmadi.

### 2. So'rov hayot yo'li (`ApiClient.get_json`)
Kesh → token bucket → kvota (yuborishdan **oldin**, rad etilsa hech narsa yuborilmaydi) → so'rov (timeout) → 429/5xx/timeout/transport xatosida `tenacity` bilan qayta urinish (equal-jitter eksponensial kutish, `Retry-After` dan qisqa emas, maksimal 120 s) → `x-ratelimit-requests-limit/remaining` sarlavhalari bilan kvotani moslash. Har bir urinish haqiqiy so'rov, shuning uchun alohida hisoblanadi. 4xx (429 dan tashqari) qayta urinilmaydi. Kalit faqat sarlavhada (`x-apisports-key`), URL va parametrlar loglanadi, kalit hech qachon.

### 3. Kunlik kvota va 30% qo'lda zaxira
`api_usage.requests_used` — kunning barcha so'rovlari, `api_usage.reserved_manual` — ulardan **qo'lda** qilinganlari soni (zaxira hajmi emas: u siyosat, `limit × 0.30`, saqlanmaydi). Qoida: qo'lda so'rov qolgan hammasini ishlatishi mumkin; avtomatik so'rov qo'lda hali ishlatilmagan zaxiraga tegmaydi: `avtomatik_mavjud = qolgan − max(zaxira − qo'lda_ishlatilgan, 0)`. Shunday qilib avtomatik ishlar foydalanuvchini hech qachon "och qoldirmaydi", foydalanuvchi zaxirasini ishlatib bo'lsa, qolgani umumiy. Kun — UTC (API-Sports hisoblagichi 00:00 UTC da yangilanadi). Tekshirish-va-hisoblash bitta `asyncio.Lock` ostida (ilova bitta jarayon, ADR 0002). Provayder sarlavhalari hisobni faqat **oshiradi** (provayder qayta o'rnatish vaqtidagi farq ortiqcha kvota bermasligi uchun). `SqlUsageStore` har bir o'zgarishni o'z qisqa tranzaksiyasida commit qiladi: yuborilgan so'rov chaqiruvchining rollback'i bilan "unutilmasligi" kerak.

### 4. Kesh
Jarayon ichidagi LRU + TTL (2048 yozuv). Standart TTL 10 daqiqa (bir xil ma'lumot 10 daqiqada qayta so'ralmaydi); kelajakdagi kunlar koeffitsientlari 6 soat. Kesh faqat optimallashtirish: qayta ishga tushganda yo'qolishi so'rov sarflaydi, xatoga olib kelmaydi. Shuning uchun Redis/disk keshi qo'shilmadi (YAGNI).

### 5. Chegarada validatsiya
Konvert (`errors`, `paging`, `response`) pydantic bilan; `response` dagi har bir yozuv **alohida** validatsiya qilinadi, buzilgani loglanib tashlanadi. API HTTP 200 bilan `errors` qaytarsa (`token`, `plan`, `rateLimit`) — `ApiFootballError`. Koeffitsient narxi xom satr sifatida saqlanadi: bitta yaroqsiz narx butun o'yin yozuvini emas, faqat o'zini tushiradi.

### 6. Futbol adapteri: so'rovlarni tejash
- O'yinlar: oynadagi kunlar soni liga-mavsumlar sonidan kam bo'lsa `fixtures?date=` (kuniga 1 so'rov, barcha ligalar), aks holda liga-mavsum bo'yicha `fixtures?league&season&from&to`. Jamoa: `fixtures?team&season&from&to` (API `from/to` ni faqat `season` bilan qabul qiladi). 1-iyuldan o'tuvchi oyna ikki mavsumga bo'linadi.
- Koeffitsientlar: `fetch_fixtures` ko'rgan o'yinlar (liga, mavsum, kun) bo'yicha guruhlanadi; ≥ 2 o'yinli guruh `odds?league&season&date` (sahifalab), qolganlari `odds?fixture`. `taken_at` = olingan vaqt (provayderning `update` maydoni emas: `kind` qachon olinganini bildiradi).
- Natijalar: `fixtures?ids=` 20 tadan. xG: `with_expected_goals` faqat yangi tugaganlar uchun (har biri 1 so'rov). Jarohat va tarkiblar: adapterning qo'shimcha metodlari (protokolda yo'q).
- `for_purpose(MANUAL)`: foydalanuvchi harakatlari zaxiradan hisoblanadi.

### 7. Xaritalash qoidalari (`app.sports.football_mapping`)
- Holatlar: `TBD/NS` → scheduled; `1H HT 2H ET BT P SUSP INT LIVE` → live; `FT AET PEN` → finished; `PST` → postponed; `CANC` → cancelled; `ABD` → abandoned (to'xtagan paytdagi hisob bilan); `AWD/WO` → abandoned **hisobsiz** (texnik g'alaba futbol natijasi emas, modelni o'qitmasligi kerak). Noma'lum kod — yozuv tashlanadi.
- Hisob: `score.fulltime` (asosiy vaqt); `goals` faqat oddiy `FT` uchun zaxira (AET/PEN da `goals` qo'shimcha vaqtni o'z ichiga oladi).
- Bozorlar nomi bo'yicha: `Match Winner` → 1X2, `Double Chance`, `Goals Over/Under` (0.25 setkadagi chiziqlar; `2.3` kabilar tashlanadi), `Both Teams Score`, `Asian Handicap`. Boshqalari (aniq hisob, birinchi bo'lim) e'tiborsiz. Bitta bukmeker, bozor va tanlov uchun birinchi narx olinadi.
- **Osiyo forasi:** API har bir chiziqni ikkala tomon uchun beradi (`Home -0.5`, `Away -0.5`). Taxmin (`HOME_LINE`, standart): son ikkala qiymatda ham **uy jamoasining chizig'i** — domen konvensiyasi bilan bir xil. Kalitsiz tasdiqlab bo'lmadi. `scripts/probe_api.py` haqiqiy narxlarda ikkala o'qishni solishtiradi (to'g'ri o'qish ko'proq juftlikni ishonchli marja, 1.00–1.15, bilan bog'laydi); noto'g'ri chiqsa `HandicapConvention.OWN_LINE` ga o'tkaziladi.
- Bukmekerlar: nom → slug (`1xBet` → `1xbet`, `William Hill` → `william-hill`); domen'ga `BOOKMAKER_1XBET` qo'shildi.

### 8. Ligalar va jamoalar identifikatsiyasi
API-Football liga ID lari: EPL 39, Championship 40, La Liga 140, Bundesliga 78, Serie A 135, Ligue 1 61, Eredivisie 88, Primeira Liga 94, Belgiya Pro League 144, Süper Lig 203 (bilimga asoslangan; probe har ID uchun provayder nomini hisobotga yozadi). Tarix (football-data, `Man United`) va jonli ma'lumot (API-Football, `33 / Manchester United`) bitta klubga ishora qilishi uchun: qo'lda tuzilgan alias jadvali → normallashgan nom aynan mosligi → `rapidfuzz` WRatio ≥ 90 va keyingi nomzoddan ≥ 5 ball farq. Ikki provayder jamoasi bitta tarix jamoasiga da'vo qilsa yoki natija yaqin bo'lsa — "ambiguous", past ball — "unmatched"; **hech narsa jim birlashtirilmaydi**. `IdentityMap` faqat ishonchli juftliklardan quriladi va adapterga `with_identity` orqali beriladi: o'yin o'z API-Football ID si bilan qoladi, lekin jamoa va liga tarixdagi identifikatsiyani oladi. Natijada bir o'yin ikki manbadan ikki qator bo'lishi mumkin; `finished_fixtures` ularni `(liga, mavsum, uy, mehmon)` bo'yicha birlashtiradi va `preferred_sources` tartibida bittasini qoldiradi. Sxemaga yangi jadval qo'shilmadi (alias jadvali kodda, F0 dan keyin "unmatched" hisobotidan to'ldiriladi).

### 9. Repository
Funksiyalar (`app.db.repo`) chaqiruvchining tranzaksiyasida ishlaydi (flush, commit emas), domen obyektlari yoki `frozen` DTO qaytaradi, ORM qator hech qachon tashqariga chiqmaydi. Bog'liq qatorlar bitta so'rovda `joinedload` bilan; signal ro'yxati 3 ta so'rov (signallar+o'yinlar, oxirgi versiyalar, natijalar), N+1 yo'q. SQL portativ (`ON CONFLICT`, `CASE`, `UPDATE ... RETURNING` — SQLite ≥ 3.35 va PostgreSQL); pul Python'da yig'iladi (ADR 0002).
- **Signal:** kalit foydalanuvchi + o'yin + bozor + chiziq + tanlov; qayta tahlil qatorni yangilaydi va `signal_versions` ga versiya qo'shadi. `recommended_at` birinchi marta qo'yilgan paytdagi chegaralar (`min_prob`, `min_ev`, `odds_min`, `odds_max`) bilan bir marta yoziladi va o'zgarmaydi. Retro qoralama retro bo'lmagan signalni hech qachon ustidan yozmaydi. Hisobot o'qishlari: `recommended_at` bor, retro emas, yashirilganlari **kiradi**.
- **Natija:** signal uchun bitta; qo'lda belgilangan natijani avtomatik (`api`) natija almashtirmaydi.
- **Qayta hisob navbati:** lease asosida. `claim_due_recalcs` bajarilmagan va muddati kelgan vazifalarning `done_at` ustuniga *egallash vaqtini* yozadi (shartli `UPDATE ... RETURNING`, ikki ishchi bitta vazifani ololmaydi); `complete_recalc` → `done=true`; `release_recalc` qaytaradi; 15 daqiqadan eski egallash qayta olinadi (jarayon qulasa vazifa yo'qolmaydi). Sxema o'zgarmadi.

### 10. Migratsiya `0003`
Servis shartnomasi v1.1 uchun `signals.recommended_at`, `min_prob`, `min_ev`, `odds_min`, `odds_max` (NULL bo'lishi mumkin: oldingi qatorlarda yo'q) va `(user_id, recommended_at)` indeksi. `SignalSource` ga `analysis`, `retro` qo'shildi (enum ustunlarida CHECK yo'q, DDL kerak emas). `downgrade` bor.

### 11. Jamoa qidiruvi
So'rov va nomlar bitta kalitga keltiriladi: o'zbek kirill → lotin transliteratsiyasi (`ў` → `o'`, `қ` → `q`, `ғ` → `g'`, `ҳ` → `h`, `х` → `x`, ...; `ʻ ʼ ’` → `'`), so'ng `normalize_name`. `rapidfuzz` WRatio, nom boshi bilan mos kelsa +15, so'z boshi bilan +10; ≥ 60 ball, teng ballda qisqa nom oldin. Teskari (lotin → kirill) funksiya ham bor. Nomzodlar bazadan o'qiladi (bir necha ming jamoa, xotirada saralash yetarli); liga nomi jamoaning eng so'nggi o'yinidan.

## Ko'rib chiqilgan muqobillar
- **`aiolimiter`/`httpx-retries` kutubxonalari:** yangi bog'liqlik; token bucket va tenacity bilan 100 qatordan kam kod yetarli.
- **Kvotani faqat jami bo'yicha (70/30 qattiq bo'linish):** sodda, lekin foydalanuvchi ertalab ko'p ishlatsa avtomatik yangilash to'xtaydi. Rad etildi.
- **Jamoa aliaslari uchun jadval (`team_aliases`):** yangi migratsiya va shartnoma o'zgarishi; hozircha statik jadval va hisobot yetarli. Kerak bo'lsa keyingi to'lqinda.
- **Koeffitsientni har doim `odds?fixture`:** oddiy, lekin 10 ligada kuniga 30–60 so'rov. Rad etildi.

## Oqibatlar va ochiq savollar
- F0 (kalit bilan): `scripts/probe_api.py` → `docs/api_probe_report.md`; AH yozilishi, liga ID lari, bukmeker nomlari va `from/to` qoidalari tasdiqlanadi, `tests/fixtures/api_football/recorded/` dagi haqiqiy javoblar bilan testlar yangilanadi.
- API sozlamalari (`daily_limit`, rate) hozircha `create_api_football_client` parametrlari; `config.py` (infra egaligi) ga `API_FOOTBALL_DAILY_LIMIT` qo'shish taklif qilinadi.
