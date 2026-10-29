# ADR 0025: API-Football olib tashlandi; Football-Data.org API servisi

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-29
- **Bog'liq:** [ADR 0023](0023-football-data-org-client.md), [ADR 0024](0024-live-matches-from-football-data.md), [ADR 0021](0021-db-only-analysis.md), [ADR 0007](0007-api-client-and-repositories.md)

## Kontekst
Foydalanuvchi talabi: "API-Football dan butunlay voz kech, umuman ishlatilmasin"; "barcha ma'lumot football-data.org dan olinsin"; "quickstart hujjatini tahlil qilib, API lar uchun alohida servis qil, request va response larni aniq qil".

Hujjat tahlili (`football-data.org/documentation/quickstart`, `docs.football-data.org/general/v4`):
- autentifikatsiya: `X-Auth-Token` sarlavhasi; asos `https://api.football-data.org/v4`;
- limit faqat daqiqalik: bepul 10, Standard 30, yuqori tariflar 60 so'rov/daqiqa (kunlik limit yo'q);
- xatolar: `400` noto'g'ri filtr, `403` tarifda yo'q resurs, `404` mavjud emas, `429` limitdan oshdi; tanasi `{"error": ...}` (amalda `{"message", "errorCode"}` ham);
- resurslar: areas, competitions (+ matches/standings/scorers/teams), teams (+ matches), persons (+ matches), matches (+ head2head);
- koeffitsient faqat 1X2 o'rtachasi (`odds.homeWin/draw/awayWin`) va faqat Odds paketi bilan; tarkib, gollar, kartochkalar `GET /matches/{id}` da (tarifga bog'liq);
- ro'yxat endpointlarida tafsilotlar "yig'ilgan": `X-Unfold-Lineups/Bookings/Subs/Goals` so'rov sarlavhalari bilan ochiladi.

## Qarorlar
1. **API-Football to'liq olib tashlandi**: `clients/api_football*.py`, `sports/football.py`, `sports/football_mapping.py`, `pipeline.provider.FootballProvider`, `scripts/probe_api.py`, `API_FOOTBALL_KEY`, `HANDICAP_CONVENTION` va unga bog'liq start tekshiruvi; testlar va yozib olingan javoblar. `SportProvider` porti, kvota va refresh mexanizmi provayderdan mustaqil bo'lgani uchun qoldi (provayderlar ro'yxati bo'sh).
2. **Saqlangan identifikatorlar o'zgarmadi**: `Provider.API_FOOTBALL` (eski jurnal qatorlari o'qilishi uchun), bazadagi `api-football:<id>` manba nomlari va liga katalogi (`sports.football_leagues`) — bu ma'lumot, so'rov emas.
3. **Football-Data.org API servisi** (`app.clients.football_data_org`): hujjatdagi har bir endpoint uchun bitta metod, ikki tomonlama aniq turlar:
   - so'rovlar — `football_data_org_requests` dagi frozen filtrlar (`CompetitionMatchFilters`, `StandingsFilters`, `ScorersFilters`, `CompetitionTeamsFilters`, `TeamMatchFilters`, `PersonMatchFilters`, `MatchFilters`, `Head2HeadFilters`, `CompetitionListFilters`; `StatusFilter`, `Venue`). Provayder 400 beradigan so'rov (yolg'iz `dateFrom`, teskari oraliq, `season=26`, `limit=0`) yuborilmasdan rad etiladi;
   - javoblar — `football_data_org_models` (Area, Competition, Season, Team, Person, Match + Goal/Booking/Substitution/Referee/Odds, Standings, Scorers, Head2Head, ErrorBody);
   - yo'l segmentlari (kod, id) URL ga tushishdan oldin tekshiriladi; noto'g'ri tuzilmali javob keshda qolmaydi.
4. **Limit sozlamasi**: `FOOTBALL_DATA_REQUESTS_PER_MINUTE` (standart 10 = bepul tarif). Foydalanuvchi 30 deb aytgan edi, lekin hujjat bo'yicha 30 — Standard tarif; bepul tarifda 30 bilan 429 lar ketma-ket keladi. Tarif Standard bo'lsa `.env` da 30 qo'yiladi.
5. Kesh TTL: areas/competitions 24 soat, teams/persons 6 soat, o'yin 5 daqiqa, qolganlari 10 daqiqa.

6. **Pipeline Football-Data.org ga ulandi va bazaga yozadi** (foydalanuvchi tasdig'i, ADR 0024 dagi "bazada faqat so'rovlar" qoidasi pipeline uchun bekor): `sports.football_data_org.FootballDataAdapter` + `pipeline.provider.FootballDataProvider`. Kunlik `refresh`, natijalar (`settle`), pre-match va tarix shu provayder orqali `fixtures`/`results`/`odds_snapshots` ga yoziladi; "⚽ O'yinlar" ro'yxati esa avvalgidek jonli (bazadan o'qimaydi).
   - so'rovlar: kun oralig'i uchun `GET /matches?dateFrom&dateTo` (10 kunga 1 so'rov, barcha ligalar), natija/koeffitsient uchun `GET /matches?ids=` (20 tadan), tarix uchun `GET /competitions/{code}/matches?season=`; tarifda yo'q liga (403) log qilinib o'tkaziladi;
   - manba nomi `football-data-org` (football-data.co.uk ning `football-data` dan farqli); ligalar katalogida har ligaga kod: `PL, ELC, PD, BL1, SA, FL1, DED, PPL` (bepul), `BJL, TSL` (pullik); jamoa nomlari tarix (CSV) jamoalariga avvalgidek moslanadi;
   - natija — 90 daqiqalik hisob (`score.regularTime` qo'shimcha vaqtdan keyin); bilinmasa natija yozilmaydi (taxmin qilinmaydi);
   - natija va yopilish koeffitsienti keshsiz so'raladi (`fresh=True`): bir xil `ids=` so'rovi koeffitsient uchun keshda turgan eski holatni qaytarmasligi uchun (test buni ushladi);
   - koeffitsient: faqat Odds paketi bilan, 1X2 o'rtachasi, `market_avg` psevdo-bukmeker sifatida; `PipelineConfig.bet_bookmaker = market_avg` (yagona narx manbai): EV o'rtacha narxga nisbatan, Pinnacle yo'qligi sababli CLV hisoblanmaydi;
   - xG yo'q (`enrich_results` o'zgartirmaydi); tarkib `GET /matches/{id}` dan;
   - so'rovlar soni: kunlik limit yo'q, lekin har kungi son `api_usage` ga yoziladi (`usage_counter`: shifti = daqiqalik limit × 1440, hech qachon rad etmaydi); har so'rov chaqiruvlar jurnaliga ham yoziladi;
   - bitta HTTP klient (bitta tezlik cheklagichi) jonli ro'yxat va pipeline uchun umumiy, ikkalasi birga ham tarif limitidan oshmaydi.

## Oqibatlar
- **Signal/EV tahlili faqat Odds paketi bilan ishlaydi**: paketsiz o'yinlar va natijalar yoziladi, lekin narx yo'q, shuning uchun tahlil "narx yo'q" deb o'tkazadi. Paket bilan ham faqat 1X2 bozori (totals, BTTS, fora yo'q).
- `refresh` CLI `FOOTBALL_DATA_ORG_KEY` yo'qligida konfiguratsiya xatosi (kod 2) bilan to'xtaydi.
- Bepul tarifda Belgiya (`BJL`) va Turkiya (`TSL`) tarixi 403 beradi: log qilinadi, qolgan ligalar ishlaydi.
- `/holat` provayder yo'q bo'lsa kvota qatorini uydirmaydi.
- `X-Unfold-*` sarlavhalari hali ishlatilmaydi (`ApiClient.get_json` da so'rov sarlavhasi yo'q); tarkib `GET /matches/{id}` dan olinadi.
- Head2Head `aggregates` maydonlari (`numberOfMatches`, `totalGoals`, `homeTeam/awayTeam.wins/draws/losses`) quickstart da ko'rsatilmagan; v4 javobi bo'yicha yozilgan, birinchi haqiqiy javobda tekshirilishi kerak.
