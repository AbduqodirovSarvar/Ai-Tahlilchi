# AI Tahlilchi

Sport o'yinlarini statistik model (Poisson / Dixon-Coles, bozor modeli) va Gemini yordamida tahlil qilib, "value" topilgan o'yinlar bo'yicha Telegram bot orqali tavsiya beruvchi tizim. Bot shaxsiy: faqat ruxsat berilgan foydalanuvchilarga (`ADMIN_IDS`) va faqat bot bilan **shaxsiy chatda** javob beradi (guruh va kanallar e'tiborsiz qoldiriladi).

> **Holat:** bot, pipeline va LLM qatlami tayyor; deploy skriptlari, zaxira va [RUNBOOK](RUNBOOK.md) T5 da qo'shildi (serverda hali ishga tushirilmagan). Batafsil holat: [docs/progress.md](docs/progress.md).

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
| `APP_SERVICES` | yo'q | `fake` (standart) yoki `real` (kalitlar tayyor bo'lgach) |
| `HEALTHCHECKS_URL` | yo'q | healthchecks.io ping URL (maxfiy); kunlik sikldan keyin ping |
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
- Konteyner non-root (`uid 10001`) foydalanuvchi bilan, faqat o'qiladigan fayl tizimida ishlaydi. Baza `bot-data` nomli Docker volume'ida (`/data`: `app.db`, `fsm.db`); volume image'dagi egalikni oladi, `chown` kerak emas.
- **Ishga tushish tartibi:** [deploy/entrypoint.sh](deploy/entrypoint.sh) avval `alembic upgrade head` ni bajaradi (muvaffaqiyatsiz bo'lsa ilova ishga tushmaydi, kod `1`), keyin ilovani ishga tushiradi va `SIGTERM` ni unga uzatadi (polling to'xtaydi, ishlayotgan joblar 60 s gacha tugatiladi; `stop_grace_period: 75s`).
- **Qayta ishga tushirish:** `restart: unless-stopped`, ya'ni server qayta yuklangandan keyin ham bot o'zi ko'tariladi. Sozlama xatosi (kod `2`) da entrypoint chiqishdan oldin 60 s kutadi, shuning uchun tez halqa bo'lmaydi; konteyner `unhealthy` ko'rinadi. `.env` ni tuzatib `docker compose up -d` qiling. Sabab: [ADR 0014](docs/adr/0014-deploy-and-ops.md).
- **Healthcheck:** `python -m app.ops.healthcheck` (baza, disk joyi, real rejimda rejalashtiruvchi tirikligi). `docker compose ps` da `healthy`/`unhealthy`.
- **O'rganish halqasi:** `LEARNING_MODE=off|shadow|apply` (standart `shadow`: haftalik job hisoblaydi va jurnalga "nima qilgan bo'lardi" deb yozadi, lekin tahlilga ta'sir qilmaydi). `apply` ga o'tish odam qarori, paper trading davomida shadow jurnalini ko'rgandan keyin ([RUNBOOK 4.1](RUNBOOK.md), [ADR 0013](docs/adr/0013-learning-loop-math.md)).
- **Servislar rejimi:** `APP_SERVICES=fake` (standart: namunaviy ma'lumot, kalitlarsiz sinash) yoki `real` (pipeline + rejalashtiruvchi; `TELEGRAM_BOT_TOKEN`, `API_FOOTBALL_KEY`, `GEMINI_API_KEY` kerak). `.env` ga yozing va `docker compose up -d`.
- Resurs chegaralari 1–2 GB VPS uchun: `mem_limit: 768m`, `cpus: 1.5`; Docker loglari 5 × 10 MB.
- TLS'ni ushlab qoluvchi korporativ proksi ortida build: `docker build --secret id=ca_bundle,src=ca.pem .` (sertifikat image'ga yozilmaydi).
- Qo'lda ishlatiladigan skriptlar image ichida (`/app/scripts`): `docker compose run --rm --no-deps --entrypoint python bot /app/scripts/probe_api.py --help`.

## Serverga deploy (F9)
To'liq qo'llanma: **[RUNBOOK.md](RUNBOOK.md)** (o'zbekcha): birinchi deploy, kalitlar, F0 probe va backtest serverda, yangilash/rollback, zaxira va tiklash, kalit almashtirish, loglar, muammolar, production ro'yxati.

| Nima | Qayerda |
|---|---|
| Server tayyorlash (Ubuntu 24.04: SSH faqat kalit, ufw, fail2ban, avto-yangilanish, Docker; `--dry-run`) | [deploy/bootstrap.sh](deploy/bootstrap.sh) |
| CI/CD: `v*` teg yoki qo'lda → GHCR → SSH → zaxira → `up -d` → healthcheck → avtomatik rollback | [.github/workflows/deploy.yml](.github/workflows/deploy.yml), [deploy/remote-deploy.sh](deploy/remote-deploy.sh) |
| Zaxira (SQLite `.backup` → tekshiruv → gzip → ixtiyoriy `rclone`, 14 kun) va tiklash (tasdiq + xavfsizlik nusxasi) | [scripts/ops/backup.sh](scripts/ops/backup.sh), [scripts/ops/restore.sh](scripts/ops/restore.sh) |
| systemd: kunlik zaxira taymeri, ixtiyoriy boot xizmati | [deploy/](deploy/) `*.service`, `*.timer` |
| Dead-man's switch | `HEALTHCHECKS_URL` (healthchecks.io), [src/app/ops/heartbeat.py](src/app/ops/heartbeat.py) |

GitHub secret'lari (`Settings → Secrets → Actions`): `SSH_HOST`, `SSH_USER`, `SSH_KEY` (+ tavsiya: `SSH_KNOWN_HOSTS`). Ular bo'lmasa deploy workflow **o'tkazib yuboriladi** (xato emas). Hozircha hech qanday secret yo'q.

## CI
GitHub Actions ([.github/workflows/ci.yml](.github/workflows/ci.yml)) har push va PR da: lint, format, mypy, import-linter, deptry, shellcheck, actionlint, pytest + coverage, pip-audit, gitleaks va Docker build + smoke test (migratsiya, healthcheck, exit 2). Hech qanday secret talab qilinmaydi. Deploy alohida workflow: yuqoriga qarang.
