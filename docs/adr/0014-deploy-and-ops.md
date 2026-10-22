# ADR 0014: Deploy va operatsiyalar (F9)

Holat: qabul qilingan · To'lqin T5 · infra-engineer

## Kontekst
F9 qabul mezoni: server qayta yuklangandan keyin tizim o'zi tiklanadi; zaxiradan tiklash sinovdan o'tgan; deploy bitta merge/teg bilan bajariladi. Haqiqiy serverga ulanib bo'lmaydi (kalitlar va VPS hali yo'q), shuning uchun hamma narsa skript, workflow va testlar sifatida tayyorlandi; serverda bajarish foydalanuvchiga qoldirilgan ([RUNBOOK.md](../../RUNBOOK.md)).

## Qarorlar

### 1. Qayta ishga tushirish: `restart: unless-stopped` + entrypoint'dagi cheklov
Ochiq masala #1: `on-failure:5` Docker daemon qayta ishga tushganda yoki server reboot bo'lganda konteynerni ko'tarmaydi. Variantlar:

| Variant | Reboot'dan keyin | Sozlama xatosi (exit 2) | Murakkablik |
|---|---|---|---|
| `on-failure:5` (eski) | yo'q (hisoblagich nolga tushmaydi, daemon restartda ko'tarmaydi) | 5 urinishdan keyin to'xtaydi | past |
| `unless-stopped` yalang'och | ha | cheksiz tez halqa | past |
| **`unless-stopped` + entrypoint'da exit 2 kechiktirish** (tanlandi) | ha | har urinish orasida `CONFIG_ERROR_BACKOFF_SECONDS` (60 s) + Docker'ning o'z backoff'i; konteyner `unhealthy` ko'rinadi | past (60 qatorli sh skript) |
| systemd unit (`Restart=on-failure`, `RestartPreventExitStatus=2`) | ha | to'liq to'xtaydi | o'rta: Docker `restart: no`, ikki nazoratchi |

Tanlov: `unless-stopped`. `deploy/entrypoint.sh` ilova exit 2 bilan chiqsa, sababni logga yozadi, healthcheck uchun belgi fayl (`/tmp/ai-tahlilchi-config-error`) qo'yadi va chiqishdan oldin 60 s kutadi. Natija: halqa sekin (daqiqasiga bir urinish), sabab logda, `docker ps` da `unhealthy`; `.env` tuzatilib `docker compose up -d` qilinsa tiklanadi. systemd varianti `deploy/ai-tahlilchi.service` sifatida tayyor (`RESTART_POLICY=no` bilan), lekin standart emas. Oqibat: exit 2 da bot hech qachon "butunlay to'xtamaydi", faqat sekin urinadi; bu halqa yo'qotadigan narsa yo'q (so'rov yubormaydi: token tekshiruvi `getMe` 1 ta so'rov, 401 da to'xtaydi).

### 2. Migratsiya ilovadan oldin, entrypoint'da
ADR 0007 12 va ADR 0008: `alembic upgrade head` ilovadan oldin majburiy. `Dockerfile` endi `alembic/` va `alembic.ini` ni image'ga qo'shadi (ochiq masala #3); entrypoint migratsiyani ishga tushiradi, muvaffaqiyatsiz bo'lsa ilova **ishga tushmaydi** (exit 1). Migratsiya orqaga qaytarilmaydi: rollback faqat image'ni qaytaradi; sxema qo'shimcha (additive) bo'lsa eski image yangi sxemada ishlaydi, aks holda `pre-deploy` zaxirasidan tiklanadi (RUNBOOK).

### 3. Healthcheck `python -m app.ops.healthcheck` (faqat o'qiydi)
Tekshiruvlar: sozlama xatosi belgisi yo'q; baza ochiladi (`mode=ro`); disk joyi ≥ 200 MB (baza va WAL hajmi hisobotda); `APP_SERVICES=real` bo'lsa rejalashtiruvchi tirik. Rejalashtiruvchi tirikligi: APScheduler har ishdan keyin jobstore'dagi `next_run_time` ni yangilaydi, `daily-tick` har daqiqada ishlaydi, shuning uchun uning `next_run_time` i 5 daqiqadan ko'proq kechiksa, event loop qotgan yoki rejalashtiruvchi o'lgan. Ilova kodiga teginish yoki ledger'ga har daqiqada yozish kerak emas. Bot polling shu jarayon va shu event loop'da: qotsa, tekshiruv yiqiladi; polling o'lsa jarayon tugaydi va Docker qayta ishga tushiradi. "Bot ulangan" uchun alohida Telegram so'rovi **yo'q**: har daqiqada API so'rovi 429/kvota xavfini oshiradi.

### 4. Graceful stop
Entrypoint SIGTERM ni ilovaga uzatadi va uning haqiqiy chiqish kodini kutadi; ilova pollingni to'xtatadi, rejalashtiruvchi ishlayotgan joblarni 60 s gacha kutadi (`STOP_TIMEOUT_SECONDS`). `stop_grace_period: 75s` shundan uzunroq bo'lishi kerak (test bilan mahkamlangan). Kutish paytida SIGTERM kelsa (exit 2 kechiktirishi) entrypoint darhol chiqadi.

### 5. FSM: SQLite fayli, alohida baza
`SqliteStorage` (`src/app/bot/storage.py`) `fsm.db` faylida, asosiy bazada **emas**: (a) `alembic check` toza qoladi (FSM jadvallari Alembic sxemasida yo'q), (b) wizard yozuvlari pipeline'ning yagona yozuvchi qulfi bilan raqobatlashmaydi, (c) fayl yo'qolsa faqat tugallanmagan wizardlar tiklanadi, shuning uchun zaxiraga kiritilmaydi. Buzilgan JSON bo'sh holat sifatida o'qiladi (ogohlantirish bilan). Redis kerak emas (bitta jarayon).

### 6. Zaxira: Python orqali onlayn nusxa, ilova image'ida
`sqlite3` CLI serverda kerak bo'lmasligi va mantiq testlanishi uchun `app.ops.sqlite_tool` (`Connection.backup` = CLI `.backup` bilan bir xil mexanizm): onlayn nusxa → `PRAGMA integrity_check` → gzip → fayl. Shell skriptlar (`scripts/ops/backup.sh`, `restore.sh`) uni konteynerda `compose run --no-deps -T --entrypoint python bot -m app.ops.sqlite_tool` orqali ishga tushiradi (host faqat Docker talab qiladi). Muhim xossalar: buzilgan nusxa fayl qoldirmaydi (`.part` + atomik nom); tiklash avval tekshiradi, joriy bazani `pre-restore/` ga saqlaydi, eski `-wal/-shm` ni o'chiradi va bitta atomik `rename` bilan almashtiradi; tiklashdan keyin entrypoint migratsiyani yurgizadi (eski sxemali zaxira avtomatik yangilanadi). **Tiklash mashqi** testi (`tests/ops/test_backup_restore_scripts.py`): migratsiyalangan baza → zaxira → bazani buzish → tiklash → mazmun + `alembic check`. Saqlash: 14 kun mahalliy (+ `rclone` bo'lsa masofaviy, `--min-age`). Mahalliy zaxira bir xil diskda: u disk/server yo'qolishidan himoya qilmaydi, shuning uchun `RCLONE_REMOTE` tavsiya etiladi (RUNBOOK).

### 7. Deploy: teg yoki qo'lda, serverda skript, avtomatik rollback
`deploy.yml` (`v*` teg yoki `workflow_dispatch`): `gate` ishi `SSH_HOST/SSH_USER/SSH_KEY` borligini tekshiradi, yo'q bo'lsa qolgan ishlar **o'tkazib yuboriladi** va workflow yashil tugaydi. Keyin: image → GHCR (`vX.Y.Z` va `sha-...` teglari) → fayllarni SCP → `remote-deploy.sh` SSH orqali. Mantiq workflow'da emas, serverdagi skriptda (qo'lda ham ishlaydi, soxta `docker` bilan testlanadi): zaxira (muvaffaqiyatsiz bo'lsa to'xtaydi) → `pull` (muvaffaqiyatsiz bo'lsa hech narsa o'zgarmaydi) → `deploy.env` yangilanadi → `up -d` → ketma-ket 2 marta `healthy` kutiladi (5 daqiqagacha) → bo'lmasa oldingi image'ga avtomatik qaytish (`deploy.env.prev`). GHCR tokeni SSH stdin orqali uzatiladi (buyruq satrida/muhitda emas) va deploydan keyin `docker logout` qilinadi. Harakatlar commit SHA bilan mahkamlangan (`ci.yml` kabi), sirlar faqat `env:` orqali, `environment: production` (ixtiyoriy tasdiqlovchilar), `concurrency` bilan bir vaqtda ikki deploy yo'q. Host kaliti `SSH_KNOWN_HOSTS` bilan mahkamlanadi (bo'lmasa birinchi ulanishda ishonish + ogohlantirish).
Rad etilgan: har `main` merge'da deploy (F9.3 matni): paper trading davrida har merge prodga chiqishi xavfli; teg ongli reliz degani. Talab qilinsa `on: push: branches: [main]` qo'shish bir qator.

### 8. Monitoring: healthchecks.io dead-man's switch
`app.ops.heartbeat.Heartbeat` kunlik sikl joblaridan (`settle`, `refresh`) keyin ping yuboradi, job xato bilan tugasa `/fail`. Hech qachon istisno ko'tarmaydi (timeout 10 s, 3 urinish, backoff), URL logga yozilmaydi (UUID maxfiy). Check sozlamasi: period 1 kun, grace 3–6 soat (refresh 08:30 Toshkent). Ichki ogohlantirish (`alert_throttled`, adminga Telegram) pipeline'da mavjud: ketma-ket xatolar adminga boradi; dead-man's switch esa jarayon o'lgan holatni ushlaydi. Disk/baza hajmi healthcheck chiqishida.

### 9. Server: oddiy va qaytariladigan
`deploy/bootstrap.sh`: idempotent, `--dry-run`, Ubuntu 24.04. Kalitsiz parolni o'chirish taqiqlangan (kalit fayli majburiy argument), sshd sozlamasi `sshd -t` bilan tekshirilib qo'llaniladi, `ufw` faqat SSH (Docker port e'lon qilmaydi: bot Telegram'dan tortadi), 2 GB va kamroq RAM da 1 GB swap. Hajm: 2 vCPU / 2 GB / 20+ GB yetarli; 1 GB + swap ham ishlaydi (compose `mem_limit: 768m`).

## Oqibatlar
- Haqiqiy server, GHCR, healthchecks.io va rclone bilan **sinalmagan** qismlar: `bootstrap.sh` (faqat `--dry-run` va `shellcheck`), `deploy.yml` (faqat `actionlint` va statik testlar), masofaviy zaxira. Birinchi deploy RUNBOOK bo'yicha qo'lda kuzatib bajariladi.
- Migratsiya orqaga qaytmasligi: har yangi migratsiya bir reliz davomida orqaga mos (additive) bo'lishi kerak; qoida RUNBOOK da.
- Healthcheck `daily-tick` job identifikatoriga bog'liq; `app.scheduler.JOB_DAILY_TICK` o'zgarsa, test yiqiladi.
- `fsm.db` zaxiraga kirmaydi (ongli qaror).
- Chiqish kodi 2 da konteyner 60 s tirik turadi (unhealthy): bu `docker compose ps` da ko'rinadigan signal.
