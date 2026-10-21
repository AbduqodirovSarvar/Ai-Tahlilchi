# ADR 0001: Instrumentlar, loyiha tuzilmasi va sifat darvozalari

- **Holat:** qabul qilingan
- **Sana:** 2026-10-21
- **Muallif:** infra-engineer (T1)

## Kontekst
Loyiha bir nechta agent tomonidan parallel quriladi. Har bir agent bir xil muhit, bir xil qoidalar va avtomatik darvozalarga tayanishi kerak (`CLAUDE.md` → "Kod sifati standarti"). API kalitlari hali yo'q, shuning uchun ilova, testlar va CI kalitsiz ishlashi shart.

## Qarorlar

### 1. Python 3.13 (3.12 emas)
Lokal mashinada Python 3.13 o'rnatilgan; barcha stek kutubxonalari (numpy, scipy, pandas, scikit-learn, matplotlib, aiogram, pydantic) 3.13 uchun tayyor wheel'larga ega. `requires-python = ">=3.13"`, Docker: `python:3.13.x-slim-trixie` (teg + digest).
*Oqibat:* PEP 695 `type` aliaslari va `typing.override` ishlatiladi.

### 2. src layout (`src/app/`)
Mahsulot egasi qarori. Paket nomi `app` (importlar `app.models`, `app.bot` …). `uv sync` loyihani **o'rnatadi** (build backend: `hatchling`, `packages = ["src/app"]`), testlar o'rnatilgan paketni import qiladi.
*Afzalligi:* testlar repo ildizidan tasodifan import qila olmaydi: paketlash xatolari (yo'qolgan fayl, noto'g'ri `packages`) darhol ko'rinadi.
*Oqibat:* barcha yo'llar `src/app/...`; `mypy src`; Docker image'ga loyiha `uv sync --frozen --no-dev --no-editable` bilan wheel sifatida o'rnatiladi.

### 3. `uv` + bitta `uv.lock`
Butun `CLAUDE.md` steki (runtime) va dev vositalari boshidanoq e'lon qilingan va qotirilgan: barcha agentlar bitta lock-faylda ishlaydi, `pyproject.toml` ni har biri o'zgartirmaydi.
*Oqibat:* `deptry` "e'lon qilingan, lekin ishlatilmagan" (DEP002) xatosini beradi. Hali import qilinmagan paketlar `[tool.deptry.per_rule_ignores] DEP002` ro'yxatida; paket ishlatila boshlanganda ro'yxatdan **o'chiriladi**. `tzdata` doimiy istisno (`zoneinfo` uni bilvosita yuklaydi; Windows va minimal konteynerlarda tizim tz bazasi yo'q).

### 4. Sifat darvozalari
| Darvoza | Sozlama | Izoh |
|---|---|---|
| `ruff check` | `CLAUDE.md` dagi qoidalar + `C90` (murakkablik ≤ 10), `PLR` (parametrlar ≤ 5, statementlar ≤ 50), `PLR1702` (ichma-ichlik ≤ 3, preview qoida, `explicit-preview-rules` bilan faqat shu yoqilgan) | "Funksiya ≤ 50 qator" uchun ruff'da qator sanovchi qoida yo'q: eng yaqin majburiy proksi `max-statements = 50`. `D` (Google) faqat `src/` uchun |
| `ruff format` | satr 100 | Markdown chiqarib tashlangan: reja hujjatlaridagi Python misollari ishlaydigan kod emas |
| `mypy --strict` | `src/` + `pydantic.mypy` plugin, `warn_unreachable`, `ignore-without-code` | `disallow_any_explicit` yoqilmagan: aiogram middleware protokoli `dict[str, Any]` talab qiladi; ommaviy signaturalarda `Any` yo'qligi sharhda tekshiriladi |
| `import-linter` | `layers` + `forbidden` shartnomalar | `bot → services`; `services → pipeline`; `pipeline → models, sports, db, llm`; `models` faqat `domain`/`errors` (va `aiogram`, `httpx`, `sqlalchemy`, `google` … taqiqlangan); `domain`/`errors` hech narsaga bog'liq emas. `models/sports/db/llm` o'zaro mustaqil qatlamlar. `config`, `logging`, `errors`, `domain` kesib o'tuvchi modullar |
| `pytest-cov` | branch coverage, umumiy ≥ 85% | `src/app/models` va `src/app/pipeline` ≥ 90%: `scripts/ops/coverage_gate.py` (pytest-cov faqat bitta umumiy chegarani qo'llaydi; hali kod bo'lmagan paket o'tadi) |
| `pip-audit` | `uv run pip-audit` (o'rnatilgan muhit) | |
| `deptry` | 3-bandga qarang | |
| `gitleaks` | pre-commit + CI (to'liq git tarixi, Docker image `v8.30.1`) | |

Testlarda `filterwarnings = error`, `--strict-markers`, `--strict-config`, `xfail_strict`.

### 5. Konfiguratsiya: kalitlar yuklashda ixtiyoriy
`Settings` barcha kalitlarni `SecretStr | None` sifatida yuklaydi; bo'sh qiymat (`KEY=`) "yo'q" degani. Komponent ishga tushganda `require_secret()` / `require_admin_ids()` aniq `ConfigurationError` beradi. Bot bo'sh whitelist bilan ishga tushmaydi (hech kimga javob bermaydigan bot: noto'g'ri sozlama). Chiqish kodi: `2`.

### 6. Loglash
`structlog` + stdlib `ProcessorFormatter`: kutubxona (aiogram) loglari ham bir xil formatda stdout'ga chiqadi. `prod`: JSON, `dict_tracebacks`; `dev`: konsol, **oddiy** traceback (structlog standart `rich` formatteri sekin va Windows cp1252 konsolida `UnicodeEncodeError` beradi). Kalit nomi `token`, `secret`, `password`, `api_key`, `authorization` ni o'z ichiga olgan qiymatlar `***` bilan yashiriladi.

### 7. Docker
Multi-stage: builder'da `uv sync --frozen --no-dev` (bog'liqliklar keshlangan alohida qatlamda), runtime'da faqat venv. Non-root (`uid 10001`), compose'da `read_only`, `tmpfs /tmp`, `cap_drop: ALL`, `no-new-privileges`, `init: true`, log rotatsiyasi. Korporativ TLS proksi ortida build uchun ixtiyoriy BuildKit secret `ca_bundle` (image qatlamiga yozilmaydi, CI da ishlatilmaydi).

### 8. CI (GitHub Actions)
To'rtta parallel job: `lint`, `test`, `security`, `docker`. Action'lar commit SHA bilan qotirilgan, `permissions: contents: read`, secret ishlatilmaydi, `uv` keshi `uv.lock` bo'yicha.

## Ko'rib chiqilgan muqobillar
- **Flat layout (`app/`)**: oddiyroq, lekin testlar o'rnatilmagan kodni import qilib, paketlash xatolarini yashiradi. Mahsulot egasi src layout ni tanladi.
- **Faqat ishlatilgan bog'liqliklarni e'lon qilish**: `deptry` toza, lekin har agent `pyproject.toml`/`uv.lock` ni o'zgartiradi (egalik va merge ziddiyatlari). Rad etildi.
- **`pytest-socket`** bilan tarmoqni bloklash: Windows'da asyncio event loop socketpair ishlatadi va bloklanadi. Rad etildi; HTTP testlari `respx` bilan.

## Oqibatlar
- Yangi kutubxona kerak bo'lsa, infra-engineer'ga so'rov yuboriladi (egalik).
- Paket ishlatila boshlanganda `DEP002` ro'yxatidan o'chirilishi kerak (aks holda u "ishlatilmagan"ligi aniqlanmay qoladi).
- `uv` pre-commit hook'lari uchun PATH da bo'lishi kerak.
