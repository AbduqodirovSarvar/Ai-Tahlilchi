# ADR 0004: Servis shartnomasi (bot ⇄ tizim) va soxta servis

- **Holat:** qabul qilingan (frozen v1 orkestr tomonidan tasdiqlangan, 2026-10-21; `docs/decisions_log.md` #8, #9). v1.1 qo'shimchalari (T3) — pastdagi 10-bo'lim (orkestr tasdig'i kutilmoqda).
- **Sana:** 2026-10-21
- **Muallif:** pipeline-engineer (T2, T3)

## Kontekst
Bot (T3) pipeline (T4) tayyor bo'lishidan oldin qurilishi kerak. Bot faqat `app.services` ni import qila oladi (`import-linter`), servislar esa `db`/`models`/`sports`/`llm` ni bevosita emas, faqat `pipeline` orqali ishlatadi. Shuning uchun bot ehtiyojlaridan (asosiy reja §6.2–6.13) kelib chiqqan barqaror shartnoma va uning to'liq soxta amalga oshirilishi kerak.

## Qarorlar

### 1. Bitta "god-interface" emas, mas'uliyat bo'yicha 10 ta kichik `Protocol`
`AnalysisService`, `SignalService`, `CatalogService`, `SettingsService`, `TemplateService`, `WatchlistService`, `BetService`, `ResultService`, `ReportService`, `SystemStatusService` va ularni bog'laydigan `Services` (frozen dataclass). Sabab: bot handlerlari faqat keraklisini oladi, testda bitta protokolni almashtirish oson, real amalga oshirish bosqichma-bosqich (F4, F7, F8) almashtiriladi. `Protocol` (strukturaviy tip) — fake va real servis meros olmaydi, `mypy` mosligini tekshiradi.

### 2. DTO: `frozen` dataclass, validatsiya konstruktorda
Domen bilan bir xil uslub (ADR 0002). Noto'g'ri kiritma `ValidationFailedError(field, message)` beradi: bot aynan qaysi qadamni qayta so'rashni biladi. Chegaralar nomlangan konstantalar (`MIN_PROBABILITY_LOWER` ...) — bot ularni tugmalar va xabarlar uchun ishlatadi, raqamlar takrorlanmaydi. Pydantic tanlanmadi: kiritma bot FSM idan keladi (JSON emas), dataclass yengilroq va domen bilan bir xil.

### 3. Sanalar: nisbiy `DateSpec` + foydalanuvchi mintaqasidagi kalendar sanalari
"Ertaga", "Dam olish kunlari", "7 kun" shablonlarda saqlanadi va har ishga tushganda qayta hisoblanadi, shuning uchun `AnalysisQuery.dates` — `DateSpec`, aniq `DateWindow` emas. "Bugun" ni servis o'z soati va foydalanuvchi vaqt mintaqasi bo'yicha hal qiladi (bot va servis soati farq qilmasligi uchun). 14 kunlik horizon tekshiruvi ham shu paytda (`ensure_within_horizon`) — chunki u "bugun"ga bog'liq. O'tgan oyna (retro) ruxsat, oyna uzunligi ≤ 31 kun (taxmin: retro tahlilni cheklash uchun; reja raqam bermagan).

### 4. Sozlamalar: bitta DTO, to'liq almashtirish
`UserSettingsView` ham o'qish, ham yangilash DTO si: bot `dataclasses.replace(current, min_ev=0.07)` qiladi, servis to'liq saqlaydi. "O'zgartirilmagan" sentinel li patch-DTO dan soddaroq (nullable maydonlarda `None` = "o'chiq" ma'nosi bor). Bitta foydalanuvchi uchun "lost update" xavfi amalda yo'q. `risk_profile` ni servis chegaralardan qayta chiqaradi: preset bilan aynan mos bo'lsa shu profil, aks holda `custom`.

### 5. Xatolar — strukturali ma'lumot bilan
`ServiceError(AppError)` → `ValidationFailedError`, `NotFoundError`, `CooldownActiveError(retry_after)`, `BudgetExceededError(needed, available)`. Nomlar vazifadagi (`ValidationFailed` ...) o'zagini saqlaydi, lekin loyiha lint qoidasi (ruff N818, CI) talabiga ko'ra `Error` qo'shimchasi bilan. Boshqa foydalanuvchining obyekti `NotFoundError` (mavjudligini oshkor qilmaydi).

### 6. Byudjet va qayta hisob semantikasi servisda
Cooldown (10 daq./o'yin), qo'lda so'rovlar zaxirasi (30%) va kesh servis qatlamida (pipeline-engineer qoidasi). `analyze` zaxiradan oshsa xato beradi (bot qisqartirishni taklif qiladi); qayta hisobda esa zaxira tugasa xato emas, keshdagi raqamlar `data_as_of` bilan qaytadi (reja §6.4: "09:00 holatiga").

### 7. Ichki ID lar shaffof emas
`SignalId`/`BetId`/`TemplateId` — `int`, qolganlari `EntityId = str` (≤ 64 bayt, `callback_data` ga sig'adi). Bot domen `ExternalRef` yoki baza ID siga bog'lanmaydi.

### 8. Soxta servis: deterministik, tasodifsiz
Raqamlar `blake2b(key)` dan olinadi (`random` yo'q, ruff S311 yo'q), shuning uchun bir xil soat → bir xil dunyo; bot snapshot-testlari barqaror. Soat injeksiya qilinadi (`Clock`), real `sleep` yo'q. Fake modellarni import qilmaydi (`import-linter`): EV va hisob-kitob ishonarli, lekin sintetik (ADR 0003 dagi formulalar emas). Ichki modullar: `_fake_world.py` (ma'lumot), `_fake_metrics.py` (hisobot arifmetikasi), `fake.py` (servislar).

### 9. Qayta ishlatiladigan shartnoma testlari
`tests/services/contract_suite.py` dagi `*Contract` klasslari ma'lumotdan mustaqil (liga/jamoa/o'yinlarni katalog orqali topadi) va faqat `services`, `clock`, `user_id` fixture larini talab qiladi. F4 da real servis shu to'plamni seed qilingan baza bilan ishga tushiradi: bot fake da ko'rgan xatti-harakat real servisda ham kafolatlanadi.

### 10. v1.1: hisobot halolligi va tavsiya identifikatsiyasi (T3, kod sharhi natijasida)
Kontekst: sharh hisobotlarni buzishi mumkin bo'lgan yo'llarni topdi — yashirilgan mag'lubiyatlar yield'ni oshirardi, retro tahlil natijalari haqiqiy signal sifatida saqlanardi, keyingi qattiq maxsus tahlil tavsiyani `no_value` ga aylantirib hisobotdan chiqarardi, `scope=all` sozlamadagi ligalarga amal qilmasdi.
- **Hisobot to'plami bitta va qat'iy:** "tavsiya qilingan va retro bo'lmagan" signallar (yashirilganlari ham). Yashirish — faqat ko'rinish. Bu reja §6.13 C dagi "barcha signallar hisobga olinadi, tanlov xatosi bo'lmasin" qoidasining bajarilishi.
- **Retro — alohida manba (`SignalSource.RETRO`), saqlanadi, lekin hech qachon tavsiya emas.** Variantlar: (a) umuman saqlamaslik — karta tugmalari (batafsil) uchun ID kerak bo'lgani uchun rad etildi; (b) belgili saqlash — tanlandi, retro kaliti haqiqiy signal kalitidan alohida.
- **Tavsiya "yopishqoq" (`recommended_at`):** birinchi marta chegaradan o'tgan vaqt saqlanadi va o'zgarmaydi; `no_value` signal keyinroq o'tsa — o'sha keyingi vaqt bilan (orqaga sanab "tavsiya qilingan" bo'lib qolmaydi). Signal o'sha paytdagi chegaralarni saqlaydi va recalc verdikti shular bilan hisoblanadi (keyin sozlama o'zgarsa, eski signal "yomonlashdi" deb ko'rinmaydi).
- **Sevimlilar kesimdan tashqarida:** §6.7 "har doim kiritiladi" — `max_signals` va `matched` ga ta'sir qilmaydi.
- **Horizon:** "14 kun" = bugun bilan 14 kalendar kuni (`today+13`); `next_days(14)` va `between()` bir xil chegarada. Avvalgi `today+14` bilan `next_days` maksimumi mos kelmas edi.
- **Qo'shimcha DTO/metodlar faqat qo'shish orqali:** `date_to` ni ixtiyoriy qilish (standart `None`) mavjud chaqiruvlarni buzmaydi; protokolga `list_sports` qo'shildi (real servis ham uni amalga oshiradi).
- **Fake hech qachon productionga kirmaydi:** alohida `import-linter` qoidasi.

## Oqibatlar
- Bot T3 da to'liq ishlay oladi; real servis F4 da shu protokollarni amalga oshiradi va `contract_suite` dan o'tishi shart.
- Shartnomaga keyin faqat qo'shish mumkin; yangi imkoniyat (masalan, ekspress §6.11, `/ozgarishlar` §6.13 C) yangi protokol yoki metod sifatida qo'shiladi.
- `daily_loss_limit`/`weekly_loss_limit` (reja §6.9) baza sxemasida yo'q: data-engineer ga qo'shish so'rovi.
- Pipeline → bot yo'nalishidagi xabarlar (kunlik xabar, pre-match, sevimlilar bildirishnomalari) bu shartnomada yo'q: alohida "Notifier" shartnomasi F4 da kerak bo'ladi.
