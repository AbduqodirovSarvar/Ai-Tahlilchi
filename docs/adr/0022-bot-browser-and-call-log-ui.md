# ADR 0022: Bot: baza va so'rovlar ekranlari (T7-D)

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-26
- **Muallif:** telegram-bot-engineer
- **Bog'liq:** [ADR 0020](0020-provider-call-log.md), [ADR 0021](0021-db-only-analysis.md), [ADR 0006](0006-bot-architecture.md)

## Kontekst
Foydalanuvchi bazadagi hamma narsani va API-Football/Gemini ga ketgan har bir so'rovni Telegramda ko'rishni xohladi. Entity id lari 40 baytgacha (`callback_data` 64 bayt), ro'yxatlar uzun, so'rov/javob matnlari 20 000 belgigacha.

## Qarorlar
1. **Raqamlangan ro'yxat + raqam tugmalari.** Liga/jamoa/o'yin nomi tugmaga sig'maydi; matnda raqamlangan qatorlar, ostida 1..10 tugmalar. Id lar tugmada emas: ekran `BrowseNav` yozuvini FSM data da saqlaydi (id lar, sahifa, filtr). Yozuv JSON, shuning uchun persistent storage restartdan o'tkazadi.
2. **Token + ekran kodi.** Menyu kirishlari (`/baza`, Ligalar, Jamoalar, Hamma o'yinlar, qidiruv natijasi) yangi token beradi; eski xabar tugmasi tokeni mos kelmasa "eskirgan" alerti. Ekran ichidagi navigatsiya tokenni o'zgartirmaydi (sahifa tugmasini ikki marta bosish zararsiz), tugma esa chizilgan ekran kodini olib yuradi: boshqa ekranga tegishli tugma faqat joriy ekranni qayta chizadi. Tanlandi, chunki oddiy "har bosishda token" ikki marta bosishni xatoga aylantirardi.
3. **Yangilash alohida xabarda.** "🔄 Bazani yangilash" doim yangi "⏳" xabar yuboradi va uni natija bilan tahrirlaydi, shuning uchun u tasdiq ekrani yoki xato xabari ostida ham asl ekranni buzmaydi. Bir vaqtda bitta yangilash: `BackgroundJobs.tasks` da `db_refresh` nomli vazifa borligi tekshiriladi (jarayon ichida; servis ledger `claim` i restart va ko'p jarayonni qoplaydi).
4. **`DataStaleError`.** `middleware.error_reply` foydalanuvchi mintaqasida "oxirgi: …" matni va yangilash tugmasini beradi; ErrorMiddleware, tahlil/maxsus tahlil fon vazifalari va qayta hisoblash bir xil funksiyadan foydalanadi. Tugma whitelist dagi hammaga ko'rinadi (shaxsiy bot: whitelist = adminlar); admin bo'lmaganga servis aniq rad javobi beradi. Tasdiq ekrani `CostEstimate.data_stale` bo'lsa ogohlantiradi va ▶️ Boshlash ni yashiradi.
5. **Uzun matn.** `<pre>` bir necha qatorga cho'ziladi, umumiy `split_message` uni buzardi; shuning uchun batafsil ekrani `<pre>` bloklarini o'zi to'liq elementlar qilib yig'adi (har biri ≤ 3900 belgi), 3500 dan uzun matn ekranda qisqartiriladi va to'liq matn `.txt` hujjat sifatida beriladi.
6. **So'rovlar ekrani stateless**: tugmada faqat filtr, sahifa va butun `call_id`.

## Oqibatlar
- Yangi `/baza` ochilsa avvalgi baza xabaridagi ichki tugmalar eskiradi (bitta ko'rish sessiyasi).
- Karta 4096 dan oshsa `edit_html` bo'ladi (kartada faqat bir qatorli teglar).
