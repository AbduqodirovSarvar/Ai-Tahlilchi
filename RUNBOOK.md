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
Docker volume ai-tahlilchi_bot-data (/data): app.db (+ -wal), fsm.db, pre-restore/
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

- **Tavsiya:** 2 vCPU / 2 GB RAM / 20+ GB SSD, Yevropa (API'larga yaqin), Ubuntu 24.04. Hetzner CX22 va o'xshashlari ≈ $4–6/oy. Bot bo'sh turganda ≈ 200 MB ishlatadi, SQLite yillar davomida 1 GB dan kichik.
- 1 GB RAM ham ishlaydi (bootstrap 1 GB swap yaratadi; konteyner chegarasi 768 MB), lekin backtest/import ni (7-bo'lim) ko'proq xotira talab qiladi.
- Oylik: VPS $4–6 + API-Football (bepul 100 so'rov/kun; joriy mavsum uchun pullik ≈ $19/oy bo'lishi mumkin: F0 probe natijasiga qarang) + Gemini bepul tarif ($0). Jami: $4–6 (bepul API) yoki ≈ $25 (pullik ma'lumot bilan). healthchecks.io bepul tarifi yetarli.

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
| `SSH_KEY` | deploy kalitining **maxfiy** qismi (butun fayl, `-----BEGIN` dan `END` gacha) |
| `SSH_KNOWN_HOSTS` | (tavsiya) `ssh-keyscan -t ed25519 SERVER` natijasi, serverning haqiqiy kaliti ekanini tekshirgach. Bo'lmasa workflow birinchi ulanishda ishonadi va ogohlantiradi |

Secret'lar yo'q bo'lsa `Deploy` workflow **o'tkazib yuboriladi va yashil tugaydi** (hech narsa buzilmaydi). Ixtiyoriy: Settings → Environments → `production` → Required reviewers (har deploy'dan oldin tasdiqlash). GHCR paketi repozitoriy bilan bog'lanadi; server tokenni deploy paytida stdin orqali oladi, doimiy parol saqlanmaydi.

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

1. Kalitlarni oling (F0): `API_FOOTBALL_KEY` (api-sports.io), `GEMINI_API_KEY` (Google AI Studio), ixtiyoriy `ODDS_API_KEY`.
2. `nano /opt/ai-tahlilchi/.env`: kalitlarni yozing. **Hali** `APP_SERVICES=real` qilmang.
3. F0 probe (7-bo'lim): API reja nimani berishini tekshiring, `HANDICAP_CONVENTION` ni birinchi haqiqiy koeffitsientdan **oldin** belgilang (keyin o'zgartirib bo'lmaydi).
4. `APP_SERVICES=real` ga o'zgartiring, `bin/dc up -d --no-build bot` (yangi muhit bilan qayta yaratiladi; `restart` yetmaydi).
5. Loglarda `services_backend ... backend=real`, `scheduler_started jobs=[settle, refresh, daily-tick, recalc-tick]`. Healthcheck endi rejalashtiruvchi tirikligini ham tekshiradi.
6. Botda `/holat`: API limitlari va oxirgi ishlar ko'rinadi. Birinchi `refresh` 08:30 (Toshkent) da yoki qo'lda: `bin/dc exec bot python -m app.pipeline.refresh`.

## 5. Monitoring

- **Docker healthcheck** (har 60 s): sozlama xatosi belgisi yo'q, baza ochiladi, disk ≥ 200 MB bo'sh, (real rejimda) `daily-tick` 5 daqiqadan ortiq kechikmagan. Natija: `bin/dc exec bot python -m app.ops.healthcheck` (JSON: tekshiruvlar, baza/WAL hajmi, oxirgi ishlar).
- **healthchecks.io (dead-man's switch):** hisob oching → yangi check: *Period* 1 kun, *Grace* 4 soat → ping URL'ni `.env` dagi `HEALTHCHECKS_URL` ga yozing (URL maxfiy), `bin/dc up -d --no-build bot`. Ilova `settle` (08:00) va `refresh` (08:30) muvaffaqiyatli tugagach ping yuboradi, xato bo'lsa `/fail`. Ping to'xtasa, healthchecks.io sizga e-pochta/Telegram orqali xabar beradi: bu jarayon o'lgan, qotgan yoki server o'chgan holatni ushlaydi. Ikkinchi check (ixtiyoriy): zaxira uchun, URL'ni `/opt/ai-tahlilchi/backup.env` ga `BACKUP_PING_URL=...` deb yozing.
- **Admin Telegram ogohlantirishi:** job qayta-qayta xato bersa pipeline adminlarga xabar yuboradi (cheklangan chastota bilan); `/holat` oxirgi xatolarni ko'rsatadi.
- **Disk:** healthcheck chiqishida `free ... MB`; 200 MB dan kam bo'lsa konteyner `unhealthy`. `docker system df` va `du -sh backups` bilan qo'lda ham qarang. Loglar Docker'da 5 × 10 MB bilan aylanadi.

## 6. Yangilash va orqaga qaytarish

**Yangilash:** `git tag vX.Y.Z && git push origin vX.Y.Z`. Workflow: image quradi → serverda: zaxira (`pre-deploy`) → `pull` → `up -d` (entrypoint avval `alembic upgrade head` ni bajaradi) → 5 daqiqagacha `healthy` kutadi. `healthy` bo'lmasa **avtomatik oldingi image'ga qaytadi** va workflow qizil tugaydi.

**Qo'lda yangilash/rollback** (serverda, bir buyruq):
```bash
bin/remote-deploy.sh --dry-run ghcr.io/<egasi>/ai-tahlilchi:v0.9.0     # rejani ko'rish
bin/remote-deploy.sh ghcr.io/<egasi>/ai-tahlilchi:v0.9.0               # GHCR'ga kirish kerak bo'lsa: echo TOKEN | GHCR_USER=... bin/remote-deploy.sh ...
cat deploy.env deploy.env.prev                                          # joriy va oldingi image
```
**Sxema ogohlantirishi:** migratsiya orqaga qaytmaydi. Rollback faqat image'ni almashtiradi. Migratsiyalar bir reliz davomida orqaga mos (faqat qo'shish) yozilishi kerak. Eski image yangi sxemada ishlamasa, bazani `pre-deploy` zaxirasidan tiklang (8-bo'lim).

**Konfiguratsiyani yangilash** (`.env`): o'zgartiring, so'ng `bin/dc up -d --no-build bot`.

## 7. F0 probe va haqiqiy ma'lumot bilan backtest (serverda)

Skriptlar image ichida (`/app/scripts`). Natijalar `/data` ga yoziladi (konteynerning boshqa joyi o'qish uchun). Ish konteyneri botdan alohida (`run`), shuning uchun bot ishlayotgan bo'lsa ham bo'ladi; og'ir backtest paytida xotira uchun botni to'xtatish ixtiyoriy.

```bash
cd /opt/ai-tahlilchi
RUN="bin/dc run --rm --no-deps --entrypoint python bot"

# F0.7 API probe (≈ 24 so'rov; API_FOOTBALL_KEY kerak). Hisobot va yozib olingan javoblar /data/probe ga:
$RUN /app/scripts/probe_api.py --report /data/probe/api_probe_report.md --record-dir /data/probe/recorded
bin/dc run --rm --no-deps --entrypoint cat bot /data/probe/api_probe_report.md

# Tarixiy ma'lumot (football-data.co.uk) → baza, so'ng backtest:
$RUN /app/scripts/import_history.py --cache-dir /data/history --db-path /data/history.db
$RUN /app/scripts/backtest.py --db-path /data/history.db --out-dir /data/backtest --label server
bin/dc run --rm --no-deps --entrypoint ls bot /data/backtest
```
- Tarix **alohida** `history.db` ga yoziladi (jonli `app.db` ga emas). Jonli bazaga import kerak bo'lsa, `--db-path /data/app.db` ni botni to'xtatib ishlating.
- football-data.co.uk lokal (korporativ) mashinada filtr/proksi sabab ochilmasligi mumkin; server odatda to'g'ridan-to'g'ri ulanadi, shuning uchun haqiqiy ma'lumotli backtest shu yerda bajariladi. Internet bo'lmasa `--offline` kesh bilan.
- Hisobotni olish: `bin/dc run --rm --no-deps -T --entrypoint cat bot /data/backtest/<fayl>.md > ~/hisobot.md`, so'ng `scp`.
- **F3 darvozasi:** backtest natijasi (CLV/Brier/kalibratsiya) mezonlardan o'tmasa, real rejimga o'tmang ([docs/adr/0005-backtest-methodology.md](docs/adr/0005-backtest-methodology.md)).

## 8. Zaxiradan tiklash

Zaxiralar: `/opt/ai-tahlilchi/backups/app-*.db.gz` (14 kun) + `RCLONE_REMOTE` bo'lsa masofaviy nusxa. **Mahalliy zaxira server disk'i bilan birga yo'qolishi mumkin: masofaviy nusxa sozlang:**
```bash
sudo apt-get install -y rclone && rclone config        # masalan Backblaze B2 / Google Drive / S3
echo 'RCLONE_REMOTE=remote:ai-tahlilchi' >> /opt/ai-tahlilchi/backup.env      # taymer shuni o'qiydi
```

**Tiklash** (bot avtomatik to'xtatiladi va qayta ishga tushiriladi):
```bash
ls -lh backups/                                            # qaysi zaxira
bin/restore.sh --dry-run backups/app-20261101T023000Z-daily.db.gz     # tekshiradi (butunlik + migratsiya raqami), o'zgartirmaydi
bin/restore.sh backups/app-20261101T023000Z-daily.db.gz               # "RESTORE" deb tasdiqlang
bin/dc logs --tail 50 bot ; # botda /holat
```
Xavfsizlik: tiklashdan oldin joriy baza `/data/pre-restore/` ga saqlanadi; buzuq zaxira hech narsani o'zgartirmaydi; eski sxemali zaxira keyingi startda avtomatik migratsiya qilinadi. Masofaviy zaxirani avval yuklab oling: `rclone copy remote:ai-tahlilchi/app-....db.gz backups/`.

**Oylik mashq (5 daqiqa):** eng so'nggi zaxirani `bin/restore.sh --dry-run` bilan tekshiring. Sinalmagan zaxira zaxira emas. (Avtomatik test: `tests/ops/test_backup_restore_scripts.py::test_restore_drill_recovers_content_and_schema`.)

Zaxira bajarilganini tekshirish: `systemctl list-timers ai-tahlilchi-backup.timer`, `journalctl -u ai-tahlilchi-backup.service -n 30`, `ls -lt backups | head`.

## 9. Kalitlarni almashtirish

Umumiy tartib: yangi kalit oling → `.env` ni tahrirlang → `bin/dc up -d --no-build bot` → loglarda xato yo'qligini va `/holat` ni tekshiring → **eski kalitni provayderda bekor qiling**.

| Kalit | Qayerda | Izoh |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/revoke` yangi token beradi, eskisi darhol ishlamay qoladi | `.env` ni tezda yangilang |
| `API_FOOTBALL_KEY` | api-sports dashboard | kunlik limit hisobi saqlanadi |
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
| `migrations_failed` (kod 1), ilova ishga tushmaydi | migratsiya xatosi yoki `/data` yozib bo'lmaydi | Logdagi alembic xatosini o'qing; deploy bo'lsa avtomatik rollback bo'ladi. Sxema buzilgan bo'lsa `pre-deploy` zaxirasidan tiklang |
| `healthy` emas: `scheduler lag ...` | event loop qotgan yoki jarayon og'ir ishda (masalan, backtest xotira yetmasligi) | `bin/dc logs`, `free -m`, `bin/dc restart bot`. Takrorlansa, server hajmini oshiring |
| `disk free ... < min` | zaxiralar yoki loglar disk'ni to'ldirdi | `du -sh backups /var/lib/docker`, `docker system prune -f`, eski zaxiralarni o'chiring, `RETENTION_DAYS` ni kamaytiring |
| **API-Football kvotasi tugadi** (`/holat` da limit 0, logda `quota`) | kunlik 100 so'rov | Pipeline o'zi to'xtaydi va keshdan ishlaydi; ertasi kuni UTC 00:00 da tiklanadi. Takrorlansa, ligalar sonini kamaytiring yoki pullik rejaga o'ting. Kvotani qo'lda sarflamang |
| **Telegram 429** (flood control) | juda ko'p xabar | Bot bir marta kutib qayta urinadi; uzoq davom etsa kunlik xabar hajmini tekshiring. Doimiy bo'lsa logdagi `retry_after` ga qarang |
| **Gemini limiti/xatosi** | bepul tarif kunlik so'rov/token chegarasi (LLM byudjeti), tarmoq xatosi | LLM yo'q bo'lsa tahlil **model bo'yicha davom etadi** (izohsiz, ehtiyotkor holat); byudjet ertasi kuni tiklanadi. Doimiy bo'lsa kalit/model nomini (`GEMINI_MODEL`) tekshiring |
| healthchecks.io "late" xabari | job ishlamadi yoki ping ketmadi | `bin/dc logs bot \| grep -E 'job_failed\|heartbeat'`, `/holat`; server ishlayotganini tekshiring |
| Deploy workflow qizil, "did not become healthy" | yangi versiya sog'lom emas, rollback bajarildi | Actions logida server chiqishi; `cat deploy.env` oldingi image'ni ko'rsatadi; xatoni tuzatib yangi teg qo'ying |
| `permission denied` docker'da | `deploy` foydalanuvchisi `docker` guruhida emas | `sudo usermod -aG docker deploy`, qayta kiring |
| Botga hech kim javob olmayapti, container healthy | polling/tarmoq (api.telegram.org) | `bin/dc logs`, `curl -sS https://api.telegram.org`, `bin/dc restart bot` |

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
