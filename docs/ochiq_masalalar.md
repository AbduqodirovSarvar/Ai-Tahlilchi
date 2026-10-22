# Ochiq masalalar

> 2 tuzatish aylanishidan keyin ham qolgan 🟠/🟡 topilmalar va hal qilinmagan savollar ([agentlar_rejasi.md, 10.2](../agentlar_rejasi.md)). 🔴 topilmalar bu yerga yozilmaydi: ular tuzatilmaguncha ish davom etmaydi.

| # | Sana | Daraja | Masala | Manba (to'lqin / agent) | Holat | Izoh |
|---|---|---|---|---|---|---|
| 1 | 2026-10-21 | 🟡 | `restart: on-failure:5` server qayta yuklanganda konteynerni ishonchli ko'tarmasligi mumkin; F9 mezoni "reboot'dan keyin o'zi tiklanadi". Prod uchun `unless-stopped` + healthcheck yoki systemd unit, exit 2 halqasini alohida cheklash | T1 / infra-engineer | Yopildi (T5 / infra-engineer): `restart: unless-stopped` + healthcheck, exit 2 halqasi entrypoint'da 60 s kechiktiriladi; systemd varianti tayyor ([ADR 0014](adr/0014-deploy-and-ops.md)). Reboot sinovi serverda: RUNBOOK 3.6 | — |
| 2 | 2026-10-21 | 🟡 | Jonli Telegram `/start`, `TelegramUnauthorizedError` va GitHub Actions/pip-audit jonli tekshiruvi kalitlar va CI ishga tushgandan keyin | T1 / test-engineer | F0 bilan kechiktirilgan | — |
| 3 | 2026-10-21 | 🟡 | Dockerfile `alembic/` va `alembic.ini` ni image'ga ko'chirmaydi; deployda `alembic upgrade head` uchun kerak | T2 / data-engineer | Yopildi (T5 / infra-engineer): `alembic/` va `alembic.ini` image'da, entrypoint `alembic upgrade head` ni ilovadan oldin bajaradi (CI smoke-test tekshiradi) | — |
