# ADR 0016: T5 bot fix round (QA F-1..F-10) va F8 bot qismi (natijalar, kecha yakuni, `/ozgarishlar`), shartnoma v1.8

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-23
- **Muallif:** bot-engineer
- **Kod:** `src/app/bot/{paging,recovery,result_buttons,result_ui,learning_ui}.py`, `src/app/bot/handlers/{results,learning}.py`, `src/app/services/{_fake_learning,report_math,express_math,interfaces}.py`, `src/app/services/real/learning.py`, `src/app/pipeline/{settle,learning_digest,data}.py`, `src/app/scheduler.py`
- **Bog'liq:** [ADR 0012](0012-t5-bot-features.md), [ADR 0013](0013-learning-loop-math.md), [ADR 0015](0015-feedback-loop-pipeline.md), qarorlar jurnali #18, QA hisoboti [docs/qa/T5-bot.md](../qa/T5-bot.md), shartnoma v1.8 (`docs/contracts.md`)

## Kontekst
T5 QA bot qismida 4 ta 🟠 va 6 ta 🟡 topilma (F-1..F-10) qayd etdi; hammasi qat'iy `xfail` bilan qotirilgan. Shu vaqtda F8 ning bot qismi (F8.1 natijalarni belgilash, F8.2 digest ko'rinishi, F8.8 `/ozgarishlar`) qolgan edi. Pipeline tomoni ([ADR 0015](0015-feedback-loop-pipeline.md), [ADR 0013](0013-learning-loop-math.md)) tayyor.

## Qarorlar

### 1. Cashout = bukmeker to'lagan pul summasi (qaror #18, F-2)
Foydalanuvchi **summani** yozadi; stavka foydasi aniq `payout - stake` (`Decimal`). Eski "unit" yondashuvi bir signalda ikki xil summali stavka bo'lganda bitta raqam ikkalasi uchun ham to'g'ri bo'la olmasligini ko'rsatdi.
- Shartnoma (additiv): `ResultMarkRequest.cashout_bet_id` + `cashout_payout`. Signal darajasidagi `cashout_profit_units` saqlandi (F8.1 tugmasi shu bilan ishlaydi: "tavsiya etilgan stavkaga nisbatan unit").
- Pipeline (minimal): `settle.BetPayout(bet_id, stake, amount)`; `settle_manually(..., payout=)` signal natijasiga `stake_units * (payout - stake) / stake` ni yozadi, `_store` esa **shu stavkaga** `payout - stake` ni aniq qo'yadi (`Decimal` bo'linish xatosi yo'q: 300 stavka, 100 to'lov = aniq −200). Signaldagi boshqa stavkalar o'sha daromad foizi bilan yopiladi (eski `_bet_profit` yo'li o'zgarmadi).
- Bot: matn "bukmeker to'lagan summa" (stavka ko'rsatiladi), `parse_money(allow_zero=True)`: 0 — to'liq yo'qotish; manfiy/son emas/10¹² dan katta — tushunarli xato, holat ochiq qoladi.
- Rad etilgan variant: faqat prompt matnini "unit"ga aniqlashtirish — pul ko'rsatkichi baribir noto'g'ri bo'lib qolardi.

### 2. CSV in'eksiyasi (F-3)
`report_math.safe_cell`: matn ustunlarida (`league`, `home`, `away`, bet `fixture`) `= + - @ \t \r` bilan boshlansa oldiga `'`. Raqam va sana ustunlariga tegilmaydi (`-50` raqam bo'lib qoladi). Fayl `utf-8-sig` (BOM) bilan — Excel kirill/o'zbek apostrofini to'g'ri o'qiydi. Natija: mavjud testlar `utf-8-sig` bilan o'qiydi.

### 3. Ekspress EV (F-4)
`ev = Π(1 + evᵢ) − 1`: mustaqil oyoqlar uchun aniq; ikkilik oyoqda `P·O − 1` bilan bir xil. Choraklik Asian oyoqda "ehtimollik" = yutish ehtimoli (yarim yutuq yutuq deb sanaladi), shuning uchun `P·O − 1` EV emas — bot EV ni oyoqning **o'z** EV idan oladi. Ko'rsatish: ehtimollik ko'paytmasi hamon ko'rsatiladi (hujjatlangan), EV va adolatli koeffitsient (`Π fairᵢ`) esa EV dan. QA misoli: −10% va 0% oyoq → −10%. Bog'liq oyoqlar (bir o'yindan ikki tanlov) hamon rad etiladi.

### 4. Ro'yxatlarni sahifalash (F-7) va tugma kaliti (F-9)
`bot/paging.py`: `paginate` (sahifa chegaraga kesiladi — o'chirilgan elementdan keyingi eski sahifa tugmasi oxirgi sahifani ko'rsatadi) va `pager_row` (`◀️ n/m ▶️`). 10 tadan; sevimlilar va shablonlar ro'yxatlarida "…va yana N ta" yozuvi olib tashlandi: har bir element tugmaga ega. Element ekranidan qaytish ro'yxatning o'sha sahifasiga (`WatchCb.page`, `TplCb.value`).
Sevimlilar kaliti `crc32` (8 hex, qo'lda to'qnashuv yasash mumkin) o'rniga `blake2b` 48 bit (12 hex); hamon bir nechta yozuvga mos kelsa — "noma'lum" (birinchisini tanlamaydi).

### 5. Bank (F-6)
Jami hisob (yopilgan soni, ✅/❌/↩️/💸, foyda, yield) servisdan: `BankView.totals` (`report_math.bank_totals`, fake va real uchun bitta). Botdagi `summarize` va `BANK_RECENT` olib tashlandi ("takrorlash yo'q"). Ochiq stavkani topish — `BetService.get_bet` (100 ta chegarasiz). Ochiq stavkalar tugmasi soni `bank.open_bets`. `ValidationFailedError` xabari endi haqiqiy sababni aytadi (boshlanmagan / retro / bekor qilingan). **Qoldiq:** ochiq stavkalar ekranida tugmalar hamon 10 ta (`MAX_BET_BUTTONS`), qolganlariga sahifalash yo'q — riskda eslatilgan.

### 6. FSM (F-1, F-5)
- F-5: `states.leave_flow` — yon oqimlar (sevimlilar, sozlamalar, shablonlar, hisobot, ekspress) holatni `None` ga tushirganda ishlayotgan tahlil himoyasini (`AnalysisFlow:running`, `CustomFlow:running`) o'chirmaydi. `/start` va `/bekor` hamon hammasini tozalaydi (chiqish yo'li). **Qoldiq:** "➕ jamoa qo'shish", cashout/izoh so'rovi kabi o'z holatini o'rnatadigan qadamlar ishlayotgan tahlil paytida: izoh/cashout himoyani saqlaydi (alert), sevimli qo'shish holatni almashtiradi.
- F-1: doimiy FSM saqlagich (`SqliteStorage`) ochilganda `*:running` holatlarini tozalaydi (fon vazifasi jarayon bilan o'ladi) va chatlar ro'yxatini eslab qoladi; `recovery.recover_running_analyses` (`main.poll`, polling oldidan) ularga "bot qayta ishga tushdi, tahlilni qaytadan boshlang" yuboradi. Bloklangan foydalanuvchi qolganlarni to'xtatmaydi. "Allaqachon bajarilmoqda" matnlariga `/bekor` qo'shildi.
- `edit_html` uzun matnni kesmaydi: birinchi qism tahrir qilinadi, qolgani yangi xabar, klaviatura oxirgisida. `$…$` liga nomlari grafikda `\$` bilan escape (F-8); `money()` kvantlangan nolni "0" deb yozadi (F-10).

### 7. F8.1 natijalarni belgilash
- Karta: `signal_keyboard(signal, now)` — o'yin boshlangan (yoki natija bor: tuzatish) tavsiya qilingan, retro/bekor bo'lmagan kartada `[✅ O'tdi] [❌ O'tmadi] [↩️ Void] [💸 Cashout]` va `[💬 Izoh]`. Settled xabarida (`send_result_settled`) ham shu tugmalar. Qo'lda belgi API natijasidan ustun (pipeline `settle_signal` qo'lda belgini hech qachon bosmaydi — testlangan).
- `/natijalar`: `pending_results`, sahifada 6 signal, har biriga bir bosishli belgilar; belgilangach ro'yxat shu sahifada yangilanadi.
- `ResultCb` (prefiks `rs`, ≤ 64 bayt, hypothesis bilan tekshirilgan). Cashout/izoh — `ResultFlow` holati + FSM ma'lumoti; eskirgan so'rov `StaleDraftError`. Boshqa foydalanuvchining signali `NotFoundError` (soxta callback hech narsani o'zgartirmaydi va ko'rsatmaydi). Izoh oddiy matn, `MAX_NOTE_LENGTH` gacha, ko'rsatishda `html.escape`.
- Signal darajasidagi cashout: tavsiya etilgan stavkaga nisbatan **unit**; pul bo'yicha cashout — 🎟 Stavkalarim orqali (matn buni aytadi).

### 8. F8.2 digest ko'rinishi
`format_digest_title`: shablon digesti shablon nomi bilan (kecha bloki yo'q); "Kecha: 6 ta signal · 4 ✅ · 2 ❌ · +2.3 unit · CLV +3.1%" bloki oldindan mavjud edi — testlar bilan qotirildi. `DailyDigest.loss_limit` bo'lsa bitta "Zarar limitiga yetildi…" xabari (bo'sh "0 signal" emas). Qo'lda tahlil natijasida `AnalysisResult.loss_limit` banneri (retro oynada yo'q). Kartadagi ⭐ endi `home_team_id/away_team_id/league_id` dan (katalog qidiruvi yo'q; id yo'q karta kamroq tanlov beradi).

### 9. F8.8 `/ozgarishlar`, haftalik xulosa, `/holat`
- **Servis (additiv):** `LearningService` (`list_changes`, `rollback_change`) va `Services.learning`. Real amalga oshirish pipeline'ning `learning_changes` jadvali va `rollback_change` ini ishlatadi (STALE himoyasi, qo'lda ushlab turish). Jurnal tizim bo'yicha umumiy (model bitta), shuning uchun har bir whitelist foydalanuvchisi ko'radi va qaytara oladi — bot hozir faqat adminlar uchun (`ADMIN_IDS`), alohida rol ajratilmadi.
- **Ko'rinish:** yangi → eski, sahifalangan. `applied=False` — "👁 kuzatuv rejimi: shunday qilgan bo'lardi", qo'llangan — "✅ qo'llangan", qaytarilgan — "↩️". Qaytarish tugmasi faqat haqiqiy, hali turgan, qaytariladigan turdagi yozuvga; **tasdiq so'raladi** ("Qaytarilgan o'zgarishni model 30 kun qayta o'zgartirmaydi"); `STALE` — aniq tushuntirish (yangi holatni ustiga yozmaydi); takroriy bosish — "allaqachon qaytarilgan".
- **Haftalik xulosa:** `Notifier.send_learning_summary` (additiv) + pipeline `learning_digest.announce_learning`. Scheduler'dagi `learning` ishi (`PipelineJobs._learning`) haftalik ko'rib chiqishdan keyin `record_run("learning")` yozadi va xulosani chaqiradi — `models/learning` ichki qismlariga tegilmadi. Xabar **jurnaldan** quriladi (review natijasidan emas), shuning uchun review tugab xabar yuborilmay jarayon o'lsa, keyingi chaqiruv kalit bo'yicha (`learning_summary`, hafta, foydalanuvchi) yetishmaganlarga yuboradi; `off` rejimida yuborilmaydi, tinch soatdagi foydalanuvchi o'tkazib yuboriladi (kalit sarflanmaydi), bitta foydalanuvchi xatosi boshqalarni to'xtatmaydi. Xabarda joriy rejim ko'rsatiladi. Rad etilgan variant: `Delivery` protokoliga majburiy metod qo'shish (barcha test/NullDelivery dublyorlarini sindirardi) — o'rniga ixtiyoriy `SummaryDelivery` porti (`UndeliverableAware` naqshi).
- **`/holat`:** "🧠 Model o'rganishi" bloki — rejim, `learning`/`lessons`/`postmortem` oxirgi ishlari va "hafta yo'qolgan" ogohlantirishi (`SystemStatus.learning_missed`: rejim `off` emas va oxirgi yozilgan run 8 kundan eski). Hali hech ishlamagan bo'lsa ogohlantirilmaydi ("hali ishlamagan").

## Oqibatlar
- Shartnoma v1.8 — faqat qo'shish; `Services.learning` majburiy maydon (ikkala yig'uvchi yangilandi).
- CSV fayllar BOM bilan: o'qiydigan kod `utf-8-sig` ishlatishi kerak (testlar yangilandi).
- Eski `BANK_CASHOUT_PROMPT` ("unit") o'zgardi; signal darajasidagi cashout alohida matn.
- Qoldiq xavflar: ochiq stavkalar tugmalari 10 ta; haftalik xulosada "yangi saboqlar" yo'q (saboqlar alohida ish, dushanba 09:30); xabar yetkazilmasa keyingi urinish keyingi `learning` ishigacha (haftada bir) kutadi.
