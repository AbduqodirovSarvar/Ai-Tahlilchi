# QA hisoboti: T2 — Ma'lumotlar qatlami (F2.1, F2.4, F2.7)

- **Tekshiruvchi:** test-engineer (mustaqil QA)
- **Sana:** 2026-10-21
- **Branch:** `wave/T2-qa` (`main` `d99c7de` dan); QA testlari: `d3d42af`
- **Muhit:** Windows 11, Python 3.13.15, uv 0.13.4. Tarmoq yo'q (football-data.co.uk korporativ filtr bilan bloklangan, [decisions_log #7](../decisions_log.md); chetlab o'tishga urinilmadi). Barcha HTTP `respx` bilan.

## Yakuniy xulosa

### ✅ Darvozadan o'tadi

🔴 va 🟠 topilma yo'q. data-engineer'ning barcha da'volari mustaqil tasdiqlandi. 5 ta 🟡 topilma `xfail(strict=True)` testlari bilan qayd etilgan: tuzatilganda testlar XPASS bo'lib yiqiladi, shunda belgini olib tashlash kerak. Ulardan hech biri standart import doirasida (10 liga, 2021–2026 mavsumlar, HTML blok sahifasi) ma'lumotni buzmaydi. Lekin F-1 ADR 0002 dagi "eng so'nggi `taken_at` ni oling" tavsiyasini noto'g'ri qiladi, shuning uchun F3 (backtest) dan oldin yopish tavsiya etiladi.

## data-engineer da'volarining tekshiruvi

| Da'vo | Natija | Dalil |
|---|---|---|
| 352 test o'tadi | ✅ | `python -m uv run --no-config pytest` → `352 passed`, `Total coverage: 100.00%` (QA dan oldin) |
| Branch coverage 100% | ✅ | QA dan keyin: 1420 stmt, 182 branch, 0 miss; `coverage_gate.py` → exit 0 (`models`/`pipeline` hali bo'sh: SKIP) |
| Migratsiya upgrade → downgrade → upgrade | ✅ | CLI: `alembic -x db_path=<tmp>/mig.db upgrade head` → `downgrade base` → `upgrade head` xatosiz |
| `alembic check` toza | ✅ | `No new upgrade operations detected.` |
| Import idempotent | ✅ | Ikkinchi ishga tushirish: jadval sonlari bir xil va **barcha `odds_snapshots` qatorlari (id bilan) bir xil**: joyida yangilanadi, qayta yaratilmaydi (`test_second_import_changes_nothing`) |
| Parser sana formatlari, yo'q ustunlar, g'alati baytlarga chidamli | ✅ | Mavjud testlar (`dd/mm/yy`, `dd/mm/yyyy`, hypothesis ixtiyoriy bayt) + QA: `nan`, `inf`, `-inf`, `1e400`, `"1,9"`, `"—"` → faqat o'sha narx guruhi tashlanadi, fayl yiqilmaydi. Istisno: 1990-yillar 2 xonali yili (F-3) |
| Bloklangan yuklash aniqlanadi va keshlanmaydi | ✅ (qisman, F-4) | 403 + HTML, 200 + HTML (`text/csv` sarlavhasi bilan), BOM + bo'sh qator + HTML, 302 → blok sahifasi: exit 1, keshda fayl ham, `.partial` ham yo'q, baza bo'sh. **Lekin** oddiy matnli blok sahifasi keshlanadi (F-4) |
| Offline rejim ishlaydi | ✅ | `--offline` + catch-all `respx` route: 0 so'rov; eskirgan joriy mavsum fayli (7 kun) keshdan import qilinadi |
| ruff / format / mypy / lint-imports | ✅ | `All checks passed!`, `79 files already formatted`, `no issues found in 36 source files`, `Contracts: 5 kept, 0 broken.` |

## F2 qabul mezonlari (shu to'lqin doirasi)

| ID | Mezon | Holat | Dalil |
|---|---|---|---|
| F2.1 | SQLAlchemy modellari (7-bo'limdagi barcha jadvallar) + Alembic boshlang'ich migratsiyasi | ✅ | Migratsiya round-trip va `alembic check` yuqorida. QA testlari bazani `create_all` emas, **haqiqiy migratsiya** bilan quradi |
| F2.1 | Unique kalitlar NULL bilan buzilmaydi | ✅ | Migratsiya qilingan DDL'da `signals.line` va `odds_snapshots.line` NOT NULL (xom `sqlite3` INSERT → `NOT NULL constraint failed`). `line` = `0.0`, `-0.0`, `0` (int) — hammasi bitta qiymat: UNIQUE dublikatni rad etadi. `1x2` da `line=0.5` → CHECK xatosi. Xulosa: **baza dublikat signalga yo'l qo'ymaydi** |
| F2.4 | Domen modellari va `SportAdapter` (shartnoma §1–2) | ✅ | Mavjud unit testlar; shartnoma kodga mos (`docs/contracts.md` §1–3 bilan solishtirildi). Fora konvensiyasi CSV dan bazagacha tekshirildi (pastda) |
| F2.7 | football-data CSV → `fixtures` + `odds_snapshots` (closing) | ✅ | Commit qilingan 3 ta fixture CSV offline import: aniq sonlar qo'lda hisoblangan oracle bilan mos (pastda) |
| F2 umumiy | `python -m app.pipeline.refresh`, ≤ 40 so'rov | — | F2.2/F2.3/F2.5 keyingi to'lqinda; bu to'lqinda tekshirilmaydi |

### Commit qilingan fixture'lar: qo'lda hisoblangan oracle

| | Kutilgan | Bazada |
|---|---|---|
| leagues / teams / fixtures / odds | 3 / 18 / 9 / 198 | ✅ bir xil |
| Bukmeker × tur | pinnacle closing 33, opening 30; bet365 closing 21, opening 30; market_avg 21/21; market_max 21/21 | ✅ |
| Bozor | 1x2 102, totals 48, asian_handicap 48 | ✅ |
| Liga-mavsum | E0/2024: 4, SP1/2021: 3, D1/2023: 2 | ✅ |
| Pinnacle closing 1X2 qamrovi | E0 2/4, SP1 3/3, D1 2/2 | ✅ (log: 50% / 100% / 100%) |
| Qiymatlar | Man United–Fulham: `2024-08-16 19:00 UTC` (20:00 BST), 1–0, HT 0–0; closing `taken_at` = kickoff, opening = kickoff − 1 kun; `Cádiz`, `Alavés` (cp1252), `Düsseldorf`, `Köln` (UTF-8 BOM) to'g'ri | ✅ |

Eslatma: oracle avval data-engineer test izohiga tayanib yozilgan va 3 ta narxda xato bo'lgan (Arsenal–Villa qatori Bet365 emas, **Pinnacle** closing). Qiymatlar CSV kataklaridan qayta sanaldi, kod emas.

## Test natijalari

| | Oldin | QA dan keyin |
|---|---|---|
| Testlar | 352 passed | **380 passed, 5 xfailed** (strict) |
| Branch coverage | 100% | 100% (1420 stmt, 182 branch) |
| `coverage_gate.py` | OK | OK |
| 10 000 qatorli CSV importi | — | ≈ 10 s (byudjet < 60 s): 10 000 o'yin, 560 000 narx |

**Barqarorlik:** yangi modul (perf testisiz) 20 marta ketma-ket: 20/20 yashil, 0 yiqilish. 2 ta ishga tushirish sekin bo'ldi (433 s va 52 s, odatda ≈ 7 s); `--durations` bilan 3 marta qayta ishga tushirildi, sekinlik takrorlanmadi va biror test sababchi emas. Ehtimoliy sabab: shu mashinada parallel ishlayotgan boshqa agentlar yuklamasi. Isbotlanmagan, ochiq aytiladi.

### QA qo'shgan testlar (`tests/integration/`)
- `conftest.py`: `import_history` (skript moduli), `migrated_db` (haqiqiy Alembic), `workspace`.
- `history_helpers.py`: to'liq football-data sarlavhasi (24 narx guruhi), `full_row`, `write_csv`, `ImportWorkspace` (importni ishga tushirish va xom `sqlite3` orqali o'qish: ORM va mapper'larga bog'liq bo'lmagan oracle).
- `test_history_import.py` (28 test + 5 xfail):
  - commit qilingan fixture'lar: aniq sonlar, qiymatlar, idempotentlik (id lar bilan);
  - tuzatish bilan qayta import: hisob va narx joyida yangilanadi; yangi fora chizig'i yangi bozor sifatida qo'shiladi, eskisi qoladi (ADR bo'yicha);
  - BST/GMT ikkala chegarasi (28–29.10.2023, 30–31.03.2024), takrorlanadigan soat 01:30 (birinchi, BST), vaqtsiz qator, 23:30 GMT; closing `taken_at` = kickoff;
  - yil chegarasidan o'tuvchi mavsum (31.12.24 / 01.01.25 bitta mavsum 2024);
  - fora belgisi: `AHh=1.75` → uy +1.75, mehmon narxi o'sha (uy) chizig'ida; `AHCh` alohida closing chizig'i; favorit `-1.0`;
  - bitta CSV da dublikat qatorlar (birinchisi yutadi), bo'sh fayl (faqat o'zi yiqiladi, exit 1), faqat sarlavha (0 qator, exit 0);
  - `nan`/`inf`/buzilgan kataklar, 10k qator byudjeti;
  - offline'da tarmoq yo'q, 403/HTML/redirect blok sahifalari keshlanmaydi;
  - `signals`/`odds_snapshots` da `line` NULL bo'lolmaydi, unique kalit ishlaydi.

## Topilmalar

🔴 yo'q. 🟠 yo'q.

### 🟡 F-1. Kickoff vaqti tuzatilsa, barcha narxlar ikki baravar bo'ladi va "eng so'nggi `taken_at`" eski narxni beradi
- **Joy:** `src/app/db/history.py:54-62` (`_ODDS_IDENTITY` da `taken_at`), `src/app/sports/history/football_data.py:396-398` (`taken_at` kickoff'dan hisoblanadi); `docs/adr/0002-domain-and-schema.md:60`.
- **Takrorlash:** `python -m uv run pytest --runxfail tests/integration/test_history_import.py::test_reimport_after_kickoff_time_correction_does_not_duplicate_odds` → `assert 112 == 56`.
- **Ta'sir:** o'yin 17:30 → 12:30 ga tuzatilsa, kickoff yangilanadi, lekin 56 ta eski narx qoladi (har bir selection uchun ikki `closing`). Eski closing `taken_at` (16:30Z) yangisidan (11:30Z) **keyin**, shuning uchun ADR dagi "eng so'nggi `taken_at` ni oling" qoidasi eskirgan narxni tanlaydi. Deduplikatsiya qilmaydigan backtest o'yinni ikki marta sanaydi.
- **Ehtimollik:** past (football-data tugagan o'yin vaqtlarini kamdan-kam o'zgartiradi).
- **Tavsiya:** football-data uchun identity `(fixture, bookmaker, market, selection, line, kind)` (`taken_at` siz), yoki faylni qayta importda o'sha o'yinning shu manba snapshot'larini almashtirish. Hech bo'lmaganda ADR tavsiyasini "`kind` bo'yicha eng so'nggi `id`" ga o'zgartirish. **Kimga:** data-engineer.

### 🟡 F-2. Sana tuzatilsa, bitta o'yin ikki marta saqlanadi
- **Joy:** `src/app/sports/history/football_data.py:333-335` (`external_id` ga sana kiradi).
- **Takrorlash:** `pytest --runxfail ...::test_reimport_after_match_date_correction_does_not_duplicate_the_fixture` → `assert 2 == 1`.
- **Ta'sir:** bir mavsumda bir xil uy/mehmon juftligi bilan ikki `fixtures` qatori; model va backtestda dublikat. Ehtimollik past. Muqobil kalit (`liga/mavsum/uy/mehmon`) bir mavsumda juftlik bir marta uchrashiga tayanadi: kubok yo'q, lekin pley-off/qayta o'yinlarda ehtiyot kerak. Tuzatish o'rniga ADR da cheklov sifatida qayd etish ham qabul qilinadi.

### 🟡 F-3. 1990-yillar fayllaridagi 2 xonali yil 2090-yillarga aylanadi
- **Joy:** `src/app/sports/history/football_data.py:49`, `:425` (`TWO_DIGIT_YEAR_BASE = 2000`).
- **Takrorlash:** `pytest --runxfail ...::test_two_digit_years_of_a_1999_season_stay_in_the_1990s` → kickoff `'2099-08-07 14:00:00'`.
- **Ta'sir:** standart import (2021+) ga ta'sir qilmaydi, lekin CLI `--seasons 1999` ni qabul qiladi va xatosiz 2099-yilgi o'yinlar yozadi. **Tavsiya:** yilni mavsumdan aniqlash (`season` yoki `season + 1`) yoki `MIN_SEASON` ni 2000 ga cheklash.

### 🟡 F-4. Oddiy matnli blok sahifasi CSV deb keshlanadi va qayta yuklanmaydi
- **Joy:** `src/app/sports/history/download.py:75-79` (`looks_like_csv`: birinchi qatorda `,` va `Date` bo'lsa yetarli).
- **Takrorlash:** `pytest --runxfail ...::test_plain_text_block_page_is_not_cached`: javob `Access denied by policy (Gambling). Date: Tue, 21 Oct 2026 ...` → `data/history/2324/E0.csv` yaratiladi.
- **Ta'sir:** import shu fayl uchun `failed` (exit 1) bo'ladi, lekin fayl keshda qoladi; tugagan mavsum keyin hech qachon qayta yuklanmaydi (`needs_download` faqat mavjudligini tekshiradi), faqat `--refresh` yoki qo'lda o'chirish yordam beradi. Shu mashinadagi haqiqiy blok javobi 403 (to'g'ri ishlaydi), shuning uchun ehtimollik past. **Tavsiya:** saqlashdan oldin sarlavhani parser'ning majburiy ustunlari (`Date`, `HomeTeam|HT`, `AwayTeam|AT`, `FTHG|HG`, `FTAG|AG`) bilan tekshirish; keshdagi fayl parse bo'lmasa uni o'chirish yoki karantinga ko'chirish.

### 🟡 F-5. `incomplete_price_groups` umuman bo'lmagan fora bukmekerlarini ham sanaydi
- **Joy:** `src/app/sports/history/football_data.py:376-379` (umumiy `AHh`/`AHCh` chiziq ustuni `cells` ga qo'shiladi, shuning uchun `any(cells)` doim rost).
- **Takrorlash:** `pytest --runxfail ...::test_unquoted_handicap_bookmakers_are_not_counted_as_incomplete`: faqat `AHh,PAHH,PAHA` bor fayl → `incomplete_price_groups == 3` (B365/Max/Avg AH yo'q, buzilgan emas).
- **Ta'sir:** faqat log ko'rsatkichi (ma'lumot to'g'ri). Hozirgi football-data fayllarida barcha AH guruhlari bor, shuning uchun amalda kam uchraydi; ko'rsatkich ma'lumot sifatini kuzatishda chalg'itadi. **Tavsiya:** guruh borligini faqat narx ustunlari bo'yicha tekshirish.

## Xavflar / eslatmalar (topilma emas)
- **Vaqt mintaqasi taxmini:** barcha ligalar (SP1, D1, ...) uchun CSV vaqti Buyuk Britaniya vaqti deb olinadi (ADR 0002). Tarmoq bloklangani uchun haqiqiy fayllarda tekshirib bo'lmadi. F2.5 (API-Football) bilan o'yinlarni moslashtirishda soat farqi chiqsa, birinchi shu taxminni tekshiring.
- **Offline + bo'sh kesh → exit 0:** fayllar `missing` bo'lsa skript hech narsa import qilmay 0 bilan chiqadi (`scripts/import_history.py:13` "0 all files imported"). Kelajakdagi mavsumlar uchun bu to'g'ri, lekin avtomatlashtirishda "0 ta o'yin" ni muvaffaqiyat deb qabul qilmaslik kerak (`coverage_total fixtures=0` logiga qarang).
- **Ekstremal narxlar:** domen faqat `odds > 1` va chekli bo'lishini talab qiladi; `1e308` kabi xato narx saqlanadi. Model/backtest kirishida mantiqiy chegaralar kerak bo'ladi (F3).
- **Fixture dublikati:** `tests/sports/history/test_import_script.py` dagi `script`/`db_path` fixture'lari QA `tests/integration/conftest.py` bilan qisman takrorlanadi; keyingi tozalashda bitta joyga ko'chirish mumkin.
- **Muhit:** worktree'lar asosiy repo ichida (`.claude/worktrees/`) bo'lgani uchun `uv` sozlamalarni yuqoriga qarab qidiradi; asosiy checkout'dagi `pyproject.toml` boshqa agent tomonidan tahrir qilinayotganda (TOML xatosi) `uv run` yiqildi. Barcha buyruqlar `python -m uv run --no-config ...` bilan bajarildi (loyiha `pyproject.toml` baribir o'qiladi).
