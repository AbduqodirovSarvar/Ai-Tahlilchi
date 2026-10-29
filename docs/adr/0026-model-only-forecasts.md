# ADR 0026: Koeffitsientsiz model bashorati ("📊 Prognoz")

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-29
- **Bog'liq:** [ADR 0025](0025-football-data-only.md), [ADR 0024](0024-live-matches-from-football-data.md)

## Kontekst
Football-Data.org bepul tarifda koeffitsient bermaydi. Tahlil zanjiri (`valuation.value_fixture`) bukmeker narxidan boshlanadi va narx bo'lmasa o'yinni modelga yetkazmasdan "narx yo'q" deb o'tkazadi, shuning uchun bot hech narsa ko'rsatmasdi. Foydalanuvchi talabi: "koeffitsientsiz tahlil qila olmaymizmi: kim yutadi va boshqalar".

## Qarorlar
1. **Alohida bashorat yo'li, signal emas.** `pipeline.forecast.Forecaster` provayder o'yinini domen fiksturasiga o'giradi, jamoalarni tarix (CSV) jamoalariga moslaydi, ligadagi moslangan Dixon-Coles modelidan (`ModelCache`) ehtimollarni oladi. EV va stavka hajmi yo'q (narx kerak). Natija bazaga yozilmaydi: signallar jadvali `odds > 1` talab qiladi va bashorat narxsiz.
2. **Bozorlar:** 1X2, ikki natijadan biri, Total 1.5/2.5/3.5, BTTS va 3 ta eng ehtimolli hisob. Har biri uchun adolatli koeffitsient `1 / p`.
3. **Ishonch darajasi** mavjud `assess_confidence` qoidasidan (bozor narxisiz, xG va tarkibsiz), eng ehtimolli 1X2 natija uchun.
4. **Bashorat bo'lmasa sababi aytiladi** (`ForecastSkip` / `ForecastStatus`): liga kuzatilmaydi, jamoalar aniqlanmagan, liga modeli yo'q, tarix kam, jamoa tarixda topilmadi, namunaviy rejim.
5. **Shartnoma v1.13 (faqat qo'shish):** `MatchesService.forecast_day` va `forecast_match`, `MatchForecast`, `OutcomeChance` (`fair_odds`), `ForecastLine`, `ForecastStatus`. Soxta servis namunaviy bashorat beradi. `APP_SERVICES=fake` da (baza yo'q) real o'yinlar ro'yxatida bashorat `UNAVAILABLE`.
6. **Bot:** o'yinlar ro'yxatida "📊 Kun prognozi" (sahifali), o'yin kartasida "📊 Prognoz" (barcha bozorlar, adolatli koeffitsient, hisoblar, ishonch).
7. **Jamoa nomlari:** Football-Data to'liq rasmiy nomlarni beradi (`Manchester City FC`, `AFC Bournemouth`, `FC Bayern München`). Avvalgi alias jadvali API-Football nomlari uchun edi va 35 ta tekshirilgan nomdan birortasi ham ishonchli mos kelmadi. `team_matching` ga ikkala tomonda huquqiy shakl so'zlarini (`FC`, `AFC`, `CF`, `SSC`, `VfB`, `Calcio`...) olib tashlaydigan `club_key` bosqichi va Football-Data ga xos aliaslar qo'shildi: 35 tadan 35 tasi ishonchli mos keladi. Bu kunlik yangilashda saqlanadigan o'yinlar uchun ham muhim (tarix bilan bir jamoa = bitta qator).

8. **"🎯 Eng ehtimolli tanlovlar" (v1.14):** foydalanuvchi talabi — "koeffitsientsiz fozilarda o'tish ehtimoliga qarab eng yuqorilarini bersin". `MatchesService.top_picks(day_from, day_to)` barcha tayyor bashoratlardan tanlovlar yig'adi: 1X2, "yutqazmaydi" (1X/X2/12), Total 1.5/2.5/3.5, BTTS ha/yo'q va yangi "jamoa gol uradi" (`TeamPick`, score matritsasidan `1 - P(0 gol)`). Qoida bitta joyda (`rank_picks`): faqat boshlanmagan o'yinlar, foydalanuvchining `min_probability` sidan yuqori, bir o'yindan ko'pi bilan 2 ta, eng ehtimollisi birinchi, jami 15 ta (Telegram xabar chegarasi).
9. **Tahlil narxsiz qolsa:** "📊 Tahlil" natijasida signal bo'lmasa va sabab narx yo'qligi bo'lsa, xuddi shu kunlar uchun tanlovlar avtomatik yuboriladi (ko'pi bilan 10 kun). Bu xato bersa, natija yetkazilgan bo'ladi, faqat "olib bo'lmadi" deyiladi.
10. **Kartochkalar va hakam hali yo'q:** tarix (football-data.co.uk importi) va baza sxemasi sariq/qizil kartochka va hakamni saqlamaydi. Kartochka modeli uchun alohida ish kerak: sxema migratsiyasi, CSV dagi `HY/AY/HR/AR` (va faqat ba'zi ligalarda `Referee`) importi, kelgusi o'yin hakami (Football-Data `referees`, tarifga bog'liq) va kartochka modeli.

## Oqibatlar
- Bashorat sifati tarixga bog'liq: tarix import qilinmagan ligada `NO_MODEL`, o'yini kam jamoada `THIN_HISTORY`.
- Model bozor narxi bilan aralashtirilmaydi (ansambl yo'q), shuning uchun narxli tahlildan kamroq aniq; karta buni "faqat model bashorati" deb aytadi.
- O'rganish halqasining kalibrlashi bashoratga qo'llanmaydi (xom model).
- Qolgan nom farqlari birinchi haqiqiy sinxronlashdagi `team_matching_incomplete` logidan aliaslarga qo'shiladi.
