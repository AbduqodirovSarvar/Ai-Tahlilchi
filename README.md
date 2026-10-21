# AI Tahlilchi

Sport o'yinlarini statistik model (Poisson / Dixon-Coles, bozor modeli) va Gemini yordamida tahlil qilib, "value" topilgan o'yinlar bo'yicha Telegram bot orqali tavsiya beruvchi tizim. Bot shaxsiy: faqat ruxsat berilgan foydalanuvchilarga (`ADMIN_IDS`) va faqat bot bilan **shaxsiy chatda** javob beradi (guruh va kanallar e'tiborsiz qoldiriladi).

> **Holat:** T1 (loyiha skeleti). Hozircha bot faqat `/start` ga "Ishlayapti ✅" deb javob beradi. Batafsil holat: [docs/progress.md](docs/progress.md).

## Hujjatlar
- [Loyiha holati — bajarilgan va qolgan ishlar](docs/HOLAT.md)
- [Loyiha rejasi (funksional talablar)](ai_sport_tahlilchisi_va_telegram_bot_loyihasi_rejasi.md)
- [Ishlab chiqish rejasi (F0–F12)](ishlab_chiqish_rejasi.md)
- [Agentlar orqali implementatsiya](agentlar_rejasi.md)
- [Loyiha qoidalari](CLAUDE.md)
- [Holat](docs/progress.md) · [Shartnomalar](docs/contracts.md) · [Qarorlar](docs/decisions_log.md) · [ADR](docs/adr/)

## Talablar
- Python **3.13**
- [`uv`](https://docs.astral.sh/uv/) (paket va virtual muhit boshqaruvi)
- Git
- *(ixtiyoriy)* Docker Desktop / Docker Engine + Compose v2.24+

> Windows'da `uv` PATH da bo'lmasa, barcha `uv ...` buyruqlarini `python -m uv ...` shaklida ishga tushiring.

## Loyiha tuzilmasi
```
src/app/            ilova kodi (paket nomi: app)
  config.py         sozlamalar (faqat shu modul muhit o'zgaruvchilarini o'qiydi)
  logging.py        structlog sozlamasi
  errors.py         istisnolar ierarxiyasi
  main.py           kirish nuqtasi: python -m app.main
  bot/ services/ pipeline/ models/ sports/ db/ llm/ clients/ domain/
tests/              testlar (src/app tuzilmasini takrorlaydi)
scripts/            yordamchi skriptlar (scripts/ops: CI va server)
docs/               progress, shartnomalar, ADR, QA, backtest hisobotlari
```

## Sozlash
```bash
uv sync                      # bog'liqliklar + loyiha (.venv)
cp .env.example .env         # keyin .env ni to'ldiring
```

`.env` dagi asosiy qiymatlar (to'liq ro'yxat: [.env.example](.env.example)):

| O'zgaruvchi | Majburiy | Izoh |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | bot uchun | @BotFather dan |
| `ADMIN_IDS` | bot uchun | Telegram `user_id` lar, vergul bilan: `123,456` |
| `API_FOOTBALL_KEY` | ma'lumot yangilash uchun | api-sports.io |
| `GEMINI_API_KEY` | LLM izohlari uchun | Google AI Studio |
| `APP_ENV` | yo'q | `dev` (rangli loglar) yoki `prod` (JSON loglar) |
| `DISPLAY_TZ` | yo'q | foydalanuvchiga vaqtni ko'rsatish mintaqasi, standart `Asia/Tashkent` (bazada doim UTC). POSIX `TZ` ishlatilmaydi |
| `DB_PATH`, `LOG_LEVEL` | yo'q | standart: `data/app.db`, `INFO` (katta-kichik harf farqi yo'q) |

`.env` bo'lmasa ham ilova, testlar va CI ishlaydi. Kalit kerak bo'lgan komponent faqat ishga tushirilganda aniq xabar bilan to'xtaydi (masalan, `TELEGRAM_BOT_TOKEN is not set; add it to .env`).

Chiqish kodlari: `0` normal to'xtash, `1` kutilmagan xato (`fatal_error` logi), `2` sozlama xatosi: kalit yo'q, noto'g'ri qiymat (xabarda faqat o'zgaruvchi nomi bo'ladi, qiymati emas), token noto'g'ri formatda yoki Telegram uni rad etgan. `.env` ni tuzating, qayta urinish yordam bermaydi.

> 🔐 `.env` hech qachon gitga qo'shilmaydi. Kalitlarni kodga, logga yoki chatga yozmang.

## Lokal ishga tushirish
```bash
uv run python -m app.main
```
Telegram'da botga shaxsiy chatda `/start` yuboring: javob "Ishlayapti ✅". Whitelist'da bo'lmagan foydalanuvchilarga, shuningdek guruh va kanallarda (admin yozsa ham) bot umuman javob bermaydi. To'xtatish: `Ctrl+C`.

## Testlar va sifat tekshiruvlari
Vazifa faqat **hammasi yashil** bo'lganda bajarilgan hisoblanadi (CI ham aynan shularni bajaradi):
```bash
uv run pytest                                  # testlar + branch coverage (umumiy >= 85%)
uv run python scripts/ops/coverage_gate.py     # models, pipeline >= 90%
uv run ruff check .                            # lint
uv run ruff format --check .                   # format
uv run mypy src                                # tiplar (--strict)
uv run lint-imports                            # qatlam chegaralari
uv run deptry .                                # bog'liqliklar
uv run pip-audit                               # zaifliklar
```

Git hook'lar (bir marta): `uv run pre-commit install`. Hammasini qo'lda: `uv run pre-commit run --all-files` (`gitleaks` ham shu yerda; hook'lar uchun `uv` PATH da bo'lishi kerak).

## Docker
```bash
docker compose up -d --build     # ishga tushirish
docker compose logs -f bot       # loglar (JSON)
docker compose down              # to'xtatish
```
- Konteyner non-root (`uid 10001`) foydalanuvchi bilan, faqat o'qiladigan fayl tizimida ishlaydi. Baza `bot-data` nomli Docker volume'ida (`/data`); volume image'dagi egalikni oladi, `chown` kerak emas. Volume'ni ko'rish: `docker volume inspect ai-tahlilchi_bot-data`.
- Qayta ishga tushirish siyosati `on-failure:5`: xato bilan chiqsa ko'pi bilan 5 marta qayta uriniladi. `.env` bo'lmasa yoki noto'g'ri bo'lsa, konteyner sababni logga yozib (kod `2`) to'xtaydi; kalitlarni qo'shib `docker compose up -d` qiling.
- TLS'ni ushlab qoluvchi korporativ proksi ortida build: `docker build --secret id=ca_bundle,src=ca.pem .` (sertifikat image'ga yozilmaydi).

## CI
GitHub Actions ([.github/workflows/ci.yml](.github/workflows/ci.yml)) har push va PR da: lint, format, mypy, import-linter, deptry, pytest + coverage, pip-audit, gitleaks va Docker build + smoke test. Hech qanday secret talab qilinmaydi.
