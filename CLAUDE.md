# AI Tahlilchi — Loyiha Qoidalari

Barcha agentlar va sessiyalar uchun umumiy qoidalar.

## Hujjatlar
- Funksional talablar: `ai_sport_tahlilchisi_va_telegram_bot_loyihasi_rejasi.md`
- Fazalar va vazifa ID lari (F0–F12): `ishlab_chiqish_rejasi.md`
- Agentlar, egalik va to'lqinlar: `agentlar_rejasi.md`
- Interfeys shartnomalari: `docs/contracts.md`
- Holat kuzatuvchisi: `docs/progress.md`

## Til
- Kod (identifikatorlar, commit xabarlari, kod ichidagi izohlar): **ingliz tili**.
- Bot foydalanuvchiga ko'rsatadigan matnlar va hujjatlar: **o'zbek tili (lotin)**. Bot matnlari faqat `app/bot/texts.py` da saqlanadi.

## Stek
Python 3.13, `uv`, `aiogram 3`, `httpx` + `tenacity`, `SQLAlchemy 2` (async) + `aiosqlite` + `Alembic`, `pydantic-settings`, `APScheduler`, `numpy/scipy/pandas/scikit-learn`, `google-genai`, `rapidfuzz`, `matplotlib`, `structlog`; testlar: `pytest`, `pytest-asyncio`, `respx`, `hypothesis`. To'liq jadval: `ishlab_chiqish_rejasi.md`, 1-bo'lim.

## Buyruqlar
> Bu mashinada `uv` PATH da yo'q: barcha `uv ...` buyruqlari **`python -m uv ...`** shaklida ishga tushiriladi (masalan, `python -m uv run pytest`). CI va Docker da oddiy `uv` ishlatiladi.

```
uv sync                      # bog'liqliklar
uv run pytest                # testlar
uv run ruff check . --fix    # lint
uv run ruff format .         # format
uv run mypy app              # tiplar
uv run alembic upgrade head  # migratsiyalar
```
Vazifa faqat **hammasi yashil** bo'lgandan keyin "bajarildi" deb hisoblanadi.

## Kod qoidalari
- Har joyda async. Bloklovchi chaqiruvlar (pandas, scipy) og'ir bo'lsa `asyncio.to_thread` orqali bajariladi.
- Vaqt bazada va kodda **UTC** (`datetime` timezone bilan). `Asia/Tashkent` ga faqat ko'rsatishda o'tkaziladi.
- Barcha tashqi sozlamalar faqat `app/config.py` orqali olinadi; `os.environ` ni to'g'ridan-to'g'ri o'qish taqiqlanadi.
- Loglash `structlog` orqali; `print` taqiqlanadi.
- Pul summalari uchun `Decimal`; koeffitsient va ehtimolliklar uchun `float`.
- Testlarda tarmoq yo'q: HTTP `respx` bilan, Gemini mock bilan, baza vaqtinchalik SQLite.
- Testlar `tests/` da `app/` tuzilmasini takrorlaydi (`app/models/poisson.py` → `tests/models/test_poisson.py`).

## Senior Muhandislik Standarti (barcha agentlar uchun majburiy)

Har bir agent o'z sohasida 10+ yillik tajribaga ega **senior mutaxassis** kabi ishlaydi. Bu quyidagi aniq xatti-harakatlarni anglatadi:

**1. Koddan oldin tushunish**
- Vazifa, shartnomalar (`docs/contracts.md`) va mavjud kod o'qib chiqiladi. Mavjud uslub va naqshlarga moslashiladi.
- Noaniq talablarda taxmin yashirilmaydi: taxmin hisobotda aniq yoziladi yoki savol beriladi.
- Talab noto'g'ri yoki xavfli bo'lsa, ko'r-ko'rona bajarilmaydi: muammo va muqobil variant asoslab taklif qilinadi.

**2. Loyihalash qarorlari**
- Muhim yoki qaytarish qiyin qarorlar (kutubxona tanlash, sxema, algoritm, arxitektura) `docs/adr/NNNN-nom.md` ga qisqa yoziladi: kontekst, variantlar, tanlov, oqibatlar.
- **Soddalik ustun:** eng oddiy to'g'ri yechim. Kelajak uchun "ehtimol kerak bo'ladi" degan abstraksiyalar qo'shilmaydi (YAGNI). Lekin aniq ma'lum kengaytirish nuqtalari (yangi sport, yangi bozor) ochiq qoldiriladi.

**3. Nosozlik rejimlari haqida o'ylash**
Har bir tashqi chaqiruv va holat o'zgarishi uchun javob beriladi: timeout bo'lsa nima bo'ladi? 429/5xx bo'lsa? Jarayon o'rtada to'xtasa va qayta ishga tushsa? Ikki marta chaqirilsa? Ma'lumot bo'sh yoki buzilgan bo'lsa?

**4. Sifat**
- Ommaviy funksiyalar to'liq type hint va qisqa docstring bilan yoziladi (docstring "nima uchun"ni tushuntiradi, "nima"ni emas).
- Testlar faqat "baxtli yo'l"ni emas, **chegaraviy holatlar va xatolarni** ham qamraydi. Model va parserlar uchun `hypothesis` (property-based) testlar ishlatiladi.
- Xatolar yutib yuborilmaydi: aniq istisno turlari, kontekstli log, foydalanuvchiga tushunarli xabar.
- Loglar tuzilmali va kontekstli (`fixture_id`, `user_id`, `job`); ichida maxfiy ma'lumot bo'lmaydi.
- Ishlash byudjetlari hurmat qilinadi (API so'rovlari, token, javob vaqti) va o'lchanadi.

**5. Topshirishdan oldin o'z-o'zini tekshirish (self-review)**
Hisobot qaytarishdan oldin har bir agent o'z `git diff`ini sharhlovchi ko'zi bilan qayta o'qiydi va quyidagi ro'yxatni tekshiradi:
- [ ] Barcha tekshiruvlar yashil (`pytest`, `ruff`, `mypy`)
- [ ] Chegaraviy holatlar va xatolar testlangan
- [ ] Egalik va shartnoma qoidalari buzilmagan
- [ ] Maxfiy ma'lumot yo'q; foydalanuvchi kiritgan ma'lumot tekshirilgan yoki escape qilingan
- [ ] Ortiqcha kod, debug qoldiqlari va TODO lar yo'q (qolgan TODO lar hisobotda sababi bilan ko'rsatilgan)
- [ ] Nomlar aniq, funksiyalar kichik va bitta vazifali
- [ ] Muhim qarorlar ADR da yozilgan

**6. Halol hisobot**
- "Bajarildi" faqat **dalil** bilan aytiladi: buyruq, natija, raqam.
- Qisman bajarilgan, o'tkazib yuborilgan yoki shubhali joylar ochiq aytiladi. Natijalarni bo'rttirish taqiqlanadi.
- Topilgan, lekin o'z doirasidan tashqaridagi muammolar "Xavflar / eslatmalar" bo'limida yoziladi.

## Kod Sifati Standarti (CI da avtomatik majburlanadi)

Sifat "iltimos" bilan emas, **avtomatik darvozalar** bilan ta'minlanadi. Quyidagilardan biri buzilsa, CI qizil bo'ladi va kod `main` ga kirmaydi.

| Soha | Talab | Vosita |
|---|---|---|
| Lint | Qoidalar to'plami: `E, W, F, I, N, UP, B, A, C4, SIM, RET, ARG, PTH, PL, RUF, S, ASYNC, DTZ, T20, ERA, PERF, D` (D: Google uslubi, faqat `app/`) | `ruff check` |
| Format | Yagona format, satr uzunligi 100 | `ruff format --check` |
| Tiplar | `mypy --strict` butun `app/` uchun. Ommaviy signaturalarda `Any` yo'q. `# type: ignore` faqat xato kodi va sabab izohi bilan | `mypy` |
| Murakkablik | Siklomatik murakkablik ≤ 10, funksiya ≤ 50 qator, parametrlar ≤ 5, ichma-ich joylashuv ≤ 3 daraja | `ruff` (C90, PLR) |
| Qatlam chegaralari | `bot` → faqat `services`; `services` → `pipeline`; `pipeline` → `models`, `sports`, `db`, `llm`; `models` hech narsani import qilmaydi (sof). Teskari importlar taqiqlangan | `import-linter` |
| Test qamrovi | Umumiy ≥ 85% (branch coverage), `app/models` va `app/pipeline` ≥ 90% | `pytest --cov --cov-fail-under` |
| Xavfsizlik | `S` (bandit) qoidalari, bog'liqliklardagi zaifliklar | `ruff`, `pip-audit` |
| Bog'liqliklar | Ishlatilmagan yoki e'lon qilinmagan bog'liqlik yo'q | `deptry` |
| Maxfiy ma'lumot | Kalit yoki token commit qilinmaydi | `gitleaks` (pre-commit) |

**Yozish qoidalari:**
- **Nomlar** ma'noni aytadi (`implied_probability`, `p` emas). Qisqartmalar faqat sohada umumqabul qilinganlari (`ev`, `xg`, `clv`).
- **Bitta funksiya: bitta vazifa.** Hisoblash, I/O va formatlash bitta funksiyada aralashtirilmaydi.
- **Sehrli raqamlar yo'q:** chegaralar (`0.05`, `1.40`) nomlangan konstantalarda yoki sozlamalarda.
- **O'zgarmaslik:** domen obyektlari va DTO lar `frozen` dataclass yoki pydantic modellar.
- **Takrorlash yo'q:** bir formula yoki mantiq faqat bir joyda yoziladi (EV, de-vig, vaqt o'tkazish).
- **Izohlar** "nima uchun"ni tushuntiradi. O'chirib qo'yilgan (commented-out) kod qoldirilmaydi.
- **Xatolar:** loyihaning istisnolar ierarxiyasi (`app/errors.py`) ishlatiladi. Yalang'och `except:` va `except Exception: pass` taqiqlanadi.
- **Commitlar** kichik va mantiqiy bir butun (Conventional Commits). Har bir commit testlardan o'tadi.
- **Boy-scout qoidasi:** o'z egaligidagi kodga tegilganda, uni topilgan holatidan toza qoldiriladi.

## Xavfsizlik
- Kalitlar va tokenlar faqat `.env` da saqlanadi. `.env` gitga kirmaydi. Kalitni hech qachon kodga, testga, logga yoki hisobotga yozma.
- Bot faqat `ADMIN_IDS` / whitelist dagi foydalanuvchilarga javob beradi.

## Egalik va shartnomalar
- Har bir agent faqat o'ziga tegishli papkalarni o'zgartiradi (`agentlar_rejasi.md`, 2-bo'lim).
- `app/domain/`, `app/sports/base.py`, `app/services/interfaces.py` umumiy **shartnomalar**. Ularni o'zgartirish kerak bo'lsa, faqat yangi narsa qo'shiladi, mavjudi buzilmaydi. O'zgarish `docs/contracts.md` ga yoziladi va hisobotda alohida ko'rsatiladi.

## Hisobot
Har bir agent ish oxirida `agentlar_rejasi.md` 5-bo'limidagi formatda hisobot qaytaradi va `docs/progress.md` dagi tegishli vazifalarni belgilaydi.
