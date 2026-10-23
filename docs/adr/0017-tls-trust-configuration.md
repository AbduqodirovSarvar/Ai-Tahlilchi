# ADR 0017: Chiquvchi HTTPS uchun ixtiyoriy TLS ishonch sozlamasi

- **Holat:** taklif (orkestr ko'rib chiqadi)
- **Sana:** 2026-10-23
- **Muallif:** infra-engineer
- **Kod:** `src/app/tls.py`, `src/app/config.py`, `src/app/main.py`, `src/app/clients/{http,gemini}.py`, `src/app/sports/history/download.py`, `src/app/ops/heartbeat.py`, `docker-compose.proxy.yml`

## Kontekst
Dasturchining korporativ tarmog'ida TLS'ni ushlovchi proksi trafikni korporativ CA bilan qayta imzolaydi. aiogram (aiohttp, faqat `certifi`), httpx va google-genai shu CA'ni tanimaydi: `ClientConnectorCertificateError`. Python 3.13 ba'zi korporativ CA'larni PEM faylda qat'iy RFC 5280 tekshiruvi sabab rad etadi ("Basic Constraints of CA cert not marked critical"), OS ombori esa ularni qabul qiladi.

## Variantlar
1. `SSL_CERT_FILE` muhit o'zgaruvchisi: aiogram uni o'qimaydi (u `certifi.where()` ni qattiq yozadi), shuning uchun yetarli emas.
2. Tekshiruvni o'chirish (`verify=False`): **rad etildi**, xavfsizlikni buzadi.
3. Bitta `ssl.SSLContext` ni sozlamalardan qurib, uni hamma HTTP steklariga berish. **Tanlandi.**

## Qaror
- Ikki ixtiyoriy sozlama, standart o'chiq: `TLS_USE_SYSTEM_TRUST_STORE` (bool) va `TLS_CA_BUNDLE` (PEM yo'li). Ikkalasi bo'sh bo'lsa xatti-harakat o'zgarmaydi (kutubxona standarti).
- `app.tls.build_ssl_context`: OS ombori bo'lsa `truststore.SSLContext(PROTOCOL_TLS_CLIENT)` (yangi ish vaqti bog'liqligi `truststore`: kichik, sof Python, Windows/macOS/Linux ombori; muqobili qo'lda sertifikat eksporti va Python'ning qat'iy tekshiruvi bilan kurashish edi); aks holda `ssl.create_default_context(cafile=...)`; aks holda `None`. Ikkalasi berilsa OS ombori ustun.
- PEM uchun `VERIFY_X509_STRICT` bayrog'i o'chiriladi (Python <= 3.12 xatti-harakati): zanjir, muddat va host nomi tekshiruvi qoladi. Dockerda haqiqiy korporativ bundle aynan shu bayroqsiz ishlamadi (`CERTIFICATE_VERIFY_FAILED ... Basic Constraints`), bayroq bilan TLS o'tdi.
- Fayl yo'q yoki o'qilmasa `ConfigurationError` (chiqish kodi 2); xabarda yo'l bor, mazmun hech qachon yo'q.
- Ulanish: httpx (`verify=`) API-Football, football-data, healthchecks; Gemini `HttpOptions(client_args/async_client_args)`; aiogram `AiohttpSession` ostki sinfi (aiogram'da ochiq `ssl` parametri yo'q, shuning uchun bitta himoyalangan `_connector_init["ssl"]` yozuvi ostki sinfda; aiogram yangilanganda `tests/test_tls.py::TestLoopback` buzilishni ushlaydi).
- `app.tls` qatlamdan tashqari (config/logging kabi); `app.models`, `app.domain` va `app.errors` uni import qila olmaydi (import-linter ro'yxatiga qo'shildi).
- Kodda `verify=False`, `ssl=False`, `CERT_NONE` va hokazolar yo'qligini test tekshiradi (manba grep).
- Docker: `docker-compose.proxy.yml` bir faylni ham build sirri, ham ishga tushirishda faqat o'qiladigan sir (`/run/secrets/ca_bundle`) sifatida ulaydi va `TLS_CA_BUNDLE` ni o'rnatadi. Asosiy `docker-compose.yml` o'zgarmadi.

## Oqibatlar
- Oddiy tarmoqda hech narsa o'zgarmaydi. `get_ssl_context()` jarayon bo'yicha keshlanadi; sozlamani o'zgartirgach qayta ishga tushirish kerak.
- Firewall siyosati (masalan, FortiGate Application Control Telegram'ni bloklaydi) TLS bilan hal bo'lmaydi: TLS o'tadi, lekin HTML bloklash sahifasi keladi. Bu tarmoq siyosati masalasi, kodda aylanib o'tilmaydi.
- Gemini SDK ning aiohttp yo'li uchun `ssl` kaliti `async_client_args` orqali beriladi (SDK hujjatlashtirgan o'rin).
