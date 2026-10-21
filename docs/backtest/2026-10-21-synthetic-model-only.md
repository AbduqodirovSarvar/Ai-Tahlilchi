# Backtest hisoboti: synthetic-model-only

> Bu **sintetik** ma'lumotdagi backtest: bozor ta'rifiga ko'ra samarali (narx = haqiqiy ehtimollik + marja), shuning uchun kutilgan natija ROI ~ -marja va ahamiyatli ustunlik yo'qligi. U backtest vositasini tekshiradi, modelni emas. **Haqiqiy ma'lumotdagi F3 darvozasi hali kutilmoqda** (qarorlar jurnali #7: football-data.co.uk bu mashinada bloklangan; serverda `scripts/import_history.py` + `scripts/backtest.py` bilan bajariladi).
> Sanity tekshiruvi (samarali bozor): kutilgan ROI (yopilish taqsimotida) -2.98%, haqiqiy ROI +7.48% [-15.14%; +29.26%], 89 ta signal. CI kutilganni o'z ichiga oladi: ha; ahamiyatli musbat ustunlik: yo'q; o'rtacha CLV -3.19%.

- **Rejim:** sintetik
- **Sana:** 2026-10-21T10:48:36+00:00
- **F3 darvozasi (shu ma'lumotda):** ❌ bajarilmadi

## Qayta olinadiganlik

| Parametr | Qiymat |
|---|---|
| Git commit | `5c004828412584d3dc6b867535931f0344e7905c` |
| Ma'lumot izi (SHA-256) | `64a643c158179830f5b4170e7ae806e66ab89ab18d39736d9e41b409e436c5c9` |
| Seed (bootstrap) | `20261021` |
| O'yinlar (tugagan) | 5700 |
| Ligalar | Synthetic League 0, Synthetic League 1, Synthetic League 2 |
| Mavsumlar | 2019, 2020, 2021, 2022, 2023 |
| Refit oralig'i | 7 kun |
| Tarix oynasi | 1100 kun |
| Koeffitsient oralig'i | 1.50-3.00 |
| Bootstrap qayta tanlashlar | 2000 |
| synthetic.leagues | `3` |
| synthetic.teams_per_league | `20` |
| synthetic.seasons | `5` |
| synthetic.first_season | `2019` |
| synthetic.seed | `20261021` |
| synthetic.intercept | `0.15` |
| synthetic.home_advantage | `0.25` |
| synthetic.rho | `-0.08` |
| synthetic.strength_sd | `0.3` |
| synthetic.season_drift_sd | `0.1` |
| synthetic.match_shock_sd | `0.15` |
| synthetic.bookmakers | `pinnacle/opening/3.0%, pinnacle/closing/2.5%, market_avg/opening/5.5%, market_avg/closing/5.0%` |

## Metodologiya

- **Walk-forward:** har bir o'yin o'z ligasining `as_of` dan **qat'iy oldingi** o'yinlari bilan fit qilingan Dixon-Coles modeli bilan bashorat qilinadi (`as_of` = refit sanasi <= o'yin sanasi; sana = UTC kickoff sanasi). Fitga `as_of` yoki undan keyingi o'yin kirsa model xato beradi; testda har bir fit tekshiriladi.
- **Bo'linish:** train (faqat tarix) 2019, 2020, 2021 · validatsiya 2022 · hold-out 2023 (bir marta).
- **Stavka narxi:** `pinnacle` **opening**. **CLV** va bozor bazasi: `pinnacle` **closing** (de-vig: shin). Darvozadagi 'yopilishdagi ROI' o'sha stavkalarni yopilish narxida qayta hisoblaydi.
- **Bozor modeli:** ustuvor pinnacle, zaxira market_avg; bashorat vaqtida faqat **opening** narxlari ishlatiladi.
- **Ansambl:** `p = w * p_DC + (1 - w) * p_bozor` (to'liq natija taqsimoti bo'yicha); har o'yindan EV bo'yicha eng yaxshi **bitta** stavka.
- **Stavka hajmi:** flat 1 birlik va Kelly x 0.25 (maks. 2%) 100 birlik **o'zgarmas** bankdan.
- **Baholash:** log-loss va Brier (bozor bazasi = de-vig qilingan yopilish), kalibratsiya, ROI/CLV uchun bootstrap 95% CI (seed bilan).

## Parametr tanlash (validatsiya 2022)

- Sinalgan konfiguratsiyalar soni: **144** (xi x w x min EV x min ehtimollik). Eng yaxshi validatsiya ROI si ko'p sinov (multiple testing) sababli **optimistik**; haqiqiy baho - hold-out.
- 1-bosqich: `(xi, w)` validatsiya log-loss bo'yicha; 2-bosqich: chegaralar ROI bo'yicha (>= 50 signal).
- **Tanlangan:** xi = 0.0019, w = 0.25, min EV = +8.00%, min ehtimollik = 30.0%.
- ⚠️ Chegaraviy w = 0.25 tanlandi (grid chekkasi): sof DC; grid kengaytirilsa yaxshiroq w bo'lishi mumkin.

Validatsiya ROI bo'yicha eng yaxshi 10 ta qator:

| xi | w | min EV | min p | signallar | ROI | log-loss (ansambl) |
|---|---|---|---|---|---|---|
| 0.0019 | 0.25 | +8.00% | 30.0% | 94 | +14.01% | 0.7678 |
| 0 | 0.25 | +8.00% | 30.0% | 109 | +9.39% | 0.7681 |
| 0 | 0.25 | +5.00% | 30.0% | 291 | +7.58% | 0.7681 |
| 0.0019 | 0.25 | +5.00% | 30.0% | 277 | +7.56% | 0.7678 |
| 0 | 0.25 | +5.00% | 55.0% | 110 | +4.05% | 0.7681 |
| 0 | 0.25 | +5.00% | 45.0% | 216 | +3.18% | 0.7681 |
| 0 | 0.75 | +8.00% | 45.0% | 669 | +2.70% | 0.7819 |
| 0 | 0.75 | +8.00% | 30.0% | 750 | +2.09% | 0.7819 |
| 0 | 0.25 | +0.00% | 30.0% | 864 | +2.07% | 0.7681 |
| 0 | 1 | +8.00% | 45.0% | 800 | +1.66% | 0.7943 |

## Hold-out natijasi (bir marta baholandi): mavsum 2023

Bashoratlar: 1140 · o'tkazib yuborilgan: yo'q · fitlar: 114 (muvaffaqiyatsiz: 0)

| Ko'rsatkich | Qiymat |
|---|---|
| Signallar | 89 |
| ROI / yield (flat) | +7.48% [-15.14%; +29.26%] |
| Foyda (flat, birlik) | +6.66 |
| Hit rate (foyda bilan yopilgan) | 43.8% |
| Maksimal pasayish (flat, birlik) | 10.28 |
| ROI yopilish narxida (89 ta) | +7.98% [-15.16%; +31.16%] |
| CLV: o'rtacha log(odds / yopilish adolatli) (89 ta) | -3.19% [-3.33%; -3.05%] |
| Yopilish narxi bor stavkalar (CLV qamrovi) | 89 / 89 |
| Yopilishsiz stavkalar ROI, olingan narxda (0 ta) | - |
| Yopilishni yutgan ulush | 0.0% |
| Yopilish taqsimotida kutilgan EV | -2.98% |
| Kelly 1/4: ROI · bank daromadi · maks. pasayish | +4.18% · +6.76% · 19.44 |

**Proper scoring (bozor nusxasiga o'rtacha; kichik = yaxshi):**

| Guruh | n | log-loss ansambl | log-loss DC | log-loss bozor (yopilish) | Brier ansambl | Brier DC | Brier bozor |
|---|---|---|---|---|---|---|---|
| 1x2 | 1132 | 0.9279 | 0.9506 | 0.9273 | 0.5487 | 0.5628 | 0.5487 |
| asian_handicap (x.5) | 294 | 0.6954 | 0.7287 | 0.6918 | 0.5021 | 0.5311 | 0.4987 |
| totals 2.5 | 1136 | 0.6105 | 0.6302 | 0.6097 | 0.4237 | 0.4393 | 0.4232 |
| all | 2562 | 0.7605 | 0.7830 | 0.7594 | 0.4879 | 0.5044 | 0.4873 |

**Kalibratsiya (ansambl):**

| Oraliq | n | o'rtacha bashorat | haqiqiy chastota | og'ish |
|---|---|---|---|---|
| 0%-10% | 283 | 6.2% | 6.0% | -0.2 p.p. |
| 10%-20% | 694 | 15.7% | 16.6% | +0.9 p.p. |
| 20%-30% | 1212 | 25.2% | 23.3% | -1.9 p.p. |
| 30%-40% | 961 | 34.6% | 35.4% | +0.8 p.p. |
| 40%-50% | 1038 | 45.3% | 46.1% | +0.7 p.p. |
| 50%-60% | 978 | 54.5% | 55.1% | +0.6 p.p. |
| 60%-70% | 498 | 64.9% | 64.5% | -0.4 p.p. |
| 70%-80% | 335 | 74.4% | 73.7% | -0.7 p.p. |
| 80%-90% | 200 | 84.4% | 85.0% | +0.6 p.p. |
| 90%-100% | 57 | 93.1% | 93.0% | -0.1 p.p. |

**Liga kesimi:**

| Kesim | signallar | ROI (95% CI) | CLV |
|---|---|---|---|
| Synthetic League 0 | 17 | +41.76% [-18.13%; +100.01%] | -3.24% |
| Synthetic League 1 | 28 | -2.50% [-36.44%; +33.75%] | -3.46% |
| Synthetic League 2 | 44 | +0.59% [-32.76%; +33.71%] | -3.00% |

**Bozor kesimi:**

| Kesim | signallar | ROI (95% CI) | CLV |
|---|---|---|---|
| 1x2 | 20 | +65.50% [+10.55%; +115.86%] | -2.61% |
| asian_handicap | 40 | +7.60% [-19.28%; +33.01%] | -3.01% |
| totals 2.5 | 29 | -32.69% [-71.18%; +9.04%] | -3.84% |

## Validatsiya (tanlangan konfiguratsiya, optimistik): mavsum 2022

Bashoratlar: 1140 · o'tkazib yuborilgan: yo'q · fitlar: 114 (muvaffaqiyatsiz: 0)

| Ko'rsatkich | Qiymat |
|---|---|
| Signallar | 94 |
| ROI / yield (flat) | +14.01% [-9.35%; +38.96%] |
| Foyda (flat, birlik) | +13.17 |
| Hit rate (foyda bilan yopilgan) | 46.8% |
| Maksimal pasayish (flat, birlik) | 5.88 |
| ROI yopilish narxida (94 ta) | +14.54% [-10.32%; +39.18%] |
| CLV: o'rtacha log(odds / yopilish adolatli) (94 ta) | -3.10% [-3.22%; -2.98%] |
| Yopilish narxi bor stavkalar (CLV qamrovi) | 94 / 94 |
| Yopilishsiz stavkalar ROI, olingan narxda (0 ta) | - |
| Yopilishni yutgan ulush | 0.0% |
| Yopilish taqsimotida kutilgan EV | -2.95% |
| Kelly 1/4: ROI · bank daromadi · maks. pasayish | +7.66% · +12.86% · 11.53 |

**Proper scoring (bozor nusxasiga o'rtacha; kichik = yaxshi):**

| Guruh | n | log-loss ansambl | log-loss DC | log-loss bozor (yopilish) | Brier ansambl | Brier DC | Brier bozor |
|---|---|---|---|---|---|---|---|
| 1x2 | 1139 | 0.9265 | 0.9466 | 0.9275 | 0.5489 | 0.5624 | 0.5497 |
| asian_handicap (x.5) | 283 | 0.6893 | 0.7152 | 0.6888 | 0.4962 | 0.5192 | 0.4957 |
| totals 2.5 | 1140 | 0.6287 | 0.6560 | 0.6245 | 0.4400 | 0.4634 | 0.4368 |
| all | 2562 | 0.7678 | 0.7918 | 0.7663 | 0.4946 | 0.5136 | 0.4935 |

**Kalibratsiya (ansambl):**

| Oraliq | n | o'rtacha bashorat | haqiqiy chastota | og'ish |
|---|---|---|---|---|
| 0%-10% | 225 | 6.4% | 6.2% | -0.2 p.p. |
| 10%-20% | 659 | 16.0% | 14.6% | -1.4 p.p. |
| 20%-30% | 1264 | 25.3% | 24.5% | -0.8 p.p. |
| 30%-40% | 996 | 34.6% | 35.1% | +0.6 p.p. |
| 40%-50% | 1096 | 45.3% | 47.0% | +1.7 p.p. |
| 50%-60% | 984 | 54.7% | 53.3% | -1.4 p.p. |
| 60%-70% | 522 | 64.7% | 65.7% | +1.0 p.p. |
| 70%-80% | 331 | 74.6% | 75.2% | +0.7 p.p. |
| 80%-90% | 146 | 83.9% | 83.6% | -0.4 p.p. |
| 90%-100% | 40 | 93.1% | 97.5% | +4.4 p.p. |

**Liga kesimi:**

| Kesim | signallar | ROI (95% CI) | CLV |
|---|---|---|---|
| Synthetic League 0 | 28 | +43.43% [-4.01%; +89.75%] | -3.14% |
| Synthetic League 1 | 28 | +26.70% [-15.29%; +69.54%] | -3.08% |
| Synthetic League 2 | 38 | -17.03% [-51.19%; +17.28%] | -3.09% |

**Bozor kesimi:**

| Kesim | signallar | ROI (95% CI) | CLV |
|---|---|---|---|
| 1x2 | 35 | +43.86% [-2.14%; +87.97%] | -2.68% |
| asian_handicap | 36 | -7.43% [-37.61%; +23.24%] | -2.99% |
| totals 2.5 | 23 | +2.13% [-51.22%; +58.00%] | -3.91% |

## Grafiklar

![Kalibratsiya](2026-10-21-synthetic-model-only-calibration.png)

![Foyda egri chizig'i](2026-10-21-synthetic-model-only-equity.png)

## F3 darvozasi (hold-out, qaror #11)

| Mezon | Nuqta bahosi | 95% CI | n | Holat | Izoh |
|---|---|---|---|---|---|
| Kalibratsiya: har 10% oraliqda (n >= 30) og'ish <= 5 p.p. | eng katta 1.9 p.p. | - | 10 oraliq / 6256 bashorat | ✅ | ansambl, partition bozorlar |
| >= 500 signal; CLV o'rtachasi > 0 va 95% CI pastki chegarasi > 0 | -3.19% | [-3.33%; -3.05%] | 89 / 89 | ❌ | faqat yopilish narxi bor stavkalar; signal kam (< 500) |
| ROI: nuqta >= 0% va 95% CI pastki chegarasi >= -2.00% | +7.48% | [-15.14%; +29.26%] | 89 | ahamiyatsiz | flat, olingan koeffitsientda |
| Kamida 3 ta ligada (har biri n >= 100) CLV > 0 | 0 liga | - | 0 / 3 liga n >= 100 | ❌ | - |
| Sof bozor (w = 0) bazaviy varianti bilan solishtirish (log-loss) | 0.7605 vs 0.7595 | - | hold-out partition bozorlar | ❌ | model bozordan yaxshi emas: bozor modeli rejimi |

Darvoza faqat barcha mezonlar ✅ bo'lsa o'tadi. 'ahamiyatsiz' = nuqta bahosi bajarilgan, lekin 95% CI bajarilmagan (qarorlar jurnali #11): ✅ hisoblanmaydi.
