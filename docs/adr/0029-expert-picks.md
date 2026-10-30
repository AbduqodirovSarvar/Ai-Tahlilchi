# ADR 0029: Ekspert tahlili (koeffitsientsiz, har o'yinga 1–2 tanlov)

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-30
- **Bog'liq:** [ADR 0026](0026-model-only-forecasts.md), [ADR 0027](0027-yellow-card-model.md)

## Kontekst
Foydalanuvchi "professional sport tahlilchisi" promptini berdi. Talablar:
- har bir o'yin uchun koeffitsient va EV filtrisiz kamida 1, ko'pi bilan 2 ta tanlov;
- o'tish ehtimoli va xavf darajasi (🟢 ≥70%, 🟡 55–69%, 🔴 40–54%);
- forma, uy/safar, gollar, H2H va tarkibga asoslangan qisqa sabab;
- 1X2, ikki karra imkoniyat, totallar, BTTS, fora (0, ±1) va individual totallar.

## Variantlar
1. Gemini ehtimollikni o'zi baholaydi (prompt aynan shunday). Kamchiligi: kalibrlanmagan, bir o'yin bugun 68%, ertaga 55% bo'lishi mumkin, har o'yin Gemini limitidan yeydi.
2. **Gibrid (tanlandi, foydalanuvchi tasdiqladi):** ehtimollikni Dixon-Coles modeli beradi, Gemini faqat model variantlari ichidan 1–2 tasini tanlaydi va faktlardan sabab yozadi.
3. LLM siz: faqat qoida va shablon sabab.

## Qarorlar
1. **Bozorlar:** bashoratga Fora 0, -1, +1 qo'shildi (butun chiziq: aniq farqda pul qaytadi, `push` alohida ko'rsatiladi). Individual total: jamoa gol uradi (0.5) va 1.5 dan ko'p. Kartochkalar ekspert tanloviga kirmaydi (prompt ro'yxatida yo'q).
2. **Faktlar** (`pipeline.match_facts`) faqat bazadan olinadi, provayder so'rovi ketmaydi:
   - so'nggi 5 o'yin formasi;
   - mezbonning uydagi va mehmonning safardagi so'nggi 5 o'yini;
   - so'nggi 10 o'yindagi o'rtacha gollar, Total 2.5 va BTTS ulushi;
   - joriy mavsum jadvali (bazadagi natijalardan hisoblanadi);
   - 3 yil ichidagi so'nggi 5 ta o'zaro o'yin.

   Jarohatlar Football-Data bepul tarifida yo'q, shuning uchun ular aytilmaydi.
3. **Gemini** (`match_picks.v1`): bitta so'rovda 8 tagacha o'yin yuboriladi. Har o'yin uchun har oiladan eng ehtimolli 4 tagacha variant beriladi: "natija" (1X2, ikki karra imkoniyat, fora) va "gollar". Faqat berilgan `option_id` qabul qilinadi. Sabab mavjud guardlardan o'tadi: uzunlik, belgi va kirishda yo'q ismlar. O'tmasa, sabab tashlanadi, tanlov qoladi. Ehtimollikni o'zgartirish taqiqlangan.
4. **Fallback qoidasi:** Gemini o'chiq bo'lsa, limit tugasa, ishlamasa yoki noto'g'ri javob bersa, eng ehtimolli variant tanlanadi. Ikkinchi tanlov boshqa oiladan, 40% dan past bo'lmasa qo'shiladi. Sababni bot faktlardan shablon bilan yozadi (matnlar `texts.py` da). Ehtimolligi 40% dan past yagona tanlov ham ko'rsatiladi (⚪ "juda tavakkal"), chunki talab "har o'yinga kamida 1".
5. **Shartnoma v1.16 (faqat qo'shish):** `MatchesService.expert_picks`, `ExpertReport`, `RiskLevel` va boshqalar. Bot: o'yinlar ekranida "🧠 Ekspert tahlili" tugmasi; narxsiz "📊 Tahlil" dan keyin avvalgi "eng ehtimolli tanlovlar" o'rniga ekspert tahlili yuboriladi.

## Oqibatlar
- Tahlil faqat modeli bor ligalarda ishlaydi; qolganlari "bashorat yo'q" deb sanaladi.
- Gemini chaqiruvi kunlik LLM byudjetini sharhlovchi bilan bo'lishadi.
- Prompt dagi JSON formati backend uchun alohida chiqarilmagan: `ExpertReport` DTO o'sha ma'lumotni tipli shaklda beradi.
