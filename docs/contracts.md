# Interfeys shartnomalari

> Qoidalar: [agentlar_rejasi.md, 3-bo'lim](../agentlar_rejasi.md). Qotirilgan shartnomaga **faqat yangi narsa qo'shiladi**; buzuvchi o'zgarish orkestr ruxsati bilan va barcha foydalanuvchi agentlar yangilangandan keyin.
> Har bir o'zgarish pastdagi "O'zgarishlar jurnali"ga yoziladi.

**Umumiy holat:** hali qotirilmagan (T2 da yoziladi).

| # | Shartnoma | Fayl | Egasi | Foydalanuvchilar | Holat |
|---|---|---|---|---|---|
| 1 | Domen modellari | `src/app/domain/*.py` | data-engineer | hamma | hali qotirilmagan |
| 2 | Sport adapteri | `src/app/sports/base.py` (`SportAdapter`) | data-engineer | pipeline, quant | hali qotirilmagan |
| 3 | Baza sxemasi | `src/app/db/schema.py` | data-engineer | pipeline, bot (DTO orqali) | hali qotirilmagan |
| 4 | Tahlil servisi | `src/app/services/interfaces.py` | pipeline-engineer | bot | hali qotirilmagan |
| 5 | Soxta servis | `src/app/services/fake.py` (`FakeAnalysisService`) | pipeline-engineer | bot | hali qotirilmagan |
| 6 | Model API si | `src/app/models/__init__.py` | quant-modeler | pipeline | hali qotirilmagan |
| 7 | LLM API si | `src/app/llm/service.py` | llm-engineer | pipeline | hali qotirilmagan |

## Platforma (T1 da mavjud, infra-engineer)
Bular rasmiy shartnoma emas, lekin barcha agentlar ishlatadi; o'zgartirish faqat qo'shish orqali:
- `app.config.Settings` / `get_settings()`; maxfiy kalitlar `Settings.require_secret(name)` orqali (kalit yo'q bo'lsa `ConfigurationError`).
- `app.errors`: `AppError` → `ConfigurationError`, `ExternalServiceError`, `DataError`.
- `app.logging.get_logger(name)`.
- Qatlam chegaralari: `pyproject.toml` → `[tool.importlinter]`.

---

## 1. Domen modellari — `src/app/domain/`
**Holat:** hali qotirilmagan · **Versiya:** —
- Turlar: `Fixture`, `OddsSnapshot`, `Market`, `Selection` (F2.4)
- Invariantlar: (to'ldiriladi)

## 2. Sport adapteri — `src/app/sports/base.py`
**Holat:** hali qotirilmagan · **Versiya:** —
- `SportAdapter` protokoli: (to'ldiriladi)

## 3. Baza sxemasi — `src/app/db/schema.py`
**Holat:** hali qotirilmagan · **Versiya:** —
- Jadvallar: asosiy rejaning 7-bo'limi (to'ldiriladi)

## 4. Tahlil servisi — `src/app/services/interfaces.py`
**Holat:** hali qotirilmagan · **Versiya:** —
- `AnalysisQuery`, `AnalysisService`, `SignalView`, `RecalcDiff`: (to'ldiriladi)

## 5. Soxta servis — `src/app/services/fake.py`
**Holat:** hali qotirilmagan · **Versiya:** —
- `FakeAnalysisService`: (to'ldiriladi)

## 6. Model API si — `src/app/models/__init__.py`
**Holat:** hali qotirilmagan · **Versiya:** —
- `predict_markets`, `compute_ev`, `stake`, `confidence`: (to'ldiriladi)

## 7. LLM API si — `src/app/llm/service.py`
**Holat:** hali qotirilmagan · **Versiya:** —
- `review_candidates`, `postmortem`: (to'ldiriladi)

---

## O'zgarishlar jurnali
| Sana | Shartnoma | O'zgarish | Kim | Ruxsat |
|---|---|---|---|---|
| — | — | — | — | — |
