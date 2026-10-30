# ADR 0029: Tahlil markazi: stavka tavsiyasisiz, sodda menyu va o'yinning chuqur tahlili

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-30
- **Bog'liq:** [ADR 0026](0026-model-only-forecasts.md), [ADR 0027](0027-yellow-card-model.md), [ADR 0028](0028-data-aggregator.md)

## Kontekst
Bot EV, minimal ehtimollik, bozor turi kabi filtrlarni foydalanuvchidan so'rardi va koeffitsientga qattiq bog'langan edi. Natijada foydalanuvchi ko'pincha "qiymat topilmadi" kabi bo'sh javob olardi. Foydalanuvchi konsepsiyani o'zgartirdi:
- botdan stavka tavsiyasi emas, o'yinning xolis va chuqur tahlili kutiladi;
- foydalanuvchidan hech qanday parametr so'ralmaydi;
- menyu 4 tugmadan iborat: 📅 Bugungi o'yinlar, 🗓 Ertangi o'yinlar, 🏆 Top Ligalar tahlili, ℹ️ Yordam / Bot haqida;
- o'yin tugmasi tahlilni ochadi, tagida faqat 🔄 Yangilash va ⬅️ O'yinlar ro'yxatiga qaytish turadi.

Shu kuni qilingan "🧠 Ekspert tahlili" (har o'yinga 1–2 tanlov va %) ishi yangi konsepsiyaga zid bo'lgani uchun foydalanuvchi qarori bilan olib tashlandi. Talab .NET/C# deb yozilgan edi. Foydalanuvchi mavjud Python stekini refaktoring qilishni tanladi.

## Variantlar
1. Faqat Gemini: ma'lumotni to'g'ridan-to'g'ri AI ga berish. Kamchiliklari: AI o'chiq yoki limit tugasa, bo'sh javob qaytadi; raqamlarni to'qish xavfi bor.
2. **Gibrid (tanlandi):** raqamlarni pipeline hisoblaydi, AI esa faqat matn yozadi.
   - Raqamlar: bazadagi tarix faktlari (forma, uy/safar, gollar, jadval, H2H), model ehtimolliklari va kutilayotgan gollar, provayderdan tarkiblar.
   - AI shu raqamlardan 5 bo'limli matn yozadi.
   - Har bir bo'lim alohida guarddan o'tadi: uzunlik, belgilar va kirishda yo'q ismlar. O'tmagan bo'lim o'rniga bot o'sha bo'limni faktlardan shablon bilan yozadi.
   - Natijada tahlil hech qachon bo'sh chiqmaydi.

## Qarorlar
1. **Prompt** `match_analysis.v1`: bitta so'rov bitta o'yin uchun. Unda 5 bo'lim bor: forma, uy/safar, tarkib va yo'qotishlar, taktika, xulosa. Stavka so'zlari, jarohat va kirishda yo'q o'yinchilar taqiqlangan. `injuries: null` aniq beriladi, chunki manbada jarohat ma'lumoti yo'q.
2. **Kesh:** tahlil 30 daqiqa qayta ishlatiladi. 🔄 Yangilash yangi tahlil so'raydi, lekin oxirgi tahlil 2 daqiqadan yosh bo'lsa, o'shani beradi. Shu bilan bosib turish Gemini kunlik byudjetini yemaydi. Kesh jarayon xotirasida turadi: qayta ishga tushganda har o'yin uchun bitta qo'shimcha so'rov ketadi.
3. **Top ligalar:** kuzatiladigan 8 liga (PL, PD, SA, BL1, FL1, DED, PPL, ELC). Tanlangan liganing keyingi 7 kundagi o'yinlari chiqadi; bu bitta `list_matches` so'rovi.
4. **Shartnoma v1.16:**
   - `MatchesService.analyse_match`, `MatchAnalysis`, `MatchOutlook`, `AnalysisSections`, fakt DTO lari, `ANALYSIS_TTL_SECONDS` va `ANALYSIS_REFRESH_SECONDS`.
   - `Forecast.expected_goals` (pipeline).
   - v1.16 hali chiqmagan edi; ekspert tanlovlari (`expert_picks`, `RiskLevel`) shu versiyadan olib tashlandi.
5. **Bot:** dispetcherda faqat `start`, `analytics`, `status` (/holat) va `fallback` routerlari qoldi.
   - Eski oqimlar handlerlari, ularning UI modullari va testlari o'chirildi: tahlil ustasi, sozlamalar, maxsus tahlil, shablonlar, sevimlilar, stavkalar/bank, hisobot, ekspress, natijalar, o'rganish, baza, so'rovlar, eski o'yinlar ekrani.
   - Servis va pipeline qatlamidagi signal, bank va o'rganish kodi tegilmadi: rejalashtiruvchi undan foydalanadi (bazani yangilash, settle, o'rganish).

## Oqibatlar
- Tarix va model faqat kuzatiladigan ligalarda bor. Boshqa ligalar (masalan, CL) uchun tahlil tarkib va AI matni bilan cheklanadi, faktlar bo'limida "tarix yo'q" deyiladi.
- Football-Data.org bepul tarifida tarkiblar odatda kelmaydi, jarohatlar esa umuman yo'q. "Tarkib va yo'qotishlar" bo'limi buni ochiq aytadi.
- `texts.py`, `keyboards.py`, `callbacks.py` va `formatters.py` da eski oqimlardan qolgan, endi ishlatilmaydigan konstantalar va funksiyalar bor. Ularni tozalash keyingi alohida qadam.
- Avval sozlamalar orqali yoqilgan kunlik signal va shablon yuborishlari rejalashtiruvchida hali ishlaydi. Foydalanuvchi ularni endi o'chira olmaydi. Ularni o'chirish yoki olib tashlash ham alohida qaror talab qiladi.
