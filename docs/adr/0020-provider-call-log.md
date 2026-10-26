# ADR 0020: Provayder chaqiruvlari jurnali (API-Football va Gemini so'rovlari bazada)

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-26
- **Muallif:** data-engineer (T7-A: `db`, `clients`)
- **Bog'liq:** [ADR 0007](0007-api-client-and-repositories.md) (klientlar, kesh, kvota), [ADR 0018](0018-t6-fix-round-a.md) §8 (maxfiy ma'lumot tozalash), [ADR 0018](0018-t6-fix-round-a.md) §9 (kesh)

## Kontekst
Foydalanuvchi API-Football ga va Gemini ga yuborilgan **har bir** so'rov (Gemini uchun prompt va javob) saqlanishini va admin Telegramda ko'ra olishini talab qildi. Savollar: "bugun kvota nimaga ketdi", "Gemini nima javob berdi", "nega bu kartada ma'lumot yo'q". Hozir faqat log qatorlari bor: ular qisqa (matn ataylab yo'q, maxfiylik uchun) va konteynerdan tashqarida o'qilmaydi. Bu ADR faqat saqlash tomonini hal qiladi; ko'rsatish (servis/bot) alohida ish.

## Variantlar
1. **Log fayli / structlog** — oddiy, lekin so'rab bo'lmaydi (sahifalash, filtr, statistika yo'q), bot uchun alohida parser kerak, retention yo'q.
2. **Alohida SQLite fayl** — asosiy bazaga yozish bilan raqobat yo'q, lekin ikkinchi migratsiya/zaxira/WAL yo'li; yagona operatsion model (bitta fayl, bitta zaxira) buziladi.
3. **Asosiy bazada jadval `provider_calls`** (tanlandi) — bitta Alembic zanjiri, bitta zaxira, SQL bilan filtr/statistika, PostgreSQL ga ko'chganda ham ishlaydi.

## Qaror
- Jadval `provider_calls` (Alembic `0010`): `at` (UTC), `provider`, `operation`, `outcome` (`ok`/`cached`/`error`/`quota_blocked`), `duration_ms`, `request`, `response`, `http_status`, `quota_cost`, `size_bytes`, `error`. Indekslar `(at)` va `(provider, at)`. Orqaga qarab yuruvchi `ORDER BY at DESC` uchun alohida DESC indeks kerak emas (SQLite ham, PostgreSQL ham indeksni teskari o'qiydi; DESC indeks Alembic `check` da farq beradi).
- **Nima yoziladi.** API-Football: har bir yuborilgan HTTP urinish (qayta urinishlar alohida qator, har biri kvotadan 1 oladi), keshdan javob (`cached`, narx 0, javob matni yo'q), kvota rad etishi (`quota_blocked`, narx 0). `request` = so'rov parametrlari JSON; kalit sarlavhada, shuning uchun yozuvga umuman tushmaydi. Gemini: bir `generate_json` = bir qator (urinishlar `quota_cost` da), `request` = to'liq `system` + `user` matn, `operation` = javob sxemasi nomi (chaqiruvchi vazifa nomini bermaydi; shartnomani o'zgartirmaslik uchun).
- **Hajm.** Matn `MAX_LOGGED_TEXT` = 20 000 belgigacha qirqiladi (`clip_text`, belgi bilan). Javob tanasi **qisqartirilgunga (`PayloadShape`) qadar** yoziladi (xom javob), chunki foydalanuvchi "provayder nima javob berdi" ni ko'rishni xohlaydi; qirqish hajmni cheklaydi.
- **Retention.** `CALL_LOG_RETENTION_DAYS` (standart 30). Mavjud haftalik `retention` job odds tozalashdan keyin shu jadvalni ham tozalaydi: 5 000 qatorlik partiyalar, har biri alohida qisqa tranzaksiya (SQLite bitta yozuvchi). Natijada qator 30–37 kun yashaydi. Haftalik ledger kaliti o'zgarmadi: shu hafta allaqachon ishlagan bo'lsa, tozalash keyingi haftaga qoladi (zarar yo'q).
- **Maxfiylik.** Saqlashdan oldin har matn `scrub` (konfiguratsiyadagi maxfiy qiymatlar + tanilgan kalit shakllari: `pipeline.redaction.scrub_text`) dan o'tadi, **keyin** qirqiladi (qirqish maxfiy qiymatni ikkiga bo'lib, tozalashdan qochirmasin). Qatlam qoidasi (`db` → `pipeline` import qilmaydi) sababli tozalovchi `SqlCallRecorder` ga ixtiyoriy emas, majburiy argument sifatida `pipeline/runtime` dan beriladi. Foydalanuvchi izohlari (`ExperienceContext`) prompt orqali jurnalga tushishi mumkin: bu ma'lumot allaqachon Geminiga ketadi va faqat admin ko'radi; 30 kunlik retention bilan chegaralangan.
- **Yozish xatosi siyosati.** Jurnal diagnostika, mahsulot emas: yozish xatosi provayder chaqiruvini **hech qachon** buzmaydi, kechiktirmaydi va natijasini o'zgartirmaydi. `SqlCallRecorder.record` aniq istisnolarni (`SQLAlchemyError`, `DataError`, `OSError`, `ValueError`, `TypeError`) ushlaydi va `call_log_write_failed` ogohlantirishini yozadi (provayder, operatsiya, natija, istisno **turi**). Istisno matni loglanmaydi: SQLAlchemy xatolari bog'langan parametrlarni (aynan saqlanayotgan prompt/tanani) o'z ichiga oladi. Bekor qilish (`CancelledError`) yutilmaydi. Yozuv yo'qolishi qabul qilinadi (qayta urinish va navbat yo'q: YAGNI).
- **Blokirovka.** Recorder alohida engine ustida (`create_usage_engine`, `busy_timeout` 250 ms) ishlaydi: ilova uzoq yozuv tranzaksiyasini ushlab tursa, so'rov yo'li 5 soniya kutmaydi, yozuv tashlanadi va log yoziladi. WAL tufayli o'quvchilar (bot) yozuvchini to'smaydi.
- **Ro'yxat arzon.** `list_calls` javob matnini o'qimaydi (SQL `substr` bilan so'rovning birinchi 200 belgisi); to'liq matn faqat `get_call` da.

## Hajm baholash (TAXMIN, o'lchanmagan)
Asos: bepul tarif 100 so'rov/kun; Gemini bepul tarifda kuniga taxminan 20–50 chaqiruv; kesh javoblari ~200/kun (kichik qator).
- API-Football: ≤ 100 yuborilgan so'rov × o'rtacha ~10–20 KB (20 KB da qirqiladi; koeffitsient javoblari katta) ≈ 1–2 MB/kun.
- Gemini: ~30 chaqiruv × (prompt ~8 KB + javob ~3 KB) ≈ 0.3–0.5 MB/kun.
- Kesh/kvota-rad qatorlari: ~200 × ~0.3 KB ≈ 0.06 MB/kun.
- **Jami: kuniga ~350–450 qator, ~1.5–3 MB (yuqori chegara ~3.5 MB).** 30 kunda ~12 000 qator va ~45–90 MB (yuqori chegara ~105 MB), asosiy baza hajmiga nisbatan sezilarli, lekin SQLite uchun muammo emas. Haqiqiy raqamni ishlatilgandan keyin `SELECT count(*), sum(length(request)+coalesce(length(response),0)) FROM provider_calls` bilan tekshiring; kerak bo'lsa `CALL_LOG_RETENTION_DAYS` kamaytiriladi yoki `MAX_LOGGED_TEXT` pasaytiriladi (shartnoma konstantasi, orkestr roziligi bilan).

## Oqibatlar
- Plyus: admin har bir so'rovni ko'radi; kvota sarfi `call_stats` bilan tushuntiriladi; xatolar kontekst bilan saqlanadi.
- Minus: baza ~100 MB gacha o'sadi; har provayder chaqiruvi bitta qo'shimcha qisqa yozuv tranzaksiyasi (~ms) qo'shadi.
- Cheklovlar: yozuv yo'qolishi mumkin (qulf, to'liq disk); Gemini `operation` — T7-C dan boshlab vazifa nomi (`llm_review`, `post_mortem`; `generate_json(..., operation=)`), berilmasa sxema nomi; bir vaqtda bir xil so'rovni kutgan chaqiruvchilar (single-flight) bitta yozuv oladi (bitta haqiqiy so'rov); `ApiClient` yozuvlarda doimo `Provider.API_FOOTBALL` deb belgilanadi (hozircha yagona HTTP provayder).
- Kelajak: ikkinchi HTTP provayder qo'shilsa, `ProviderConfig` ga `Provider` maydoni qo'shiladi.
