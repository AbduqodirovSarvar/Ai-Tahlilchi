# Ochiq masalalar

> 2 tuzatish aylanishidan keyin ham qolgan 🟠/🟡 topilmalar va hal qilinmagan savollar ([agentlar_rejasi.md, 10.2](../agentlar_rejasi.md)). 🔴 topilmalar bu yerga yozilmaydi: ular tuzatilmaguncha ish davom etmaydi.

| # | Sana | Daraja | Masala | Manba (to'lqin / agent) | Holat | Izoh |
|---|---|---|---|---|---|---|
| 1 | 2026-10-21 | 🟡 | `restart: on-failure:5` server qayta yuklanganda konteynerni ishonchli ko'tarmasligi mumkin; F9 mezoni "reboot'dan keyin o'zi tiklanadi". Prod uchun `unless-stopped` + healthcheck yoki systemd unit, exit 2 halqasini alohida cheklash | T1 / infra-engineer | F9 da hal qilinadi | — |
| 2 | 2026-10-21 | 🟡 | Jonli Telegram `/start`, `TelegramUnauthorizedError` va GitHub Actions/pip-audit jonli tekshiruvi kalitlar va CI ishga tushgandan keyin | T1 / test-engineer | F0 bilan kechiktirilgan | — |
