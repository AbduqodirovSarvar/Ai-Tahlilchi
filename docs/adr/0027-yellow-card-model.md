# ADR 0027: Sariq kartochka modeli va hakam

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-29
- **Bog'liq:** [ADR 0026](0026-model-only-forecasts.md), [ADR 0025](0025-football-data-only.md), [ADR 0002](0002-domain-and-schema.md)

## Kontekst
Foydalanuvchi talabi: o'yinni "hakamlik qiladigan odam va jamoaning holatiga qarab sariq kartochkalar" bo'yicha ham tahlil qilish. Tizimda kartochka va hakam ma'lumoti umuman yo'q edi: tarix importi, domen va baza ularni saqlamasdi.

## Qarorlar
1. **Ma'lumot (faqat qo'shish):** `FixtureResult` ga ixtiyoriy `home_yellow`, `away_yellow`, `home_red`, `away_red`, `referee`; `fixtures` jadvaliga mos nullable ustunlar (Alembic `0011`). Birlashtirish xG kabi: yangi qiymat yutadi, yo'q qiymat saqlanganini o'chirmaydi; qoldirilgan yoki bekor qilingan o'yin ularni tozalaydi.
2. **Manbalar:** football-data.co.uk CSV (`HY`, `AY`, `HR`, `AR`; `Referee` faqat ingliz fayllarida). Buzuq kartochka katagi natijani tashlab yubormaydi, faqat kartochka `None` bo'ladi. Football-Data.org: `bookings` dan sanaladi (`YELLOW` sariq, `YELLOW_RED` va `RED` qizil, CSV kabi), bosh hakam `referees` dan (`type = REFEREE`). Ro'yxatlarda bookings yig'ilgan bo'ladi, shuning uchun `X-Unfold-Bookings: true` yuboriladi (`ProviderConfig.extra_headers`). Bo'sh bookings "kartochka yo'q" emas, "noma'lum" deb olinadi: 0 deb taxmin qilish modelni buzadi.
3. **Model** (`models.cards`, sof): `λ_uy = o'rtacha_uy · intizom[uy] · qo'zg'atish[mehmon] · qattiqlik[hakam]`, mehmon uchun ham shunday. Har bir koeffitsient kuzatilgan/kutilgan nisbat, liga o'rtachasiga shrinkage bilan (jamoa uchun 10, hakam uchun 15 o'yinlik prior). Vaqt og'irligi `exp(-xi · kunlar)` gol modelidagi kabi. Jami kartochka Poisson. Liga uchun kamida 40 ta kartochkali o'yin kerak, aks holda bashorat yo'q.
4. **Hakam nomlari:** manbalar har xil yozadi (`Michael Oliver` / `M Oliver`), shuning uchun `referee_key` = ism bosh harfi + familiya.
5. **Bashorat:** `pipeline.card_forecast` (liga va kun bo'yicha LRU kesh). Jami 3.5/4.5/5.5 va har jamoa uchun 1.5/2.5, ko'p yoki kam. Kelgusi o'yin hakami Football-Data dan; noma'lum bo'lsa liga o'rtachasi olinadi va karta buni aytadi.
6. **Shartnoma v1.15 (faqat qo'shish):** `CardScope`, `CardChance`, `CardOutlook`, `MatchForecast.cards`, `TopPick.card`. Kartochka tanlovlari "🎯 Eng ehtimolli tanlovlar" saralashiga ham kiradi.
7. **Bot:** o'yin prognozida "🟨 Sariq kartochkalar" bo'limi (kutilgan son, hakam va uning qattiq/o'rtacha/yumshoq bahosi, chiziqlar), kunlik prognozda qisqa qator.

## Oqibatlar
- **Mavjud bazada kartochka yo'q:** tarixni qayta import qilish kerak (`scripts/import_history.py`), upsert kartochkalarni mavjud qatorlarga yozadi. Qayta importgacha karta "kartochka ma'lumoti yetarli emas" deydi.
- Hakam qattiqligi faqat hakam nomi bor ligalarda (asosan Angliya) o'rganiladi; qolganlarida kartochka modeli jamoa ta'siri bilan ishlaydi.
- Poisson kartochkalarning haqiqiy tarqoqligidan (overdispersion) biroz tor: dumlar (6.5+) kamroq baholanadi. Asosiy chiziqlar (3.5–5.5) uchun mo'ljallangan.
- Qizil kartochka saqlanadi, lekin modellashtirilmaydi (juda kam uchraydi).
- Model hali backtest qilinmagan: aniqligi haqiqiy ma'lumotda tekshirilishi kerak.
