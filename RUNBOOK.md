# RUNBOOK: AI Tahlilchi serverda ishlatish

Bu hujjat serverni birinchi marta sozlashdan boshlab kundalik ishlatish, xatolarni tuzatish va production'ga o'tish mezonlarigacha bo'lgan qadamlarni beradi. Qarorlar sababi: [ADR 0014](docs/adr/0014-deploy-and-ops.md). Barcha buyruqlar serverda `deploy` foydalanuvchisi nomidan, `/opt/ai-tahlilchi` papkasida bajariladi (aksi aytilmasa).

> Hamma narsa skript va testlar bilan tayyorlangan, lekin **haqiqiy serverda hali sinalmagan**. Birinchi deploy'ni kuzatib turing va 3-bo'limdagi tekshiruvlarni o'tkazing.

## 1. Umumiy ko'rinish

```
GitHub (teg v*)  ──►  CI: image qurish ──► GHCR ──► SSH ──► server: bin/remote-deploy.sh
                                                              ├─ zaxira (pre-deploy)
                                                              ├─ docker pull + up -d
                                                              └─ healthy kutiladi, bo'lmasa avtomatik rollback
Server /opt/ai-tahlilchi/
  docker-compose.yml   compose fayli (deploy yuklaydi)
  .env                 SIRLAR (chmod 600, faqat serverda)
  deploy.env           joriy image (AI_TAHLILCHI_IMAGE=...), deploy.env.prev oldingisi
  backups/             kunlik zaxiralar (app-YYYYMMDDTHHMMSSZ-daily.db.gz)
  bin/                 dc, backup.sh, restore.sh, remote-deploy.sh
Docker volume ai-tahlilchi_bot-data (/data): app.db (+ -wal), jobs.db, fsm.db, pre-restore/
  (jobs.db = rejalashtiruvchi jadvali: zaxiraga KIRMAYDI, har startda qayta quriladi, 8-bo'lim)
```

Qisqa buyruqlar (`bin/dc` = loyiha sozlamalari bilan `docker compose`):

| Maqsad | Buyruq |
|---|---|
| Holat va sog'liq | `bin/dc ps` · `bin/dc exec bot python -m app.ops.healthcheck` |
| Loglar | `bin/dc logs -f --tail 200 bot` |
| Qayta ishga tushirish | `bin/dc restart bot` |
| To'xtatish / ishga tushirish | `bin/dc stop bot` / `bin/dc up -d --no-build bot` |
| Qo'lda zaxira | `bin/backup.sh` |
| Botda | `/holat` (rejim, API limitlari, oxirgi ishlar va xatolar) |

## 2. Server tanlash va xarajat

- **Tavsiya:** 2 vCPU / 2 GB RAM / 20+ GB SSD, Yevropa (API'larga yaqin), Ubuntu 24.04. Hetzner CX22 va o'xshashlari ≈ $4–6/oy. Bot bo'sh turganda ≈ 200 MB ishlatadi. Baza hajmi (**taxmin, paper trading davomida o'lchang**: `bin/dc exec bot python -m app.ops.healthcheck` baza/WAL hajmini beradi): fix A dan oldin barcha bukmekerlarni saqlash ≈ 7 GB/yil edi; endi faqat 2 bukmeker (Pinnacle va 1xBet) ≈ 3 MB/kun, haftalik `retention` bilan barqaror holat ≈ 0.3 GB va keyin ≈ 0.2 GB/yil. `INGEST_EXTRA_BOOKMAKERS` bilan qo'shilgan har bir kontor buni oshiradi.
- 1 GB RAM ham ishlaydi (bootstrap 1 GB swap yaratadi; konteyner chegarasi 768 MB), lekin backtest/import ni (7-bo'lim) ko'proq xotira talab qiladi.
- Oylik: VPS $4–6 + Football-Data.org (bepul tarif 10 so'rov/daqiqa, kunlik limit yo'q; koeffitsient va tarkiblar pullik paketlarda) + Gemini bepul tarif ($0). healthchecks.io bepul tarifi yetarli.

## 3. Birinchi deploy

### 3.1 Tayyorgarlik (sizning kompyuteringizda)
1. SSH kalit juftligi: `ssh-keygen -t ed25519 -f ~/.ssh/ai_tahlilchi_deploy -C ai-tahlilchi-deploy` (parol qo'yish tavsiya etiladi; GitHub Actions uchun **alohida**, parolsiz kalit yaratish ham mumkin: `-f ~/.ssh/ai_tahlilchi_ci`).
2. VPS yarating (Ubuntu 24.04), root parol/kalit bilan kiring.
3. Telegram: @BotFather dan token, @userinfobot dan o'z `user_id` (F0.1). API kalitlari 4-bo'limda.

### 3.2 Serverni sozlash (root sifatida, bir marta)
```bash
# kompyuteringizdan: repozitoriydan deploy/ papkasini serverga nusxalang
scp -r deploy root@SERVER:/root/deploy
scp ~/.ssh/ai_tahlilchi_deploy.pub root@SERVER:/root/deploy_key.pub     # CI kalitining .pub ini ham shu faylga qo'shing
ssh root@SERVER
cd /root/deploy && chmod +x bootstrap.sh
./bootstrap.sh --dry-run --ssh-key-file /root/deploy_key.pub      # nima qilinishini ko'ring
./bootstrap.sh --ssh-key-file /root/deploy_key.pub
```
Skript idempotent (qayta ishga tushirish xavfsiz). U: `deploy` foydalanuvchisi, faqat kalit bilan SSH (parol va root login o'chiriladi), `ufw` (faqat SSH), `fail2ban`, avtomatik xavfsizlik yangilanishlari, Docker, `/opt/ai-tahlilchi`, bo'sh `.env` (chmod 600), kunlik zaxira taymeri (02:30 UTC).

> **Chiqib ketishdan oldin** YANGI terminalda `ssh -i ~/.ssh/ai_tahlilchi_deploy deploy@SERVER` bilan kirib ko'ring. Ishlamasa, root sessiyasi ochiq turganda tuzating.

### 3.3 Sirlar serverda
```bash
ssh deploy@SERVER
cd /opt/ai-tahlilchi
nano .env        # TELEGRAM_BOT_TOKEN va ADMIN_IDS ni kiriting; APP_SERVICES=fake qoldiring
ls -l .env       # -rw------- bo'lishi kerak
```

### 3.4 GitHub sozlamalari
Repozitoriy → Settings → Secrets and variables → Actions → **Repository secrets**:

| Secret | Qiymat |
|---|---|
| `SSH_HOST` | server IP yoki domen |
| `SSH_USER` | `deploy` |
| `SSH_KEY` | deploy kalitining **maxfiy** qismi (butun fayl, `-----BEGIN` dan `END` gacha); **`production` environment ichida** saqlang (pastga qarang) |
| `SSH_KNOWN_HOSTS` | **majburiy.** `ssh-keyscan -t ed25519 SERVER` natijasi, serverning haqiqiy kaliti (barmoq izi) ekanini boshqa kanal orqali tekshirgach. Birinchi ulanishga ko'r-ko'rona ishonish (TOFU) yo'q: bu secret bo'lmasa workflow o'tkazib yuboriladi |

`SSH_HOST`, `SSH_USER`, `SSH_KNOWN_HOSTS` yo'q bo'lsa `Deploy` workflow **o'tkazib yuboriladi va yashil tugaydi** (hech narsa buzilmaydi).

**`production` environment (tavsiya etiladi):** Settings → Environments → `production` → *Required reviewers* (o'zingizni qo'shing) va `SSH_KEY` ni (xohlasangiz `SSH_HOST`/`SSH_USER` ni ham) shu yerdagi *Environment secrets* ga ko'chiring, repozitoriy secret'laridan o'chiring. Shunda kalitni faqat siz tasdiqlagan deploy ishi o'qiy oladi: boshqa workflow yoki shoxlardagi kod unga yetmaydi. `SSH_KEY` environment ichida bo'lgani uchun `gate` uni ko'rmaydi; yo'qligi deploy ishida aniq xato bilan chiqadi.

**Deploy faqat yashil CI da:** deploy qilinadigan commit uchun `ci.yml` ning barcha tekshiruvlari (lint, tezkor va sekin testlar, xavfsizlik, Docker build) `success` bo'lishi shart, aks holda `ci` ishi workflow'ni to'xtatadi va image qurilmaydi. Shuning uchun tegni oldin `main` ga CI o'tgan commit'ga qo'ying. Image'ga `APP_VERSION` (teg) va `GIT_SHA` (commit) quriladi (6.1).

GHCR paketi repozitoriy bilan bog'lanadi; server tokenni deploy paytida stdin orqali oladi, doimiy parol saqlanmaydi. Haftalik `Dependency audit` workflow (`pip-audit`) va Dependabot (Python, Actions, Docker) yangi zaifliklar va yangilanishlarni o'zi ko'rsatadi: audit qizil bo'lsa Actions xabari keladi, issue ochilmaydi.

### 3.5 Birinchi reliz
```bash
git tag v0.9.0 && git push origin v0.9.0           # yoki Actions → Deploy → Run workflow (birinchi marta "skip_backup" ni yoqing)
```
Workflow: image → GHCR → serverga fayllar → `remote-deploy.sh`. Birinchi deploy'da zaxira olinmaydi (baza yo'q).

### 3.6 Tekshiruv (F9 qabul mezoni)
1. `bin/dc ps`: `bot` holati `Up ... (healthy)`.
2. Telegram'da botga `/start`: javob keladi. Whitelist'da bo'lmagan hisobdan javob **kelmasligi** kerak.
3. **Reboot sinovi:** `sudo reboot`, 2–3 daqiqadan keyin `bin/dc ps` (healthy) va `/start`.
4. **Tiklash mashqi:** `bin/backup.sh`, keyin `bin/restore.sh --dry-run backups/<fayl>`; vaqt topib to'liq `bin/restore.sh` ni ham bir marta sinab ko'ring (7-bo'lim).
5. **Rollback sinovi:** `bin/remote-deploy.sh ghcr.io/<egasi>/ai-tahlilchi:<oldingi-teg>` oldingi versiyaga qaytaradi (bir buyruq).
6. healthchecks.io: 5-bo'lim.

## 4. Kalitlarni qo'shish va real rejimga o'tish

`APP_SERVICES=fake` (standart) botni namunaviy ma'lumotlar bilan ishga tushiradi (log: `services_backend ... sample_data=true`). Real rejim uchun:

1. Kalitlarni oling: `FOOTBALL_DATA_ORG_KEY` (football-data.org), `GEMINI_API_KEY` (Google AI Studio). Tarifingiz Standard bo'lsa `FOOTBALL_DATA_REQUESTS_PER_MINUTE=30`. (`API_FOOTBALL_KEY`, `HANDICAP_CONVENTION`, `ODDS_API_KEY` endi ishlatilmaydi; `.env` da qolgan bo'lsa e'tiborga olinmaydi, ADR 0025.)
2. `nano /opt/ai-tahlilchi/.env`: kalitlarni yozing. **Hali** `APP_SERVICES=real` qilmang.
3. `APP_SERVICES=real` ga o'zgartiring, `bin/dc up -d --no-build bot` (yangi muhit bilan qayta yaratiladi; `restart` yetmaydi).
4. Loglarda `services_backend ... backend=real`, `scheduler_started jobs=[...]` (9 ta job: `settle`, `refresh`, `daily-tick`, `template-tick`, `recalc-tick`, `learning`, `feedback`, `lessons`, `retention`; ro'yxat `app.scheduler.JOB_NAMES` da; `retention` har yakshanba 04:00 (Toshkent) da 90 kundan eski oraliq narx suratlarini o'chiradi va `/holat` da ko'rinadi). Healthcheck endi rejalashtiruvchi tirikligini ham tekshiradi.
5. Botda `/holat`: ishlayotgan versiya (`Versiya: ...`) va oxirgi ishlar ko'rinadi. "⚽ O'yinlar" Football-Data.org dan jonli ishlaydi. Kunlik `refresh` (08:30, `REFRESH_AT`) yoki qo'lda `bin/dc exec bot python -m app.pipeline.refresh` o'yin, natija va (Odds paketi bo'lsa) 1X2 koeffitsientlarini Football-Data.org dan bazaga yozadi ([ADR 0025](docs/adr/0025-football-data-only.md)). Odds paketisiz signal tahlili narx topmaydi.

### 4.1 O'rganish halqasi rejimi (`LEARNING_MODE`)

Haftalik o'rganish (dushanba 09:00) uch rejimda ishlaydi: `off` (hech narsa), **`shadow` (standart)** va `apply`. `shadow` da job hisoblaydi va "nima qilgan bo'lardi" ni `learning_changes` jadvaliga `applied=0` bilan yozadi, lekin tahlil xom model bilan ishlaydi: EV chegarasi ko'tarilmaydi, segment o'chirilmaydi, kalibratsiya qo'llanmaydi. Paper trading (F10) davomida jurnalni ko'rib turing: serverda `sqlite3` dasturi yo'q, shuning uchun image ichidagi vosita (faqat o'qiydi): `bin/dc exec -T bot python -m app.ops.sqlite_tool query --db /data/app.db "SELECT created_at, kind, subject, reason FROM learning_changes WHERE applied=0 ORDER BY id DESC LIMIT 20"` (bot to'xtagan bo'lsa `exec` o'rniga `bin/dc run --rm --no-deps -T --entrypoint python bot -m app.ops.sqlite_tool query ...`; natija TSV, eng ko'pi 1000 qator). `apply` ga o'tish faqat odam qarori: `.env` da `LEARNING_MODE=apply`, keyin `bin/dc up -d --no-build bot`. Qo'lda rollbackdan keyin segment va kalibratsiya `LEARNING_MANUAL_HOLD_DAYS` (standart 30) kun muzlatiladi. Sabab va simulyatsiyalar: [ADR 0013](docs/adr/0013-learning-loop-math.md). Botda: `/ozgarishlar` — jurnal (kuzatuv rejimidagi yozuvlar "shunday qilgan bo'lardi" deb belgilanadi), qo'llangan o'zgarishni tasdiq bilan qaytarish (`STALE` bo'lsa rad etiladi); har dushanba haftalik "model nimani o'rgandi" xabari; `/holat` da rejim va haftalik ish 8 kundan ko'p ishlamasa "hafta yo'qolgan" ogohlantirishi ([ADR 0016](docs/adr/0016-t5-bot-fix-and-f8-ui.md)).

## 5. Monitoring

- **Docker healthcheck** (har 60 s): sozlama xatosi belgisi yo'q, baza ochiladi, disk ≥ 200 MB bo'sh, (real rejimda) `daily-tick` 5 daqiqadan ortiq kechikmagan (rejalashtiruvchi o'z faylida: `/data/jobs.db`). Natija: `bin/dc exec bot python -m app.ops.healthcheck` (JSON: tekshiruvlar, baza/WAL hajmi, oxirgi ishlar).
- **healthchecks.io (dead-man's switch):** hisob oching → yangi check: *Period* 1 kun, *Grace* 4 soat → ping URL'ni `.env` dagi `HEALTHCHECKS_URL` ga yozing (URL maxfiy), `bin/dc up -d --no-build bot`. Ilova `settle` (08:00) va `refresh` (08:30) muvaffaqiyatli tugagach ping yuboradi, xato bo'lsa `/fail`. Ping to'xtasa, healthchecks.io sizga e-pochta/Telegram orqali xabar beradi: bu jarayon o'lgan, qotgan yoki server o'chgan holatni ushlaydi. Ikkinchi check (ixtiyoriy): zaxira uchun, URL'ni `/opt/ai-tahlilchi/backup.env` ga `BACKUP_PING_URL=...` deb yozing.
- **Ichki qorovul (watchdog):** rejalashtiruvchining `daily-tick` ishi `WATCHDOG_STALL_MINUTES` (standart 10) daqiqa ishlamasa, jarayon logga `watchdog_stalled` yozadi, tartibli to'xtaydi va **kod 3** bilan chiqadi; `restart: unless-stopped` uni qayta ko'taradi (Docker healthcheck o'zi qayta ishga tushirmaydi, shuning uchun bu kerak). Start va migratsiya paytida ishlamaydi (hisob rejalashtiruvchi ishga tushgandan boshlanadi). Tekshirish: `bin/dc logs bot | grep -E 'watchdog|scheduler_stalled_exit'`; `bin/dc ps` da qayta-qayta restart ko'rinsa sababini logdan qidiring.
- **Admin Telegram ogohlantirishi:** job qayta-qayta xato bersa pipeline adminlarga xabar yuboradi (cheklangan chastota bilan); `/holat` oxirgi xatolarni ko'rsatadi.
- **Disk:** healthcheck chiqishida `free ... MB`; 200 MB dan kam bo'lsa konteyner `unhealthy`. `docker system df` va `du -sh backups` bilan qo'lda ham qarang. Loglar Docker'da 5 × 10 MB bilan aylanadi.

## 6. Yangilash va orqaga qaytarish

**Yangilash:** `git tag vX.Y.Z && git push origin vX.Y.Z`. Workflow: CI shu commit uchun yashilligini tekshiradi → image quradi → serverda: zaxira (`pre-deploy`) → `pull` → `up -d` (entrypoint avval `alembic upgrade head` ni bajaradi) → 5 daqiqagacha `healthy` kutadi. `healthy` bo'lmasa **avtomatik oldingi image'ga qaytadi** va workflow qizil tugaydi.

**Qo'lda yangilash/rollback** (serverda, bir buyruq):
```bash
bin/remote-deploy.sh --dry-run ghcr.io/<egasi>/ai-tahlilchi:v0.9.0     # rejani ko'rish
bin/remote-deploy.sh ghcr.io/<egasi>/ai-tahlilchi:v0.9.0               # GHCR'ga kirish kerak bo'lsa: echo TOKEN | GHCR_USER=... bin/remote-deploy.sh ...
cat deploy.env deploy.env.prev                                          # joriy va oldingi image
```
**Sxema ogohlantirishi:** migratsiya orqaga qaytmaydi. Rollback faqat image'ni almashtiradi. Migratsiyalar bir reliz davomida orqaga mos (faqat qo'shish) yozilishi kerak. Eski image yangi sxemada ishlamasa, bazani `pre-deploy` zaxirasidan tiklang (8-bo'lim).

### 6.1 Qaysi versiya ishlayapti

Image'ga build vaqtida `APP_VERSION` (teg, `v` siz) va `GIT_SHA` (to'liq commit) quriladi. Ilova har startda `app_started version=... sha=...` yozadi:
```bash
bin/dc logs bot | grep app_started | tail -1
bin/dc exec -T bot python -c "import os; print(os.environ['APP_VERSION'], os.environ['GIT_SHA'])"
```
Lokal build: `APP_VERSION=0.6.1 GIT_SHA=$(git rev-parse HEAD) docker compose build` (bermasangiz `dev` / `unknown`). `/holat` da ko'rsatish uchun bot/servis qatlami `app.ops.version.get_version().label` ni chaqiradi.

**Konfiguratsiyani yangilash** (`.env`): o'zgartiring, so'ng `bin/dc up -d --no-build bot`.

## 7. F0 probe va haqiqiy ma'lumot bilan backtest (serverda)

Skriptlar image ichida (`/app/scripts`). Natijalar `/data` ga yoziladi (konteynerning boshqa joyi o'qish uchun). Ish konteyneri botdan alohida (`run`), shuning uchun bot ishlayotgan bo'lsa ham bo'ladi; og'ir backtest paytida xotira uchun botni to'xtatish ixtiyoriy.

```bash
cd /opt/ai-tahlilchi
RUN="bin/dc run --rm --no-deps --entrypoint python bot"

# Tarixiy ma'lumot (football-data.co.uk) → baza, so'ng backtest:
$RUN /app/scripts/import_history.py --cache-dir /data/history --db-path /data/history.db
$RUN /app/scripts/backtest.py --db-path /data/history.db --out-dir /data/backtest --label server
bin/dc run --rm --no-deps --entrypoint ls bot /data/backtest
```
- Tarix **alohida** `history.db` ga yoziladi (jonli `app.db` ga emas). Jonli bazaga import kerak bo'lsa, `--db-path /data/app.db` ni botni to'xtatib ishlating.
- **Sariq kartochka prognozi** (ADR 0027) uchun jonli bazada kartochkalar bo'lishi kerak: `0011` migratsiyasidan keyin tarixni jonli bazaga bir marta qayta import qiling (botni to'xtatib, `--db-path /data/app.db`). Upsert mavjud o'yinlarga kartochka va hakamni qo'shadi; qayta import xavfsiz (idempotent).
- football-data.co.uk lokal (korporativ) mashinada filtr/proksi sabab ochilmasligi mumkin; server odatda to'g'ridan-to'g'ri ulanadi, shuning uchun haqiqiy ma'lumotli backtest shu yerda bajariladi. Internet bo'lmasa `--offline` kesh bilan.
- Hisobotni olish: `bin/dc run --rm --no-deps -T --entrypoint cat bot /data/backtest/<fayl>.md > ~/hisobot.md`, so'ng `scp`.
- **F3 darvozasi:** backtest natijasi (CLV/Brier/kalibratsiya) mezonlardan o'tmasa, real rejimga o'tmang ([docs/adr/0005-backtest-methodology.md](docs/adr/0005-backtest-methodology.md)).

## 8. Zaxiradan tiklash

Zaxiralar: `/opt/ai-tahlilchi/backups/app-*.db.gz` (14 kun) + `RCLONE_REMOTE` bo'lsa masofaviy nusxa. Zaxiraga faqat `app.db` kiradi. `jobs.db` (rejalashtiruvchi, pickle saqlaydi) va `fsm.db` kirmaydi: rejalashtiruvchi jadvali har startda o'zi qayta quriladi.

**Mahalliy zaxira server disk'i bilan birga yo'qolishi mumkin: masofaviy nusxa sozlang.** Zaxira siqilgan, lekin **shifrlanmagan** baza: unda stavkalar, bank va sozlamalar bor. Tashqi omborga shifrlab yuklang: `rclone crypt` remote yarating (`rclone config` → *New remote* → type `crypt`, remote sifatida B2/Drive/S3 ni ko'rsating, parol va tuz saqlang) va `RCLONE_REMOTE` ni **crypt remote** ga yo'naltiring. Parolni zaxiradan alohida joyda saqlang: usiz zaxirani tiklab bo'lmaydi.
```bash
sudo apt-get install -y rclone && rclone config        # masalan Backblaze B2 / Google Drive / S3
echo 'RCLONE_REMOTE=ai-tahlilchi-crypt:' >> /opt/ai-tahlilchi/backup.env      # taymer shuni o'qiydi (crypt remote!)
```

**Tiklash** (bot avtomatik to'xtatiladi va qayta ishga tushiriladi):
```bash
ls -lh backups/                                            # qaysi zaxira
bin/restore.sh --dry-run backups/app-20261101T023000Z-daily.db.gz     # tekshiradi (butunlik + migratsiya raqami), o'zgartirmaydi
bin/restore.sh backups/app-20261101T023000Z-daily.db.gz               # "RESTORE" deb tasdiqlang
bin/dc logs --tail 50 bot ; # botda /holat
```
> **Faqat o'zingiz olgan zaxirani tiklang.** Zaxira ichidagi bazani ilova ishonchli deb o'qiydi, boshqa odam yasagan fayl esa kutilmagan ma'lumot yoki sxema olib kelishi mumkin. Rejalashtiruvchi jadvali (`jobs.db`) pickle saqlaydi: begona `jobs.db` ni nusxalash serverda kodni ishga tushirishi mumkin. Shuning uchun `jobs.db` zaxiraga kiritilmaydi va tiklanmaydi; begona fayldan tiklamang va `jobs.db` ni hech qachon tashqaridan olib kelmang (zarur bo'lsa, o'chiring: keyingi startda qayta quriladi, o'tkazib yuborilgan bitta kunlik ish tushishi mumkin).

Xavfsizlik: tiklashdan oldin joriy baza `/data/pre-restore/` ga saqlanadi (eng so'nggi **5** nusxa qoladi, eskilari o'zi o'chadi); buzuq zaxira hech narsani o'zgartirmaydi; eski sxemali zaxira keyingi startda avtomatik migratsiya qilinadi. Masofaviy zaxirani avval yuklab oling: `rclone copy remote:ai-tahlilchi/app-....db.gz backups/`.

**Oylik mashq (5 daqiqa):** eng so'nggi zaxirani `bin/restore.sh --dry-run` bilan tekshiring. Sinalmagan zaxira zaxira emas. (Avtomatik test: `tests/ops/test_backup_restore_scripts.py::test_restore_drill_recovers_content_and_schema`.)

Zaxira bajarilganini tekshirish: `systemctl list-timers ai-tahlilchi-backup.timer`, `journalctl -u ai-tahlilchi-backup.service -n 30`, `ls -lt backups | head`.

## 9. Kalitlarni almashtirish

Umumiy tartib: yangi kalit oling → `.env` ni tahrirlang → `bin/dc up -d --no-build bot` → loglarda xato yo'qligini va `/holat` ni tekshiring → **eski kalitni provayderda bekor qiling**.

| Kalit | Qayerda | Izoh |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/revoke` yangi token beradi, eskisi darhol ishlamay qoladi | `.env` ni tezda yangilang |
| `FOOTBALL_DATA_ORG_KEY` | football-data.org hisobi | tarif limiti daqiqasiga (bepul 10, Standard 30; `FOOTBALL_DATA_REQUESTS_PER_MINUTE`), kunlik limit yo'q; har so'rov chaqiruvlar jurnaliga yoziladi |
| `GEMINI_API_KEY` | Google AI Studio | |
| `HEALTHCHECKS_URL` | healthchecks.io: check'ni qayta yarating | URL = parol |
| `SSH_KEY` (CI) | yangi kalit → serverda `~deploy/.ssh/authorized_keys` ga qo'shing → GitHub secret'ni almashtiring → eskisini `authorized_keys` dan o'chiring | |
Kalit chatga/logga tushib qolgan bo'lsa, darhol almashtiring. Loglar ma'lum kalitlarni `***` bilan yashiradi, lekin bunga tayanmang.

## 10. Loglarni o'qish

Loglar stdout'da JSON qatorlar (Docker rotatsiyasi: 5 × 10 MB):
```bash
bin/dc logs --tail 200 bot
bin/dc logs --since 2h bot | grep -E '"level": "(error|critical|warning)"'
bin/dc logs bot | grep '"event": "job_failed"'                     # job xatolari (job=settle/refresh/...)
bin/dc logs bot | grep -E 'startup_failed|migrations_failed|config_error_backoff'
```
Foydali maydonlar: `event`, `level`, `job`, `fixture_id`, `user_id`. Loglarda kalit/token bo'lmaydi.

## 11. Tez-tez uchraydigan muammolar

| Belgi | Sabab | Yechim |
|---|---|---|
| Konteyner `unhealthy`, logda `startup_failed`, `config_error_backoff` (**chiqish kodi 2**) | `.env` da token/`ADMIN_IDS` yo'q yoki noto'g'ri, Telegram tokenni rad etdi | Xabar sababni aytadi (faqat o'zgaruvchi nomi). `.env` ni tuzating → `bin/dc up -d --no-build bot`. Halqa daqiqada bir marta (60 s kutish), qayta urinish yordam bermaydi |
| `watchdog_stalled` logda, konteyner qayta-qayta ko'tariladi (**kod 3**) | rejalashtiruvchi to'xtab qoldi (`daily-tick` yo'q) | Qayta ishga tushish odatda tuzatadi. Takrorlansa `bin/dc logs bot` da undan oldingi xatoni toping (`job_failed`, xotira), `free -m`; `WATCHDOG_STALL_MINUTES` ni oshirish faqat og'ir ishlar uzoq bo'lsa |
| `migrations_failed` (kod 1), ilova ishga tushmaydi | migratsiya xatosi yoki `/data` yozib bo'lmaydi | Logdagi alembic xatosini o'qing; deploy bo'lsa avtomatik rollback bo'ladi. Sxema buzilgan bo'lsa `pre-deploy` zaxirasidan tiklang |
| `healthy` emas: `scheduler lag ...` | event loop qotgan yoki jarayon og'ir ishda (masalan, backtest xotira yetmasligi) | `bin/dc logs`, `free -m`, `bin/dc restart bot`. Takrorlansa, server hajmini oshiring |
| `disk free ... < min` | zaxiralar yoki loglar disk'ni to'ldirdi | `du -sh backups /var/lib/docker`, `docker system prune -f`, eski zaxiralarni o'chiring, `RETENTION_DAYS` ni kamaytiring |
| **Football-Data 429** (logda `provider_request ... status=429`) | daqiqalik limitdan oshildi | Klient `Retry-After` ga qarab qayta urinadi. Takrorlansa, `FOOTBALL_DATA_REQUESTS_PER_MINUTE` tarifingizdagi qiymatdan katta emasligini tekshiring (bepul: 10) |
| **Telegram 429** (flood control) | juda ko'p xabar | Bot bir marta kutib qayta urinadi; uzoq davom etsa kunlik xabar hajmini tekshiring. Doimiy bo'lsa logdagi `retry_after` ga qarang |
| **Gemini limiti/xatosi** | bepul tarif kunlik so'rov/token chegarasi (LLM byudjeti), tarmoq xatosi | LLM yo'q bo'lsa tahlil **model bo'yicha davom etadi** (izohsiz, ehtiyotkor holat); byudjet ertasi kuni tiklanadi. Doimiy bo'lsa kalit/model nomini (`GEMINI_MODEL`) tekshiring |
| healthchecks.io "late" xabari | job ishlamadi yoki ping ketmadi | `bin/dc logs bot \| grep -E 'job_failed\|heartbeat'`, `/holat`; server ishlayotganini tekshiring |
| Deploy workflow qizil, "did not become healthy" | yangi versiya sog'lom emas, rollback bajarildi | Actions logida server chiqishi; `cat deploy.env` oldingi image'ni ko'rsatadi; xatoni tuzatib yangi teg qo'ying |
| `permission denied` docker'da | `deploy` foydalanuvchisi `docker` guruhida emas | `sudo usermod -aG docker deploy`, qayta kiring |
| `ClientConnectorCertificateError` / `CERTIFICATE_VERIFY_FAILED` (Telegram, Football-Data.org, Gemini) | TLS ni ushlab qoluvchi korporativ proksi o'z CA si bilan qayta imzolaydi (odatda lokal/ofis mashinasida; oddiy VPS da bo'lmaydi) | Pastdagi 11.1 bo'limi |
| `startup_failed ... unexpected content from api.telegram.org (not JSON)` (**kod 2**) | tarmoq yoki firewall Telegram o'rniga HTML sahifa qaytaryapti (korporativ filtr, captive portal) | **Telegram is unreachable:** `curl -sS https://api.telegram.org/bot<TOKEN>/getMe` (token o'rniga o'zingiznikini, natijani hech kimga yubormang) JSON qaytarmasa, bu mashinadan Telegram'ga yo'l yopiq: serverda yoki boshqa tarmoqda ishga tushiring (VPS da bunday bo'lmaydi). Logda javobning faqat birinchi 300 belgisi chiqadi |
| Kunlik xabar kelmadi, `/holat` da `daily` xato, admin ogohlantirishi "digest ... gave up" | Telegram ~6 soatdan uzoq ishlamadi (qayta urinishlar 1,2,4,8,15,30,60,120,120 daqiqada) yoki foydalanuvchi botni bloklagan | Signallar baribir saqlangan: botda "⚡ Bugungi signallar". Telegram qaytgach ertasi kuni xabar odatdagidek keladi. Bitta karta rad etilsa (logda `notify_card_rejected` va signal id), qolganlari baribir yuboriladi |
| Botga hech kim javob olmayapti, container healthy | polling/tarmoq (api.telegram.org) | `bin/dc logs`, `curl -sS https://api.telegram.org`, `bin/dc restart bot` |

### 11.1 Korporativ proksi ortida (lokal mashina)

Sozlamalar ixtiyoriy va standart o'chiq; sertifikat tekshiruvi hech qachon o'chirilmaydi ([ADR 0017](docs/adr/0017-tls-trust-configuration.md)).

```bash
# Lokal, Windows korporativ mashina: OS ombori (korporativ CA allaqachon ishonchli)
#   .env:  TLS_USE_SYSTEM_TRUST_STORE=true
python -m uv run python -m app.main

# Lokal, PEM fayl bilan:   .env:  TLS_CA_BUNDLE=C:/certs/corp-ca.pem

# Docker: build ham, ishga tushirish ham bitta fayl bilan
CA_BUNDLE_FILE=C:/certs/corp-ca.pem \
  docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d --build
```
- Docker'da fayl `/run/secrets/ca_bundle` ga faqat o'qiladigan qilib ulanadi (bitta faylni ulash Docker Desktop for Windows va Linux'da ishlaydi; yo'lda `/` ishlating) va `TLS_CA_BUNDLE` shunga o'rnatiladi. Fayl repoga yoki image'ga tushmaydi.
- CA ni eksport qilish: `certmgr.msc` > Trusted Root Certification Authorities > CA > All Tasks > Export > Base-64 X.509 (.CER), keyin `.pem` deb nomlang.
- Xabar `TLS_CA_BUNDLE ... missing file` yoki `could not be loaded` (kod 2): yo'l/faylni tuzating yoki `TLS_USE_SYSTEM_TRUST_STORE=true` ni sinang.
- Sertifikat xatosi o'rniga `Application Blocked` / `FortiGate` HTML sahifasi (`ClientDecodeError`) kelsa: TLS ishlayapti, lekin firewall Telegram'ni siyosat bo'yicha bloklaydi. Bu IT yoki boshqa tarmoq masalasi.
- Serverda (VPS) bu sozlamalar kerak emas.

## 12. Production'ga o'tishdan oldingi ro'yxat (F10/F11)

**Deploy tayyorligi (F9):**
- [ ] Reboot sinovi o'tdi (3.6/3), bot o'zi tiklandi
- [ ] Zaxira har kuni olinmoqda, masofaviy nusxa bor, **tiklash mashqi bir marta bajarilgan**
- [ ] healthchecks.io check yashil, "late" xabari sinab ko'rilgan (botni 1 kunga to'xtatmasdan: check'da "Pause/Start" bilan)
- [ ] `.env` `chmod 600`, kalitlar faqat serverda; SSH faqat kalit bilan; `ufw status` faqat 22
- [ ] Rollback sinovi o'tdi (3.6/5)

**F10 paper trading (4–6 hafta):** tizim serverda to'liq rejimda, stavkalar **faqat qog'ozda**. Haftalik ko'rib chiqish: ROI, CLV, kalibratsiya, xatolar, API sarfi; model parametrlari faqat haftalik va jurnal bilan o'zgaradi (kunlik emas).

**F10 darvozasi: real pulga o'tish mezonlari** ([ishlab_chiqish_rejasi.md](ishlab_chiqish_rejasi.md)):
- [ ] ≥ 150–200 yakunlangan signal
- [ ] **CLV o'rtachasi musbat** (asosiy mezon), ROI ≥ −2%
- [ ] Tizim 14 kun davomida qo'lda aralashuvsiz ishladi, kritik xato yo'q
- [ ] API va Gemini limitlari hech qachon tugamagan
Mezonlar bajarilmasa: paper trading davom etadi yoki model qayta ishlanadi. **Real pul bilan foydalanishga o'tilmaydi.**

**F11 (v1.0):**
- [ ] `CHANGELOG.md`, reliz `v1.0.0` (teg orqali deploy)
- [ ] Bot sozlamalarida bank, unit va kunlik/haftalik zarar limitlari yoqilgan
- [ ] Birinchi oy minimal stavkalar (bankning 0.5–1%)
- [ ] Oylik ko'rib chiqish: CLV, ROI, segmentlar, xarajatlar
