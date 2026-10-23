# ADR 0018: T6 tuzatish raundi A (pul, o'rganish rejimi, xotira va jadval o'sishi)

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-23
- **Muallif:** pipeline-engineer (data-engineer doirasi: `db`, `clients`, `sports`)
- **Bog'liq:** qarorlar jurnali #17, #19–#23; [ADR 0005](0005-backtest-methodology.md) (aylanma ta'rifi), [ADR 0013](0013-learning-loop-math.md), [ADR 0007](0007-api-client-and-repositories.md) (klientlar va kesh)

## Kontekst
T6 sharhi (xavfsizlik+pul, arxitektura+ekspluatatsiya) ishga tushirilgan paper trading oldidan tuzatish kerak bo'lgan topilmalarni berdi. Orkestr #19–#23 qarorlarini qabul qildi; bu ADR ularning amalga oshirilishini va orkestr qaror qilmagan texnik tanlovlarni yozadi.

## Qarorlar

### 1. Ochiq stavkasi bor signal har doim yopiladi (#19)
`unsettled_signals` endi `recommended_at IS NOT NULL` **yoki** ochiq (`outcome IS NULL`) foydalanuvchi stavkasi bor signallarni qaytaradi. Shunday signal (maxsus tahlildagi qiymatsiz pik) boshqalardek yopiladi, stavka foydasi bankka tushadi. U **tavsiya** emas: hisobot (`report_signals`) va o'rganish uni baribir hisobga olmaydi, ya'ni "tavsiya ROI" tanlov siljishisiz qoladi, foydalanuvchining shaxsiy P&L i esa (`bank_summary`, stavkalar ro'yxati) to'liq.

### 2. Cashout bitta stavkaga tegishli (#22)
Summa (`payout`) berilganda faqat o'sha stavka yopiladi (`profit = payout - stake`), boshqa stavkalar ochiq qoladi va API natijasi bilan o'zining koeffitsienti bo'yicha yopiladi. Signal natijasi (`CASHOUT`, tavsiya qilingan stavkadagi shu daromad ulushi) faqat signalda hali natija yo'q **va** boshqa ochiq stavka qolmagan bo'lsa yoziladi; aks holda signal o'zgarmaydi. Qo'lda kiritilgan aniq pul keyingi qo'lda belgilash/avtomatik yopishda qayta masshtablanmaydi (`CASHOUT` stavkasi `_store` da o'tkazib yuboriladi).

Chegaralar (`MAX_MONEY = 10^12`, shartnoma v1.9): to'lov `[0, stake × odds_taken]` ichida; signal darajasidagi cashout `[−stake_units, stake_units × (odds − 1)]` ichida. Buzilsa `ManualResultError` (pipeline) → `ValidationFailedError` (servis). Bot ikkalasini oldindan tekshiradi va tushunarli xabar beradi.

### 3. Bitta xato qator qolganini to'xtatmaydi
`run_settlement` har signalni `try/except` ichida yopadi: xato ledger (`record_error`) va admin ogohlantirishiga (throttled) yoziladi, ish `ok=False` deb belgilanadi, keyingi ishga tushirishda qayta uriniladi.

### 4. Aylanma ta'rifi: void stavka xavf ostida emas (#21)
Hisobot ROI, bank (`bank_totals`) va backtest (`Bet.flat_at_risk`/`kelly_at_risk`; push → 0) bir xil: `ROI = foyda / xavf ostidagi stavkalar`. ADR 0005 dagi avvalgi ta'rif (void aylanmaga kiradi) almashtirildi; sintetik hisobotlar qayta yaratildi (ROI +7.48% → +8.22%, chunki push stavkalar maxrajdan chiqdi).

### 5. `LEARNING_MODE=shadow/off` da LLM o'rganilgan kontekst olmaydi (#20)
`build_reviewer`: `DbExperience` (saboqlar, segment statistikasi, foydalanuvchi izohlari) faqat `apply` da ulanadi. Shadow'da post-mortem/saboq **to'planadi**, lekin tavsiyaga ta'sir qilmaydi. Foydalanuvchi izohlari ham shu blokda ketgani uchun `shadow/off` da Geminiga tushmaydi; bu qasddan: paper trading davrida shadow haqiqatan "toza" bo'lishi shart.

### 6. `shadow → apply` o'tishi jurnalga yoziladi (#23)
Shadow'da saqlangan segment/kalibratsiya holati DB da jismonan faol, `apply` yoqilgach `DbLearningSource` uni darhol o'qiydi. `promote_shadow_state` shu holatni `applied=1` yozuvlari sifatida jurnalga qo'shadi (`reason = shadow_promoted`, `before` = neytral holat), shuning uchun `/ozgarishlar` ro'yxati va qaytarish ishlaydi. Qachon: mode `apply` va oxirgi ishga tushirish `shadow` bo'lgan (yoki `apply` hech ishlamagan); `learning.mode` ledger tamg'alari (`shadow`/`apply`) bilan aniqlanadi. Ishga tushirish: `open_runtime` (jarayon boshida) va haftalik job (claim'dan oldin). Qayta chaqirish bo'sh. Yozuv va tamg'a alohida tranzaksiya: orada jarayon o'lsa yozuvlar takrorlanadi (har biri alohida qaytariladigan), yo'qolmaydi.

### 7. Kelly joriy balansdan; stavka asosi o'zgarmaydi
Kelly `UserProfile.balance` (boshlang'ich bank + yopilgan stavkalar foydasi) dan hisoblanadi. Stavka qo'yilgan signalning `stake_units` ni qayta hisoblash (analiz yoki recalc) o'zgartirmaydi (`keep_backed_stake`): signal foydasi va foydalanuvchi stavkasi shu asosga qurilgan. `get_bet` endi to'g'ridan-to'g'ri qidiruv (N+1 yo'q).

### 8. Maxfiy ma'lumot tozalash
`pipeline/redaction.scrub_text`: sozlangan qiymatlar (`register_secrets`, `main` boshida chaqirsa) + tanilgan shakllar (Telegram token, `AIza…`, `Bearer`, `key=/token=…`, sarlavhalar). Qo'llanadi: `Ledger.record_error` va `fail(detail)` (hamma xato yo'llari, shu jumladan `scheduler`), admin ogohlantirishlari (`alert_throttled`), `/holat` o'qilganda (eski yozuvlar uchun). `Heartbeat.ping` endi erkin matn yubormaydi: faqat birinchi ish-nomi tokeni (`settle`), ichki xato matni uchinchi tomonga chiqmaydi. Ledger'ning boshqa `detail` lari (saqlangan digest) o'zgarmaydi.

### 9. HTTP kesh chegaralangan va qisqartirilgan tana saqlaydi
Sabab: `fixtures?date=` butun dunyo ligalari (megabaytlar) 10 daqiqa, 2048 yozuvgacha saqlanardi, muddati o'tgan yozuv faqat qayta so'ralganda o'chardi. Endi: `set` da muddati o'tganlar tozalanadi; ≤ 128 yozuv va ≤ 32 MiB (taxmin: JSON uzunligi × 4, Python obyektlari qiymati); budjetdan katta qiymat saqlanmaydi. `PayloadShape` tana keshga tushishidan **oldin** qisqartiradi (kalitga variant qo'shiladi, torroq tana kengroq so'rovga berilmaydi): kunlik o'yinlar kuzatiladigan ligalarga, koeffitsientlar kerakli bukmekerlarga. Muqobil (rad etildi): hamma endpointni keshlamaslik, chunki §9 "bir xil ma'lumot 10 daqiqada ikki marta so'ralmaydi" talabi va testi bor.

### 10. `odds_snapshots` o'sishi
Qabul qilinadigan bukmekerlar: `pinnacle` + `bet_bookmaker` (1xBet) + sozlanadigan qo'shimchalar (`PipelineConfig.extra_bookmakers`, standart bo'sh). Bu 15+ bukmekerdan ~2 gacha (taxminan ×7 kam qator). Sabab: model faqat Pinnacle'ni (zaxira `market_avg` tarixiy CSV'dan) va foydalanuvchi bukmekerini o'qiydi. Saqlash (`pipeline/retention.run_retention`, haftalik, ledger kaliti): tugagan o'yinning 90 kundan eski `morning/recalc/prematch` snapshotlari o'chiriladi; `opening/closing` va signali bor o'yinlar saqlanadi; 5000 qatorlik bo'laklar, har biri alohida qisqa tranzaksiya, `VACUUM` yo'q (bo'shagan sahifalar qayta ishlatiladi), idempotent.

Baza hajmi taxmini (RUNBOOK dagi "SQLite yillar davomida 1 GB dan kichik" jumlasi uchun; **taxmin**, real o'lchov paper trading davrida `/holat` dagi baza hajmidan olinadi): o'n ligada ≈ 14 o'yin/kun, bir bukmekerdan bir suratda ≈ 80 narx qatori (1X2, DC, totallar, BTTS, handikaplar), bir o'yinga ≈ 6 surat (opening, morning, 24 soat, pre-match, closing). Hammasini saqlash (12–15 bukmeker) ≈ 80 000 qator/kun ≈ 20 MB/kun ≈ 7 GB/yil edi, `1 GB` jumlasi esa noto'g'ri bo'lardi. Endi 2 bukmeker ≈ 13 000 qator/kun ≈ 3 MB/kun, va 90 kundan keyin oraliq suratlar (5 dan 3 tasi) o'chadi: barqaror holat ≈ 0.3 GB (oxirgi 90 kun) + ≈ 0.2 GB/yil doimiy qism. Demak "yillar davomida 1 GB dan kichik" faqat filtr va saqlash ishlaganda to'g'ri (≈ 3 yil).

### 11. O'lik kod va takrorlar
`daily.next_send_after`, `pipeline/queries` shimi (importlar `app.db.repo` ga), 6 ishlatilmagan matn, `rollback_learning_change` o'ramasi olib tashlandi. Ism "buklash" (`normalize_name`) ikki joyda emas: `db/search.py` transliteratsiyadan keyin o'sha bitta funksiyani chaqiradi, `llm/builder.py` esa boshqa vazifa (prompt tozalash, NFKC) — birlashtirilmadi.

## Oqibatlar
- `main.py` da (infra) `register_secrets` chaqirilishi tavsiya etiladi; chaqirilmasa ham shakl asosidagi tozalash ishlaydi.
- `scheduler.py` da (infra) haftalik ro'yxatga bitta qator: `run_retention(ctx)` (`app.pipeline.retention`).
- `INGEST_EXTRA_BOOKMAKERS` sozlamasi (infra) `PipelineConfig.extra_bookmakers` ga ulanadi; hozircha bo'sh.
- Mavjud bazadagi eski cashout bilan yopilgan signallar o'zgarmaydi (faqat yangi belgilashlar).
