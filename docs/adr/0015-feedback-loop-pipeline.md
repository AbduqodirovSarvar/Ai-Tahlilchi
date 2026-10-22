# ADR 0015: Teskari aloqa halqasi pipeline'da — shablonlar, zarar limiti, post-mortem, saboqlar, tajriba konteksti

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-22
- **Muallif:** pipeline-engineer (+ llm-engineer doirasi: F8.3, F8.4, F8.7)
- **Kod:** `src/app/pipeline/{templates,limits,limit_notice,digest_store,postmortem,lessons,experience,form}.py`, `src/app/db/repo/feedback.py`, `src/app/llm/prompts/postmortem.v2.md`, `src/app/scheduler.py`
- **Bog'liq:** [ADR 0008](0008-pipeline-and-real-services.md) (ledger, tick, yetkazish), [ADR 0009](0009-llm-integration.md) (LLM qatlami), [ADR 0012](0012-t5-bot-features.md) (F7), [ADR 0013](0013-learning-loop-math.md) (kalibratsiya, segmentlar), qarorlar jurnali #14, #15, #17

## Kontekst
T5 botda shablonlar jadvali saqlanardi, lekin hech kim ishga tushirmasdi; zarar limiti faqat `/bank` da ko'rinardi; `postmortems` va `lessons` jadvallari bo'sh edi; LLM ga `ExperienceContext` va forma berilmasdi. §6.9 va §6.13 (A–C) ni yopish uchun pipeline tomoni kerak. Cheklovlar o'zgarmagan: bitta jarayon, SQLite, LLM ixtiyoriy va byudjetli, hamma xabar idempotent.

## Qarorlar

### 1. Jadvalli shablonlar (F7.4)
`template-tick` (har daqiqa) `saved_queries.schedule_weekdays/schedule_at` ni o'qiydi. **Vaqt foydalanuvchi mintaqasida**: `local_moment(zone, kun, vaqt)` har mahalliy kunga aynan bitta UTC lahzani beradi (mavjud bo'lmagan vaqt — sakrashdan keyingi lahza, takrorlanadigan vaqt — birinchi uchrashuvi; kunlik xabar bilan bir xil qoida). Kun nomzodlari: bugun va kecha (yarim tundan oshib ketgan, hali `daily_catch_up` = 12 soat ichidagi ishga tushirish kechagi kun kaliti bilan yuboriladi). Kalit `(template, mahalliy kun, user, template id)`: **tarmoqdan oldin** egallanadi, shuning uchun tick'lar, restart va parallel ishchilar soni qanday bo'lmasin, bir kunda aynan bir marta. Tahlil — yagona `AnalysisEngine` yo'li (avtomatik kvota, `skip_retro`, oyna o'tmishga/gorizontdan tashqariga qirqiladi); natija kunlik xabar kabi yetkaziladi (`DailyDelivery.label` = shablon nomi), tinch soatda ushlab turiladi, muvaffaqiyatsizlik ledger backoff bilan qayta uriniladi (5 urinishdan keyin adminga). Tahlil natijasi ledger'da saqlanadi (`digest_store`): qayta urinish LLM ni takrorlamaydi. Har shablon alohida `try`: bittasining xatosi boshqalarni to'xtatmaydi.
- `DateSpec.resolve` ni takrorlovchi `resolve_dates` pipeline'da (servis qatlamini import qila olmaydi); farq bo'lmasligi uchun test 14 kun × barcha rejim bo'yicha ikkalasini solishtiradi, gorizont konstantasi ham shartnomaning `horizon_end` i bilan tekshiriladi.

### 2. Zarar limiti (§6.9) — qaror
- **Pul, `Decimal`, bank valyutasida.** Zarar = foydalanuvchi **yopilgan stavkalarining** sof foydasi salbiy bo'lganda moduli, stavka **yopilgan** mahalliy kun (kunlik) yoki mahalliy ISO hafta (dushanba — bugun) bo'yicha. `/bank` (`BankView.loss_today`) bilan **bir xil qoida** (`pipeline.limits` yagona joy; `BankView` shuni ishlatadi). Stavka qilmagan foydalanuvchida zarar 0, ya'ni cheklanmaydi: limit haqiqiy pulni himoya qiladi.
- **Chegara aniq:** `loss >= limit` (to'liq limitga teng — yetildi), `None` — o'chiq. Kunlik va haftalik bo'lsa kunlik birinchi.
- **Ta'sir:** kunlik xabar va jadvalli shablonlar o'sha kun/hafta **yangi signal yubormaydi va tahlil ham qilmaydi** (signal yaratilib yuborilmay qolmasligi, API/LLM sarflanmasligi uchun). Buning o'rniga foydalanuvchi **kuniga bir marta** "Zarar limitiga yetildi" xabarini oladi: `(limit, mahalliy kun, user, notice)` kaliti, birinchi bloklangan yetkazish yuboradi, qolganlari jim yakunlanadi. Xabar `DailyDigest` ning ikki yangi maydoni orqali ketadi (`loss_limit`, signalsiz `result`) — `Notifier` protokoli o'zgarmaydi. Haftalik limit hafta davomida har kuni bitta xabar beradi (kunlik xabar o'rniga).
- Mavjud signallarning 24 soat/pre-match yangilanishlari, natija xabarlari va hisobotlar **to'xtamaydi** (yangi signal emas). Qo'lda/maxsus tahlil **ishlaydi**, natijada `AnalysisResult.loss_limit` (banner uchun); retro oynada banner yo'q.

### 3. `SignalView` ID lari
`home_team_id`, `away_team_id`, `league_id` (`EntityId | None`, standart `None`): katalogdagi (`CatalogService`) id lar bilan bir xil. Manba: `FixtureRecord` ga qo'shilgan `league_id/home_team_id/away_team_id` (repo `to_record`, additive). Fake va real to'ldiradi; shartnoma testi id ning katalog qidiruvi/ligalar ro'yxatida topilishini tekshiradi.

### 4. Post-mortem (F8.3)
Kunlik `feedback` job (08:45, settle 08:00 va refresh 08:30 dan keyin). Tanlov: tavsiya qilingan, retro bo'lmagan, **to'liq mag'lubiyat** (`LOSS`; yarim yutqazish shovqinli, `POSTMORTEM_OUTCOMES` bir joyda), natijasi `postmortem_lookback` (30 kun) ichida, post-mortemi yo'q, **yangisi birinchi** (yangi saboqlar qimmatliroq; LLM bir necha kun o'chiq bo'lsa eski orqada qolgani eskirib ketadi).
- **Bitta tanlov — bitta tushuntirish:** bir xil (o'yin, bozor, chiziq, tanlov) turli foydalanuvchilarda bitta hodisa; bitta LLM chaqiruvi, javob har bir signalga nusxalanadi; keyin yopilgan "egizak" mavjud javobni LLM siz oladi.
- **Byudjet:** bir ishga tushirishda ko'pi bilan `postmortems_per_run` (10) LLM chaqiruvi; ketma-ket 3 ta "javob yo'q" (o'chiq/limit/uzilish) dan keyin ishga tushirish to'xtaydi. LLM yo'q/limit tugagan → hech narsa yozilmaydi, signal keyingi kunda qayta uriniladi; **settle ga hech qachon xalaqit bermaydi** (alohida job).
- **Idempotentlik:** signal uchun bitta qator (`add_postmortem` mavjudini qaytaradi); LLM javobi va yozuv orasida o'lish — bitta takroriy chaqiruv, takroriy qator emas.
- **Kirish (case):** `p_model`, birinchi tavsiya koeffitsienti, EV, ishonch, forma (o'yindan oldingi 5 o'yin), hisob, xG (bo'lsa), asl LLM verdikti va izohi (`PostmortemInput.original_verdict/original_note`, prompt `postmortem.v2`: izoh dalil emas, ma'lumot). **Jarohat/tarkib bazada saqlanmaydi**, shuning uchun `absences` bo'sh (kelajakda saqlansa, ulash joyi tayyor). Javob LLM qatlamida tozalangan; bo'sh `reason` (qo'riq rad etgan) bo'sh saqlanadi va saboqqa kirmaydi.
- Toifa xaritasi: `omadsizlik→bad_luck`, `ma'lumot yetishmadi→missing_info`, `model xatosi→model_error`. Teglar: `team:<id>` (ikkala jamoa), `league:<id>`, `market:<tur>`.

### 5. Saboqlar (F8.4) — hisoblab chiqiladigan jadval
Saboq — post-mortemlarning **sof funksiyasi** (`derive_lessons(facts, now)`): oxirgi 90 kunlik (chegara: aynan 90 kunlik dalil allaqachon tashqarida) faqat qaytariladigan sabablar (`missing_info`, `model_error`; omadsizlik emas) va bo'sh bo'lmagan sababli post-mortemlar. Qamrov: bitta jamoa (`team:<id>`) yoki liga × bozor (`league:<id>`+`market:<tur>`), doim `category:<nom>` bilan. **Tasdiqlar = turli o'yinlar soni** (bir o'yinni 3 foydalanuvchi yutqazsa ham 1; mustaqillik). Holat: ≥ 2 → `active`; 1 → `pending`; dalil eskirgan yoki faol bo'lgandan keyin 2 dan pasaygan → `expired` (yangi dalil bilan qayta tiriladi). Shaxsiyat — teglar to'plamining aniq mosligi. `run_lessons` — to'liq qayta qurish: idempotent, ikki marta ishga tushsa o'zgarmaydi, qulashga chidamli (bitta tranzaksiya). Kunlik `feedback` yangilik bo'lsa (tushuntirilgan/nusxalangan) qayta quradi, haftalik `lessons` job (dushanba 09:30) eskirishni tozalaydi — `learning.py` ga tegmaydi.
- **Matn:** shablon (o'zbekcha) + son + qamrov nomi + eng so'nggi tozalangan `reason` (sig'sa, 200 belgigacha, so'z chegarasida "…"). Boshqa hech qanday model matni saboqqa kirmaydi.

### 6. Tajriba konteksti (F8.7)
`DbExperience.build(candidates)` — **har sharh paketi uchun**, faqat shu nomzodlarga tegishli, faqat o'qish:
- `segment_stats` (≤ 4 qator): nomzod ligasi × bozorining `segment_stats` qatorlari (koeffitsient oraliqlari bo'yicha n bilan o'rtachalangan ROI va kalibratsiya og'ishi; **n < 50 bo'lsa umuman yo'q**) va jamoalarning oxirgi 90 kundagi yagona-tanlov hisobi (≥ 3 signaldan, ✅/❌);
- `lessons` (≤ 5): faqat `active`, qamrovi nomzod teglari ichida to'liq bo'lganlari, kuchlisi birinchi;
- `user_notes` (≤ 3): shu foydalanuvchining shu jamoalar signallari bo'yicha eng yangi izohlari (90 kun), `dd.mm: matn`.
Har element qat'iy uzunlik bilan chegaralangan, token byudjetini `build_review_prompt` ichidagi deterministik qisqartirish saqlaydi. Xato bo'lsa faqat blok tushib qoladi (sharh davom etadi). `ReviewCandidate` ga `user_id`, `league_id`, jamoa id lari, `forms` (oxirgi 5 o'yin, faqat o'yindan oldingi — look-ahead yo'q), `absences` qo'shildi (additive).

### 7. F8.2 "Kecha yakuni"
Mavjud ekan (`NotifierDelivery.daily` → `day_summary(kecha)` → `DailyDigest.yesterday`): foydalanuvchi mahalliy kuni (o'yin boshlanishi bo'yicha), tavsiya qilingan retro bo'lmagan signallar, yashirilganlar ham, boshqa foydalanuvchi va retro hisobga olinmaydi; signallar/✅/❌/void/kutilmoqda/unit/o'rtacha CLV. Bo'shliqlar: yo'q. Shablon xabarida bu blok yo'q (faqat ertalabki xabarda). Chegaraviy test qo'shildi (mahalliy yarim tun ikki tomonida, retro, begona).

## Nosozlik rejimlari
| Holat | Natija |
|---|---|
| Shablon yetkazish xatosi / `None` | kalit `failed`, backoff, tahlil saqlangani uchun LLM takrorlanmaydi; 5-urinishdan keyin admin ogohlantirishi |
| Jarayon yuborishdan keyin o'ldi | kalit lease (15 daq) tugaguncha qayta berilmaydi, keyin bir xil `DeliveryKey` bilan qayta yuboriladi (kamida bir marta) |
| DST sakrashi / takrorlanishi | mahalliy kunda bitta lahza, bitta kalit |
| Zarar limiti yetkazish xatosi | chaqiruvchi kalit (kunlik/shablon) backoff bilan qayta urinadi; xabar kuniga bir marta |
| LLM yo'q / limit tugagan | post-mortem yozilmaydi, keyingi kun qayta; lesson qayta qurish o'zgarmaydi; sharh tajriba blokisiz |
| Tajriba bloki o'qilmadi | blok yo'q, sharh davom etadi |
| Ma'lumot buzilgan (hisob ≥ 100, uzun nom) | `invalid` hisoblanadi, LLM chaqirilmaydi, boshqalar davom etadi |

## Ko'rib chiqilgan muqobillar
- **Zarar limitini signal birliklarida (unit) hisoblash:** foydalanuvchi limitni pulda kiritadi (`daily_loss_limit` — `Money`) va `/bank` pulda ko'rsatadi; ikki xil birlik chalkashlik tug'diradi.
- **Limitda tahlilni bajarib, yetkazmaslik:** signallar `recommended` bo'lib hisobot/kalibratsiyaga kirar, lekin foydalanuvchi ko'rmagan bo'lardi; API va LLM ham behuda sarflanardi.
- **Limit xabari uchun yangi `Notifier` metodi:** protokolni buzadi (bot `TelegramNotifier`, fake, log-only hammasi o'zgarishi kerak); `DailyDigest` maydoni additive.
- **Saboqlarni `confirmations += 1` bilan oshirib borish:** qayta ishga tushirish/ikki marta chaqirishda ikki marta sanaladi, eskirish uchun alohida tarix kerak; post-mortemlardan qayta qurish idempotent va soddaroq.
- **Saboq matnini LLM ga yozdirish:** erkin model matni keyingi promptlarga o'tadi (injection, drift); shablon + tozalangan sabab xavfsizroq.

## Oqibatlar va cheklovlar
- **Bot (keyingi vazifa):** `DailyDigest.template_name` (sarlavha), `DailyDigest.loss_limit` ("Zarar limitiga yetildi" xabari) va `AnalysisResult.loss_limit` (banner) hali ko'rsatilmaydi — `TelegramNotifier` ularni e'tiborsiz qoldiradi, ya'ni shablon xabari nomsiz, limit xabari bo'sh "0 signal" kabi ko'rinadi, to'g'ri matn botda qo'shilmaguncha; kartadagi ⭐ endi `SignalView.*_id` dan foydalanishi mumkin. F8.1/F8.2/F8.8 UI ham botda.
- Jarohat va tarkib bazada yo'q → `CandidateInput.absences` bo'sh; faqat forma to'ldiriladi.
- Segment/jamoa statistikasi `segment_stats` (haftalik) va hisoblangan natijalarga bog'liq: ma'lumot kam bo'lsa blok bo'sh (to'g'ri xatti-harakat).
- Jadvalli shablon kuniga bir tick oynasi emas, `daily_catch_up` (12 soat) ichida yetkaziladi; undan kech qolgani o'tkazib yuboriladi.
- `lessons` va `postmortems` jadvallari uchun Alembic migratsiyasi kerak emas (sxema mavjud edi).

## Boshqa agentlarga so'rovlar
- **bot-engineer:** `texts.py` ga "Zarar limitiga yetildi" va shablon sarlavhasi; `TelegramNotifier.send_daily_digest` da `digest.loss_limit`/`template_name`; analiz natijasida `result.loss_limit` bannerini; kartadagi ⭐ ni `SignalView.home_team_id/away_team_id/league_id` ga o'tkazish; F8.1/F8.2/F8.8 UI.
- **quant-modeler:** `pipeline/learning.py` ga tegilmagan; tajriba bloki `list_segment_rows` ni faqat o'qiydi (`MIN_SEGMENT_SAMPLE` ni import qiladi).
