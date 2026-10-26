# ADR 0021: Tahlil faqat bazadan ishlaydi; baza ko'rish shartnomasi (T7-B)

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-26
- **Muallif:** pipeline-engineer
- **Bog'liq:** qaror #25 ([decisions_log](../decisions_log.md)); [ADR 0007](0007-api-client-and-repositories.md) (I/O qoidasi, kvota); [ADR 0008](0008-pipeline-and-real-services.md); [ADR 0019](0019-t6-fix-round-c.md); shartnoma v1.11 ([contracts.md](../contracts.md))

## Kontekst
Foydalanuvchi talabi: "har kuni update qilsin bazaga, keyin to'liq bazadan tahlil qilsin; bazaga yuklab olingan ligalarni, o'yinlarni (vaqti va qaysi ligada ekanini ham) ko'ra olay, faqat bazamizdagini; bazamizdagi barcha ma'lumotlarni ko'ra olay."

Hozirgacha `AnalysisEngine` har so'rovda "yetishmayotganini" (o'yin kunlari, narxlar, natijalar) provayderdan yuklardi (`is_covered` + TTL, `ingest_*`), qayta hisoblash ham o'yin boshiga 2 so'rov sarflardi. Bu: (1) 100 so'rovlik bepul kunlik limitni foydalanuvchi bosishlariga bog'lardi, (2) bir xil tugma turli vaqtda turli ma'lumot (va natija) berardi, (3) tahlil nimaga asoslanganini ko'rish uchun bazani ko'rish yo'li yo'q edi.

## Variantlar
1. **Hammasi shunday qolsin**, faqat bazani ko'rish qo'shilsin. Rad: tahlil hamon so'rov sarflaydi va "bazadagi" bilan "tahlil ishlatgan" farq qiladi.
2. **Tahlil faqat bazadan; yuklash faqat kunlik job + adminning aniq tugmasi + settle/pre-match.** Tanlandi.
3. Tahlil bazani o'qiydi, lekin yetishmasa jimgina yuklaydi (hozirgi). Rad: 1-variantning kamchiliklari.

## Qarorlar

### 1. Provayderga murojaat qilinadigan yagona joylar
(a) kunlik `refresh` (08:30, `REFRESH_AT`); (b) adminning "Bazani yangilash" i (`refresh_manual`); (c) `settle` (natija, yopilish koeffitsienti) va rejalashtirilgan 24 soat/pre-match qayta hisoblashlar (ular avtomatik zaxiradan sarflaydi va o'yinga yaqin yangi narx/tarkib ularning vazifasi: yopilish koeffitsientlari bilan bir turdagi). Foydalanuvchi bosgan tahlil, qayta hisoblash, maxsus tahlil va retro **hech qachon** chaqirmaydi: `AnalysisEngine`dan `_needs/_ensure/_fetch`, `AnalysisRequest.fetch`, `ingest.is_covered/mark_covered` va `fixtures_ttl/odds_ttl` olib tashlandi; `RealAnalysisService` qayta hisoblashni `RecalcOptions(fetch=False)` bilan ishga tushiradi. Test provayder mocki "chaqirilmadi" deb tasdiqlaydi.

### 2. "Baza yangilanmagan" holati
`pipeline.freshness`: `fixtures` **va** `odds` qadamlari muvaffaqiyatli o'tgan har bir refresh (kunlik yoki qo'lda) ledger'ga `db.refreshed` muhrini qo'yadi. Kelajakka tegadigan oyna uchun muhr yo'q yoki `max_quote_age` dan eski bo'lsa, qo'lda (MANUAL) so'rov `StaleDataError` → servis `DataStaleError(last_refresh_at)` bilan to'xtaydi, avtomatik (kunlik, shablon) so'rov to'xtamaydi, `data_stale=True` qo'yadi (digest buni allaqachon ko'rsatadi). Butunlay o'tmishdagi (retro) oyna hech qachon eski hisoblanmaydi. Xatoni bot "Baza yangilanmagan: oxirgi yangilanish ..., admin 'Bazani yangilash' ni bosing" deb ko'rsatadi. Natija yarim yangilangan bo'lsa (`results` qadami o'tmagan, `fixtures`/`odds` o'tgan) baza yangi hisoblanadi: natijalarsiz ham kelajak o'yinlarni baholash mumkin va `results` qadami kunlik qayta urinishda to'ldiriladi.

### 3. `max_quote_age` 6 soatdan 30 soatga
Qaror narxi "6 soatdan eski bo'lsa narx yo'q" edi (avtomatik yangilanish 3 soatda deb faraz qilinardi). Kuniga bir marta yangilansa, ertalabki narxlar tushdan keyin barchasi `skipped_stale_price` bo'lib, tahlil hech narsa bermasdi. 30 soat = bir kun + zaxira. `max_quote_skew` (bukmeker va anchor narx orasidagi farq, 3 soat) o'zgarmadi: ikkalasi bitta refreshdan keladi. **Oqibat:** kechqurun ertalabki narxga qarab qaror chiqadi; signal kartasi `data_as_of` ni ko'rsatadi, pre-match qayta hisoblash o'yinga yaqin yangi narx oladi, foydalanuvchi esa tugmani bosmay ham bazani qanchalik eski ekanini `/baza` da ko'radi.

### 4. Kunlik refresh qamrovi (mahalliy kun = ikki UTC kun)
Eski kod `[ctx.clock().date(), +N)` ni UTC sana bilan oldi: Toshkentda (UTC+5) mahalliy 01:00 o'yini oldingi UTC kuniga tushadi va kechqurun ishga tushgan refreshda o'tib ketardi, oxirgi mahalliy kunning kechki o'yinlari esa kesilishi mumkin edi. Endi `PipelineConfig.display_zone` kunlari `utc_bounds` bilan UTC oraliqqa aylantiriladi va uni qoplaydigan barcha UTC kunlari olinadi (`refresh.local_utc_days`); narxlar uchun ham xuddi shunday (`odds_days_ahead` mahalliy kun). Narxi: provayderning kunlik so'rovi bitta UTC kunga ko'p (liga/mavsum rejimida 0). Test: Toshkent 01:00 bugungi o'yin va keyingi mahalliy kun 23:30 o'yini ikkalasi ham olinadi. Vaqt `REFRESH_AT` bilan sozlanadi (`PipelineScheduler` uni `PipelineConfig.refresh_at` dan oladi; jobstore'dagi trigger o'zgarsa qayta rejalashtiriladi).

### 5. Adminning "Bazani yangilash" i
`refresh_manual` kunlik qadamlarning o'zi, lekin MANUAL maqsadda (qo'lda zaxira va umumiy qoldiq hisobidan): (1) ikki bosish orasida `MANUAL_REFRESH_MIN_MINUTES` (30) — ledger `claim(lease=interval)` bilan, shuning uchun parallel ikki bosish va qayta ishga tushish ham himoyalangan; qulf hech narsa yozilmagan bo'lsa (taxmin zaxiradan ko'p, provayder ishlamaydi, hamma qadam o'tkazilgan, bekor qilish) qaytariladi; (2) so'rovdan oldin yuqori chegara taxmini (`estimate_requests`: o'yin kunlari, liga-kun narxlari, kutilayotgan natijalar) qoldiqdan ko'p bo'lsa `BudgetExceededError` va **hech narsa yuborilmaydi** (aks holda yuklangan sahifalar yozilmasdan yo'qolardi); (3) hisobot: yozilgan o'yin/natija/narx, sarflangan so'rov (kvota hisoblagichi farqi), o'tkazib yuborilgan qadamlar. Faqat `Core.admin_ids` ga kirgan foydalanuvchi (`None` = bot whitelist'i) — aks holda `NotAuthorizedError`.

### 6. Baza ko'rish shartnomasi (`DataBrowserService`)
Faqat o'qiydigan alohida port (mavjud `CatalogService` ni kengaytirish o'rniga: u tahlil oynasi, gorizont va foydalanuvchi ligalariga bog'langan, ko'rish esa "bazada nima bor" — chegarasiz). `Services.browser` yangi maydon. Izolyatsiya: liga/jamoa/o'yin/narx — umumiy provayder ma'lumoti; signal va stavka jadvallari faqat so'rovchi uchun (jadval hisoblari `own=True`, o'yin tafsilotidagi signallar faqat uning). Repo: `db.repo.browse` (yangi, faqat o'qish; data-engineer egaligi — minimal qo'shimcha, hisobotda). Hajm: PRAGMA (`page_count*page_size`), fayl yo'li kerak emas. Sahifalash `OFFSET/LIMIT` + `COUNT`; narxlar tafsilotda 500 tagacha eng yangisi (`truncated`), to'liq ro'yxat `list_stored_odds` da.

## Oqibatlar
- Tahlil tezroq va deterministik; kunlik limit foydalanuvchi bosishlariga bog'liq emas, qo'lda zaxira faqat adminning yangilashi uchun qoladi (qayta hisoblashlar uni sarflamaydi, `/qayta` ning API sarfi 0).
- Foydalanuvchi qayta hisoblashi endi yangi narx olmaydi: u bazadagi eng yangi narx bilan qayta baholaydi va `data_as_of` ni ko'rsatadi (haqiqiy yangi narx: admin yangilashi yoki pre-match job). Bu mahsulot qarori sifatida qabul qilingan (qaror #25).
- Birinchi deploydan keyin refresh ishlamaguncha (08:30 yoki admin) kelajak tahlillari `DataStaleError` beradi: kutilgan xatti-harakat; RUNBOOK'da qayd etiladi.
- Refresh vaqti o'zgartirilsa, `feedback` (08:45) kabi refreshdan keyin deb faraz qilingan jobning tartibi o'zgaradi — `REFRESH_AT` ni 08:45 dan oldin tuting.
- `AnalysisRequest.fetch` olib tashlangani uchun uni uzatgan testlar yangilandi.
- Ochiq: refresh gorizonti (`refresh_days_ahead=3`, narx `odds_days_ahead=2`) ichidan tashqaridagi kelajak kunlar tahlilda "narx yo'q" deb o'tkazib yuboriladi; zarur bo'lsa sozlama oshiriladi (so'rov narxi bilan birga).
