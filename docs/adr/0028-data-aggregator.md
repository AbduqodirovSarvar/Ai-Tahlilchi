# ADR 0028: Ma'lumot agregatori (Football-Data.org + football-data.co.uk)

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-30
- **Bog'liq:** [ADR 0025](0025-football-data-only.md), [ADR 0026](0026-model-only-forecasts.md)

## Kontekst
Football-Data.org bepul tarifida koeffitsient yo'q. Foydalanuvchi talabi: o'yinlarni va tarkiblarni Football-Data.org dan olish, Bet365 1X2 koeffitsientlarini football-data.co.uk CSV dan qo'shish va ikkalasini sana va jamoa nomi bo'yicha bog'lash (`CombinedMatchDto`).

Muhim fakt: mavsum fayli (`mmz4281/2627/E0.csv`) faqat **o'ynalgan** o'yinlarni saqlaydi. Bo'lajak o'yinlar koeffitsienti faqat `fixtures.csv` da bor (barcha ligalar, `Div` ustuni). U odatda juma (dam olish kunlari uchun) va seshanba (hafta o'rtasi uchun) chiqadi, shuning uchun 3–4 kundan uzoqdagi o'yinlarda hali qator bo'lmaydi.

## Qarorlar
1. **Joylashuv:** `app.sports.aggregator` (sports qatlami: `clients` va `team_matching` dan foydalanadi), ishlab chiqarish ulanishi `app.pipeline.aggregator.open_data_aggregator`, sinov uchun CLI `scripts/aggregate_matches.py`.
2. **CSV yuklovchi** (`csv_odds.OddsCsvLoader`) tarix importining yuklab olish va kesh kodini qayta ishlatadi (`download_file` umumiy qilindi). Kesh papkasi `ODDS_CSV_CACHE_DIR` (standart `data/history`, import bilan bir xil). Tugagan mavsum abadiy, joriy mavsum 12 soat, `fixtures.csv` 3 soat saqlanadi. Yangilash xato bersa, keshdagi nusxa ishlatiladi.
3. **Moslash** (`matching.MatchingEngine`): jamoalar `match_teams` orqali (alias, aniq, huquqiy shaklsiz kalit, keyin qattiq fuzzy), faqat ishonchli juftliklar ishlatiladi. Uy/mehmon almashtirilmaydi. Sana Buyuk Britaniya vaqtida solishtiriladi (faylning o'z soati), ±1 kun bardosh bilan, eng yaqin qator olinadi. To'liq jamoa ro'yxati uchun joriy mavsum fayli ham yuklanadi.
4. **Natija:** `CombinedMatchDto` (id, UTC vaqt, liga, jamoalar, tarkiblar yoki `None`, `MatchOdds` yoki `None`) va `LinkStatus`: `matched`, `no_odds`, `no_row`, `teams_unmatched`, `league_not_covered`, `source_unavailable`. Koeffitsient faqat `matched` da beriladi.
5. **So'rov byudjeti:** 10 kunlik oralig'iga bitta `/matches` so'rovi va ko'pi bilan 5 ta `/matches/{id}`. Tarkib faqat boshlanishiga 2 soatdan kam qolgan (yoki 3 soat ichida boshlangan) o'yinlar uchun so'raladi. Ro'yxatda kelgan tarkib so'rov talab qilmaydi. Tezlik nazorati va kesh `ApiClient` da.
6. **Xatolar:** o'yinlar ro'yxati xatosi ko'tariladi (`ExternalServiceError`). CSV yoki tarkib xatosi loglanadi, o'yin esa koeffitsient yoki tarkibsiz qaytadi.

## Oqibatlar
- Bet365 narxlari football-data.co.uk ning yig'ish vaqtidagi narxi, jonli narx emas.
- Bepul tarifda tarkiblar odatda bo'sh (`None`).
- Agregator hali bot yoki tahlil pipeline'iga ulanmagan: alohida servis va CLI. Tahlilga ulash keyingi qadam (narx `market_avg` o'rniga Bet365 bo'ladi, buni alohida hal qilish kerak).
- Noma'lum nomlar `odds_team_matching_incomplete` logida chiqadi va aliaslarga qo'shiladi.
