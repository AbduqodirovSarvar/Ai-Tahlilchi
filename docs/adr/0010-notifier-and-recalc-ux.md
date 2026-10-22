# ADR 0010: Telegram Notifier va qayta hisoblash UX (T4 bot)

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-22
- **Muallif:** bot-engineer (F6.7–F6.9, `Notifier`, minimal stavkalar)
- **Kod:** `src/app/bot/notifier.py`, `notify_formatters.py`, `account_formatters.py`, `handlers/{recalc,today,status,bets}.py`
- **Bog'liq:** [ADR 0006](0006-bot-architecture.md), [ADR 0008](0008-pipeline-and-real-services.md), shartnoma v1.2 (`docs/contracts.md` §4), qarorlar jurnali #15

## Kontekst
Pipeline (T4) o'zi yozadigan xabarlarni (kunlik tahlil, 24 soat / pre-match yangilanishi, bekor qilish, sevimlilar, natija, admin ogohlantirishi) `Notifier` portiga beradi; bot uni Telegram'da amalga oshirishi kerak. Shu bilan birga foydalanuvchi o'zi so'raydigan qayta hisoblash (F6.7), bugungi signallar (F6.8), `/holat` (F6.9) va kartadagi "✅ Stavka qildim" tugmasi servis v1.2 imkoniyatlariga (`BusyError`, `ServiceUnavailableError`, `recalc_window`, `get_signals`, `client_token`) ulanadi.

## Qarorlar

### 1. `TelegramNotifier`: yetkazadi, qaror qilmaydi
- Tinch soatlar, idempotentlik (`DeliveryKey`), kechikish — pipeline'niki (qaror #15). Notifier faqat formatlaydi va yuboradi; takroriy kalitga bardoshli (holatsiz).
- **Hech qachon istisno tashlamaydi.** `TelegramForbiddenError` (bloklagan) → `None`, qayta urinish yo'q; boshqa `TelegramAPIError` (chat topilmadi, tarmoq) → `None` + `WARNING`; `TelegramRetryAfter` → `retry_after` (≤ 60 s) kutib **bir marta** qayta yuboradi, ikkinchisida `None`; formatlash/sozlamalar xatosi → `logger.exception` va `None`. Log faqat `job`, `user_id`, `subject`, `delivered` (matn va maxfiy ma'lumot yo'q).
- Uzun xabar `split_message` bilan ≤ 4096 belgiga bo'linadi (HTML teglar yopilib-qayta ochiladi); tugmalar oxirgi bo'lakda; qaytgan `MessageRef` — birinchi bo'lakniki. Keyingi bo'lak yiqilsa, yuborilgan qism uchun `MessageRef` baribir qaytadi.
- Barcha model/foydalanuvchi matni (jamoa, liga, LLM izohi, admin xabari, `detail`) `escape()` dan o'tadi (shartnoma §7 qoida 5); admin xabari 1500 belgigacha qisqartiriladi.
- Bot `Notifier` kerak bo'lganda hali ulanmagan bo'ladi (servislar bot'dan oldin quriladi): `TelegramNotifier.bind(bot)` dispatcher `startup` hook'i (`on_startup`) orqali chaqiriladi; ulanguncha chaqiruvlar `None` qaytaradi. Foydalanuvchi vaqt mintaqasi `attach_settings(SettingsService)` orqali olinadi (xato bo'lsa — `display_timezone`).

### 2. Yangilanish xabari: tahrirlanmaydi, **javob** sifatida yuboriladi
Topshiriq "asl kartani tahrirlash, bo'lmasa yangisini yuborish"ni so'ragan. Pipeline `reply_to` sifatida **kunlik xabar** havolasini beradi (kalit — bitta xabar), u esa bitta xabar emas: sarlavha + har bir karta alohida xabar. Birinchi xabarni tahrirlash kartaga tegishli emas, boshqa signalning ma'lumotini buzadi va "avval → keyin" raqamlarini yo'qotadi (farq xabarining maqsadi shu). Shuning uchun update/bekor xabari `reply_parameters` (`allow_sending_without_reply=True`: asl xabar o'chirilgan bo'lsa ham yuboriladi) bilan **yangi javob xabari** bo'ladi. Karta o'rnida tahrirlash faqat foydalanuvchi o'zi bosgan 🔄 da bo'ladi — u yerda tugma ostidagi aniq xabar ma'lum.

### 3. Kunlik xabar tuzilishi
Bitta sarlavha xabari (☀️ sana, 📆 kecha natijasi, natijalar xulosasi: shortfall / "Value topilmadi" / ⭐ eslatma, [🔄 Hammasini yangilash]) va undan keyin xuddi qo'lda tahlildagidek kunlar sarlavhasi + har bir karta o'z tugmalari bilan (🔄 ✅ 🙈 ishlaydi). Karta tugmalari bitta xabarga sig'maydi, shuning uchun har karta alohida; 30 signalgacha oqim `FloodControl` + notifier'ning bir martalik qayta urinishi bilan chidaydi.

### 4. 🔄 Qayta hisoblash
- **Karta:** callback darhol javob oladi ("⏳ Qayta hisoblanmoqda…"), keyin servis chaqiriladi, karta joyida yangilanadi (tugmalar `signal_keyboard`: retro/bekor qilingan signalda 🔄 va ✅ yo'q) va farq xabari yuboriladi. Javob oldindan berilgani uchun xatolar (cooldown, boshlangan o'yin) alert emas, xabar bo'lib keladi — bu uzoq API chaqiruvida "query is too old" xavfidan arzon.
- **Ommaviy** (🔄 Hammasini yangilash, `/qayta`): fon vazifasi (`BackgroundJobs`), progress xabari (`ProgressReporter`, `RECALC_STAGES`), oxirida bitta xulosa (≤ 10 o'zgarish ro'yxati, qolgani "…va yana N ta"; cooldown / boshlangan / xato sabablari alohida satrda). Foydalanuvchi qulfi ushlanmaydi.
- **Tugma ma'lumoti:** `RecalcAllCb(kind, ref)`. `EntityId` ichida `:` bo'lishi mumkin (aiogram separatori), shuning uchun so'rov id si URL-safe base64 (≤ 40 bayt → ≤ 54 belgi, jami ≤ 59 bayt); kunlik xabar tugmasi sana (ISO) olib yuradi va `recalc_window(DateSpec.between(d, d))` ni chaqiradi — `daily:<sana>` id si `recalc_query` da saqlanmagan.
- `/qayta [bugun|ertaga|dam|Nkun|oraliq]` — `/tahlil` parseri qayta ishlatiladi; sport, ehtimollik, jamoa va son rad etiladi.
- Xatolar `ErrorMiddleware` / `user_message_for` da: `BusyError` ("tahlil allaqachon bajarilmoqda"), `ServiceUnavailableError` ("manba javob bermayapti, holat saqlangan"), `BudgetExceededError`, `CooldownActiveError` (yuqoriga yaxlitlangan daqiqa), `NotFoundError`. Fon vazifalari ham xuddi shu xaritadan foydalanadi.

### 5. Stavka (minimal) va idempotentlik
- ✅ Stavka qildim → tasdiq xabari (signal koeffitsienti, tavsiya: `unit × stake_units`, unit sozlanmagan bo'lsa faqat unit) → [✅ Yozish] / [✏️ Boshqa koeff./summa] / [❌]. Tavsiya summasi aniq bo'lmasa (unit yo'q yoki "stavka qilinmaydi") [✅ Yozish] ko'rsatilmaydi.
- **`client_token` callback id dan emas, xabardan olinadi.** Telegram'da ikki marta bosish — ikki xil callback id (qayta yetkazish faqat bir xil id ni takrorlaydi), shuning uchun id bo'yicha dublikat to'xtamaydi. Token = `bet:<signal_id>:<tasdiq xabari id>`: bir xabardagi tugmani necha marta bossa ham bitta stavka (servis birinchisini qaytaradi, xabar "yozildi" ga aylanadi). Yozilgan qiymat uchun token = foydalanuvchi xabarining id si (Telegram qayta ishlatmaydi). Ikki alohida tasdiq xabari (kartani ikki marta bosish) — ikki ongli stavka.
- Qo'lda kiritish `BetFlow.value_input` holatida (`1.90` yoki `1.90 50000`); boshqa oqim (wizard, `running`) ochiq bo'lsa boshlanmaydi (`running` ustiga yozish ▶️ ning "bir vaqtda bitta tahlil" kafolatini buzardi). Noto'g'ri qiymat holatni yopmaydi.
- Bank ko'rinishi shartnoma semantikasi bo'yicha: "Balans = boshlang'ich bank + yopilgan stavkalar foydasi", ochiq stavkalar balansni kamaytirmaydi (matnda aytilgan); zarar limitiga yetilgan bo'lsa tasdiqda ogohlantirish (to'sib qo'yilmaydi — qaror foydalanuvchiniki).

### 6. `/holat`
`SystemStatus` ni ko'rsatadi: rejim, API kvotasi (ishlatilgan / limit, qoldi, qo'lda), yangilanish vaqti + "N soat oldin", oxirgi ishlar, ≤ 5 oxirgi xato (escape + qisqartirilgan). "ℹ️ Holat / Yordam" menyusi holatni ko'rsatadi, buyruqlar va atamalar `/yordam` da.
**Rejim** `build_dispatcher` ga yangi parametr qo'shmasdan workflow data (`dispatcher["backend"]`, standart `fake`) orqali beriladi — `main.run_real` `real` qo'yadi (funksiya parametrlari soni chegarasi).
**Shartnoma so'rovlari** (bot o'zgartirmaydi, pipeline-engineer qaror qiladi):
1. `SystemStatus` da **LLM byudjeti** (bugungi so'rov/token va limit; `LlmBudget.snapshot()` bor) va **ma'lumot yangiligi** (`data_as_of` faqat karta darajasida) yo'q. Taklif: `SystemStatus.llm: LlmQuota | None` (`requests_used`, `requests_limit`, `tokens_used`, `tokens_limit`) — qo'shimcha maydon, standart `None`.
2. `ApiQuota.reserved_manual` ma'nosi ikki amalda farq qiladi: fake — zaxira hajmi (30), real — qo'lda sarflangan. Bot hozir betaraf "qo'lda: N" deb ko'rsatadi; ma'noni aniqlashtirish yoki ikkita maydonga ajratish (`manual_reserve`, `manual_used`) tavsiya etiladi.

## Ko'rib chiqilgan muqobillar
- **Update'ni asl xabarda tahrirlash:** yuqorida (2-bo'lim) — noto'g'ri xabarni buzadi.
- **Kunlik xabar = bitta uzun matn:** tugmalar (🔄 ✅ 🙈) har karta uchun kerak; bitta xabarda yo'q.
- **`recalc_query` kunlik xabar uchun:** `daily:<sana>` id si ledger'da so'rov sifatida saqlanmaydi (`NotFoundError`); `recalc_window` ishonchli va signal ro'yxatini servis o'zi aniqlaydi.
- **Callback id dan token:** ikki marta bosishni to'xtatmaydi (5-bo'lim).

## Oqibatlar
- Bot `Notifier` ni bergach `NotifierDelivery` kunlik xabar, update'lar va natijalarni haqiqatan yuboradi (`tests/e2e/test_notifier_real_pipeline_e2e.py`: haqiqiy pipeline DTO lari → Telegram so'rovlari, HTML va `callback_data` ≤ 64 tekshiriladi).
- `ProgressReporter(stages=...)` endi keyword-only parametr oladi.
- Cheklovlar: `Notifier` bir foydalanuvchi uchun xabarlarni ketma-ket yuboradi (30 kartada ~30 so'rov); persistent FSM storage (ADR 0006) hanuz yo'q — qayta ishga tushgach stavka kiritish qadami yo'qoladi (matn "tushunmadim" javobini oladi, stavka yozilmaydi).
