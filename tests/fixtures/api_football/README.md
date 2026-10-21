# API-Football test javoblari

> **Sintetik, sxema asosida — F0 da haqiqiy kalit bilan qayta yozib olinsin.**

Bu papkadagi `*.json` fayllar haqiqiy API javoblari **emas**. Loyihada hali API kaliti yo'q
(qarorlar jurnali #1) va ishlab chiqish mashinasidan api-sports.io ga tarmoq orqali murojaat
qilinmaydi (#7). Fayllar API-Football v3 javob formatiga (`get`, `parameters`, `errors`,
`results`, `paging`, `response`) muvofiq qo'lda tuzilgan va ataylab chegaraviy holatlarni o'z
ichiga oladi:

| Fayl | Nima tekshiriladi |
|---|---|
| `fixtures_league_39_2024.json` | FT, PST, CANC, ABD, NS; UTC dan farqli offset (`-02:00`); jamoasiz buzilgan yozuv |
| `fixtures_date_2024-10-26.json` | jonli (`1H`), AET va PEN (asosiy vaqt hisobi), AWD, TBD, kuzatilmaydigan liga |
| `fixtures_ids.json`, `fixtures_empty.json` | `ids=` paketi, bo'sh javob |
| `odds_fixture_1208100.json` | Pinnacle/Bet365/1xBet; 1X2, ikki natija, totallar (2.75 chorak va 2.3 setkadan tashqari), BTTS, AH; noma'lum bozorlar; `1.00` va raqam bo'lmagan narx |
| `odds_league_39_2024-10-26_page{1,2}.json` | sahifalash (`paging.total = 2`) |
| `injuries_*`, `lineups_*`, `statistics_*` | jarohatlar, tarkiblar (bo'sh ham), xG (`expected_goals`, bo'lmagan holat ham) |
| `error_token.json`, `error_plan.json` | HTTP 200 + `errors` (kalit yo'q, tarif mavsumni yopgan) |
| `status.json` | hisob holati (shaxsiy maydonlar `probe_api.py` da olib tashlanadi) |

**Qayta yozib olish (F0.7):** `.env` ga `API_FOOTBALL_KEY` qo'shilgach
`uv run python scripts/probe_api.py` haqiqiy javoblarni tozalab (hisob ma'lumotlarisiz; kalit
faqat sarlavhada yuboriladi va javobga tushmaydi) `recorded/` papkasiga yozadi. So'ng testlar
`recorded/` dagi fayllarga o'tkaziladi va sintetik fayllardagi taxminlar (ayniqsa AH yozilishi,
ADR 0007) solishtiriladi.
