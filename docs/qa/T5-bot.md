# QA hisoboti: T5 — bot F7 imkoniyatlari (F7.1–F7.8) + xizmatlar v1.6

- **Tekshiruvchi:** code-reviewer + test-engineer (birlashgan o'tish)
- **Sana:** 2026-10-22
- **Branch:** `wave/T5-qa-bot` (`main` `df72652` dan)
- **Muhit:** Windows 11, Python 3.13, aiogram 3. Tarmoq yo'q. Yangi e2e testlar **haqiqiy dispatcher** (whitelist, rate limit, xato middleware, barcha routerlar) orqali va ikki xil servis bilan ishlaydi: tez `FakeServices` va **HAQIQIY pipeline servislari** (`tests/e2e/real_world.py` — `test_real_contract.py` dagi seeded SQLite + boshqariladigan soat). Haqiqiy rejimda avtomatik hisob-kitob o'chirilgan: natijalar faqat qo'lda belgilanadi, shuning uchun har bir stavkaning natijasi oldindan ma'lum.
- **Doira:** `git diff 63c8035..HEAD -- src/app/bot src/app/services tests/bot tests/e2e` ning T5 bot qismi. Ishlab chiqarish kodi (`src/`) **o'zgartirilmadi**; xatolar `xfail(strict=True)` testlari bilan qayd etildi.

> **Yangilanish (2026-10-23, bot-engineer):** F-1..F-10 va kuzatuvlar (`ValidationFailed` sababi, `edit_html` kesilishi) tuzatildi, qat'iy `xfail` testlar olib tashlandi. Qaror va oqibatlar: [ADR 0016](../adr/0016-t5-bot-fix-and-f8-ui.md); cashout qarori #18.

## Yakuniy xulosa

### ✅ Darvozadan o'tadi (shartli) — "Merge mumkin"

🔴 topilma **yo'q**: foydalanuvchilar izolyatsiyasi, whitelist, zarar limiti chegarasi, balans/foyda hisobi, ekspress ehtimollik arifmetikasi, hisobot davrlari (DST va yarim tun) va callback_data hajmi mustaqil oracle bilan tasdiqlandi. 4 ta 🟠 va 6 ta 🟡 topilma (hamda bir nechta kuzatuv) bor; topilmalarning hammasi `xfail(strict=True)` bilan qayd etilgan (tuzatilganda XPASS bo'lib yiqiladi va belgini olib tashlash kerak bo'ladi).

**Shartlar (merge dan keyin, T5 yopilishidan oldin):**
1. **F-2** (cashout birligi) — pul ko'rsatkichi noto'g'ri bo'lishi mumkin: jonli ishlatishdan oldin tuzatilsin yoki prompt matni aniqlashtirilsin.
2. **F-3** (CSV in'eksiya) — bir qatorlik tuzatish, `/eksport` ni ochishdan oldin.
3. **F-4** (ekspress, choraklik handikap) va **F-7** (20 tadan keyingi shablon/sevimlilar) — keyingi to'lqinda.
4. Jonli Telegram'da qo'lda sinov (token yo'qligi sababli) **bajarilmadi** — `docs/test_scenarios.md` 11–19 bo'limlari avtomatik testlar bilan qisman qoplangan, xolos.

## Topilmalar

### 🟠 F-2. Cashout "unit" da yoziladi, lekin o'zingizning stavkangiz bo'yicha ko'paytiriladi
- **Joy:** `src/app/pipeline/settle.py:356-358` (`_bet_profit`: `stake * profit_units / signal.stake_units`), prompt `texts.py` `BANK_CASHOUT_PROMPT` ("natijasini **unit** da yozing").
- **Ssenariy:** unit = 100, stavka qo'lda 250 (signalning tavsiya etilgan stavkasi 1 unit), cashout `0.8` yoziladi. Foydalanuvchi "+0.8 unit = +80" kutadi, bank **+200** yozadi (250·0.8/1). Tavsiya etilgan stavka bilan (100) natija to'g'ri (+80), shuning uchun xato faqat qo'lda kiritilgan summalarda chiqadi. Bitta signalda ikki xil summali stavka bo'lsa, bitta raqam ikkalasi uchun ham to'g'ri bo'la olmaydi.
- **Dalil:** `test_qa_t5_money_e2e.py::test_cashout_units_mean_the_users_unit_whatever_the_stake` (xfail; `--runxfail`: `200.0000 == 80`); nazorat: `test_cashout_of_a_default_stake_bet_is_units_times_the_unit` o'tadi.
- **Tuzatish:** (a) prompt "…tavsiya etilgan stavkaga nisbatan unit" desin, yoki (b) cashout stavka **summasi** bo'yicha so'ralsin (`ResultMarkRequest.cashout_amount`, shartnomaga qo'shimcha) va foyda = summa − stavka.

### 🟠 F-3. CSV eksportida formula in'eksiyasi
- **Joy:** `src/app/services/report_math.py:215-262` (`export_row`, `export_file`), `:265-` (`bets_export_file`).
- **Ssenariy:** liga/jamoa nomi API-provayderdan keladi. `=HYPERLINK(...)`, `+cmd|…`, `-2+3`, `@SUM(…)` bilan boshlanuvchi nom Excel/Sheets da formula sifatida bajariladi (`/eksport` fayli foydalanuvchi o'z kompyuterida ochadi).
- **Dalil:** `test_signals_csv_neutralises_formula_cells[4]`, `test_bets_csv_neutralises_formula_cells[4]` (xfail). Nazorat: `test_csv_keeps_numbers_numeric_and_round_trips_awkward_text` — tuzatish `-50` kabi raqamlarni matnga aylantirmasligi shart (faqat matn ustunlari: liga, jamoalar, fixture).
- **Tuzatish:** matn ustunlarida `=+-@\t\r` bilan boshlansa oldiga `'` qo'yish. Bundan tashqari UTF-8 BOM yo'q: Excel o'zbekcha apostrof/kirill nomlarni buzishi mumkin (🟡).

### 🟠 F-4. Ekspress EV choraklik Asian handikapda noto'g'ri
- **Joy:** `src/app/services/express_math.py:65-76` (`probability = prod(p)`, `ev = P·O − 1`).
- **Ssenariy:** v1.5 shartnomasi: choraklik tanlovda yarim yutuq/yarim yutqazish bor, `ev ≠ p·odds − 1`, `zero_ev_odds ≠ 1/p`. Ekspress shunday oyoqni oddiy ikkilik deb narxlaydi: p=0.5, koef. 2.0, `ev=−10%` oyoq + EV 0% oyoq → to'g'ri ekspress EV = (1−0.10)(1+0)−1 = **−10%**, bot **0%** ko'rsatadi (qiymatsiz ekspressni qiymatli deb ko'rsatadi).
- **Dalil:** `test_qa_t5_express_e2e.py::test_a_quarter_line_leg_contributes_its_own_ev` (xfail). Oddiy (ikkilik) oyoqlar uchun arifmetika mustaqil oracle (`Fraction`/`Decimal`) va hypothesis bilan tasdiqlandi — pastga qarang.
- **Tuzatish:** mustaqil oyoqlar uchun EV = Π(1+evᵢ) − 1 (ikkilik oyoqda `P·O − 1` bilan bir xil, umumiy holatda ham aniq). `fair_odds` ni ham shunga moslang.

### 🟠 F-7. 20 tadan keyingi sevimli va shablonlar bot orqali ochilmaydi/o'chirilmaydi
- **Joy:** `watch_ui.py:24,76,106` (`MAX_LISTED_ITEMS = 20`), `template_ui.py:28,79,108` (`MAX_LISTED_TEMPLATES = 20`).
- **Ssenariy:** 24 jamoa kuzatilsa yoki 25 shablon saqlansa, ro'yxatda faqat 20 tugma bor; qolganlarini ochib bo'lmaydi, bildirishnoma bayroqlarini o'zgartirib yoki o'chirib bo'lmaydi ("…va yana N ta" yozuvi bor, lekin yo'l yo'q). Servis cheklov qo'ymaydi.
- **Dalil:** `test_every_followed_team_can_be_opened_from_the_list` (20 ≠ 24), `test_every_saved_template_has_a_button` (20 ≠ 25) — xfail.
- **Tuzatish:** sahifalash (◀️ ▶️) yoki servisda soni cheklash (masalan, 20) va chegarada aniq xabar.

### 🟡 F-1. Restartdan keyin "running" holati osilib qoladi (persistent FSM)
- **Joy:** `handlers/custom.py:278` (`open_custom`), `main.py` (startda tozalash yo'q).
- **Ssenariy:** 🔍 tahlil ishlayotganda bot qayta ishga tushsa, SQLite FSM `CustomFlow:running` ni saqlab qoladi, fon vazifasi esa o'lgan. 🔍 tugmasi abadiy "allaqachon bajarilmoqda" deydi; chiqish yo'li — `/start` yoki `/bekor` (foydalanuvchiga aytilmaydi). Wizardda ham eski ▶️ "allaqachon bajarilmoqda" deydi, lekin 📊 menyusi tiklaydi (nazorat testi o'tadi).
- **Dalil:** `test_a_dead_custom_run_does_not_lock_the_menu_after_a_restart` (xfail), `test_a_dead_wizard_run_is_recoverable_from_the_menu` (o'tadi).
- **Tuzatish:** startda `*:running` holatlarni tozalash (yoki holat bilan birga vaqt belgisi) va `CUSTOM_ALREADY_RUNNING` matniga "/bekor" ni qo'shish.

### 🟡 F-5. ⭐ Sevimlilar, `/kuzat` va ⚙️ Sozlamalar ishlayotgan tahlilning himoyasini o'chiradi
- **Joy:** `handlers/watch.py:71-76` (`_leave` → `set_state(None)`), sozlamalar ham shunday.
- **Ssenariy:** tahlil ishlayotganda shu menyularni ochish FSM holatini `None` ga qaytaradi; botdagi "allaqachon bajarilmoqda" himoyasi yo'qoladi. Haqiqiy servisda ikkinchi tahlilni `BusyError` to'xtatadi, shuning uchun API sarfi ikki hissa bo'lmaydi — faqat bot himoyasi yolg'on.
- **Dalil:** `test_opening_another_screen_*[⭐ Sevimlilar / ⚙️ Sozlamalar / /kuzat]` (xfail, 2 × 3). Qolgan 8 kirish nuqtasi (shablonlar, hisobot, bank, bugun, `/ekspress`, `/bank`, `/hisobot`, `/holat`) holatni saqlaydi.
- **Tuzatish:** `_leave` faqat o'z holatlarini (`WatchFlow.*`) tozalasin.

### 🟡 F-6. Bank ekrani "Foyda/zarar" va "Yopilgan" faqat oxirgi 100 ta stavkani sanaydi
- **Joy:** `handlers/bank.py:54,68`, `bank_ui.py:summarize`; `texts.BANK_RECENT` hech qaerda ishlatilmaydi.
- **Ssenariy:** 105 ta yopilgan stavka (+1 dan): Balans 1 105 (to'g'ri, barchasi bo'yicha), "Foyda/zarar" **+100**, "Yopilgan: 100 ta". Raqamlar bir ekranda mos kelmaydi va sababi yozilmaydi. Shuningdek `_open_bet` (`bank.py:71`) 100 tadan keyingi ochiq stavkani topmaydi.
- **Dalil:** `test_overview_profit_line_covers_every_bet_not_just_the_latest_hundred` (xfail).
- **Tuzatish:** jami hisobni servisdan (`BankView`) olish (mantiq botda takrorlanmasin — CLAUDE.md "takrorlash yo'q") yoki matnga `BANK_RECENT` ni qo'shish.

### 🟡 F-8. Liga nomida `$…$` bo'lsa grafik yiqiladi
- **Joy:** `charts.py:165` (`axes.barh(names…)`) — nom matplotlib mathtext sifatida o'qiladi.
- **Dalil:** `test_a_league_name_with_dollar_signs_still_draws` (xfail; `ParseFatalException`). Kirill, xitoycha, emoji va 300 belgili nomlar yiqilmaydi (glyph ogohlantirishi xolos). Haqiqiy API nomlarida `$` kam uchraydi.
- **Tuzatish:** yorliqlarda `$` ni `\$` bilan almashtirish.

### 🟡 F-9. Sevimlilar tugmasi `crc32` bilan: to'qnashuvda birinchi yozuv tanlanadi
- **Joy:** `watch_ui.py:entity_key`, `find_item`. Ehtimol ~1/4·10⁹ juftga, lekin oson yasaladi (`team:29685295` ≠ `team:32060020` bir xil crc). **Dalil:** `test_two_entities_with_the_same_crc_are_told_apart` (xfail). **Tuzatish:** tugmada indeks + crc, yoki ikkalasi to'qnashsa indeks.

### 🟡 F-10. Juda kichik zarar "-0" ko'rinadi
- **Joy:** `formatters.py:money` (`f"{v:,.2f}"` + `removesuffix(".00")`): `-0.004 → "-0"`. Cashout stavkaga moslab kasr hosil qiladi. **Dalil:** `test_a_loss_smaller_than_a_cent_is_shown_as_plain_zero` (xfail). **Tuzatish:** kvantlangan qiymat nolga teng bo'lsa `"0"`.

### 🟡 Qolgan kuzatuvlar (test bilan qayd etilmagan)
- `handlers/bank.py:169,226`: har qanday `ValidationFailedError` "O'yin hali boshlanmagan" deb tushuntiriladi (retro yoki bekor qilingan signal ham shunday).
- `messaging.edit_html` uzun matnning faqat birinchi qismini yuboradi (ro'yxat 4096 dan oshsa qolgani indamay kesiladi); 20 ta shablon ro'yxati realda sig'adi, lekin hostile nomlarda emas (`split_message(...)[0]` yaroqli HTML ekani tekshirildi).
- Zarar limiti: avval yuborilgan "Stavka qildim" so'rovlari limitga yetilgandan keyin ham ishlaydi (`bets.py` `ok` tugmasi limitni qayta tekshirmaydi); qaror foydalanuvchiniki (ADR 0010 §5), lekin tasdiqlangan ogohlantirish chetlab o'tiladi. Natija xabarida `BET_LOSS_LIMIT` ogohlantirishi bor.
- `handlers/custom.py` (`resolve_text`, `_two_team_fixtures`): matnni jamoa/o'yinga aylantirish evristikasi (10+ katalog chaqiruvi) handlerda — servis qatlamiga ko'chirish maqsadga muvofiq ("yupqa handler" talabi). Katalog lokal bazadan o'qiydi, API sarfi yo'q; rate limit (8 ta burst, 1/s) bunga ham ta'sir qiladi (test bilan tasdiqlandi).
- Jadvalning avtomatik ishga tushishi hali ulanmagan — matn buni halol aytadi (`TPL_SCHEDULE_NOTE`, tekshirildi). "Tez orada" kabi qoldiq matn yo'q.

## Mezonlar (tekshiruv ro'yxati)

| # | Mezon | Holat | Dalil |
|---|---|---|---|
| 1a | Soxta `callback_data` bilan boshqa foydalanuvchi ma'lumotiga kirish (stavka, shablon, sevimli, signal, hisobot, eksport) | ✅ | `test_forged_callbacks_of_another_user_change_nothing_and_leak_nothing`: 33 ta tugma B dan A ning ob'ektlariga; A ning stavkasi/shablon jadvali/bayroqlari/signal holati o'zgarmadi, javoblarda A ning nomi, summasi, jamoasi yo'q; B da hech narsa yaratilmadi. Nazorat: A ning o'zi bosganda ishlaydi |
| 1b | Tugmalar JORIY foydalanuvchi ma'lumotiga qarshi qayta tekshiriladi | ✅ | Stavka/shablon/signal — `NotFoundError`; sevimli — "endi yo'q"; ekspress/maxsus/jadval — `StaleDraftError` (draft foydalanuvchining o'zida) |
| 1c | Whitelist va faqat shaxsiy chat yangi routerlarda | ✅ | Begona: 13 matn/buyruq + 33 tugma → **0 API chaqiruv**; admin guruhi → 0 |
| 1d | Oddiy matn → jamoa qidiruvi: begona nima qila oladi, rate limit | ✅ | Begonaning matni katalogga yetmaydi (spy); admin: 60 matn → katalog chaqiruvlari ≤ burst·12; barcha menyu yorliqlari qidiruvga tushmaydi |
| 2a | Zarar limiti aniq chegarada (kunlik) | ✅ | 99/100 → ruxsat, 100/100 → to'xtatish ("bugun 100"); `>=` |
| 2b | Foydalanuvchi mintaqasida kun/hafta (New York, Tashkent, Auckland) | ✅ | 6 parametrli test: 03:30 UTC = NY da kecha; yakshanba 23:30 NY = dushanba Toshkentda |
| 2c | Balans = boshlang'ich bank + yopilgan P/L; ochiq stavka tegmaydi | ✅ | 1000 +15 −7 → 1 008; foyda +8; yield 8/17 = +47.1% (void hisobga olinmaydi) |
| 2d | Ikki marta bosish | ✅ | Natija tugmasi 2× → bank +10 (20 emas); "✅ Yozish" 2× parallel → 1 stavka |
| 2e | Qo'lda belgi faqat mo'ljallangan stavkalarga | ✅ | Boshqa signaldagi stavka ochiq qoladi; bir signaldagi ikki stavka o'z koeffitsiyenti bilan (10·1.0, 30·0.5) yopiladi va ekran buni oldindan aytadi |
| 2f | Cashout birligi | ❌ | F-2 |
| 2g | Decimal formatlash / yaxlitlash | ✅ (🟡 F-10) | `-0` istisno |
| 3a | Hisobot davrlari foydalanuvchi mintaqasida | ✅ | 7 mintaqa (Toshkent, Auckland, New York, London, Kiritimati +14, Pago Pago −11, Lord Howe) × har bir kun: soni mustaqil oracle (`zoneinfo`) bilan teng, kunlar yig'indisi = jami; barcha IANA mintaqalar × 366 kun: `utc_bounds` bo'shliqsiz/ustma-ustsiz |
| 3b | Retro chiqarilgan, yashirin kiritilgan | ✅ | hisobotda ham, `signals.csv` da `hidden=True` bilan ham |
| 3c | CSV in'eksiya | ❌ | F-3 |
| 3d | PNG oqimda, headless, glyph | ✅ | `to_thread` (ish oqimi nomi asosiy oqimdan farqli), subprocess: `pyplot` import qilinmaydi, DISPLAY yo'q; bizning matnlar uchun ogohlantirishlar xato sifatida yoqilgan — o'tadi; 3000 nuqta + 60 bar < 20 s |
| 3e | Chet nomlar | ✅ / ❌ | kirill, xitoycha, emoji, 300 belgi yiqilmaydi; `$…$` yiqiladi (F-8) |
| 4a | Ekspress arifmetikasi (3×70% ≈ 34%) | ✅ | 0.7³ = 0.343 (aniq), koef. 1.5·1.6·1.7 = 4.08 (`Decimal`), EV +39.944%, fair 2.9155; hypothesis: P=ΠP, O=ΠO (Fraction), P ≤ min, tartibga bog'liq emas |
| 4b | Bog'liq oyoqlar rad etiladi | ✅ | bir o'yin ikki bozori: botda ogohlantirish, servisda `correlated`; qayta bir xil signal; boshlangan o'yin |
| 4c | MIN/MAX oyoq | ✅ | 0,1 rad; 2–4 ok; 5,6 rad; botda 5-chi tugma ogohlantirish; hisoblash <2 da ogohlantirish |
| 4d | Choraklik handikap | ❌ | F-4 |
| 5a | Restartdan keyin eskirgan tugmalar | ✅ (🟡 F-1) | 7 oqim tugmasi → "eskirgan"; persistent SQLite FSM restartdan keyin ekspress tanlovi davom etadi |
| 5b | Orqaga/bekor | ✅ | maxsus, ekspress, sevimlilar, hisobot kirishi → holat `None` |
| 5c | Parallel bosishlar | ✅ | ekspress/maxsus tanlovlari ikkalasi ham saqlanadi; sevimlini 2× olib tashlash — 1 marta |
| 5d | `callback_data` ≤ 64 bayt (adversarial) | ✅ | 12 factory eng katta qiymatlar (2⁶³−1 va h.k.) + `entity_key` har doim 8 hex (emoji, `:`, 64 belgi); hostile nomli klaviaturalar |
| 5e | 4096 bo'lish, HTML buzilmaydi | ✅ | sevimlilar (20 × `&`/`<`/`Ä`) va shablonlar ro'yxati qismlari to'g'ri HTML |
| 5f | Jadval tahriri validatsiyasi | ✅ | 10 xato vaqt (`25:00`, `24:00`, `9`, `09:60`, …) qabul qilinmaydi, so'rov ochiq qoladi, hech narsa saqlanmaydi; `09:00`, `0:00`, `23:59` saqlanadi; kunsiz saqlash rad; 2× saqlash — 1 shablon |
| 5g | Sevimlilar dublikat/limit | ✅ / ❌ | `/kuzat` 2× (turli registr) → 1 yozuv; limit — F-7 |
| 6 | Matnlar o'zbekcha, izchil | ✅ | barcha `texts` konstantalarida faqat ASCII apostrof; "tez orada/TODO" yo'q; jadval ogohlantirishi halol |
| 7 | Kod sifati, yupqa handler, takrorlash | ⚠️ | F-6 (jami hisob botda), `resolve_text` handlerda; boshqa jihatlar toza (`ruff`, `mypy`, `lint-imports` yashil) |

## Yangi testlar

| Fayl | Mazmuni |
|---|---|
| `tests/e2e/real_world.py` | haqiqiy servislar ustidagi production dispatcher (`RealBot`) |
| `tests/e2e/test_qa_t5_money_e2e.py` | limit chegarasi, mintaqalar, balans/P&L, idempotentlik, cashout (17) |
| `tests/e2e/test_qa_t5_isolation_e2e.py` | soxta callbacklar, begona, guruh, matn qidiruvi, rate limit (8) |
| `tests/e2e/test_qa_t5_report_export_e2e.py` | davrlar, retro/yashirin, CSV, grafiklar (31) |
| `tests/e2e/test_qa_t5_express_e2e.py` | mustaqil oracle, hypothesis, bot (9) |
| `tests/e2e/test_qa_t5_flows_e2e.py` | himoya holatlar, restart, eskirgan tugmalar, orqaga/bekor, parallellik (38) |
| `tests/e2e/test_qa_t5_lists_e2e.py` | jadval tahriri, sevimlilar, callback hajmi, bo'lish, matnlar (44) |

Sekin testlar `@pytest.mark.slow` bilan belgilangan (haqiqiy servislar).

## Tekshiruvlar

Natijalar pastda "Darvoza" bo'limida.

## Darvoza (main bilan birlashtirilgandan keyin)

| Buyruq | Natija |
|---|---|
| `python -m uv run pytest` | `3459 passed, 8 skipped, 23 xfailed` (1406 s), umumiy qamrov **98.33%** (`fail_under=85`) |
| `python -m uv run ruff check .` | `All checks passed!` |
| `python -m uv run ruff format --check .` | `401 files already formatted` |
| `python -m uv run mypy src` | `Success: no issues found in 185 source files` |
| `python -m uv run lint-imports` | `Contracts: 6 kept, 0 broken.` |

23 xfail — hammasi shu hisobotdagi topilmalar (F-1, F-2, F-3, F-4, F-5, F-6, F-7, F-8, F-9, F-10); tuzatilganda strict XPASS bo'lib yiqiladi. Jonli Telegram'da qo'lda sinov bajarilmadi (token yo'q).
