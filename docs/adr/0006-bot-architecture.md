# ADR 0006: Telegram bot arxitekturasi (F6 MVP)

- **Holat:** qabul qilindi (T3)
- **Sana:** 2026-10-21
- **Muallif:** bot-engineer

## Kontekst
Bot (F6.1–F6.6, F6.10) real pipeline (T4) tayyor bo'lishidan oldin quriladi va tizim bilan faqat servis shartnomasi (`app.services.interfaces`, v1.1) orqali gaplashadi. Talablar: holatli wizard (sana, kalendar, qamrov pickerlari), Telegram cheklovlari (4096 belgi, 64 baytli `callback_data`, flood control), eskirgan tugmalarga chidamlilik va testlanadigan yupqa handlerlar.

## Qarorlar

### 1. Servislar dependency injection orqali; hozircha `APP_SERVICES=fake`
`build_dispatcher(admin_ids, services, *, storage, clock, rate_limit)` — `Services` aiogram workflow data sifatida beriladi (`services`, `clock`, `notices`). `main.build_services(settings)` hozir faqat `create_fake_services()` ni qaytaradi; tanlov `Settings.app_services` (`APP_SERVICES`, `Literal["fake"]`) bilan. T4 da `"real"` qiymati qo'shiladi va shu funksiya real servislarni yig'adi — bot kodi o'zgarmaydi. Startda `services_backend` WARNING logi namunaviy ma'lumot ishlatilayotganini ko'rsatadi.

### 2. Qatlamlar
`texts.py` (barcha matnlar) → `parsing.py` (sof parserlar) → `formatters.py` (sof HTML) → `keyboards.py` + `callbacks.py` (sof klaviaturalar) → `draft.py`/`states.py` (FSM ma'lumoti) → `wizard.py` (qadam ekranlari va o'tishlar) → `session.py` (har update uchun kontekst) → `handlers/*` (yupqa). Handler kirishni o'qiydi, draftni yangilaydi va `session.go(step)` chaqiradi.

### 3. Middleware tartibi
`update` outer: whitelist (begonalar jim tashlanadi) → `UserContextMiddleware` (`user_id`, structlog kontekst) → `RateLimitMiddleware` (token bucket: 8 ta burst, 1/soniya; birinchi oshishda bitta "⏳ Juda tez" xabari). `message`/`callback_query` inner: `ErrorMiddleware` — `InputError`, `StaleDraftError` va `ServiceError` larni o'zbekcha xabarga aylantiradi (`CooldownActiveError` → "⏳ N daqiqadan keyin…"), kutilmagan xato — traceback bilan log + umumiy xabar. Callback xatosi qisqa bo'lsa alert, uzun bo'lsa yoki callback allaqachon javob olgan bo'lsa xabar sifatida.

### 4. `callback_data`: qisqa prefiks + indeks
`CallbackData` factorylari 2 harfli prefiks va kichik qiymatlar (enum kodi, int) bilan. Katalog ID lari (`EntityId`, ≤ 40 bayt, `:` bo'lishi mumkin) tugmaga **qo'yilmaydi**: picker joriy sahifa variantlarini FSM draftida saqlaydi, tugma indeksni olib yuradi. Bu 64 bayt chegarasini har qanday ID uzunligida kafolatlaydi (property test bilan tekshirilgan). Signal tugmalari `SignalId` (int) ishlatadi.

### 5. FSM: MemoryStorage + almashtiriladigan seam
FSM `MemoryStorage` da (bitta jarayon, shaxsiy bot). Draft va natijalar JSON-xavfsiz (`WizardDraft.to_data/from_data`, `StoredResults`), barcha kirish `states.py` helperlari orqali — persistent storage (Redis/SQLite) ni `build_dispatcher(storage=...)` ga berish kifoya. **Oqibat:** restartdan keyin wizard holati yo'qoladi; bu ataylab ishlangan:
- holat filtri mos kelmagan har qanday tugma `fallback` routeriga tushadi → "⌛ Bu tugma eskirgan, qaytadan boshlang" alerti;
- wizard tugmasi faqat draftdagi `message_id` ga tegishli xabarda ishlaydi (eski xabardagi tugma ham eskirgan);
- ▶️ Boshlash ikki marta bosilsa — `running` holatida "⏳ allaqachon bajarilmoqda" (kafolat 10-bo'limda: bir foydalanuvchining updatelari ketma-ket ishlanadi va `running` qulf ichida tekshiriladi va qo'yiladi);
- eski tahlil natijasining "➡️ Yana" tugmasi yangi natijani varaqlamaydi: tugma natija tokenini (`ResultsCb.token`) va sahifa raqamini olib yuradi, mos kelmasa — "eskirgan" alerti, sahifa allaqachon ko'rsatilgan bo'lsa (ikki marta bosish) — jim e'tiborsiz;
- buzilgan/eski formatdagi draft `StaleDraftError` → foydalanuvchiga qayta boshlash taklifi.
Agent qoidasi persistent storage ni talab qiladi; u keyingi to'lqinda (F9 deploy bilan) qo'shilishi tavsiya etiladi — kod tayyor.

### 6. Telegram cheklovlari
- Har xabar `send_html` orqali: `parse_mode=HTML`, ≤ 4096 belgili qismlarga satr chegarasida bo'linadi; foydalanuvchi/tashqi matn `escape()` (apostrof saqlanadi).
- `FloodControlMiddleware` bot sessiyasiga o'rnatiladi: `TelegramRetryAfter` da bir marta kutib qayta yuboradi (≤ 60 s; uzunroq kutish xato sifatida chiqadi).
- "message is not modified" va "query is too old" xatolari yutiladi (ikki marta bosish).
- Har callbackga aniq bir `answer()`: wizardda `WizardSession.answer()` + middleware xavfsizlik to'ri, boshqa handlerlarda `answer_callback`.
- Progress xabari ≥ 1.5 s oraliqda tahrirlanadi (final bosqich doim), xato tahlilni to'xtatmaydi.

### 7. `/tahlil` → tasdiqlash ekrani
Tezkor buyruq wizard bilan bir xil draftni quradi va to'g'ridan-to'g'ri tasdiqlash ekraniga o'tadi (API sarfi ko'rinadi, ✏️ va 💾 mavjud). Sabab: xato yozilgan buyruq byudjetni sarflamaydi. Parser sof funksiya, hypothesis bilan "hech qachon yiqilmaydi" xossasi tekshirilgan.

### 8. §6.6 ogohlantirishi "bir marta"
`OnceNotices` jarayon xotirasida (foydalanuvchi × ogohlantirish). Restartdan keyin bir marta qayta ko'rinishi mumkin — zararsiz. Doimiy saqlash uchun sozlamalarda bayroq kerak (servis shartnomasiga so'rov).

### 9. Shartnoma v1.1 ga moslashuv
Sport qadami va bozorlar `CatalogService.list_sports()` dan (mavjud bo'lmagan sport ko'rsatilmaydi, eksperimental — "🧪"); kalendar oxirgi kuni `horizon_end(today)`; retro signal kartasi natija bilan, stavka/qayta hisob tugmasisiz; watchlist signallari alohida "⭐ Sevimlilar" blokida, `shortfall` hisobiga kirmaydi.

### 10. Bir vaqtdagi updatelar va fon tahlili (T3 fix round)
Dastlabki variantdagi "ikki marta bosish himoyasi" faqat ketma-ket ishlov berishda ishlardi: aiogram polling updatelarni alohida task sifatida ishlatadi (`handle_as_tasks=True`), `events_isolation` berilmagani uchun ikki ▶️ bir vaqtda `analyze` ni ikki marta ishga tushirar, tez ketma-ket tugmalar (bozorlar, sozlamalar) bir-birining o'zgarishini yo'qotar edi (QA F-1).
- `Dispatcher(events_isolation=SimpleEventIsolation())`: bir foydalanuvchi/chat updatelari navbat bilan ishlanadi (har bir handler FSM ma'lumotini o'z navbatida o'qiydi va yozadi).
- Uzoq `analyze` qulf ichida bajarilmaydi (butun tahlil davomida foydalanuvchining barcha tugmalari qotib qolardi). ▶️ handleri qulf ichida holatni tekshiradi va `running` ni qo'yadi (check-and-set), progress xabarini yuboradi va tahlilni `BackgroundJobs` (`app/bot/jobs.py`) orqali fon vazifasi sifatida ishga tushirib qaytadi. Vazifa natijani progress xabari orqali xabar qiladi va faqat qisqa holat o'zgarishlari uchun (`draft`ni yopish, natijani saqlash, tasdiqlashni tiklash) o'sha qulfni qayta oladi.
- Vazifa har qanday yakunda (muvaffaqiyat, `ServiceError`, kutilmagan xato) foydalanuvchini `running` da qoldirmaydi: progress xabari yopiladi, tasdiqlash ekrani qaytariladi, xato xabari yuboriladi. Foydalanuvchi shu orada yangi wizard boshlagan bo'lsa (draftdagi `message_id` boshqa) — yangi wizardga tegilmaydi.
- `running` paytida faqat wizard tugmalari "allaqachon bajarilmoqda" oladi; eski natija kartalari va "➡️ Yana" ishlaydi.
- Cheklov: `/bekor` tahlilni to'xtatmaydi (faqat wizard holatini yopadi); natija baribir yuboriladi. Tahlilni bekor qilish alohida funksiya bo'ladi.
- Bot to'xtaganda fon vazifalari bekor qilinadi (`dispatcher.shutdown`).
- Callback allaqachon javob olgan bo'lsa (`AnswerTracker`, har update uchun `ErrorMiddleware` o'rnatadi), keyingi xato foydalanuvchiga xabar sifatida yetkaziladi.
- Sozlamalardagi ko'p tanlovli tugmalar elementni nomi bilan ko'rsatadi (sport/bozor — enum qiymati, liga — indeks + `crc32` hex), shuning uchun o'zgargan ro'yxat boshqa elementni almashtirmaydi.

## Oqibatlar
- Bot fake ustida to'liq qo'lda sinovdan o'tkazilishi mumkin (`docs/test_scenarios.md`).
- T4: `APP_SERVICES=real` + `build_services` ni kengaytirish; bot o'zgarmaydi.
- F6.7/F7 tugmalari (🔄, 📋, ✅, menyu bandlari) ulangan, "tez orada" javobi bilan.
