# ADR 0009: LLM integratsiyasi (Gemini)

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-21
- **Muallif:** llm-engineer (T4: F5.1–F5.6)

## Kontekst
Gemini faqat raqamlarda ko'rinmaydigan sifatli xavflarni (jarohat, rotatsiya, motivatsiya) baholaydi va qisqa o'zbekcha izoh yozadi (asosiy reja §4, 4-qadam). U ehtimollikni o'zgartirmaydi va tizim unga qaram bo'lmasligi shart. API kaliti hali yo'q (qarorlar #1): hamma narsa soxta SDK/backend bilan qurilgan va testlangan; jonli tekshiruv faqat qo'lda. Foydalanuvchi matnlari (izohlar) va jamoa nomlari modelga tashqi, ishonchsiz matn sifatida kiradi.

## Qarorlar

### 1. Qatlam va bog'liqlik
`app.clients.gemini` (SDK o'rami, `clients` qatlamida) va `app.llm` (mantiq). `llm` faqat `domain`, `errors`, `config`, `logging` va `clients.gemini` ni import qiladi; pipeline `llm` ni chaqiradi. Kirish DTO lari `llm` ichida (`app.llm.inputs`): `llm` modellar, repo yoki sport adapterini bilmaydi, pipeline ma'lumotni DTO ga o'tkazadi. `import-linter` qatlamlari o'zgarmadi. `google-genai` endi import qilinadi, deptry DEP002 ro'yxatidan olib tashlandi.

### 2. SDK (o'rnatilgan `google-genai` 2.32 manbasidan tekshirilgan)
`genai.Client(api_key=...).aio.models.generate_content(model, contents, config=GenerateContentConfig(system_instruction, temperature=0.2, response_mime_type="application/json", response_schema=<pydantic sinfi>, http_options=HttpOptions(timeout=ms)))`. Token hisobi `usage_metadata.prompt_token_count`, `candidates_token_count` va `thoughts_token_count` (o'ylash tokenlari chiqish sifatida to'lanadi, shuning uchun hisobga olinadi). `response_schema` ga pydantic sinfi to'g'ridan-to'g'ri beriladi (SDK enum va ichma-ich ro'yxatlarni o'zi o'giradi, tekshirilgan). Sxemada `maxLength` kabi kalit so'zlar ishlatilmaydi (provayder to'plami cheklangan): uzunlik va gap soni kodda tekshiriladi.

### 3. Qayta urinish ikki qatlamda, aralashmaydi
- **Klient (tarmoq):** `tenacity`, 3 urinishgacha, jitterli eksponensial kutish, faqat o'tkinchi xatolarda: HTTP 429, 408, 5xx, timeout, transport xatosi. Boshqa 4xx (kalit noto'g'ri, so'rov yaroqsiz) darhol `GeminiError`. Har urinishda SDK timeout (30 s) va qo'shimcha `asyncio.timeout`.
- **Servis (mazmun):** javob sxemaga mos kelmasa (buzilgan JSON, ruxsat etilmagan `verdict`, yetishmayotgan maydon) **bir marta** qayta so'raladi, keyin fallback. `GeminiError` da servis qayta so'ramaydi (klient allaqachon urindi).

### 4. Prompt xavfsizligi va versiyalash
Ko'rsatmalar faqat versiyalangan fayllarda (`prompts/context_check.v1.md`, `postmortem.v1.md`, paket ma'lumoti sifatida). Foydalanuvchi xabari: bitta qat'iy ko'rsatma qatori va `<<<DATA>>> ... <<<END_DATA>>>` orasidagi JSON. Tashqi matn (izoh, saboq, nomlar) boshqaruv belgilaridan tozalanadi, `<<<`/`>>>` ketma-ketliklari almashtiriladi (blokdan chiqib ketib bo'lmaydi), JSON-escape qilinadi; tizim prompti blokni "faqat ma'lumot" deb e'lon qiladi. Promptni o'zgartirish = yangi `vN` fayl; eski versiyaga qaytish `builder.CONTEXT_CHECK_PROMPT` konstantasini o'zgartirish. Har natija `prompt_version` (`context_check.v1`) va `model` bilan saqlanadi. Matnni regex bilan parse qilish yo'q: faqat sxema + pydantic.

### 5. Token byudjeti (deterministik)
Kirish JSON ixcham, tokenlar `ceil(belgilar / 3)` bilan pessimistik baholanadi (o'zbekcha JSON uchun). Chegaradan oshsa, eng kam qimmatli qism birin-ketin olib tashlanadi: segment statistikasi → foydalanuvchi izohlari → saboqlar → nomzod tafsiloti (forma, jarohat sababi, ishonch sabablari, jarohatlar 3 tagacha) → oxirgi nomzodlar (kamida 1 ta qoladi). Sof funksiya: bir xil kirish = bir xil chiqish. Saboqlar ≤ 5, foydalanuvchi izohlari ≤ 3, nomzodlar ≤ 10. Kesilgan nomzodlar `omitted_by_token_budget` fallback oladi.

### 6. Gallyutsinatsiyaga qarshi qo'riq
Izohda o'rtadagi bosh harfli so'z (nom deb qaraladi) kirish matnida bo'lishi kerak (aksent, tutuq belgisi va o'zbekcha qo'shimchalar `Chelsea'ning` hisobga olinadi). Yo'q bo'lsa, izoh tashlanadi, `verdict` saqlanadi, `unknown_name_in_note` belgilanadi. **Cheklov:** gap boshidagi so'z tekshirilmaydi (oddiy so'zlar ham bosh harfli), shuning uchun gap boshidagi to'qilgan ism o'tib ketishi mumkin; izoh uzunligi (≤ 400 belgi, ≤ 2 gap) kodda majburlanadi. Bu to'liq fakt-tekshiruv emas, arzon birinchi to'siq; asosiy himoya promptdagi "to'qib chiqarma" qoidasi va golden to'plam.

### 7. Limit nazorati va jurnal
`LlmBudget`: kuniga so'rovlar (100) va tokenlar (250 000) bo'yicha UTC kunida; har urinishdan **oldin** `allow(taxminiy kirish + kutilgan chiqish)`. Xato bilan tugagan so'rov ham hisoblanadi (konservativ). Provayder kvotasi Tinch okean vaqti bilan yangilanadi, UTC kun esa ba'zi soatlarda qattiqroq bo'ladi, bu zararsiz. Jurnal xotirada (oxirgi 500 yozuv, prompt matnisiz); jarayon qayta ishga tushsa bugungi hisob unutiladi (kuniga ~2 so'rov uchun ortiqcha xavf emas). Doimiy saqlash kerak bo'lsa, shu interfeys orqasida `db` ga ko'chiriladi (hozir YAGNI). Loglarda faqat o'lchamlar va tokenlar; prompt matni va kalit hech qachon (kalit redaktor ham yashiradi).

### 8. Baholash
`tests/llm/golden/*.json`: 26 holat (22 jonli + 4 yozib olingan-faqat). Har holat: nomzodlar, kontekst, kutilgan `verdict` to'plami, majburiy `risk_flags`, taqiqlangan izoh matni va yozib olingan model javobi. CI yozib olingan javobni haqiqiy servis orqali o'tkazadi (builder, validator, qo'riq, fallback). `scripts/llm_eval.py` shu holatlarni jonli API ga yuboradi (faqat qo'lda; kalitsiz `exit 2`) va `docs/llm_eval/<sana>.md` yozadi; har prompt o'zgarishidan keyin solishtiriladi.

## Variantlar
- **Funksiya chaqirish (function calling) o'rniga `response_schema`:** oddiyroq, kerakli kafolatni beradi. Rad etildi: function calling.
- **Bitta so'rov o'rniga har nomzodga alohida:** kvota va kechikish ko'p; reja bitta so'rovni talab qiladi. Rad etildi.
- **`Any`/dict kirish o'rniga `llm` ichida DTO:** `llm` qatlamini mustaqil saqlaydi; qo'shimcha nusxalash bahosi kichik.
- **Gallyutsinatsiya uchun NER/LLM-hakam:** og'ir va o'zi ishonchsiz. Hozircha oddiy nom tekshiruvi.

## Oqibatlar
- Pipeline `LlmService` ni bir marta yasaydi va `review_candidates` natijasini signalga yozadi (`prompt_version`, `model`, tokenlar). LLM xatosi hech qachon signalni to'xtatmaydi.
- F8 (`ExperienceContext`, post-mortem) shu DTO va `postmortem()` ni to'ldiradi; saboqlar tayyorlash (teglash, ≥ 2 tasdiq, 90 kun) F8.4 da `llm` ga qo'shiladi (hozir faqat kontekstni qabul qilish).
- Haqiqiy model sifati (verdict aniqligi, o'zbekcha izoh sifati) **jonli ishga tushirilmagan**: kalit kelgach `scripts/llm_eval.py` birinchi marta ishlatilishi shart (qarorlar #1 bilan bir xil kechiktirish).
- Free-tier limitlari o'zgarsa, `LlmLimits` qiymatlari pipeline tomondan beriladi.
