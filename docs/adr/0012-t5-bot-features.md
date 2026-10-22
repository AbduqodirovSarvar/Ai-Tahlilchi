# ADR 0012: T5 bot kengaytmalari (F7.1–F7.8) va shartnoma v1.6

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-22
- **Muallif:** bot-engineer
- **Kod:** `src/app/bot/{flow,custom_ui,detail_formatters,watch_ui,template_ui,bank_ui,report_ui,express_ui,charts}.py`, `src/app/bot/handlers/{custom,detail,watch,templates,bank,report,express}.py`, `src/app/services/{express_math,interfaces,report_math,fake}.py`, `src/app/services/real/{signals,notifier}.py`
- **Bog'liq:** [ADR 0006](0006-bot-architecture.md), [ADR 0010](0010-notifier-and-recalc-ux.md), qarorlar jurnali #12, #14, #17, shartnoma v1.6 (`docs/contracts.md`)
- **Raqam:** `0012` — boshqa parallel agentlar ham ADR qo'shgan bo'lsa, orkestr merge paytida raqamni tuzatadi.

## Kontekst
F7 sakkiz yangi imkoniyat qo'shadi. Ularning hammasi F6 bilan bir xil qoidalarga bo'ysunadi: bot faqat `services/interfaces.py` orqali ishlaydi, `callback_data` ≤ 64 bayt, eskirgan tugmalar, ikki marta bosish, foydalanuvchilar izolyatsiyasi, matnlar `texts.py` da.

## Qarorlar

### 1. Har funksiya = sof "ui" moduli + yupqa handler
`*_ui.py` (matn, klaviatura, JSON-xavfsiz draft — kirish/chiqish yo'q) va `handlers/*.py` (kirishni o'qiydi, servisni chaqiradi, ekran ko'rsatadi). Handlerlarning umumiy konteksti `FlowContext` (`flow.py`): servislar, `user_id`, FSM, fon vazifalari, soat, bir martalik ogohlantirishlar; `FlowContextMiddleware` uni yig'adi. Sabab: parametrlar soni chegarasi (≤ 5) va bir xil uslub; mavjud wizard `WizardSessionMiddleware` o'zgarmadi.

### 2. Tugmalarda katalog ID yo'q
F6 qoidasi davom etadi: ro'yxat tugmalari FSM draftdagi indeksni (`CustomCb`, `ExpressCb`, `WatchCb` pick) olib yuradi; ro'yxatdagi elementlar esa (`WatchCb`, `TplCb`, `BankCb`) mustaqil — ID (shablon/stavka — kichik `int`) yoki `crc32(entity_id)` (sevimlilar, 8 hex belgi) bilan, va **bosilganda yangi ro'yxatdan qayta qidiriladi**. Shuning uchun boshqa joyda o'zgargan ro'yxat boshqa elementni o'zgartira olmaydi ("Topilmadi"/"endi yo'q" xabari). Har bir ko'p qadamli oqim o'z xabariga bog'langan (`message_id` draftda): boshqa xabardagi yoki restartdan keyingi tugma `StaleDraftError` → "Bu tugma eskirgan". Barcha factorylar uchun `tests/bot/test_callbacks_t5.py` (hypothesis) 64 bayt chegarasini tekshiradi.

### 3. 🔍 Maxsus tahlil
- Qidiruv tartibi: `A - B` (yoki `vs`, `v`, uzun tire) → `search_fixtures`; aks holda `search_teams`; aniq mos kelsa yoki bitta topilsa — jamoaning keyingi 3 o'yini; bir nechtasi — tugmalar; hech biri — `Arsenal Chelsea` kabi ajratuvchisiz matn so'zlarga bo'linib, ikkala yarmi aniq jamoa nomi bo'lsa o'yin sifatida qidiriladi (≤ 6 so'z, katalogga ≤ 10 chaqiruv); oxirida `search_fixtures`.
- Tahlil `ScopeKind.FIXTURES` + `include_no_value=True`, barcha qo'llab-quvvatlanadigan bozorlar, chegaralar sozlamalardan; 10 tagacha o'yin (API sarfi). Natija: har o'yin uchun ixcham blok (barcha tanlovlar: ✅ / ▫️ / ❗) va faqat tavsiyadan o'tganlarga to'liq karta. Hech biri o'tmasa "Value topilmadi".
- Oddiy matn (`StateFilter(None)`, ≥ 2 belgi) shu qidiruvga tushadi; hech narsa topilmasa eski "Buni tushunmadim" javobi qoladi.
- Tahlil `BackgroundJobs` da (wizard bilan bir xil sabab, ADR 0006 §10); xatoda tanlov ekrani qaytadi.

### 4. 📋 Batafsil karta
Karta xabari tahrir qilinib batafsil kartaga aylanadi va [⬅️] bilan qaytadi (bitta xabar — bitta signal). Bir xabarga sig'ish kafolati: jadval qatorlari kamayadi, so'ng ixtiyoriy bo'limlar (harakat, H2H, jarohat…) tushiriladi. Jadval `<pre>` ichida, hamma matn escape.

### 5. ⭐ Sevimlilar — karta tugmasi nomi bo'yicha
`SignalView` jamoa/liga ID sini bermaydi. Karta [⭐ Kuzatish] jamoa nomini `search_teams` (aniq nom mosligi) va liga nomini `list_leagues` orqali ID ga aylantiradi. **Shartnoma so'rovi (keyingi to'lqin):** `SignalView.home_team_id/away_team_id/league_id` (qo'shimcha maydonlar) qo'shilsa, bu aylantirish yo'qoladi va nomdagi dublikatlar (bir xil nomli ikki jamoa) xavfi tugaydi.

### 6. 💾 Shablonlar
Shablon ishga tushirish = wizardning tasdiqlash ekrani (`WizardDraft.with_query`): API sarfi ko'rinadi, parametrlar o'zgartirilishi mumkin. `include_no_value` draftga qo'shildi (shablon uni yo'qotmasin). Jadval (hafta kunlari + vaqt) tahrirlanadi va saqlanadi (`save_template` nom bo'yicha almashtiradi, id saqlanadi). **Jadval bo'yicha avtomatik ishga tushirish pipeline'da hali yo'q** (shartnoma §4 "hali qilinmagan"); UI buni matnda aytadi. Takliflar: pipeline `saved_queries.schedule_*` ni scheduler orqali o'qib, kunlik xabar mexanizmi (`DeliveryKey(job="template", subject=<template_id>)`) bilan yuborsin.

### 7. 🎟 Stavkalar va bank
- Natijani qo'lda belgilash **signal** darajasida (`ResultService.mark_result`): shartnomada stavka darajasidagi belgi yo'q, shuning uchun belgi shu signaldagi barcha stavkalarga tegadi (ekranda aytilgan). Cashout — signal birligida (unit) foyda; haqiqiy summa bo'yicha cashout shartnomaga qo'shilmagan (§4 "hali qilinmagan").
- Zarar limiti: sozlamalarda limit bo'lsa va unga yetilgan bo'lsa, ✅ Stavka qildim ikki bosqichli ("Baribir davom etaman"). To'sish **ogohlantirish + aniq tasdiq**: qaror foydalanuvchiniki (ADR 0010 §5 bilan mos). "O'sha kun yangi signal yubormaslik" (§6.9) pipeline qoidasi: **hali amalga oshirilmagan**, hisobotda so'raladi.
- Grafik: `matplotlib.figure.Figure` (pyplot siz — global holat yo'q), `asyncio.to_thread`; yagona yorug' fon, bitta urg'u rangi, matnda emoji yo'q (shrift ogohlantirishi). Kalibratsiya va kesim grafiklari shu modulda.

### 8. 📈 Hisobot
Davr (7/30/hammasi/o'zim) va kesim tugmalari bir xabarni tahrirlaydi; ixtiyoriy oraliq FSM da saqlanadi (kelajak sanalar rad etiladi). Kalibratsiya jumlasi faqat eng ko'p to'lgan oraliqda n ≥ 10 bo'lsa chiqadi, aks holda "ma'lumot kam" (yolg'on ishonch bermaslik uchun). `/eksport` — `signals.csv` va `bets.csv`.

### 9. 🧩 Ekspress
Qoidalar va arifmetika `services/express_math.py` da (bitta joy, fake va real ishlatadi): 2–4 tanlov, har biri boshqa o'yindan (bir o'yindagi har qanday ikkita tanlov o'zaro bog'liq deb rad etiladi — konservativ), faol/o'ynalmagan/retro emas; ehtimollik va koeffitsient ko'paytiriladi, `EV = P·K − 1`. `app.models` import qilinmaydi (qatlam qoidasi: servislar modelga faqat pipeline orqali chiqadi), formula esa sodda. Bot erta alert beradi (bir o'yin, 5-tanlov), lekin yakuniy hakam servis.

### 10. Shartnoma v1.6 (faqat qo'shimcha)
`UndeliverableAware` porti (+ `UNDELIVERABLE_REFERENCE`), `ExpressService`/`ExpressView`/`ExpressLeg`/`MIN|MAX_EXPRESS_LEGS`/`Services.express`, `ReportService.export_bets`. Batafsil: `docs/contracts.md`. **Istisno:** `Services.express` majburiy maydon (qo'shimcha, lekin `Services(...)` quruvchilari — fake va real — yangilandi; boshqa quruvchi yo'q).

### 11. Yetkazib bo'lmaydigan foydalanuvchi (A3)
`TelegramNotifier` `TelegramForbiddenError` da foydalanuvchini 24 soatga "yetkazib bo'lmaydi" deb eslab qoladi (xotirada; bu davrda yuborishga urinmaydi). `NotifierDelivery` notifier `None` qaytarsa va `UndeliverableAware.is_undeliverable(user_id)` rost bo'lsa `UNDELIVERABLE_REFERENCE` qaytaradi — pipeline uni oddiy tasdiqlangan havola sifatida qabul qilib kalitni yakunlaydi (qayta urinish va admin ogohlantirishi yo'q). Pipeline kodi deyarli o'zgarmadi: faqat `pipeline/delivery.py` ga konstanta. Xotira restartda yo'qoladi — zararsiz: bitta qo'shimcha urinish. Vaqtinchalik xatolar (tarmoq, flood) belgilanmaydi va avvalgidek qayta uriniladi.

## Ko'rib chiqilgan muqobillar
- **`DeliveryResult` turli qaytarish turi:** `Notifier` ning barcha chaqiruvchilari va fake'lari o'zgarardi (buzuvchi).
- **Pipeline'da `PermanentDeliveryError`:** to'rt joyda `except` o'zgarishi kerak edi; havola-sentinel esa nol joyni o'zgartiradi.
- **Katalog ID ni tugmaga qo'yish:** 40 bayt + prefiks 64 dan oshishi mumkin.
- **pyplot:** global holat, parallel chaqiruvda xavfli.

## Oqibatlar
- Yangi tashqi bog'liqlik yo'q (`matplotlib` allaqachon e'lon qilingan).
- `texts.py` ~ 1100 qator: bo'limlar bo'yicha tartiblangan; kelajakda sohaga bo'lish mumkin.
- Cheklovlar va so'rovlar: §5 (ID lar), §6 (jadval bo'yicha ishga tushirish), §7 (limit bo'yicha signal to'xtatish, stavka bo'yicha cashout).
