# ADR 0024: O'yinlar jonli Football-Data.org dan (1-bosqich)

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-29
- **Bog'liq:** [ADR 0021](0021-db-only-analysis.md) (qisman bekor qilinadi), [ADR 0023](0023-football-data-org-client.md), [ADR 0020](0020-provider-call-log.md)

## Kontekst
Foydalanuvchi talabi: o'yinlar (bugungi, ertangi, istalgan sana) bazadan emas, to'g'ridan-to'g'ri API dan olinsin; bazada faqat so'rovlar haqidagi ma'lumot saqlansin; sanalar `30.10.2026 21:00` ko'rinishida chiqsin. Bot "mavjud bo'lmagan o'yinlar"ni ko'rsatayotgani aytildi.

Tanlangan yo'l (foydalanuvchi bilan kelishilgan): o'yinlar ro'yxati va sanalar Football-Data.org dan (30 so'rov/daqiqa, kunlik limit yo'q); tahlil uchun koeffitsientlar API-Football dan, faqat tahlil bosilganda. Foydalanuvchi ma'lumotlari (sozlamalar, signallar, stavkalar, bank) bazada qoladi.

"Mavjud bo'lmagan o'yinlar"ning ehtimoliy sababi: `APP_SERVICES` standarti `fake`, bu rejimda bot o'ylab topilgan namunaviy o'yinlarni ko'rsatadi.

## Qarorlar
1. **Ikki bosqich.** Tahlil quvuri (model tarixi, narxlar, signal yozuvi, `DataStaleError`, kunlik refresh) bazaga chuqur bog'langan. Uni bir o'zgarishda qayta yozish xavfli, shuning uchun 1-bosqichda faqat o'yinlar ro'yxati jonli qilinadi; tahlilni bazadan uzish 2-bosqich.
2. **Yangi `MatchesService` (shartnoma v1.12, faqat qo'shish)**: `list_matches(user_id, day_from, day_to)` va `get_match(user_id, match_id)`; DTO lar `MatchItem`, `MatchSheet`, `SideLineup`, `LineupPlayer`, `MatchState`; `MAX_MATCH_DAYS = 10`; `Services.matches` majburiy maydon.
3. **Bazaga hech narsa yozilmaydi va o'qilmaydi**: `pipeline.matches.MatchFeed` har chaqiruvda provayderdan so'raydi; yagona nusxa klientning qisqa TTL keshi (ro'yxat 10 daqiqa, o'yin 5 daqiqa). Har so'rov chaqiruvlar jurnaliga `football_data` provayderi bilan yoziladi (`ProviderConfig.log_provider`, `Provider.FOOTBALL_DATA`; enum ustuni CHECK siz, migratsiya kerak emas).
4. **Mahalliy kun → UTC**: Toshkent kuni ikki UTC kuniga to'g'ri keladi; feed ikkala UTC kunini so'rab, boshlanish vaqti mahalliy oynaga tushganlarni qoldiradi.
5. **Ikkala rejimda haqiqiy**: `FOOTBALL_DATA_ORG_KEY` bor bo'lsa, `APP_SERVICES=fake` da ham `matches` real servis bilan almashtiriladi (`main.build_services`). Kalitsiz real servis "sozlanmagan" deydi, hech narsa ko'rsatmaydi.
6. **Menyu**: "⚽ O'yinlar" birinchi qatorda; "🗄 Baza" tugmasi menyudan olindi (`/baza` buyrug'i 2-bosqichgacha qoladi, chunki tahlilning "Bazani yangilash" oqimi unga tayanadi).
7. **Sana formati**: `formatters.kickoff_stamp` (`%d.%m.%Y %H:%M`, foydalanuvchi zonasida) — o'yin ro'yxati, signal kartasi, batafsil karta, maxsus tahlil, ekspress, natijalar va sevimlilar xabarlari shu bitta funksiyadan foydalanadi.

## Oqibatlar
- Football-Data.org da koeffitsient yo'q: signal/EV tahlili hozircha avvalgidek bazadagi API-Football ma'lumotidan ishlaydi (2-bosqichgacha).
- Ikki provayderning o'yin id lari har xil; 2-bosqichda tahlil Football-Data o'yinini API-Football o'yini bilan jamoa nomlari va vaqt bo'yicha moslashi kerak (`sports.team_matching` dan foydalanish rejalashtirilgan).
- Bepul tarifda tarkiblar bo'sh: karta "e'lon qilinmagan yoki tarifda yo'q" deydi.
- `fake` rejimdagi boshqa bo'limlar (Tahlil, Signallar) hali ham namunaviy ma'lumot ko'rsatadi; haqiqiy ma'lumot uchun `APP_SERVICES=real`.
