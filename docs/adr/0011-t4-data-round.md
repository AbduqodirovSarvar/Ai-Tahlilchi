# ADR 0011: T4 ma'lumotlar raundi — ledger jadvali, shablon ustunlari, stavka tokeni, eslatmalar

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-22
- **Muallif:** data-engineer
- **Kod:** `src/app/db/schema.py`, `src/app/db/repo/{catalog,notes,operations,users}.py`, `alembic/versions/0004..0006`
- **Bog'liq:** [ADR 0007](0007-api-client-and-repositories.md), [ADR 0008](0008-pipeline-and-real-services.md) ("Boshqa agentlarga so'rovlar")

## Kontekst
T4 da pipeline-engineer to'rt narsani vaqtincha o'z yo'li bilan hal qilgan edi: `pipeline_ledger` jadvalini o'zi yaratgan (`CREATE TABLE IF NOT EXISTS`), shablon jadvali va bayroqlarini `saved_queries.schedule_cron` matniga yozgan, stavka tokenlari va foydalanuvchi eslatmalarini ledger'da saqlagan, o'qish so'rovlarini `app.pipeline.queries` da yozgan. Bular sxemadan tashqarida: migratsiyasiz, so'rovsiz (token bo'yicha qidirish ledger qatoriga bog'liq), bitta PostgreSQL'ga o'tishda xavfli.

## Qarorlar

### 1. `pipeline_ledger` — oddiy Alembic jadvali (`0004`)
`LedgerRow` `schema.py` da; `app.pipeline.ledger.ledger_table` endi `LedgerRow.__table__` (ochiq API o'zgarmagan). Ustun va nomlar eski jadval bilan bir xil, faqat indeks nomi ORM qoidasiga o'tdi (`ix_pipeline_ledger_job_done_at`). Migratsiya **moslashuvchan**: jadval allaqachon mavjud bo'lsa (pipeline o'zi yaratgan baza) yaratmaydi, faqat indeksni qayta nomlaydi, qatorlar saqlanadi. `Ledger.ensure_schema` qoldi, lekin faqat hech qachon migratsiyadan o'tmagan baza uchun zaxira (`checkfirst`). `user_id` da FK yo'q: 0 — tizim yozuvlari. Downgrade jadvalni o'chiradi: u faqat hosila holat (nima allaqachon yuborilgan/olingan), yo'qolsa pipeline xavfsiz takrorlaydi.

### 2. `saved_queries`: tuzilgan ustunlar (`0005`)
`per_day`, `include_no_value` (bool, `server_default false`), `schedule_weekdays` (JSON ro'yxat, ISO 1..7, bo'sh = jadvalsiz), `schedule_at` (`Time`, mahalliy vaqt, NULL = jadvalsiz). `schedule_cron` **olib tashlandi** — bu "faqat qo'shimcha" qoidasidan ongli istisno: ustun hech qachon haqiqiy cron bo'lmagan (pipeline uni matn-konteyner sifatida ishlatgan), yagona iste'molchisi `services/real/account.py` shu raundda yangilandi, ikki tomonlama saqlash esa ikkita haqiqat manbasini beradi. Haftalik kun uchun JSON bitmaskdan afzal: boshqa ro'yxat ustunlari (`sports`, `markets`) bilan bir xil va o'qiladigan. "Kunlar va vaqt birga yoki hech biri" invarianti `SavedQueryDraft.__post_init__` da (bazada `CHECK` yo'q: JSON uzunligi uchun portativ ifoda yo'q).
Ma'lumot migratsiyasi: eski `"M H * * d,d # per_day no_value"` matni ajratiladi; o'qib bo'lmaydigan jadval "jadvalsiz" bo'ladi, lekin bayroqlar saqlanadi; downgrade eski matnni qayta yozadi (sinovda: aylanma yo'l bir xil matn beradi).

### 3. `user_bets.client_token` va `signal_notes` (`0006`)
- **Token:** nullable `String(64)` (`MAX_BET_TOKEN_LENGTH`), **unique indeks `(user_id, client_token)`** (NULL lar to'qnashmaydi, ikkala DBMS da bir xil). Constraint o'rniga indeks: SQLite da `ADD CONSTRAINT` yo'q va batch-rejim `--sql` (offline) da ishlamaydi; indeks ikkala holatda ham oddiy `ALTER`/`CREATE INDEX`. `create_bet(..., client_token=)` idempotent: avval qidiradi, keyin savepoint ichida kiritadi, `IntegrityError` bo'lsa va token bo'yicha qator topilsa — poyga yutqazdi, mavjud stavkani qaytaradi; boshqa `IntegrityError` ko'tariladi. Ledger lease/`BusyError` aylanma yo'li kerak emas: bitta tranzaksiya, bitta haqiqat manbasi, restartdan keyin ham ishlaydi.
- **Eslatmalar:** alohida `signal_notes` (user_id, signal_id, text, created_at), signal yoki foydalanuvchi o'chirilsa CASCADE. `set_result_note` (natijadagi `user_note`) faqat yopilgan signalga yozadi, shartnoma esa kelajak signalga ham izoh ruxsat beradi, shuning uchun eslatmalar natija qatoriga bog'lanmagan. `add_signal_note` egalikni tekshiradi (begonaniki — `None`).
- **Ma'lumot ko'chirish:** ledgerdagi `job='bet'` (tugagan, bet id mavjud va shu foydalanuvchiniki) va `job='note'` yozuvlari yangi joyga ko'chadi va ledgerdan o'chadi; yetim yozuvlar (stavka/signal yo'q yoki begona) tashlanadi. Downgrade ularni ledgerga qaytaradi (`subject="<signal>:<note id>"`).

### 4. O'qish yordamchilari `app.db.repo.catalog`
`list_leagues`, `get_league`, `get_team`, `league_refs`, `team_refs`, `league_teams`, `user_ids` `app.pipeline.queries` dan ko'chirildi (mantiq o'zgarmagan) va `app.db.repo` dan eksport qilinadi. `app.pipeline.queries` yupqa re-export bo'lib qoldi: pipeline va servislardagi importlar tegilmadi (to'lqin vaqtida pipeline fayllarida to'qnashuv xavfi yo'q). Keyingi raundda pipeline importlarini `app.db.repo` ga o'tkazib, modulni o'chirish mumkin.

### 5. Ko'chirish xavfsizligi
Migratsiyalar `--sql` (offline) rejimida ham ishlaydi: ma'lumot nusxalash va `inspect` faqat jonli ulanishda bajariladi; sxema o'zgarishlari oddiy `op.add_column`/`create_index`. Barcha uch migratsiya mavjud qatorlar bilan upgrade → downgrade → upgrade sinovidan o'tadi (`tests/db/test_migrations.py`).

## Ko'rib chiqilgan muqobillar
- **`schedule_cron` ni saqlab yangi ustunlarni qo'shish:** ikkita haqiqat manbasi, eskirgan ustun abadiy qoladi.
- **Token uchun `UniqueConstraint`:** batch-rejim kerak (offline `--sql` buziladi); unique indeks teng kafolat beradi.
- **Eslatmani `results.user_note` ga yozish:** kelajak signalga izoh yozib bo'lmaydi; natija qatori bo'lmasa yaratish kerak bo'ladi.
- **Ledgerni saqlab qolish:** token bo'yicha qidirish, tozalash va egalik tekshiruvi ledger qatoriga bog'langan, restartda lease kutish bilan stavka "band" bo'lib qoladi.

## Oqibatlar
- Pipeline ochiq API o'zgarmadi; faqat `services/real/signals.py` (`add_note`, `place_bet` endi repo orqali) va `services/real/account.py` (shablon ustunlari) hamda `pipeline/data.py` (`add_signal_note` eksporti) tegildi — pipeline-engineer hisobotida ko'rsatilgan.
- `place_bet` da ledger `claim` yo'q: ikkita parallel bir xil token — bitta stavka (testlangan, 5 parallel).
- Eski baza bilan ishlovchi ilova versiyasi `0006` dan keyin tokenlarni ledgerda topa olmaydi: ilova va migratsiya birga deploy qilinadi.
