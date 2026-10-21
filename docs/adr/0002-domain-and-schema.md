# ADR 0002: Domen modeli, baza sxemasi va tarixiy import

- **Holat:** qabul qilingan (orkestr tasdig'i kutilmoqda)
- **Sana:** 2026-10-21
- **Muallif:** data-engineer (T2: F2.1, F2.4, F2.7)

## Kontekst
Domen turlari va `SportAdapter` barcha agentlar uchun umumiy shartnoma: keyin faqat qo'shish mumkin. Baza SQLite (bitta jarayon, bot + rejalashtiruvchi), lekin keyinroq PostgreSQL ga o'tish yo'li ochiq qolishi kerak (`CLAUDE.md`, F2 talablari). Tarixiy ma'lumot manbai football-data.co.uk CSV fayllari: ID lar yo'q, formatlar mavsumdan mavsumga biroz farq qiladi.

## Qarorlar

### 1. Domen: `frozen` dataclass + konstruktorda validatsiya
`@dataclass(frozen=True, slots=True)` va `__post_init__` invariantlari (pydantic emas). Sabab: yuz minglab `OddsSnapshot` import qilinadi, dataclass yengil va tez; validatsiya qoidalari bir joyda. Noto'g'ri qiymat → `DomainValidationError` (`DataError` + `ValueError`). Vaqtlar konstruktorda UTC ga o'tkaziladi, naive vaqt rad etiladi.

### 2. Identifikatsiya: `ExternalRef(source, external_id)`
Domen obyektlari baza `id` siga bog'lanmaydi. Baza `(source, external_id)` bo'yicha unique, upsert kaliti ham shu. Manba almashsa yoki bir nechta manba qo'shilsa, sxema o'zgarmaydi.

### 3. Bozor = tur + chiziq; chiziq 0.25 setkasida, fora uy jamoasi tomonidan
`Market(type, line)`: "Total 2.5" va "Total 3.5" — turli bozorlar. Chiziq 0.25 ga karrali bo'lishi va CSV dagi float shovqini (`2.4999999999`; tolerantlik 1e-9, shuning uchun `2.4999999` kabi aniq bo'lmagan qiymat rad etiladi) setkaga yopishtirilishi tenglik/unique kalitlarni ishonchli qiladi. Fora chizig'i doim uy jamoasi nuqtai nazaridan (football-data `AHh` bilan bir xil konvensiya): bitta qator ikki xil yozilmaydi.

### 4. `predict` adapterda yo'q; adapter bazaga yozmaydi
Asosiy rejadagi eskizdan farqli: bashorat `app.models` da (sof, I/O siz), saqlash pipeline'da. Adapter faqat olib keladi va domen turlariga o'giradi (`import-linter`: `sports` ↛ `db`). `refresh_history` `HistoryBatch` qaytaradi.

### 5. Baza tiplari: portativlik birinchi
- **Vaqt:** `UTCDateTime` (TypeDecorator): Python'da doim tz-aware UTC; SQLite'da offsetsiz UTC matn (to'g'ri saralanadi), PostgreSQL'da `timestamptz`.
- **Pul:** `Money` (TypeDecorator): `Decimal`, PostgreSQL'da `NUMERIC(18,4)`, SQLite'da kanonik decimal **matn** (REAL emas: ikkilik yaxlitlash xatosi bo'lmaydi). Oqibat: SQLite'da pulni SQL `SUM` bilan emas, Python'da yig'ish kerak. Unit (stavka birligi) ham pul sifatida saqlanadi.
- **Enumlar:** qiymat sifatida `VARCHAR(32)`, **baza CHECK siz** (`native_enum=False, create_constraint=False`), ORM yozishda tekshiradi. Yangi sport yoki bozor qo'shish migratsiya talab qilmaydi.
- **Chiziq NULL emas:** chiziqsiz bozorlarda `line = 0` (CHECK: faqat `totals`/`asian_handicap` da 0 dan farqli). Sabab: UNIQUE da NULL lar hech qachon to'qnashmaydi (SQLite va PostgreSQL), natijada qayta import dublikat yaratardi.
- **Cheklovlar nomi:** `MetaData(naming_convention=...)`: Alembic diff'lari va PostgreSQL'ga ko'chirish barqaror.
- **Munosabatlar:** `lazy="raise"`: tasodifiy N+1 yuklash darhol xato beradi; kerakli joyda `selectinload`.

### 6. SQLite ulanish sozlamalari
`journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`, `synchronous=NORMAL` har bir ulanishda (`connect` hodisasi). Migratsiyalar esa `foreign_keys=OFF` bilan ishlaydi (batch migratsiya jadvalni nusxa + DROP orqali qayta quradi; FK yoqiq bo'lsa ota jadvalning DROP i bola qatorlarni kaskad o'chirardi), oxirida `PRAGMA foreign_key_check` bilan tekshiriladi.

### 7. Alembic
Async `env.py`, `render_as_batch=True` (SQLite ALTER), baza yo'li sozlamalardan (`DB_PATH`) yoki `-x db_path=`. Migratsiya fayllari ilova kodini import qilmaydi: `render_item` maxsus tiplarni oddiy SQLAlchemy tiplari sifatida yozadi (`sa.DateTime(timezone=True)`, `sa.Numeric(18,4).with_variant(sa.String(20), 'sqlite')`, `sa.String(32)`). Testlar: upgrade → downgrade → upgrade, ma'lumot bor bazada qayta upgrade, `alembic check` (model va migratsiya mosligi).

### 8. Idempotent yozish
`app.db.history.upsert_batch`: `INSERT ... ON CONFLICT DO UPDATE` (SQLite va PostgreSQL dialektlari), 500 qatorlik bo'laklar. Kalitlar: liga/jamoa/o'yin — `(source, external_id)`; `opening` va `closing` koeffitsientlar — `(fixture, bookmaker, market, selection, line, kind)` (qisman unique indeks `uq_odds_snapshots_single_moment`, joyida yangilanadi: odds va `taken_at`); `morning`/`recalc`/`prematch` — vaqt qatori, kalitga `taken_at` ham kiradi. O'yin holati va hisobi `app.db.fixture_state.merge_outcome` orqali birlashtiriladi: natija (result) hal qiluvchi (holat va to'rt hisob yoziladi); xG ikkala yo'lda ham birlashtiriladi (yangi qiymat ustun, yo'q bo'lsa saqlangani qoladi); natijasiz holat hisobga zid yozilmaydi (`finished` faqat hisob ma'lum bo'lsa, aks holda oldingi holat yoki yangi o'yin uchun `scheduled`; `scheduled`/`postponed`/`cancelled` hisob va xG ni tozalaydi; `live`/`abandoned` saqlanganini qoldiradi). Shu tufayli bazada domen o'qiy olmaydigan qator paydo bo'lmaydi (invariant testi bor). Bitta paket ichidagi dublikatlar oldindan birlashtiriladi (PostgreSQL bitta statement'da bir qatorni ikki marta yangilashni rad etadi). Tranzaksiya chegarasini chaqiruvchi belgilaydi (importda: bitta CSV fayl = bitta tranzaksiya).

### 9. football-data.co.uk importi
- Kutubxona `app.sports.history.football_data` (yuklash + parsing, bazasiz), saqlash `app.db.history`, orkestratsiya `scripts/import_history.py`.
- `external_id`: jamoa uchun manbadagi nom; o'yin uchun `"{liga}/{mavsum}/{uy}/{mehmon}"` (manbada ID yo'q). Sana ID ga kirmaydi: bu ligalarda bir juftlik mavsumda bitta maydonda bir marta o'ynaydi, manba sanani tuzatsa ikkinchi o'yin yaratilmaydi (QA F-2).
- Sana: `dd/mm/yy` dagi ikki xonali yil mavsumga eng yaqin asrga o'tkaziladi (1999/2000 faylidagi `99` → 1999); `[mavsum-07-01 − 62 kun, (mavsum+1)-06-30 + 62 kun]` oralig'idan tashqaridagi sana qator xatosi (o'tkaziladi).
- Vaqt: CSV dagi sana va vaqt Buyuk Britaniya vaqti (`Europe/London`) deb olinadi va UTC ga o'tkaziladi. Vaqt yo'q bo'lsa 12:00 London (UTC sanasi o'zgarmasligi uchun) va statistikada `missing_time` sanaladi.
- Koeffitsientlar: Pinnacle (`PS*`, `PSC*`), bozor o'rtachasi/maksimumi (`Avg*`, `Max*`), Bet365 (`B365*`); 1X2, Total 2.5, fora. Yopilish ustunlari (`*C*`) → `closing` (`taken_at = kickoff`); qolganlari → `opening`. **Diqqat:** football-data "erta" koeffitsientlarni o'yindan 1–3 kun oldin yig'adi, ularning aniq vaqti noma'lum: `taken_at = kickoff − 1 kun` taxminiy, tahlilda `kind` ga tayaning.
- Bir guruh (masalan, Pinnacle closing 1X2) to'liq bo'lmasa (ustun bo'sh yoki ≤ 1.0) butun guruh tashlanadi: de-vig uchun to'liq bo'lmagan to'plam yaroqsiz.
- Buzilgan qator loglanadi va o'tkaziladi; majburiy ustunlar yo'q bo'lsa butun fayl rad etiladi (fayl darajasidagi xato, keyingi fayllar davom etadi).
- Yuklash: `httpx`, standart TLS tekshiruvi (certifi, `SSL_CERT_FILE` ham hisobga olinadi), 3 urinish; HTML javob (masalan, proksi bloklash sahifasi) CSV sifatida keshlanmaydi. Javob CSV hisoblanishi uchun birinchi qatorda `Date` va jamoa ustunlari juftligi (`HomeTeam/AwayTeam`, `Home/Away` yoki `HT/AT`) bo'lishi shart. O'tgan mavsum fayli faqat mavsum tugagandan keyin (`mavsum+1`-yil 1-iyul, UTC) saqlangan bo'lsa yakuniy; mavsum o'rtasida keshlangan nusxa qayta yuklanadi. Joriy mavsum 12 soatdan eski bo'lsa yangilanadi; yangilash muvaffaqiyatsiz bo'lsa eski nusxa ogohlantirish (`using_stale_cache`) bilan import qilinadi. `--offline` faqat keshdan o'qiydi. Har bir fayl alohida tranzaksiya: `AppError`, `SQLAlchemyError` yoki `OSError` faqat shu faylni `failed` qiladi.

## Ko'rib chiqilgan muqobillar
- **pydantic domen modellari:** qulay, lekin import hajmida sekinroq va domen uchun ortiqcha; tashqi API javoblari uchun pydantic chegarada (adapterlarda) ishlatiladi.
- **Native enum / CHECK:** butunlik kuchliroq, lekin har bir yangi qiymat migratsiya talab qiladi (SQLite'da jadvalni qayta qurish). Rad etildi.
- **`line` NULL + qisman unique indeks:** PostgreSQL'da `NULLS NOT DISTINCT` bor, SQLite'da yo'q. Rad etildi.
- **pandas bilan CSV o'qish:** qatorma-qator xatolarni ajratish qiyin, xotira ko'p. Standart `csv` tanlandi.
- **truststore (OS sertifikatlar ombori):** bu mashinada korporativ proksi bilan TLS ishlardi, lekin sayt korporativ veb-filtr tomonidan bloklangan ("Gambling" toifasi), shuning uchun bog'liqlik qo'shilmadi.

## Oqibatlar
- Yangi sport/bozor/selection: faqat enumga qiymat qo'shish (migratsiyasiz). Yangi jadval yoki ustun: yangi Alembic revizyasi (`--rev-id 0002`).
- Pipeline va bot uchun signal/foydalanuvchi enumlari `app.db.enums` da; domenga kerak bo'lsa, qo'shish orqali ko'chiriladi.
- Bitta o'yin va tanlov uchun `opening` va `closing` har doim bittadan (migratsiya `0002` dan boshlab bazada majburiy). Manba o'yin vaqtini tuzatsa, ular joyida yangilanadi (`taken_at` ko'chadi). Ko'p nuqtali qatorlar faqat `morning`/`recalc`/`prematch` da.
