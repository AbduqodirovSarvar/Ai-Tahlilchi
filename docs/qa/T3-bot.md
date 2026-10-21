# QA hisoboti: T3 — Telegram bot MVP (F6.1–F6.6, F6.10)

- **Tekshiruvchi:** test-engineer (mustaqil QA)
- **Sana:** 2026-10-21
- **Branch:** `wave/T3-qa-bot` (`main` `e8894e4` dan); QA testlari: `8af956d`
- **Muhit:** Windows 11, Python 3.13.15, aiogram 3.31.0. Tarmoq yo'q: Telegram `RecordingSession`/`YieldingSession` bilan, servislar `create_fake_services(clock=fixed_clock)`, soat qotirilgan (chorshanba 21.10.2026 11:00 Toshkent, `horizon_end` = 03.11.2026).
- **Yondashuv:** barcha testlar **haqiqiy dispatcher** (`build_dispatcher`: whitelist, rate limit, error middleware, barcha routerlar) orqali, foydalanuvchi ko'radigan natijani (`texts.py` dagi aniq matnlar, klaviaturalar, FSM holati) tekshiradi. Kod ichki funksiyalari mock qilinmaydi; faqat servis xatolarini hosil qilish uchun `analyze`/`hide_signal` almashtiriladi.

## Yakuniy xulosa

### ✅ Darvozadan o'tadi (shartli)

🔴 topilma yo'q. Bot MVP funksional mezonlari avtomatik testlarda tasdiqlandi: wizard 18 ekrani, 16 sozlama, `/tahlil`, kartalar, kunlar bo'yicha guruhlash, sahifalash, whitelist va Telegram cheklovlari. 3 ta 🟠 va 5 ta 🟡 topilma `xfail(strict=True)` testlari bilan qayd etildi: tuzatilganda test XPASS bo'lib yiqiladi va belgini olib tashlash kerak bo'ladi.

**Shartlar:**
1. **🟠 F-1** (▶️ ni ikki marta bosish tahlilni ikki marta ishga tushiradi) **T4 dan oldin** (`APP_SERVICES=real`) yopilishi shart: real servisda bu API limitini ikki barobar sarflaydi. Tuzatish bitta qator (pastda).
2. F6 rasmiy qabul mezoni — `docs/test_scenarios.md` bo'yicha **jonli Telegram'da qo'lda sinov** — token yo'qligi sababli **bajarilmadi**. Avtomatik e2e testlar ssenariylarning 0, 1, 2, 3, 4, 5, 6-bo'limlarini qamraydi, lekin jonli sinov (tarmoq kechikishi, haqiqiy Telegram HTML parseri) o'rnini to'liq bosa olmaydi.

## bot-engineer da'volarining tekshiruvi

| Da'vo | Natija | Dalil |
|---|---|---|
| 16 ta sozlama maydonining hammasi tahrirlanadi | ✅ | `test_every_settings_field_is_editable`: 25 holat, 16/16 maydon (preset, ✏️ kiritish, 🚫 o'chirish); har biri servisda saqlangani `get_settings` bilan tekshiriladi. Saqlangan qiymat keyingi wizard va `/tahlil` ga o'tadi |
| Wizardning har qadamida ⬅️ Orqaga va ❌ Bekor qilish | ✅ | `test_every_step_has_working_back_and_cancel`: 18/18 ekran. Orqaga → reja §6.2 dagi oldingi qadam (FSM holati bilan), Bekor → `CANCELLED`, holat `None`, klaviatura olib tashlanadi |
| Kalendar `horizon_end` bilan chegaralangan | ✅ | Noyabrda faqat 1–3 bosiladi, 4+ "·"; dekabrga ▶️ yo'q. Oy chegarasidan o'tuvchi oraliq 30.10–02.11 → `DateWindow(30.10, 02.11)` servisga yetadi. 90 kundan eski kunlar ham inert |
| Taxminiy API sarfi; limitdan oshsa ▶️ yo'q | ✅ | Tasdiqlash ekranida "Taxminiy API sarfi … (zaxirada N)"; `within_budget=False` yoki 0 o'yin → ▶️ yo'q, ✏️ bor (`wizard.py:418`, mavjud test + qayta ko'rib chiqildi) |
| Progress ≥ 1.5 s oraliqda tahrirlanadi | ✅ | Dispatcher orqali: 20 ta progress 0.4 s qadam bilan → tahrirlar aynan 1.6/3.2/4.8/6.4/8.0 s da + yakuniy "✅ Tahlil tugadi" (`test_progress_edits_are_throttled_through_the_dispatcher`) |
| `/tahlil` istalgan tartib, aliaslar, hech qachon yiqilmaydi | ✅ | Mavjud hypothesis testlari qayta ishga tushirildi; e2e: HTML in'eksiyali buyruqlar bitta to'g'ri HTML xato xabari beradi. Istisno: yilsiz teskari oraliq xabari (🟡 F-6) |
| Kartalar §6.2 bo'yicha, escape va bayroqlar bilan | ✅ | Haqiqiy natija kartalari qator-qator mustaqil oracle bilan (`SignalView` dan hisoblangan): sarlavha, tanlov @ koeff., ehtimollik (adolatli koeff. = 1/p), EV + ishonch, tavsiya; "⏳ Dastlabki" aynan `kickoff − now > 3 kun` bo'lganda. Retro kartada 🕰 va natija, ✅ Stavka/🔄 yo'q |
| Kunlar foydalanuvchi vaqt mintaqasida | ✅ | Mintaqa `Pacific/Auckland` ga o'zgartirildi (bot orqali): barcha kunlar sarlavhasi, hafta kuni, son va karta vaqti Auckland bo'yicha; Toshkentdan farqli sana borligi tekshiriladi (test ma'noli) |
| Sevimlilar bloki `shortfall`/sondan tashqarida | ✅ | Arsenal kuzatuvda, `2ta`: oddiy signallar ≤ 2, sarlavhadagi son faqat oddiylar, "⭐ Sevimlilar — N ta" bloki oxirida, undan keyin kun sarlavhasi yo'q; faqat sevimli bo'lsa "Value topilmadi" + ⭐ izoh, shortfall matni yo'q |
| Sahifalash | ✅ / ❌ | Barcha signal aynan bir martadan, "Ko'rsatildi: 5k/N", "(davomi)", oxirida "✅ Hammasi ko'rsatildi"; ➡️ o'zini o'chiradi. **Lekin** eski ➡️ yangi natijani varaqlaydi (🟠 F-5) |
| 4096 bo'yicha bo'lish | ✅ / ❌ | 100+ kartali natija (har sahifa, har xabar) va barcha ekranlar Telegram HTML validatoridan o'tadi. **Lekin** bitta > 4096 belgili qator entity o'rtasidan kesiladi (🟡 F-3) |
| Har bir klaviaturada `callback_data` ≤ 64 bayt | ✅ | Crawler: 18 wizard ekrani + kalendar tugash bosqichi + o'yinlar 2-sahifasi + natija kartalari + ➡️ + sozlamalar ko'rinishi va 16 muharrir — **har bir tugma** bosiladi va baytlari o'lchanadi |
| Har callbackga aniq bitta `answer` | ❌ | Wizard, kartalar, natija, sozlamalar (13/16 muharrir) — 1 ta. **Sozlamalardagi ko'p tanlovli maydonlar (sport, liga, bozor) 2 marta javob beradi** (🟡 F-4) |
| Eskirgan tugma xabari | ✅ / ❌ | Restart, bekor qilingan wizard, eski wizard xabari, boshqa foydalanuvchi tugmasi → "⌛ Bu tugma eskirgan". **Lekin** F-5 (eski ➡️) va F-2 (kutilmagan xatodan keyin karta tugmalari "eskirgan") |
| Flood control: bitta qayta urinish | ✅ | Dispatcher + `install_flood_control`: birinchi `sendMessage` 429 (retry 2 s) → `sleep(2.0)` bir marta, foydalanuvchi xush kelibsiz matnini oladi. Ikkinchi 429 / > 60 s — mavjud unit testlar |
| Catch-all router bilan whitelist va faqat shaxsiy chat | ✅ | Begona foydalanuvchi va admin guruh/superguruh/kanalda: `/start`, menyu, matn, 6 xil tugma (shu jumladan noma'lum `garbage-data`) → **0 ta API chaqiruv** (24 holat). Admin chatiga soxta begona callback → 0 chaqiruv, holat o'zgarmaydi |
| ▶️ ni ikki marta bosish — tahlil bir marta | ❌ | Ketma-ket ishlov berilganda ✅. **Bir vaqtda** (aiogram polling `handle_as_tasks=True` standart) — tahlil **2 marta** (🟠 F-1). Tahlil boshlangandan keyin kelgan bosish "⏳ allaqachon bajarilmoqda" oladi ✅ |
| Ikki foydalanuvchi bir vaqtda wizardda — holat aralashmaydi | ✅ | `YieldingSession` (har API chaqiruvda event loop'ga qaytadi) bilan ikki admin parallel: so'rovlar, qamrov va natijalar alohida; §6.6 ogohlantirishi har foydalanuvchiga alohida |

## F6 qabul mezonlari (shu to'lqin doirasi)

| ID | Mezon | Holat | Dalil |
|---|---|---|---|
| F6.1 | Karkas: routerlar, whitelist, rate limit, xato ushlash, `user_id` | ✅ | Whitelist/guruh e2e (24 holat); servis xatolari → o'zbekcha xabar (pastda); rate limit — mavjud testlar |
| F6.2 | Menyu, `/start`, `/yordam` | ✅ | Mavjud testlar + barcha menyu/noma'lum matn javoblari to'g'ri HTML |
| F6.3 | Sozlamalar: barcha parametrlar, risk profillari, 80%+ ogohlantirishi | ✅ (🟡 F-4, F-7) | 16/16 maydon; Konservativ → 60%/3%/1.30–2.00 |
| F6.4 | Wizard: sana (tugma + kalendar + matn), sport, ehtimollik, EV, qamrov (liga/jamoa/aniq o'yin, sahifalangan), son, bozor, saralash, tasdiqlash + API sarfi | ✅ (🟠 F-1, F-2) | 5 ta sana rejimi natijagacha; kalendar oy chegarasidan o'tib; 3 qamrov servisga to'g'ri `scope_ids` bilan; o'yinlar 1-va 2-sahifadan tanlanib saqlanadi; `MAX_SCOPE_IDS`=50 chegarasi (51-chi → alert) |
| F6.5 | `/tahlil` parseri | ✅ (🟡 F-6) | |
| F6.6 | Karta, kunlar, sahifalash, "Value topilmadi" | ✅ (🟠 F-5) | |
| F6.10 | 4096, flood control, `callback_data` ≤ 64 | ✅ (🟡 F-3) | |
| F6 qabul | §6.2, 6.4, 6.5, 6.6, 6.12 oqimlari **qo'lda** sinovdan o'tadi | ⏸ | Jonli Telegram token yo'q — bajarilmadi |

### Noto'g'ri kiritma: xato aniq, qadam saqlanadi

| Qadam | Kiritma | Foydalanuvchi ko'radi | Holat |
|---|---|---|---|
| Sana | `31.02` | `«31.02» — bunday sana yo'q…` | `dates`, klaviatura ishlaydi |
| Sana | `salom` | `«salom» ni sana oralig'i sifatida tushunmadim…` | `dates` |
| Sana | `21.10 - 04.11` (bugun+14) | `Oraliq juda uzoqda: … 14 kun…` | `dates`; `21.10 - 03.11` qabul qilinadi |
| Sana | `01.08 - 15.09` (retro, 46 kun) | `Oraliq juda uzun: ko'pi bilan 31 kun.` | `dates` |
| Kalendar | 60 kun oldin → bugun | alert `Oraliq juda uzun…` | `calendar`, boshlanish saqlanadi; 31 kunlik tugash qabul qilinadi |
| Ehtimollik ✏️ | `150%`, `29` | `«150%» noto'g'ri: 30–95% oralig'ida…` | `probability_input`; `65` → EV qadami |
| EV ✏️ | `-3`, `21%`, `ko'p` | `«-3» noto'g'ri: 0–20%…`, `«ko'p» son emas…` | `ev_input`; `12` → qamrov |
| Son ✏️ | `101` | `«101» noto'g'ri: 1–100…` | `count_input` |
| Shablon nomi | 65 belgi, faqat bo'shliq | `💾 Shablon nomi 1–64 belgidan…` | `template_name` |
| Retro 14.10–20.10 | — | Tasdiqlashda "🕰 retro-tahlil", kartalarda 🕰 + `Natija: ✅/❌` | — |

### Servis xatolari → o'zbekcha xabar (▶️ paytida va karta tugmasida)

| Xato | Xabar | ▶️ dan keyin |
|---|---|---|
| `CooldownActiveError(2 daq 5 s)` | `⏳ 3 daqiqadan keyin qayta urinib ko'ring.` (yuqoriga yaxlitlanadi) | progress "❌ Bekor qilindi", tasdiqlash ekrani ▶️ bilan qaytadi, holat `confirm` |
| `BudgetExceededError(45, 30)` | `💸 API limiti yetmaydi: … 45 ta so'rov kerak, … 30 ta qoldi…` | xuddi shunday |
| `ValidationFailedError("date_to")` | maydon xabari (oyna ≤ 31 kun, ≤ 14 kun oldinga) | xuddi shunday |
| `ValidationFailedError(<noma'lum maydon>)` | `❗ Noto'g'ri qiymat…` | xuddi shunday |
| `NotFoundError` | `🔎 Topilmadi: ma'lumot o'chirilgan yoki eskirgan…` | xuddi shunday |
| Kutilmagan (`RuntimeError`) | `⚠️ Kutilmagan xatolik…` (ichki matn sizmaydi) | ❌ holat `running` da qoladi, progress osilib qoladi (🟠 F-2) |

Karta tugmasida (🙈) qisqa xabar — alert (HTML teglarisiz), uzun (> 200) — oddiy xabar. Har holatda aniq bitta `answer`.

### HTML in'eksiyasi

`<b>x</b>&<i>`, `A & B <script>alert(1)</script>`, `Dam olish · APL · 60%+ <>&` — jamoa qidiruvi ("Topilmadi: «…»"), shablon nomi (`💾 Shablon saqlandi: <b>…</b>`), `/tahlil` aks-sadosi, menyu placeholder: hammasi escape qilingan, har bir yuborilgan/tahrirlangan matn Telegram HTML validatoridan o'tadi (ruxsat etilgan teglar, balans, `&` faqat entity, ochiq `<`/`>` yo'q). Shablon servisda asl nom bilan saqlanadi.

**HTML validator:** `tests/e2e/bot_client.py::telegram_html_problems` — `html.parser` asosida teg balansi (Telegram kabi yashirin yopishsiz) + entity/ochiq belgi regex'i + 4096 chegarasi. Bot har bir e2e testda shu validator bilan tekshiriladi.

## Test natijalari

| | Oldin | QA dan keyin |
|---|---|---|
| Butun to'plam | 1136 passed (T3 `main`) | **1303 passed, 11 xfailed** (strict) — 244.7 s |
| `tests/bot` + `tests/e2e` | 242 passed | **409 passed, 11 xfailed** (e2e: 167 + 11 xfail, ≈ 15 s) |
| `src/app/bot` qamrovi (branch) | 97% (45 stmt, 32 qisman branch) | **98%** (22 stmt, 23 qisman branch) |
| Umumiy qamrov | 99% | 99.27% (`fail_under=85` ✅) |

Qamrov o'sishi: QA testlari ilgari hech qachon bajarilmagan yo'llarni ochdi — `on_running` (tahlil paytidagi bosish), `MAX_SCOPE_IDS` alerti (`analysis.py:265-266`), servis xatosidan keyin tasdiqlashga qaytish. Qamralmagan qoldiq: sport ro'yxati o'zgarib qolgan eski tugma (`analysis.py:176`), noma'lum bozor kodi (`analysis.py:322`), Telegram xabarni bermagan callback (`session.py:60`) kabi deyarli imkonsiz yo'llar.

**Yangi testlar (`tests/e2e/`, 178 ta, ≈ 15 s):**
- `test_bot_wizard_e2e.py` — sana rejimlari, kalendar (oy chegarasi, bir kunlik, retro chegarasi), 3 qamrov, o'yinlar sahifalari, `MAX_SCOPE_IDS`, noto'g'ri kiritmalar, 18 ekranda Orqaga/Bekor.
- `test_bot_access_e2e.py` — whitelist/guruh/kanal (tugmalar bilan), soxta callback, bir vaqtdagi ▶️, tahlil paytidagi ▶️, ikki foydalanuvchi.
- `test_bot_buttons_e2e.py` — crawler: har bir tugma → 1 `answer`, ≤ 64 bayt, kutilmagan xato yo'q, to'g'ri HTML.
- `test_bot_errors_html_e2e.py` — 5 servis xatosi × (▶️, karta), kutilmagan xato, buzilgan draft, HTML in'eksiyasi, 4096 bo'lish.
- `test_bot_settings_results_e2e.py` — 16 sozlama, vaqt mintaqasi bo'yicha guruhlash, sahifalash to'liqligi, karta tartibi, sevimlilar, progress throttle, flood control.
- `test_bot_findings_e2e.py` — F-5…F-8.
- Yordamchilar: `bot_client.py` (`Client` — foydalanuvchi + chat, `YieldingSession`, `Snapshot` — FSM ni tiklash, HTML validator), `flows.py` (umumiy yo'llar).

**Barqarorlik:** `tests/e2e` 20 marta ketma-ket: 20/20 `167 passed, 11 xfailed`. Vaqt, soat, `monotonic` va tarmoq to'liq nazoratda; tasodif yo'q.

## Tekshiruvlar (to'liq darvoza)

| Buyruq | Natija |
|---|---|
| `python -m uv run pytest` | `1303 passed, 11 xfailed`, `Total coverage: 99.27%` |
| `python -m uv run python scripts/ops/coverage_gate.py` | `OK */app/models/*: 99.73% (min 90%)`, `SKIP */app/pipeline/*: no statements yet` |
| `python -m uv run ruff check .` | `All checks passed!` |
| `python -m uv run ruff format --check .` | `170 files already formatted` |
| `python -m uv run mypy src` | `Success: no issues found in 80 source files` |
| `python -m uv run lint-imports` | `Contracts: 6 kept, 0 broken.` |

## Topilmalar

### 🟠 F-1. ▶️ Boshlash ni ikki marta bosish tahlilni ikki marta ishga tushiradi (bir vaqtda ishlov berilganda)

- **Joy:** `src/app/bot/handlers/analysis_run.py:73-74` (`await session.answer()` → keyin `set_state(AnalysisFlow.running)`), `src/app/bot/handlers/analysis.py:393` (`on_confirm` `StateFilter(confirm)` bilan), `src/app/bot/dispatcher.py:61` (`events_isolation` berilmagan → `DisabledEventIsolation`), `src/app/main.py:77` (`start_polling`, `handle_as_tasks=True` standart).
- **Mexanizm:** ikki callback parallel task sifatida ishlaydi. Ikkalasi ham `StateFilter(confirm)` dan o'tadi, chunki birinchisi `running` ni faqat `answerCallbackQuery` tarmoq chaqiruvidan **keyin** qo'yadi. `on_running` himoyasi faqat tahlil allaqachon boshlangandan keyin kelgan bosishga ishlaydi (bu ✅ tasdiqlandi).
- **Takrorlash:** `pytest tests/e2e/test_bot_access_e2e.py::test_concurrent_double_tap_on_start_runs_analysis_once --runxfail` → `assert 2 == 1` (`analyze` 2 marta). Jonli: tasdiqlash ekranida ▶️ ni tez ikki marta bosish.
- **Ta'sir:** real servisda API limiti ikki barobar sarflanadi (qo'lda zaxira 30 so'rov), ikki marta progress va natija to'plami yuboriladi. Signal ID lari idempotent bo'lgani uchun bazada dublikat yo'q.
- **Tavsiya:** `Dispatcher(..., events_isolation=SimpleEventIsolation())` (aiogram: bir foydalanuvchi/chat updatelari ketma-ket) — bitta qator; yoki `running` holatini `answer()` dan oldin qo'yish. Ikkinchisi oynani faqat qisqartiradi, birinchisi yopadi.

### 🟠 F-2. Kutilmagan xato (`ServiceError` emas) foydalanuvchini `running` holatida qoldiradi

- **Joy:** `src/app/bot/handlers/analysis_run.py:85` (faqat `except ServiceError`), `src/app/bot/handlers/analysis.py:377` (`on_running` — callback filtri yo'q, `running` dagi **har qanday** tugmani ushlaydi).
- **Takrorlash:** natija oling → yangi wizard → `analyze` `RuntimeError` beradi → ▶️. Foydalanuvchi "⚠️ Kutilmagan xatolik" ni ko'radi, lekin: (1) progress xabari "⏳ Tahlil qilinmoqda… 0/…" abadiy qoladi; (2) eski natijadagi 🙈 Yashirish va ➡️ Yana "⌛ Bu tugma eskirgan" deydi — `/start`, `/bekor` yoki yangi 📊 Tahlil gacha. Testlar: `test_unexpected_error_does_not_leave_the_user_stuck`, `test_unexpected_error_closes_the_progress_message`.
- **Ta'sir:** T4 da real servis tarmoq/baza xatolarini (`httpx`, SQLAlchemy) `ServiceError` ga o'ramasa, bu tez-tez uchraydi. Shartnomada rejalashtirilgan `ServiceUnavailableError` ham `ServiceError` ostida bo'lishi kerak.
- **Tavsiya:** `try/finally` bilan progress xabarini yopish va holatni `confirm` ga (yoki `None`) qaytarish; `on_running` ni faqat wizard callbacklariga (`ConfirmCb`/`NavCb`…) cheklash.

### 🟠 F-5. Eski "➡️ Yana" tugmasi yangi tahlil natijasini varaqlaydi

- **Joy:** `src/app/bot/callbacks.py:99` (`ResultsCb` faqat `page`), `src/app/bot/handlers/signals.py:57-66` (oxirgi saqlangan natija bilan ishlaydi), `analysis_run.py` `save_results` (har tahlil ustidan yozadi).
- **Takrorlash:** `/tahlil 7kun futbol 30% ev0% 100ta` → ▶️ (➡️ Yana ni bosmang) → `/tahlil 7kun xokkey 30% ev0% 100ta` → ▶️ → birinchi natijadagi ➡️ Yana: **xokkey** kartalari keladi ("Ko'rsatildi: 10/N" xokkey soni bilan). Yangi natija qisqa bo'lsa — kartasiz "✅ Hammasi ko'rsatildi (3 ta signal)". Test: `test_old_more_button_does_not_page_through_a_newer_result`.
- **Ta'sir:** boshqa so'rov (boshqa chegaralar) kartalari eski so'rov davomi sifatida ko'rinadi; ADR 0006 dagi "eskirgan tugma" invarianti buziladi.
- **Tavsiya:** `ResultsCb` ga natija tokeni (masalan, `query_id` ning qisqa xeshi yoki tartib raqami) qo'shib, mos kelmasa "⌛ eskirgan" alerti.

### 🟡 F-3. `split_message` bitta uzun qatorni HTML entity o'rtasidan kesadi

- **Joy:** `src/app/bot/formatters.py:459-460` (qattiq kesish `rest[:limit]`); manba: `handlers/analysis.py:272-280` (topilmagan nomlar bitta qatorda).
- **Takrorlash:** jamoalar qadamida 360 ta nom: `&Q<000, &Q<001, …` (≈ 2900 belgi, Telegram ruxsat beradi). Escape'dan keyin "🔎 Topilmadi: …" qatori ≈ 6300 belgi → birinchi bo'lak `«&a` bilan tugaydi, ikkinchisi `mp;Q…` bilan boshlanadi → Telegram "can't parse entities" (validator: `stray '&' at 4094`). Test: `test_very_long_not_found_list_splits_into_valid_html`.
- **Ta'sir:** g'ayritabiiy kiritma kerak; xabar yuborilmaydi, foydalanuvchi "Kutilmagan xatolik" ni ko'radi. Sozlamalardagi "Ligalar" qatori real katalogda (o'nlab liga, `&` li nomlar) ham bitta qator — shu xavf ostida.
- **Tavsiya:** qattiq kesishda kesish nuqtasini oxirgi `&`/`<` dan oldinga surish (yoki bo'shliq/vergulda kesish); topilmagan nomlar ro'yxatini qisqartirish ("… va yana N ta").

### 🟡 F-4. Sozlamalarda ko'p tanlovli maydonlar callbackga ikki marta javob beradi

- **Joy:** `src/app/bot/handlers/settings.py:325` (`answer_callback(callback, SETTINGS_SAVED)`) va `:332` (ko'p tanlovli maydonda yana `answer_callback(callback)`).
- **Takrorlash:** ⚙️ → 🏅 Sportlar → 🏒 Xokkey: 2 ta `answerCallbackQuery` ("✅ Saqlandi" va bo'sh). Crawler testi: `test_every_settings_editor_button_answers_once[sports|leagues|markets]`.
- **Ta'sir:** ikkinchi javobni Telegram rad etadi (yutiladi) — foydalanuvchi sezmaydi, lekin har bosishda ortiqcha API chaqiruvi (flood limitiga hisoblanadi) va "har callbackga aniq bitta answer" da'vosi buzilgan. Mavjud `test_toggle_sports_and_markets` javoblar sonini sanamagan.
- **Tavsiya:** `action != "open"` tarmog'ida ko'p tanlovli maydon uchun `return` dan oldin muharrirni chizish (bo'sh `answer` siz).

### 🟡 F-6. Yilsiz teskari oraliq "juda uzun" deb tushuntiriladi

- **Joy:** `src/app/bot/parsing.py:260-261` (tugash boshlanishdan oldin bo'lsa, keyingi yilga o'tkaziladi).
- **Takrorlash:** sana qadamida `25.10 - 24.10` → "Oraliq juda uzun: ko'pi bilan 31 kun." (kutilgan: "Tugash sanasi boshlanishdan oldin bo'lishi mumkin emas."). Test: `test_reversed_typed_range_says_end_before_start`.
- **Tavsiya:** yilni o'tkazishni faqat natija ≤ `MAX_WINDOW_DAYS` bo'lsa qo'llash (28.12–03.01 holati saqlanadi).

### 🟡 F-7. Kasr foizlar butun songa yaxlitlab ko'rsatiladi

- **Joy:** `src/app/bot/formatters.py:53` (`percent(decimals=0)`), `wizard.py:387-388`, `formatters.py:368`.
- **Takrorlash:** ✏️ ehtimollik `62,5`, ✏️ EV `4,5` → tasdiqlash "≥ 62% · EV ≥ 4%" (Python banker's rounding: 4.5 → 4, 5.5 → 6), servisga esa 0.625/0.045 ketadi. Sozlamalar ko'rinishida ham "4%". Test: `test_typed_fractional_percent_is_shown_as_typed`.
- **Ta'sir:** foydalanuvchi kiritgan chegara noto'g'ri ko'rinadi (filtr to'g'ri ishlaydi).
- **Tavsiya:** butun bo'lmagan qiymatlar uchun bitta kasr belgisi (`:g` yoki `decimals=1` faqat kerak bo'lganda).

### 🟡 F-8. Wizard jamoa qadamida kiritilgan nomlar soni cheklanmagan

- **Joy:** `src/app/bot/handlers/analysis.py:272-274` → `wizard.py` `lookup_teams` (har nom uchun `search_teams`). `/tahlil` da esa `MAX_SCOPE_IDS` (50) va 300 belgi cheklovi bor.
- **Takrorlash:** bitta xabarda 300 ta nom → 300 ta `search_teams` chaqiruvi (test: `test_team_step_bounds_catalog_searches`, `assert 300 <= 50`).
- **Ta'sir:** real servisda fuzzy qidiruv katalog bo'yicha — bitta xabar sekin javob va bazaga yuklama (shaxsiy bot, lekin F-3 bilan birga uchraydi).
- **Tavsiya:** nomlar sonini `MAX_SCOPE_IDS − tanlanganlar` bilan cheklash va `texts.QUICK_TOO_MANY_TEAMS` kabi xabar.

### Kuzatuvlar (topilma emas)

- `/tahlil … hammasi` barcha `Sport` (mavjud bo'lmagan basketbol, voleybol, tennis ham) ni so'rovga qo'yadi; wizarddagi "🌐 Hammasi" esa faqat `available` sportlarni. Fake servis buni ko'taradi; real servis mavjud bo'lmagan sportni rad etsa, xato `ValidationFailedError("sports")` sifatida to'g'ri xabar beradi. pipeline-engineer bilan aniqlashtirish tavsiya etiladi.
- Matn bilan istalgan uzoq o'tmish oynasi (masalan, 2025-yil) kiritiladi, kalendar esa 90 kun bilan cheklangan. Shartnoma retro ni "har doim ruxsat" deydi — mos, lekin nomuvofiq UX.
- `on_results_page` har sahifada barcha saqlangan ID lar uchun `get_signal` chaqiradi (100 signal → 100 chaqiruv). Fake da sezilmaydi; real servisda sahifa ID lari bilan cheklash kerak.
- Retro kartada natija API dan kelmagan bo'lsa (fake: ≈ 15%), "Natija:" qatori umuman chiqmaydi — "natija hali yo'q" belgisi foydali bo'lardi.

## Barqarorlik

`python -m uv run pytest tests/e2e` **20 marta ketma-ket**: 20/20 bir xil natija — `167 passed, 11 xfailed`, 0 ta yiqilish, 0 ta XPASS. Bir vaqtdagi testlar (`asyncio.gather`, `YieldingSession`) deterministik: event loop navbati faqat `asyncio.sleep(0)` bilan boshqariladi, real kutish yo'q. Hech bir test `@pytest.mark.slow` talab qilmaydi (eng sekini < 0.5 s).

## Boshqa agentlarga yo'naltirish

- **bot-engineer:** F-1 (T4 dan oldin majburiy), F-2, F-5, F-3, F-4, F-6, F-7, F-8. Tuzatilganda tegishli `xfail` belgisini olib tashlang (strict: XPASS testni yiqitadi).
- **pipeline-engineer:** real servis kutilmagan infratuzilma xatolarini `ServiceError` (rejalashtirilgan `ServiceUnavailableError`) ga o'rashi kerak — aks holda F-2 tez-tez uchraydi. Mavjud bo'lmagan sport bilan `analyze`/`estimate_cost` semantikasi (kuzatuv 1).
