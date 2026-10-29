# ADR 0023: Football-Data.org v4 klienti

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-29
- **Bog'liq:** [ADR 0007](0007-api-client-and-repositories.md), [ADR 0020](0020-provider-call-log.md)

## Kontekst
O'yinlar ro'yxati, o'yin tafsilotlari va boshlang'ich tarkiblar (lineup, bench, murabbiy) uchun `api.football-data.org/v4` kerak. Limit: 30 so'rov/daqiqa, kunlik limit yo'q. Loyihada tayyor `ApiClient` bor (token bucket, 429/5xx uchun retry + `Retry-After`, TTL kesh, single-flight, kvota). Nom to'qnashuvi: `app.sports.history.football_data` — bu football-data.**co.uk** CSV fayllari, boshqa xizmat.

## Qarorlar
1. **`ApiClient` ustiga yupqa endpoint klienti** (`app/clients/football_data_org.py`), API-Football klienti kabi. Rate limit, retry va kesh qayta yozilmaydi. Fayl nomlari `_org` bilan: co.uk bilan adashmaslik uchun.
2. **Interfeys:** `FootballDataClient` (`Protocol`) va `HttpFootballDataClient`. C# dagi `IFootballDataClient` + DI o'rniga loyiha naqshi: `create_football_data_client(api_key)` fabrikasi (`create_api_football_client` kabi); kalit `Settings.football_data_org_key` (`require_secret`).
3. **DTO** — `football_data_org_models.py` (frozen pydantic, `extra="ignore"`). Ro'yxatdagi buzilgan yozuv log qilinib o'tkazib yuboriladi. Noma'lum status `MatchStatus.UNKNOWN` bo'ladi (provayder yangi status qo'shsa ham o'yin o'qiladi).
4. **Sana oralig'i:** `/matches` 10 kundan uzun oraliqni rad etadi, shuning uchun klient oraliqni 10 kunlik oynalarga bo'ladi (har biri 1 so'rov).
5. **Musobaqa kodi** URL yo'liga tushishdan oldin tekshiriladi (`[A-Za-z0-9]{2,5}` yoki raqamli id).
6. **Kunlik kvota yo'q:** provayderda kunlik limit yo'q, shuning uchun klient `QuotaGuard` siz quriladi (`ApiClient(quota=None)`; `api_usage` ga yozilmaydi). Yagona cheklov — daqiqalik token bucket: 30/daqiqa, `burst=1` (so'rovlar orasida ~2 s, 60 s lik oynadan oshmaydi). Buning uchun umumiy `ApiClient` da `quota` ixtiyoriy qilindi (qo'shimcha o'zgarish: API-Football avvalgidek kvota bilan quriladi; kvotasiz klientda `.quota` `ConfigurationError` beradi).
7. **Kesh:** ro'yxatlar — standart 10 daqiqa; o'yin tafsiloti — 5 daqiqa (tarkib o'yindan oldin e'lon qilinadi). Noto'g'ri tuzilmali javob keshda qolmaydi.

## Oqibatlar
- Tarkiblar (`lineup`/`bench`/`coach`) bepul tarifda bo'sh keladi: `MatchLineups.announced == False`. Bu xato emas.
- **Chaqiruvlar jurnaliga (ADR 0020) yozilmaydi:** `Provider` enumi domen shartnomasi va DB ustuni; yangi qiymat migratsiya talab qiladi. Kerak bo'lsa alohida vazifa.
- 429 da `Retry-After` hurmat qilinadi; Football-Data uni har doim yubormaydi (`X-RequestCounter-Reset` ni ham yuboradi) — bunda oddiy eksponensial backoff ishlaydi. Bu shared `ApiClient` ni o'zgartirmaslik uchun qabul qilingan.
- Domen (`Fixture`) ga o'tkazish va `SportAdapter` ga ulash hali yo'q: iste'molchi paydo bo'lgach (YAGNI).
