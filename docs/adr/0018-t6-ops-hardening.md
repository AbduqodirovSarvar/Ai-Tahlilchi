# ADR 0018: T6 ops mustahkamlash (jobs.db, watchdog, versiya, deploy darvozalari)

Holat: qabul qilingan · To'lqin T6 (fix-b) · infra-engineer

## Kontekst
T6 sharhlari (xavfsizlik va arxitektura/ops) bir nechta ishonchlilik va xavfsizlik kamchiliklarini topdi: ping URL logda ochiq, `/tmp` (tmpfs, xotira chegarasiga kiradi) ga baza nusxasi, rejalashtiruvchi bot ulanmasdan ishga tushishi, jim to'xtagan rejalashtiruvchini hech kim qayta ishga tushirmasligi, noma'lum handicap konventsiyasi bilan real rejim, CI/deploy zanjiridagi bo'shliqlar.

## Qarorlar

### 1. Rejalashtiruvchi jadvali alohida `jobs.db` faylda
APScheduler `SQLAlchemyJobStore` job'larni **pickle** qilib saqlaydi. Uni `app.db` ichida tutish ikki muammo: (a) zaxira/tiklash bilan begona pickle ilovaga kirishi mumkin (kod bajarilishi xavfi), (b) jadval kodga bog'liq, ma'lumot emas. Tanlov: `Settings.jobs_db_path` = `app.db` yonidagi `jobs.db`; zaxiraga kirmaydi, har startda `_register` bilan qayta quriladi. Healthcheck shu fayldan `daily-tick` ni o'qiydi. Oqibat: birinchi startda eski `apscheduler_jobs` jadvali `app.db` da yetim qoladi (zararsiz) va o'sha bir martalik deploy oynasida o'tkazib yuborilgan kunlik job (grace 6 soat ichida bo'lsa ham) yangi faylda yo'qoladi, chunki yangi fayl bo'sh. Alternativa (migratsiya bilan jadvalni o'chirish) data-engineer egaligida, foyda kam.

### 2. Rejalashtiruvchi bot ulangandan keyin ishga tushadi
Avval `scheduler.start()` bot ulanishidan oldin chaqirilardi: o'tkazib yuborilgan job darhol ishga tushib, `TelegramNotifier` hali botga ulanmaganida xabarlarni yo'qotardi. Endi start dispatcher `startup` bosqichida, `notifier.on_startup` dan keyin ro'yxatdan o'tkaziladi. Bot ulana olmasa rejalashtiruvchi umuman ishga tushmaydi. To'xtatish tartibi o'zgarmadi (avval ticking, keyin joblar tugashi, keyin yopish).

### 3. Ichki qorovul: qotgan rejalashtiruvchi = qayta ishga tushish
Docker `HEALTHCHECK` faqat `unhealthy` deb belgilaydi, qayta ishga tushirmaydi. `daily-tick` har daqiqada ishlaydi; `WATCHDOG_STALL_MINUTES` (10) davomida ishlamasa `app.ops.watchdog` `watchdog_stalled` yozadi, `dispatcher.stop_polling()` bilan tartibli to'xtatadi, jarayon **kod 3** bilan chiqadi, `restart: unless-stopped` ko'taradi. Tartibli to'xtash 150 s ichida tugamasa `os._exit(3)` (oxirgi chora). Hisob rejalashtiruvchi ishga tushgan paytdan boshlanadi, shuning uchun migratsiya/startda ishlamaydi. Variantlar: faqat healthcheck + tashqi autoheal konteyneri (qo'shimcha image/ruxsatlar, Docker soketi kerak: rad etildi); systemd watchdog (Docker ichida emas). Xavf: juda uzoq (>10 daqiqa) bitta `daily-tick` yolg'on signal berishi mumkin; bu o'zi anomaliya va `max_instances=1` bilan keyingi tick'lar ham to'xtaydi.

### 4. Versiya: build argument -> muhit -> startup logi
`APP_VERSION` va `GIT_SHA` Dockerfile `ARG`/`ENV`, deploy workflow tegdan va commit'dan beradi; `Settings.app_version/git_sha` ularni config orqali o'qiydi (`os.environ` taqiqi), `app.ops.version.get_version()` qaytaradi, `app_started version=... sha=...` logga yoziladi. `ENV` qatlami eng oxirida: yangi commit bog'liqliklar keshini buzmaydi. `/holat` da ko'rsatish servis qatlami (`SystemStatus`) o'zgarishini talab qiladi: alohida vazifa.

### 5. Handicap konventsiyasi: aniq tanlanmaguncha real rejim yo'q
Standart `home_line` taxmin; noto'g'ri bo'lsa saqlangan koeffitsientlar jimgina noto'g'ri. `Settings.require_explicit_handicap_convention()` pydantic `model_fields_set` orqali o'zgaruvchi muhit/`.env` da **berilganini** tekshiradi (bo'sh `KEY=` berilmagan hisoblanadi); `APP_SERVICES=real` bo'lmasa chiqish kodi 2 bilan to'xtaydi. Chiqish yo'li: `HANDICAP_CONVENTION=home_line` ni aniq yozish. Shablonlarda satr izohda turadi (ataylab: nusxalash o'zi tasdiq bo'lib qolmasin).

### 6. Vaqtinchalik fayllar baza yonida
Konteynerda `/tmp` tmpfs va 768 MB cgroup ga kiradi. `backup`/`verify` baza nusxasini `database.parent` (volume) da yaratadi; test `tempfile.mkdtemp` ni `dir=` siz chaqirilsa yiqiladigan qilib tekshiradi. `pre-restore/` da oxirgi 5 nusxa qoladi.

### 7. CI/deploy zanjiri
- Deploy faqat shu commit uchun CI yashil bo'lganda (check-runs API, standart token, `checks: read`); kerakli tekshiruvlar ro'yxati `ci.yml` ish nomlariga testda bog'langan.
- Masofaviy buyruqqa kiradigan qiymatlar (`IMAGE_REFERENCE`, `GHCR_USER`) runner'da qat'iy belgilar to'plamiga tekshiriladi.
- `SSH_KNOWN_HOSTS` majburiy (ssh-keyscan/TOFU olib tashlandi); `SSH_KEY` `production` environment da (required reviewers).
- CI testlari ikkiga: tezkor (`-m "not slow"`, qamrov darvozasi bilan) va sekin (qamrovsiz) ish.
- Dependabot (`uv`, Actions, Docker) va haftalik `pip-audit` (issue ochmaydi, qizil bo'ladi). Python ekotizimi uchun `pip` emas `uv`: faqat shu `uv.lock` ni yangilaydi.

### 8. Bog'liqliklar
`pandas` kodda ishlatilmagan (olib tashlandi), `scikit-learn` faqat QA oracle testida (dev guruhiga), `odds_api_key` sozlamasi o'lik edi (olib tashlandi; `extra=ignore` tufayli `.env` dagi qoldiq xato bermaydi). Image 938 MB dan 768 MB ga kamaydi.

## Oqibatlar
- Yangi chiqish kodi 3 (README/RUNBOOK da).
- `jobs.db` zaxiradan tashqarida; tiklashdan keyin rejalashtiruvchi o'zi qayta quriladi.
- Real rejimga o'tish endi `HANDICAP_CONVENTION` ni F0 natijasi bilan yozishni talab qiladi.
