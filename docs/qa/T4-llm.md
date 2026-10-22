# QA va kod sharhi: T4 — LLM qatlami (F5.1–F5.6)

- **Tekshiruvchi:** code-reviewer + test-engineer (birlashtirilgan o'tish)
- **Sana:** 2026-10-22
- **Branch:** `wave/T4-qa-llm` (`main` `4eb97ea` dan; T4 merge `59d58ec`)
- **Ko'rilgan diff:** `git diff e378c40..59d58ec -- src tests scripts docs` (`gemini.py`, `app/llm/*`, promptlar, golden to'plam, `scripts/llm_eval.py`)
- **Muhit:** Windows 11, Python 3.13, `python -m uv run --no-config`, `google-genai 2.32.0` (`.venv` manbasi o'qildi). Tarmoq yo'q. Ishlab chiqarish kodi (`src/`, `scripts/`) **o'zgartirilmadi**.

## Yakuniy xulosa

### Darvozadan o'tadi, 5 ta 🟠 topilma bilan (🔴 yo'q)

Asosiy kafolatlar dalil bilan tasdiqlandi: LLM ehtimollik/EV/koeffitsientni o'zgartira olmaydi (sxemada raqamli maydon yo'q), `verdict` va `risk_flags` qat'iy enum, injection matni ma'lumot blokidan chiqa olmaydi (17 hujum satri × 3 kirish yo'li), `review_candidates` hech qachon istisno ko'tarmaydi va natija doim kirish bilan 1:1 va bir xil tartibda (hypothesis, 300 misol + 12 istisno turi), kalit loglarga tushmaydi. SDK chaqiruvi **haqiqiy** `google-genai` bilan (faqat HTTP transport almashtirilgan) ishlaydi: so'rov shakli, `response_schema`, `usage_metadata`, 429-qayta urinish, xato sinflari to'g'ri.

Merge'ni bloklaydigan narsa yo'q. Lekin quyidagi 🟠 lar F4 (pipeline LLM'ni chaqira boshlashi) dan **oldin** tuzatilishi kerak: F-1 (aiohttp tarmoq xatolari), F-3 (parallel chaqiruvda limit oshib ketishi), F-4 (gap boshidagi to'qilgan ism o'tib ketadi), F-8 (usage yo'q bo'lsa token limiti ishlamaydi), F-9 (post-mortem `reason` himoyasiz, F8 uni saboq sifatida keyingi promptga qaytaradi).

Har bir haqiqiy nuqson `xfail(strict=True)` test bilan qotirilgan (jami 10 ta): tuzatilganda test XPASS bo'lib yiqiladi va belgini olib tashlash kerak bo'ladi.

## Topilmalar (jiddiylik bo'yicha)

### 🟠 F-1. SDK ning haqiqiy tarmoq xatolari (aiohttp) qayta urinilmaydi va `GeminiError` ga o'ralmaydi
- **Joy:** `src/app/clients/gemini.py:100-111` (`is_transient`), `:170` (`except (APIError, TimeoutError, httpx.TransportError)`).
- **Ssenariy:** `google-genai 2.32` async rejimda, `aiohttp` o'rnatilgan bo'lsa (bu muhitda o'rnatilgan, SDK ning tranzit bog'liqligi), **aiohttp** dan foydalanadi (`_api_client.py:1368 _use_aiohttp`). Uzilish/DNS/reset `aiohttp.ClientError` oilasi bilan keladi (SDK faqat ba'zilarini bir marta o'zi qayta urinadi). Klient esa faqat `httpx.TransportError` ni tanlaydi. Natija: (a) qayta urinish yo'q; (b) xato xom holda `generate_json` dan chiqadi; (c) `LlmService._attempt` faqat `GeminiError` ni ushlaydi, shuning uchun `review_candidates` ning tashqi `except Exception` i ushlaydi: `internal_error`, to'liq traceback `logger.exception` bilan; (d) jurnalga/budgetga **yozilmaydi**. "Hech qachon raise qilmaydi" saqlanadi, lekin `llm_unavailable` o'rniga `internal_error` chiqadi va vaqtinchalik uzilishda qayta urinish bo'lmaydi.
- **Test:** `tests/clients/test_gemini_sdk_shape.py::test_aiohttp_transport_error_is_wrapped_as_gemini_error` (xfail).
- **Tuzatish:** `_call` ichida SDK dan chiqqan `Exception` (APIError va TimeoutError dan tashqari) ni `GeminiTransportError` ga o'rab, `is_transient` da tranzient deb belgilash (aiohttp ni import qilmasdan: `deptry` DEP003 bo'lmasligi uchun), yoki SDK ga `httpx` transportini majburlash (`HttpOptions(httpx_async_client=...)`).

### 🟠 F-3. Parallel chaqiruvlarda kunlik limit oshib ketadi (check-then-act)
- **Joy:** `service.py:167` (`allow`) va `:206-221` (`record`) orasida `await backend.generate_json` bor; `budget.py:329-350`.
- **Ssenariy:** `max_requests_per_day=2`, 10 ta bir vaqtdagi `review_candidates` (masalan, bir nechta foydalanuvchi bir vaqtda /tahlil) — hammasi `allow()` dan o'tadi, backendga 10 so'rov ketadi. Bepul tarif kvotasi aynan shunday tugaydi.
- **Test:** `tests/llm/test_service_resilience.py::test_concurrent_calls_cannot_overshoot_the_request_cap` (xfail). Parallel hisob **to'g'riligi** (25 ta parallel, 25 yozuv, 3750 token) alohida testda ✅.
- **Tuzatish:** `allow()` o'rniga `reserve(estimated) -> Reservation` (so'rovni darrov hisoblab, `record` da haqiqiy qiymatga almashtirish), yoki servis darajasida `asyncio.Semaphore(1)`/lock.

### 🟠 F-4. Gap boshidagi to'qilgan ism gallyutsinatsiya himoyasidan o'tib ketadi
- **Joy:** `src/app/llm/guard.py:53-54` (`_is_sentence_initial` o'tkazib yuboradi; hujjatda ma'lum cheklov sifatida yozilgan).
- **Ssenariy:** model izohi ko'pincha ism bilan boshlanadi: `"Haaland o'ynamaydi."` — kirishda yo'q ism tekshirilmaydi, izoh o'tadi. Golden `recorded_hallucinated_player` ataylab ism gap o'rtasida ("Liverpool uchun Haaland ...") bo'lgani uchun bu bo'shliqni ko'rsatmaydi.
- **Test:** `tests/llm/test_adversarial.py::test_invented_name_at_the_start_of_a_sentence_is_caught` (xfail).
- **Tuzatish:** gap boshidagi so'zni ham tekshirish, lekin kichik o'zbekcha so'z ro'yxati (`Bu`, `Ushbu`, `Hozir`, ...) yoki "kirishdagi so'zlar + umumiy lug'at" bilan istisno qilish; yoki izohni "ism ... " shabloniga majburlash.

### 🟠 F-8. SDK usage bermasa, token limiti hech qachon ishlamaydi
- **Joy:** `gemini.py:202` (`usage = GeminiUsage(0, 0)`), `budget.py:349`.
- **Ssenariy:** javobda `usage_metadata` yo'q (yoki `None`) → so'rov 0 token deb yoziladi; kunlik 250 000 token chegarasi o'lik bo'lib qoladi, faqat so'rovlar soni (100) cheklaydi. Bepul tarif uchun xavfsiz emas.
- **Test:** `test_missing_usage_metadata_still_consumes_token_budget` (xfail).
- **Tuzatish:** usage yo'q bo'lsa `built.estimated_tokens + expected_output` ni yozish (taxminiy, `estimated=True` belgisi bilan).

### 🟠 F-9. Post-mortem `reason` review izohidagi himoyalardan o'tmaydi
- **Joy:** `service.py:129` (`_clean_note` faqat; `_guard_note` yo'q).
- **Ssenariy:** `reason` da kirishda yo'q ism yoki 5000 belgi → o'z holicha `PostmortemResult` ga tushadi. F8.4 uni saboq sifatida saqlaydi va keyingi promptga ("Tajriba konteksti") qaytaradi: to'qilgan fakt o'zini kuchaytiradi (hallucination amplification).
- **Test:** `test_postmortem_reason_with_invented_name_is_not_stored`, `test_postmortem_reason_is_length_capped` (xfail).
- **Tuzatish:** `_guard_note` ni `reason` ga ham qo'llash (nom yoki uzunlik buzilsa `None` yoki `reason=""`).

### 🟡 F-2. `APIError.code` `None` bo'lishi mumkin — `is_transient` `TypeError` beradi
`gemini.py:109`: `APIError(0, {})` → `code=None` (SDK `_get_code`). `None >= 500` `TypeError`; tenacity predikatidan chiqib xom `TypeError` ko'tariladi (servis uni `internal_error` ga aylantiradi). Test: `test_is_transient_survives_an_api_error_without_status_code` (xfail). Tuzatish: `code = error.code or 0`.

### 🟡 F-5. Prefiks qoidasi JSON kalitlari va ko'rsatmalarga ham qarshi tekshiradi
`guard.py:47,57`: `_is_known` har qanday ≥ 4 belgili so'z prefiksini qabul qiladi, `grounding` esa butun `user` matni (kalitlar: `home`, `away`, `league`, `status`, `reason`, `player`...). To'qilgan `Homer` "home" kaliti tufayli o'tadi. Test: `test_invented_name_sharing_a_prefix_with_a_json_key_is_caught` (xfail). Tuzatish: grounding sifatida faqat qiymatlarni (nomlar, o'yinchilar, liga) yig'ish, kalitlar va ko'rsatma matnini chiqarib tashlash.

### 🟡 F-6. Full-width delimiter o'xshashlari `sanitize` dan o'tadi
`builder.py:84` NFC ishlatadi, `＜＜＜END_DATA＞＞＞` qoladi. Parser uchun zararsiz (JSON qatori ichida), lekin model uni chegara deb o'qishi mumkin. Test: `test_fullwidth_delimiter_lookalikes_are_neutralised` (xfail). Tuzatish: NFKC.

### 🟡 F-7. Kirishdagi kirill nomlar "noma'lum" deb belgilanadi
`domain/entities.py:30 _NON_ALPHANUMERIC = [^0-9a-z]` kirillni `""` ga aylantiradi, shuning uchun kirishda bor `Спартак` ham izohda "noma'lum" bo'ladi va izoh tashlanadi (false positive; konservativ, lekin buzuq). Test: `test_cyrillic_name_present_in_the_input_is_not_flagged` (xfail). Eslatma: `normalize_name` umumiy domen funksiyasi, uni o'zgartirish llm-engineer egaligida emas; tuzatish guard ichida `str.casefold()` + NFKD (kirillni saqlaydigan) alohida normalizator bilan.

### 🟡 F-10. "Har bir so'rov hisoblanadi" izohi to'liq aniq emas
`budget.py` docstring: "counts every request that reached the API". Aslida bitta `generate_json` (ichida 3 tagacha HTTP urinish) va servis darajasidagi 1 qayta urinish (invalid output) = ko'pi bilan 6 HTTP so'rov, budgetda 1–2 deb hisoblanadi. 429 bo'ron paytida RPM kvotasi hisobdan tez tugaydi. `GeminiError` yo'lida `attempts` yo'qoladi. Tuzatish: `GeminiError.attempts` qo'shib, shuncha so'rov hisoblash.

### 🟡 F-11. Kirish tokeni bahosi tizim promptini hisobga olmaydi
`builder.py:237`: `estimated_tokens` faqat `user`. `context_check.v1.md` ≈ 2151 belgi ≈ 717 token (10 nomzodli so'rovning `1472` bahosiga qo'shilmaydi, ~ +49%). Kunlik token chegarasi shu qadar kam hisoblanadi (aniq usage kelganda tuzaladi, F-8 bilan birga muhim).

### 🟡 F-12. Bo'sh javobda sarflangan tokenlar yozilmaydi
`gemini.py:197-200`: matn yo'q → `GeminiError`, `usage` tashlanadi; so'rov 0 token bilan jurnalga tushadi (bloklangan/bo'sh javobda kirish tokenlari baribir hisoblanadi).

### 🟡 F-13. Izohning tili/aks-sadosi kodda tekshirilmaydi
Faqat prompt "o'zbekcha, 2 gap" deydi. Kod gap soni va uzunlikni majburlaydi, lekin ingliz tilidagi, HTML (`<b>`, `<a>`) yoki injection matnini takrorlagan izoh (`note_forbidden` faqat eval da) o'tib ketadi. Pipeline/bot izohni **HTML-escape** qilishi shart (T3 bot `texts.py` qoidasi); bu shartnomaga (§7 "Pipeline qoidalari") qo'shilishi kerak.

### 🟡 F-14. Kod sifati
- `MAX_NOTE_CHARS` ikki joyda, turli ma'noda: `inputs.py:276` (= 240, kirish eslatmasi) va `service.py:49` (= 400, chiqish izohi) — chalkash nom.
- `LlmService.review_candidates` da `except Exception` + `logger.exception` har bir xatoda butun traceback; kutilgan xatolar uchun `warning` kifoya (fuzz da rich-traceback tufayli har bir xato ~0.5 s edi).

### Golden to'plam (F5.6) sifati
- 26 holat, 22 tasi jonli; kerakli senariylar (jarohat, rotatsiya, ma'lumot yetishmasligi, ziddiyat, 3 ta injection, aralash to'plam) bor. Kutilgan natijalar mazmunli (`verdict_in` to'plam, `flags_include`, `note_forbidden`).
- **Cheklov (kamchilik emas, lekin ochiq aytilsin):** CI da yozib olingan javob o'zining kutilmasini qoniqtiradi; bu *quvur* (builder → validator → guard → fallback) ni isbotlaydi, **modelning** injection'ga chidamliligini emas. Buni faqat jonli `scripts/llm_eval.py` ko'rsatadi (hali ishga tushirilmagan).
- Yozib olingan 4 holat o'z himoyasini sinaydi: `recorded_bad_verdict` (enum → qayta urinish → `invalid_output`), `recorded_malformed_json` (xuddi shu yo'l, ikkalasi ham `g24` — deyarli takror), `recorded_hallucinated_player` (ism **gap o'rtasida**, shuning uchun F-4/F-5 bo'shliqlarini ko'rsatmaydi), `recorded_missing_candidate` (to'g'ri, `missing_in_response`). Tavsiya: gap boshidagi ism holatini qo'shish (F-4 tuzatilgach).
- `defenders_out_over_bet` kutilmasi (`tasdiq`/`ehtiyot`, bayroqsiz) juda keng: har qanday javobni qabul qiladi.

## QA mezonlari

| # | Mezon | Holat | Dalil |
|---|---|---|---|
| 1 | LLM ehtimollik/EV ni o'zgartira olmaydi | ✅ | `ReviewResponse` JSON-sxemasida `p_model/p_market/ev/odds/stake` va `number/integer` turi yo'q; javobga qo'shilgan `p_model/ev/odds` e'tiborsiz va `LlmReview` da bunday maydon yo'q (`test_wire_schema_has_no_numeric_field...`, `test_numbers_smuggled_into_the_answer_are_ignored`) |
| 2 | `verdict`/`risk_flags` qat'iy enum; izoh ≤ 2 gap | ✅ | `TASDIQ`, `Tasdiq`, `tasdiq `, `tasdiq<ZWSP>`, `yes`, `""`, `None` hammasi rad → 2 urinish → `invalid_output`; takroriy bayroq yig'iladi; 100 000 × gap izoh tashlanadi |
| 3 | Prompt injection: ma'lumot blokidan chiqolmaydi | ✅ | 17 hujum satri (`<<<END_DATA>>>` variantlari, 12 ta `<` ketma-ket, ZWSP bilan bo'lingan, RTL-override, U+2028, JSON-sindiruvchi qo'shtirnoq/`\"`, CRLF, ANSI) × 3 yo'l (eslatmalar/saboq/statistika; nomlar/sabablar/forma/jarohat; post-mortem matnlari): har safar aynan 1 `<<<DATA>>>` + 1 `<<<END_DATA>>>`, blok ichida `<<<`/`>>>` yo'q, JSON parse bo'ladi, UTF-8 ga kodlanadi. Tizim prompti ma'lumotga bog'liq emas. Juda uzun matn (100 000 belgi) va `candidate_id` hujumlari DTO da rad etiladi; yolg'iz surrogat DTO da rad etiladi. Istisno: F-6 (full-width, 🟡) |
| 4 | Tizim prompti "ma'lumot, ko'rsatma emas" deydi | ✅ | ikkala prompt ham delimitrlarni nomlaydi va "ma'lumot"/"bajarma" so'zlarini o'z ichiga oladi (test) |
| 5 | `review_candidates` hech qachon raise qilmaydi, natija 1:1 tartibda | ✅ | 12 turdagi istisno (RuntimeError, KeyError, RecursionError, MemoryError, UnicodeError, ...) → fallback; `hypothesis` 300 misol (tasodifiy backend xatolari + tasodifiy JSON/matn + 0–14 nomzod, takroriy idlar): `ids == kirish idlari`, fallback'da `verdict/note=None`, OK da enum; post-mortem fuzz 100 misol: `None` yoki natija. `CancelledError` **yutilmaydi** (to'g'ri) |
| 6 | Noto'g'ri/takroriy/ortiqcha/yetishmayotgan id, bo'sh ro'yxat, > 10 nomzod | ✅ | parametrli test: bo'sh javob, yetishmayotgan, tartibi boshqa, takroriy javob (birinchisi yutadi), ortiqcha idlar, katta-kichik harf, bo'sh joy; takroriy kirish idlari → `invalid_input` hammasi, backendga murojaat yo'q; 13 nomzod → birinchi 10 OK, qolgani `omitted_by_token_budget` |
| 7 | Limit: UTC kun almashinuvi | ✅ | 23:59:59 → 1 so'rov OK, ikkinchisi `budget_exhausted`, 00:00:00 dan keyin yana OK, `snapshot().day == 2026-10-25` |
| 8 | Limit: parallel chaqiruvlar | ❌ (F-3) | 25 parallel: hisob to'g'ri ✅; lekin cheklovdan oshish mumkin (xfail) |
| 9 | Limit: usage yo'q bo'lganda tokenlar | ❌ (F-8) | xfail |
| 10 | Kalit logda/xatoda/hisobotda yo'q | ✅ | Haqiqiy `configure_logging(prod, secret_values=[KEY])` bilan istisno matniga kalit qo'yildi: chiqishda yo'q (traceback ham tozalanadi). Haqiqiy SDK + HTTP: kalit faqat `x-goog-api-key` sarlavhasida (URL va tanada yo'q); 400/401/403/404 va 503 da `GeminiError` matni va loglarda kalit va prompt yo'q. `render_report` kalit/prompt yozmaydi (kod ko'rildi). Prompt matni (eslatma, jamoa nomi) WARNING/INFO loglarda yo'q (test) |
| 11 | SDK ga mosligi (haqiqiy `google-genai 2.32.0`) | ✅ (F-1, F-2 bilan) | `genai.Client` + `httpx.MockTransport`: so'rov `models/gemini-flash-latest:generateContent`, `responseMimeType=application/json`, `temperature=0.2`, `systemInstruction`, `responseSchema` (verdict enum 3, risk_flags enum 8, 4 maydon); post-mortem sxemasi qabul qilinadi; `usageMetadata` → 11 kirish, 5+3 (fikrlash) chiqish; 429 ×2 → 3-urinishda OK; bloklangan javob (candidates yo'q) → `GeminiError("empty")`; usage yo'q → 0 token; `thoughts_token_count`, `HttpOptions.timeout` maydonlari SDK da mavjud. Mock bilan o'tib, haqiqiy SDK da yiqiladigan chaqiruv **topilmadi** (🔴 yo'q) |
| 12 | Token byudjeti kesish: determinizm va ustuvorlik | ✅ | Mavjud testlar ko'rib chiqildi (statistika → eslatma → saboq → tafsilot → nomzod; oxirgi nomzod hech qachon tashlanmaydi; bir xil kirish → bir xil chiqish) va to'liq `hypothesis` testi bor; kod `_reduce` sof funksiya. Eslatma: F-11 |
| 13 | Gallyutsinatsiya himoyasi | ✅ qisman | diakritika (`München`↔`Munchen`, `İstanbul`↔`Istanbul`), 4 xil apostrof + `-ning` qo'shimchasi, qisqa so'zlar/raqamlar o'tkaziladi ✅; kirill ❌ (F-7), gap boshidagi ism ❌ (F-4), kalit prefiksi ❌ (F-5); lotin transliteratsiyasi konservativ tashlanadi (qabul qilinadi) |
| 14 | Golden to'plam sifati | ✅ (izohlar bilan) | yuqorida |
| 15 | Shartnoma/egalik | ✅ | `src/` o'zgartirilmadi; `docs/contracts.md §7` kodga mos (fallback sabablari, 1:1) |

## Qo'shilgan testlar

| Fayl | Soni | Mazmuni |
|---|---|---|
| `tests/clients/test_gemini_sdk_shape.py` | 14 (2 xfail) | haqiqiy SDK + soxta HTTP transport: so'rov shakli, sarlavha, 429/5xx/4xx, bloklangan javob, usage |
| `tests/llm/test_adversarial.py` | 70 (4 xfail) | injection korpusi (17 × 3 yo'l), DTO himoyasi, gallyutsinatsiya qirralari |
| `tests/llm/test_service_resilience.py` | 47 (4 xfail) | id nomuvofiqliklari, sxema/raqamlar, hech qachon raise qilmaslik (+ hypothesis fuzz), limit va parallellik, sir, post-mortem |

## Gate natijalari

| Buyruq | Natija |
|---|---|
| `pytest` (to'liq, `--cov`) | ✅ `1963 passed, 10 xfailed` (7 min 34 s); umumiy qamrov **99.09%** (chegara 85%); `app/llm/*` va `clients/gemini.py` 91–100% |
| `scripts/ops/coverage_gate.py` | ✅ `OK */app/models/*: 99.78% of 1930 statements`; pipeline: SKIP (hali bo'sh) |
| `ruff check .` | ✅ `All checks passed!` |
| `ruff format --check .` | ✅ `230 files already formatted` |
| `mypy src` | ✅ `no issues found in 111 source files` |
| `lint-imports` | ✅ `6 kept, 0 broken` |
| `deptry .` | ✅ `No dependency issues found` |

10 ta xfail faqat shu QA ning qotirilgan topilmalari (F-1…F-9). Boshqa tegishli bo'lmagan xfail yo'q.

## Xavflar / eslatmalar
- Jonli `scripts/llm_eval.py` hali ishga tushirilmagan (kalit yo'q): modelning haqiqiy sifati va injection chidamliligi ma'lum emas. F4 dan oldin bir marta ishga tushirish tavsiya etiladi (22 so'rov).
- `aiohttp` `pyproject.toml` da e'lon qilinmagan (SDK tranzit bog'liqligi); F-1 ni tuzatishda qaror qabul qilinsin.
- Budget xotirada: qayta ishga tushirishda kunlik hisob yo'qoladi (kod hujjatida aytilgan, qabul qilinadi).
