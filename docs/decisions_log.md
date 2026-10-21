# Qarorlar jurnali

> Avtonom rejimda orkestr va agentlar qabul qilgan qarorlar ([agentlar_rejasi.md, 10.2](../agentlar_rejasi.md)). Katta texnik qarorlar uchun alohida ADR: [docs/adr/](adr/).

| # | Sana | Qaror | Sabab | Oqibat | Kim |
|---|---|---|---|---|---|
| 1 | 2026-10-21 | **F0 kechiktirildi** | API kalitlari hali yo'q; foydalanuvchi kalitlarni loyiha tugagandan keyin qo'shadi | Ishlab chiqish soxta (fake) servislar va tarixiy CSV ma'lumotlar bilan davom etadi. Ilova, testlar va CI `.env` siz ishlaydi; kalit kerak bo'lgan komponent faqat ishga tushirilganda aniq xato beradi. Jonli tekshiruvlar (F0.7, jonli bot) kalitlar qo'shilgandan keyin bajariladi | orkestr |
| 2 | 2026-10-21 | **src layout** (`src/app/`) | Mahsulot egasi qarori; testlar o'rnatilgan paketni import qiladi | Barcha yo'llar `src/app/...`; paket nomi `app` o'zgarmaydi. Qarang: [ADR 0001](adr/0001-tooling.md) | mahsulot egasi / orkestr |
| 3 | 2026-10-21 | **`TZ` sozlamasi `DISPLAY_TZ` ga o'zgartirildi** | POSIX `TZ` ko'pincha OS tomonidan o'rnatiladi (masalan `:/etc/localtime`) va jarayon soatiga ta'sir qiladi; u bilan to'qnashuv ilovani ishga tushirmay qo'yardi (QA T1) | `.env` da `DISPLAY_TZ=Asia/Tashkent`; faqat ko'rsatish uchun, saqlash UTC | orkestr |
| 4 | 2026-10-21 | **Bot faqat shaxsiy chatda ishlaydi** | Shaxsiy bot: signallar va stavkalar boshqalar o'qiy oladigan joyga chiqmasligi kerak | Whitelist middleware guruh, superguruh, kanal va chatsiz update'larni (inline so'rovlar) admin yozsa ham tashlab yuboradi; rad etishlar DEBUG darajada loglanadi | orkestr |
