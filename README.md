# AI Tahlilchi

Sport o'yinlarini statistik model (Poisson / Dixon-Coles, bozor modeli) va Gemini yordamida tahlil qilib, "value" topilgan o'yinlar bo'yicha Telegram bot orqali tavsiya beruvchi tizim. Bot shaxsiy: faqat ruxsat berilgan foydalanuvchilarga (`ADMIN_IDS`) va faqat bot bilan **shaxsiy chatda** javob beradi (guruh va kanallar e'tiborsiz qoldiriladi).

> **Holat:** bot, pipeline, LLM qatlami, o'rganish halqasi, deploy skriptlari, zaxira va [RUNBOOK](RUNBOOK.md) tayyor (T1–T5 yopilgan, T6 tuzatishlar bosqichida; serverda hali ishga tushirilmagan). Joriy versiya: `0.6.x`, ishlayotgan build `app_started` logida (`version`, `sha`). Batafsil: [docs/HOLAT.md](docs/HOLAT.md), [docs/progress.md](docs/progress.md).

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
  scheduler.py      APScheduler (jadval alohida jobs.db faylda)
  ops/              healthcheck, heartbeat, sqlite_tool, watchdog, version
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
| `FOOTBALL_DATA_ORG_KEY` | barcha o'yin ma'lumotlari ("⚽ O'yinlar", ikkala rejimda) | football-data.org |
| `FOOTBALL_DATA_REQUESTS_PER_MINUTE` | ixtiyoriy | tarifingiz limiti: bepul `10` (standart), Standard `30` |
| `GEMINI_API_KEY` | LLM izohlari uchun | Google AI Studio |
| `APP_ENV` | yo'q | `dev` (rangli loglar) yoki `prod` (JSON loglar) |
| `DISPLAY_TZ` | yo'q | foydalanuvchiga vaqtni ko'rsatish mintaqasi, standart `Asia/Tashkent` (bazada doim UTC). POSIX `TZ` ishlatilmaydi |
| `APP_SERVICES` | yo'q | `fake` (standart) yoki `real` (kalitlar tayyor bo'lgach) |
| `STAKING_MODE` | yo'q | `flat` (standart, 1 birlik) yoki `kelly` (bankning chegaralangan chorak Kelly ulushi; bank/unit yo'q foydalanuvchida `flat`) |
| `INGEST_EXTRA_BOOKMAKERS` | yo'q | vergul bilan ajratilgan qo'shimcha bukmekerlar (masalan `bet365,unibet`); Pinnacle va 1xBet'dan tashqari koeffitsientlari saqlanadigan kontorlar. Standart bo'sh: har bir qo'shimcha kontor baza hajmini oshiradi ([ADR 0018](docs/adr/0018-t6-fix-round-a.md)) |
| `GEMINI_MODEL` | yo'q | standart `gemini-flash-latest` |
| `LEARNING_MODE` | yo'q | `off` / `shadow` (standart) / `apply`: haftalik o'rganish halqasi ([RUNBOOK 4.1](RUNBOOK.md), [ADR 0013](docs/adr/0013-learning-loop-math.md)) |
| `LEARNING_MANUAL_HOLD_DAYS` | yo'q | qo'lda rollbackdan keyin segment/kalibratsiya muzlatiladigan kunlar, standart `30` |
| `REFRESH_AT` | yo'q | kunlik baza yangilanishi vaqti (`DISPLAY_TZ` bo'yicha), standart `08:30`; tahlil faqat bazadan o'qiydi ([ADR 0021](docs/adr/0021-db-only-analysis.md)) |
| `MANUAL_REFRESH_MIN_MINUTES` | yo'q | adminning "Bazani yangilash" tugmasi ikki bosish orasidagi eng kam masofa (daqiqa), standart `30` |
| `WATCHDOG_STALL_MINUTES` | yo'q | rejalashtiruvchi `daily-tick` shuncha daqiqa ishlamasa jarayon kod `3` bilan chiqadi va Docker qayta ko'taradi; standart `10` |
| `APP_VERSION`, `GIT_SHA` | yo'q | image build argumentlari bilan o'rnatiladi (quyida); `app_started` logida chiqadi |
| `TLS_USE_SYSTEM_TRUST_STORE` (`true`/`false`), `TLS_CA_BUNDLE` (PEM fayl yo'li) | yo'q | korporativ TLS-proksi uchun, standart o'chiq; tekshiruv hech qachon o'chirilmaydi, [bo'lim](#korporativ-proksi-ortida) |
| `HEALTHCHECKS_URL` | yo'q | healthchecks.io ping URL (**maxfiy**: logga tushmaydi, httpx loglari ham ogohlantirish darajasida); kunlik sikldan keyin ping, unga faqat qisqa kod yuboriladi |
| `DB_PATH`, `LOG_LEVEL` | yo'q | standart: `data/app.db`, `INFO` (katta-kichik harf farqi yo'q) |

`.env` bo'lmasa ham ilova, testlar va CI ishlaydi. Kalit kerak bo'lgan komponent faqat ishga tushirilganda aniq xabar bilan to'xtaydi (masalan, `TELEGRAM_BOT_TOKEN is not set; add it to .env`).

Chiqish kodlari: `0` normal to'xtash, `1` kutilmagan xato (`fatal_error` logi), `3` ichki qorovul rejalashtiruvchi to'xtab qolganini topdi (`watchdog_stalled`; Docker qayta ko'taradi), `2` sozlama xatosi: kalit yo'q, noto'g'ri qiymat (xabarda faqat o'zgaruvchi nomi bo'ladi, qiymati emas), token noto'g'ri formatda yoki Telegram uni rad etgan. `.env` ni tuzating, qayta urinish yordam bermaydi.

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
APP_VERSION=0.6.1 GIT_SHA=$(git rev-parse HEAD) docker compose build   # versiya/commit bilan (bermasangiz dev/unknown)
docker compose logs -f bot       # loglar (JSON)
docker compose down              # to'xtatish
```
- Konteyner non-root (`uid 10001`) foydalanuvchi bilan, faqat o'qiladigan fayl tizimida ishlaydi. Baza `bot-data` nomli Docker volume'ida (`/data`: `app.db`, `fsm.db`, `jobs.db`: rejalashtiruvchi jadvali, zaxiraga kirmaydi va har startda qayta quriladi); volume image'dagi egalikni oladi, `chown` kerak emas.
- **Ishga tushish tartibi:** [deploy/entrypoint.sh](deploy/entrypoint.sh) avval `alembic upgrade head` ni bajaradi (muvaffaqiyatsiz bo'lsa ilova ishga tushmaydi, kod `1`), keyin ilovani ishga tushiradi va `SIGTERM` ni unga uzatadi (polling to'xtaydi, ishlayotgan joblar 60 s gacha tugatiladi; `stop_grace_period: 75s`).
- **Qayta ishga tushirish:** `restart: unless-stopped`, ya'ni server qayta yuklangandan keyin ham bot o'zi ko'tariladi. Sozlama xatosi (kod `2`) da entrypoint chiqishdan oldin 60 s kutadi, shuning uchun tez halqa bo'lmaydi; konteyner `unhealthy` ko'rinadi. `.env` ni tuzatib `docker compose up -d` qiling. Sabab: [ADR 0014](docs/adr/0014-deploy-and-ops.md).
- **Healthcheck:** `python -m app.ops.healthcheck` (baza, disk joyi, real rejimda rejalashtiruvchi tirikligi). `docker compose ps` da `healthy`/`unhealthy`. Rejalashtiruvchi to'xtab qolsa ichki qorovul (`WATCHDOG_STALL_MINUTES`) jarayonni kod `3` bilan to'xtatadi, `restart: unless-stopped` uni qayta ko'taradi.
- **Versiya:** image'ga `APP_VERSION` va `GIT_SHA` build argumentlari bilan quriladi (deploy workflow tegdan va commit'dan beradi); ilova startda `app_started version=... sha=...` yozadi, kod ichida `app.ops.version.get_version()`.
- **O'rganish halqasi:** `LEARNING_MODE=off|shadow|apply` (standart `shadow`: haftalik job hisoblaydi va jurnalga "nima qilgan bo'lardi" deb yozadi, lekin tahlilga ta'sir qilmaydi). `apply` ga o'tish odam qarori, paper trading davomida shadow jurnalini ko'rgandan keyin ([RUNBOOK 4.1](RUNBOOK.md), [ADR 0013](docs/adr/0013-learning-loop-math.md)).
- **Servislar rejimi:** `APP_SERVICES=fake` (standart: namunaviy ma'lumot, kalitlarsiz sinash) yoki `real` (pipeline + rejalashtiruvchi; `TELEGRAM_BOT_TOKEN`, `FOOTBALL_DATA_ORG_KEY`, `GEMINI_API_KEY` kerak). `.env` ga yozing va `docker compose up -d`.
- Resurs chegaralari 1–2 GB VPS uchun: `mem_limit: 768m`, `cpus: 1.5`; Docker loglari 5 × 10 MB.
- TLS'ni ushlab qoluvchi korporativ proksi ortida (build va ishga tushirish): "Korporativ proksi ortida" bo'limi (`docker-compose.proxy.yml`).

## Korporativ proksi ortida

Ba'zi tarmoqlarda proksi HTTPS trafikni ushlab, o'zining (korporativ) CA sertifikati bilan qayta imzolaydi. Standart sozlamada bot `ClientConnectorCertificateError` / `CERTIFICATE_VERIFY_FAILED` beradi (Telegram, Football-Data.org, Gemini, healthchecks.io). Ikki ixtiyoriy sozlama bor (standart: o'chiq; tekshiruv hech qachon o'chirilmaydi, [ADR 0017](docs/adr/0017-tls-trust-configuration.md)):

| O'zgaruvchi | Ma'nosi |
|---|---|
| `TLS_USE_SYSTEM_TRUST_STORE=true` | OS sertifikat ombori (Windows: korporativ CA allaqachon shu yerda). Windows'da lokal ishga tushirish uchun eng ishonchli yo'l |
| `TLS_CA_BUNDLE=C:/certs/ca.pem` | proksi CA sertifikati(lar)i bor PEM fayl. Ikkalasi berilsa, OS ombori ustun |

**Lokal ishga tushirish** (`.env` ga yozing):
```
TLS_USE_SYSTEM_TRUST_STORE=true
# yoki:  TLS_CA_BUNDLE=C:/certs/corp-ca.pem
```
Fayl topilmasa yoki o'qib bo'lmasa, ilova `ConfigurationError` bilan (kod `2`) to'xtaydi; fayl mazmuni hech qachon logga yozilmaydi.

**Docker** (build ham, ishga tushirish ham bitta fayl bilan; sertifikat image'ga yoki gitga tushmaydi):
```bash
CA_BUNDLE_FILE=C:/certs/corp-ca.pem \
  docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d --build
```
(PowerShell: avval `$env:CA_BUNDLE_FILE="C:/certs/corp-ca.pem"`, keyin xuddi shu `docker compose ...`.) Build'da fayl BuildKit sirri sifatida `uv sync` ga beriladi; ishga tushganda faqat o'qiladigan sir sifatida `/run/secrets/ca_bundle` ga ulanadi va `TLS_CA_BUNDLE` shunga ko'rsatadi. Oddiy tarmoqda `docker-compose.yml` ning o'zi ishlatiladi. Windows'da yo'lda to'g'ri slesh (`/`) ishlating.

**Korporativ CA ni PEM ga eksport qilish:** Windows'da `certmgr.msc` > "Trusted Root Certification Authorities" > kerakli CA > o'ng tugma > All Tasks > Export > "Base-64 encoded X.509 (.CER)"; kengaytmani `.pem` ga o'zgartiring (bir nechta CA bo'lsa, mazmunlarini bitta faylga ketma-ket qo'shing). Ilova PEM ni o'qiganda OpenSSL ning qat'iy RFC 5280 tekshiruvini (Python 3.13 yoqqan; "Basic Constraints not marked critical" xatosini beradi) o'chiradi; zanjir, muddat va host nomi tekshiruvi saqlanadi.

> Ogohlantirish: korporativ firewall (masalan, FortiGate Application Control) Telegram'ni siyosat bo'yicha bloklashi mumkin. Bunda TLS ishlaydi, lekin `api.telegram.org` dan "Application Blocked" HTML sahifasi keladi (`ClientDecodeError`). Bu sertifikat muammosi emas: botni boshqa tarmoqda (server, uy interneti) ishga tushiring yoki IT'dan ruxsat so'rang.

## Lokal test (Docker)
[docker-compose.local.yml](docker-compose.local.yml): server faylidan (`docker-compose.yml`) alohida, faqat sinov uchun.
```bash
cp .env.example .env     # TELEGRAM_BOT_TOKEN va ADMIN_IDS ni to'ldiring
docker compose -f docker-compose.local.yml up --build
```
- Namunaviy ma'lumot (`APP_SERVICES=fake`), API kalitlari kerak emas. O'qishga qulay (rangli) loglar; xato bo'lsa qayta-qayta urinmasdan sababini ko'rsatib to'xtaydi (`restart: "no"`).
- Bazani brauzerda ko'rish (faqat o'qish, `http://127.0.0.1:8080`, port band bo'lsa `DB_BROWSER_PORT=18080`): `docker compose -f docker-compose.local.yml --profile tools up --build`. `sqlite-web` image'i digest bo'yicha qotirilgan. Baza WAL rejimida, shuning uchun volume `:ro` ulanmaydi (`-shm` fayli ochilmay `unable to open database file` chiqadi); yozishni `sqlite_web --read-only` o'zi taqiqlaydi. Brauzersiz so'rov: `docker compose -f docker-compose.local.yml run --rm --no-deps -T --entrypoint python bot -m app.ops.sqlite_tool query --db /data/app.db "SELECT ..."`.
- Korporativ TLS-proksi ortida: `CA_BUNDLE_FILE=C:/yo'l/ca.pem docker compose -f docker-compose.local.yml -f docker-compose.proxy.yml up --build`.
- To'xtatish: `docker compose -f docker-compose.local.yml down` (bazani ham o'chirish: `down -v`).
- Bot `api.telegram.org` ga ulanadi. Ba'zi tarmoqlarda (korporativ firewall) Telegram bloklangan bo'ladi, bunda Telegram ruxsat berilgan tarmoqdan sinang.

## Ish papkasini tozalash
Parallel agent to'lqinlaridan keyin `wave/*` shoxlari va `.claude/worktrees` to'planadi. [scripts/ops/clean_worktrees.sh](scripts/ops/clean_worktrees.sh) `main` ga to'liq qo'shilgan shoxlarni va toza, kamida 6 soat tegilmagan worktree'larni topadi. Standart **dry-run** (hech narsa o'zgarmaydi): `bash scripts/ops/clean_worktrees.sh`; haqiqatan o'chirish: `--apply`. Qo'shilmagan shox yoki o'zgarishi bor worktree hech qachon o'chirilmaydi (`git branch -d`, `git worktree remove` kuchsiz rejimda).

## Serverga deploy (F9)
To'liq qo'llanma: **[RUNBOOK.md](RUNBOOK.md)** (o'zbekcha): birinchi deploy, kalitlar, F0 probe va backtest serverda, yangilash/rollback, zaxira va tiklash, kalit almashtirish, loglar, muammolar, production ro'yxati.

| Nima | Qayerda |
|---|---|
| Server tayyorlash (Ubuntu 24.04: SSH faqat kalit, ufw, fail2ban, avto-yangilanish, Docker; `--dry-run`) | [deploy/bootstrap.sh](deploy/bootstrap.sh) |
| CI/CD: `v*` teg yoki qo'lda → GHCR → SSH → zaxira → `up -d` → healthcheck → avtomatik rollback | [.github/workflows/deploy.yml](.github/workflows/deploy.yml), [deploy/remote-deploy.sh](deploy/remote-deploy.sh) |
| Zaxira (SQLite `.backup` → tekshiruv → gzip → ixtiyoriy `rclone`, 14 kun) va tiklash (tasdiq + xavfsizlik nusxasi) | [scripts/ops/backup.sh](scripts/ops/backup.sh), [scripts/ops/restore.sh](scripts/ops/restore.sh) |
| systemd: kunlik zaxira taymeri, ixtiyoriy boot xizmati | [deploy/](deploy/) `*.service`, `*.timer` |
| Dead-man's switch | `HEALTHCHECKS_URL` (healthchecks.io), [src/app/ops/heartbeat.py](src/app/ops/heartbeat.py) |
| Qotgan rejalashtiruvchini qayta ishga tushirish | [src/app/ops/watchdog.py](src/app/ops/watchdog.py), `WATCHDOG_STALL_MINUTES` |
| Yangilanishlar va zaifliklar | [.github/dependabot.yml](.github/dependabot.yml) (haftalik: Python/`uv`, Actions, Docker), [.github/workflows/audit.yml](.github/workflows/audit.yml) (haftalik `pip-audit`, issue ochmaydi, qizil bo'ladi) |
| Birgalikdagi ishdan keyin tozalash | [scripts/ops/clean_worktrees.sh](scripts/ops/clean_worktrees.sh) |

GitHub secret'lari (`Settings → Secrets → Actions`): `SSH_HOST`, `SSH_USER`, `SSH_KNOWN_HOSTS` (**majburiy**, birinchi ulanishga ko'r-ko'rona ishonish yo'q) va `SSH_KEY`, ya'ni `production` environment ichida (required reviewers bilan). `SSH_HOST`/`SSH_USER`/`SSH_KNOWN_HOSTS` bo'lmasa deploy workflow **o'tkazib yuboriladi** (xato emas). Deploy faqat shu commit uchun CI yashil bo'lganda ishlaydi. Hozircha hech qanday secret yo'q.

## CI
GitHub Actions ([.github/workflows/ci.yml](.github/workflows/ci.yml)) har push va PR da: lint, format, mypy, import-linter, deptry, shellcheck, actionlint, tezkor testlar (`-m "not slow"`) + coverage darvozalari, sekin testlar (`-m slow`, qamrovsiz, alohida ish), pip-audit, gitleaks va Docker build + smoke test (migratsiya, healthcheck, exit 2). Mahalliy to'liq tekshiruv `uv run pytest` (hammasi) yoki tezkor: `uv run pytest -m "not slow"`. Hech qanday secret talab qilinmaydi. Deploy alohida workflow: yuqoriga qarang.
