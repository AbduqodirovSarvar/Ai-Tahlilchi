# ADR 0019: T6 tuzatish raundi C (yetkazish, ulanish va qolgan QA topilmalari)

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-23
- **Muallif:** bot-engineer / pipeline-engineer / infra-engineer (yagona agent, T6 yakuniy tuzatish)
- **Bog'liq:** [QA hisoboti](../qa/T6-acceptance.md) T6-F-1…F-8; [ADR 0008](0008-pipeline-and-real-services.md) ("kamida bir marta" yetkazish); ADR 0018 (A: pul va jadval o'sishi; B: ekspluatatsiya)

## Kontekst
Mustaqil QA (T6-acceptance) 0 ta 🔴, 1 ta 🟠 va 7 ta 🟡 topilma qoldirdi; fix A va fix B birlashgandan keyin F-3 qisman (get_bet) yopilgan edi. Bu raund qolganini yopadi va boshqa agentlar so'ragan ulashlarni (retention job, sirlar ro'yxati, qo'shimcha bukmekerlar, `/holat` versiyasi) bajaradi.

## Qarorlar

### 1. Kunlik xabar: bitta karta qolganini yutmaydi (T6-F-1)
`TelegramNotifier._daily` endi har bir xabar uchun natijani **uch xil** ajratadi: yuborildi; **doimiy rad** (`TelegramBadRequest` kabi 4xx: qayta urinish foydasiz); **vaqtincha xato** (persistent 429, tarmoq, 5xx).
- Rad etilgan karta signal id si bilan loglanadi (`notify_card_rejected`) va o'tkazib yuboriladi; qolgan kartalar yuboriladi, xabar "yetkazildi" hisoblanadi.
- Vaqtincha xato bo'lsa tsikl davom etadi, lekin xabar "yetkazilmadi" (`None`) qaytadi: pipeline backoff bilan qayta urinadi va **faqat yetishmayotgan kartalarni** yuboradi (2-band).
- Foydalanuvchi botni bloklagan bo'lsa (`Forbidden`) tsikl darhol to'xtaydi va qayta urinish bo'lmaydi (eski xatti-harakat).

### 2. Yetkazish jarayoni va "kamida bir marta" oynasi (T6-F-4)
Pipeline xabar bo'yicha jarayonni ledger'da saqlaydi (`daily.progress`: sarlavha havolasi + qayta urinish kerak bo'lmagan pozitsiyalar). Shartnomaga qo'shimcha (v1.10): `DailyDigest.progress` (`DigestProgress` porti, `DigestResume`). Notifier uni ixtiyoriy ishlatadi; yo'q bo'lsa eski xatti-harakat.
- Sarlavhadan keyin **bitta ifoda** (SQLite `INSERT ... ON CONFLICT DO UPDATE`) bilan jarayon va **barcha kartalarning** xabar havolalari (`msg`) birga yoziladi (`Ledger.complete_many`): avval har karta alohida tranzaksiya edi (10 ta).
- Har yuborilgan karta (yoki doimiy rad etilgan) keyin bitta ifoda + commit bilan belgilanadi.
- **Natija:** qayta ishga tushish yoki xato bo'lsa takrorlanadigan narsa bo'lishi mumkin bo'lgan eng ko'pi **bitta xabar** (yuborilgan, lekin belgilanmagan), butun xabar (11 xabar) emas.
- **O'lchov (188 → 192 chegarada o'ldirish mashqi):** aniq bir marta 145/188 (77%) edi; endi **170/192 (88.5%)**, qolgan 22 chegara har biri aynan bitta xabarning takrori (11 xabar × 2 chegara: ifoda + commit). Bu 95% emas: pastki chegara: har bir yuborilgan xabar uchun "yuborildi" belgisi kamida bitta yozuv va commit, ular orasida jarayon o'lsa bitta xabar takrorlanadi. Uni yopish xabarni **yo'qotish** xavfiga olib keladi (belgini yuborishdan oldin qo'yish = "ko'pi bilan bir marta"), shuning uchun qasddan qabul qilindi. Mashq endi har bir o'ldirishda ko'pi bilan 1 ta ortiqcha xabarni talab qiladi.

### 3. Qayta urinish jadvali: yetkazish kalitlari uchun ~6 soat (T6-F-7)
`Ledger.fail(..., delivery=True)`: kutish 1, 2, 4, 8, 15, 30, 60, 120, 120 daqiqa (jami 6 soat), 10-muvaffaqiyatsizlik kalitni `exhausted` qiladi. `fail_key` (kunlik xabar, jadvalli shablon, haftalik o'rganish xabari) shuni ishlatadi. Boshqa kalitlar (refresh, settle, pre-match) eski 5 urinish / 30 daqiqa chegarasida qoladi: ularning ma'nosi tez eskiradi. `daily_catch_up` (12 soat) o'zgarmadi: 6 soatlik jadval uning ichida tugaydi. Admin ogohlantirishi 30 daqiqalik throttle bilan davom etadi (endi soatlar davomida bir necha ogohlantirish bo'lishi mumkin, har urinishda emas).

### 4. Rejalashtiruvchi qulflangan bazada o'lmaydi (T6-F-2)
`PipelineJobs.run` xatoni yozishni (`record_run`, `record_error`, ogohlantirish) o'z `try/except` ichiga oldi: yozib bo'lmasa `job_failed_unrecorded` loglanadi, 5 soniya (`FAILURE_BACKOFF_SECONDS`) kutiladi va keyingi tick qayta urinadi; istisno tashqariga chiqmaydi. `run_due_digests` ham bitta foydalanuvchining yozib bo'lmaydigan xatosida qolgan foydalanuvchilarni to'xtatmaydi.

### 5. Begona foydalanuvchi jarayonda hech narsa qoldirmaydi (T6-F-8)
aiogram FSM middleware'i whitelist'dan **keyin** ro'yxatga olinadi (`outer_middleware` tartibi: errors → aiogram user-context → whitelist → FSM → ...). Begona update FSM va `SimpleEventIsolation` ga yetmaydi. Chegaralangan (LRU) izolyatsiya o'rniga tartibni o'zgartirish tanlandi: soddaroq va begona uchun hech qanday holat yaratilmaydi. 60 begona uchun test mavjud.

### 6. Telegram o'rniga HTML sahifa (firewall) (T6-F-5)
`connect_bot` `ClientDecodeError` ni `ConfigurationError` ga o'raydi: "unexpected content from api.telegram.org (not JSON): the network or a firewall is blocking Telegram, see RUNBOOK ...", javob matni HTML-tegsiz va 300 belgigacha (`app.logging.summarise_body`). Chiqish kodi 2 (entrypoint 60 s kutadi, 38 KB log o'rniga qisqa qator). Vaqtincha tarmoq xatolari (timeout) o'zgarmadi: kod 1, restart.

### 7. LLM sharh vaqt chegarasi paket boshiga (T6-F-6)
`review_safely` paketlarga (`MAX_CANDIDATES`) o'zi bo'ladi: har paket `review_timeout` (30 s), butun tahlil `review_total_timeout` (90 s). Yetmagan paketning sharhlari tashlanadi, avvalgilari saqlanadi. Raqamlar sharhga bog'liq emas. Real Gemini kechikishi hali o'lchanmagan (F10 da `llm_review_done` loglaridan kuzatiladi).

### 8. N+1 (T6-F-3)
`get_bet` fix A da to'g'ridan-to'g'ri qidiruvga o'tgan edi. Ro'yxat/eksport (`_bets`) endi `db.repo.get_signals_by_ids` (500 tadan paketlab, paket uchun 3 so'rov) ishlatadi: so'rovlar soni stavkalar soniga bog'liq emas.

### 9. Ulashlar
- **Retention:** `retention` job har yakshanba 04:00 (Asia/Tashkent), `JOB_NAMES` 9 ta, natija `/holat` da `retention` qatori; healthcheck faqat `daily-tick` ni kuzatadi (o'zgarmadi).
- **Sirlar:** `main()` `register_secrets(settings.secret_values())` ni chaqiradi (bot tokeni, API-Football, Gemini kalitlari va healthchecks URL), shuning uchun ledger/ogohlantirish matnlari ham tozalanadi.
- **`INGEST_EXTRA_BOOKMAKERS`** (vergul bilan ajratilgan, kichik harf) → `PipelineConfig.extra_bookmakers`; standart bo'sh.
- **`/holat` versiyasi:** `SystemStatus.build` (additiv). Qiymat `main.py` da `get_version(settings).label` dan olinib `create_real_services(..., build=)` orqali beriladi (qatlam qoidasi: `services.real` `app.ops` ni import qilmaydi). Soxta servis qat'iy yorliq qaytaradi.

## Oqibatlar
- Yetkazishda eng yomon holat endi "bitta xabar takrori"; foydalanuvchi kartani yo'qotmaydi.
- Ledger'da yangi kalitlar: `daily.progress` (yosh bo'yicha tozalanadi, `PURGED_STAMP_JOBS`).
- Telegram 6 soatgacha ishlamasa ham kunlik xabar yetib boradi; undan keyin admin ogohlantiriladi, signallar saqlanadi (⚡ orqali ko'rinadi).
- Qolgan: 95% emas, 88.5% (2-band); real Gemini kechikishi o'lchanmagan; `retention` birinchi marta haqiqiy bazada sinalmagan.
