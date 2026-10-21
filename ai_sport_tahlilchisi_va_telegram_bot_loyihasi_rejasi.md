# AI Sport Tahlilchisi va Telegram Bot Loyihasi (Advisory System) — v2

> Bosqichma-bosqich ishlab chiqish rejasi (0 → production): [ishlab_chiqish_rejasi.md](ishlab_chiqish_rejasi.md)

## 1. Loyiha Maqsadi
Avtomatik stavka qiluvchi bot o'rniga, inson qaror qabul qilishiga yordam beruvchi tahliliy yordamchi (Copilot) yaratish. Tizim har kuni o'yinlarni saralaydi, **matematik model orqali ehtimollikni hisoblaydi**, bukmeker koeffitsienti bilan solishtiradi va faqat **musbat kutilgan qiymatga (EV > 0)** ega o'yinlar bo'yicha Telegramga signal yuboradi. AI (LLM) esa tahlilni izohlaydi va sifatli kontekst (jarohatlar, motivatsiya) qo'shadi.

Foydalanuvchi Telegram bot orqali **sana oralig'ini, sport turini, ligalarni, o'yinlar sonini, aniq jamoa yoki o'yinlarni va minimal o'tish ehtimolligini** o'zi tanlaydi. Har qanday tahlilni istalgan vaqtda **qayta hisoblatib** olishi mumkin (6-bo'lim).

### v1 dan asosiy o'zgarishlar
| # | v1 dagi muammo | v2 dagi yechim |
|---|---|---|
| 1 | "Value bet"ni LLM tanlaydi. LLM ehtimollikni aniq hisoblay olmaydi, natija har safar o'zgaradi | Ehtimollik **kodda** hisoblanadi (Poisson / Elo / bozor modeli), LLM faqat izoh va kontekst beradi |
| 2 | Har kuni majburiy "Top 10". Yaxshi o'yin bo'lmagan kunda ham 10 ta signal chiqadi | **Chegara (threshold) asosida:** kuniga 0–N ta signal, faqat EV ≥ belgilangan foiz bo'lsa |
| 3 | So'rovlar hisobi to'g'ri emas: 30 o'yin × (2 jamoa forma + jarohat + statistika) ≈ 90–120 so'rov, ya'ni 100/kun limitdan oshadi | Liga bo'yicha **bitta so'rov** bilan butun mavsum natijalari olinadi va keshlanadi. Jami ≈ 25–40 so'rov/kun |
| 4 | Gemini 1.5 Pro eskirgan (ishlab chiqarishdan olingan) | Joriy **Gemini Flash** modeli (arzon, tez, structured JSON output) |
| 5 | Muvaffaqiyat faqat "Win Rate" bilan o'lchanadi. Turli koeffitsientlarda bu ko'rsatkich chalg'itadi | **ROI, Yield, CLV (Closing Line Value), Brier score** |
| 6 | Signal 09:00 da bir marta chiqadi, tarkiblar (lineups) hali ma'lum emas | Ertalab dastlabki ro'yxat, o'yindan **~60 daqiqa oldin** tarkib bilan qayta tekshiruv |
| 7 | "Bloklash xavfi 0%" deyilgan | Bot-detection xavfi yo'qoladi, lekin bukmekerlar **yutayotgan akkauntlarga limit qo'yadi**. Bu xavf qoladi |
| 8 | Bitta foydalanuvchi uchun PostgreSQL + Docker Compose ortiqcha murakkablik | MVP uchun **SQLite**, kerak bo'lganda PostgreSQL ga o'tish |

---

## 2. Texnologik Stek

* **Dasturlash tili:** **Python 3.13** (yagona tanlov: `pandas`, `scipy` kabi statistik kutubxonalar va Gemini SDK shu tilda eng qulay).
  * `httpx` (async API so'rovlar), `python-telegram-bot` yoki `aiogram`, `APScheduler`, `pydantic` (sxema validatsiyasi), `SQLAlchemy`.
* **Statistik model:** Poisson / Dixon-Coles (gollar modeli) + bozor koeffitsientlaridan margin olib tashlangan ehtimollik.
* **AI Model:** Google Gemini Flash (joriy versiya). `response_schema` orqali qat'iy JSON, `temperature` ≈ 0.2.
* **Ma'lumotlar manbai:**
  * **API-Football** (to'g'ridan-to'g'ri `api-sports.io` yoki RapidAPI orqali): o'yinlar, natijalar, jarohatlar, tarkiblar.
  * *(Ixtiyoriy)* **The Odds API**: bir nechta bukmeker koeffitsientlari, jumladan Pinnacle (eng "aniq" bozor). Bu bozor modeli uchun kerak.
* **Interfeys:** Telegram Bot API.
* **Ma'lumotlar bazasi:** **SQLite** (MVP) → PostgreSQL (ko'p foydalanuvchi yoki katta hajm bo'lganda).
* **Infratuzilma:** Bitta Docker konteyner (yoki oddiy `systemd` servis) + VPS.

---

## 3. Arxitektura ("Quyg'ich" — Funnel, optimallashtirilgan)

```
 200–300 o'yin  ──[1. Kod filtri: liga, koeff. diapazoni]──►  20–40 o'yin
                ──[2. Statistik model: p_model, EV hisobi]──►  EV ≥ 5% bo'lganlar (0–10 ta)
                ──[3. Gemini: kontekst tekshiruvi + izoh]────►  Tasdiqlangan signallar
                ──[4. Pre-match: tarkib chiqqach qayta EV]───►  Yakuniy signal → Telegram
```

**Asosiy tamoyil:** qimmat va noaniq qadam (LLM) eng oxirida, eng kam o'yin bilan ishlaydi. Qaror raqamlarga asoslanadi, matnga emas.

---

## 4. Tizimning Ishlash Algoritmi

### 0-qadam: Kesh va ma'lumotlarni yangilash (kuniga 1 marta, 08:30)
* Kuzatiladigan har bir liga uchun `fixtures?league={id}&season={yil}` bitta so'rovda butun mavsum o'yinlari va natijalarini beradi. Bu ma'lumot bazaga yoziladi.
* Har jamoaning so'nggi 5 o'yini, gol o'rtachalari, uy/mehmon kuchi **lokal bazadan** hisoblanadi, API ga alohida so'rov ketmaydi.
* xG (agar tarif va liga qo'llab-quvvatlasa) faqat **yangi tugagan** o'yinlar uchun `fixtures/statistics` orqali olinadi (inkremental).

### 1-qadam: Kunlik ro'yxat (09:00)
* `fixtures?date=YYYY-MM-DD`: bugungi barcha o'yinlar (1 so'rov).
* Koeffitsientlar: `odds?league=..&season=..&date=..` (liga bo'yicha, sahifalash bilan) yoki The Odds API.

### 2-qadam: Birlamchi filtr (kod)
* Faqat oq ro'yxatdagi ligalar (masalan, Top-5 + 5–10 ta barqaror liga). O'rtoqlik va kubokning dastlabki bosqichlari chiqarib tashlanadi.
* Koeffitsient diapazoni: 1.40 – 3.00 (sozlanadi).
* Jamoada kamida 6 ta o'yin ma'lumoti bo'lmasa (mavsum boshi), o'yin o'tkazib yuboriladi.

### 3-qadam: Statistik model va EV hisobi (kod, asosiy qism)
1. **Model ehtimolligi (`p_model`):** Poisson modeli. Jamoaning hujum/himoya kuchi × liga o'rtachasi × uy ustunligi asosida kutilgan gollar (λ) topiladi, so'ng 1X2, Total 2.5, BTTS ehtimolliklari hisoblanadi.
2. **Bozor ehtimolligi:** koeffitsientdan margin olib tashlanadi: `p_bozor = (1/koeff) / Σ(1/koeff_i)`.
3. *(Ixtiyoriy, kuchli usul)* **Bozor modeli:** Pinnacle koeffitsientidan margin olib tashlab "haqiqiy" ehtimollik olinadi va 1xBet koeffitsienti bilan solishtiriladi. Bu amalda eng barqaror value manbai.
4. **Kutilgan qiymat:** `EV = p × koeff − 1`. Faqat `EV ≥ 0.05` (5%) va `p ≥ 0.35` bo'lgan tanlovlar qoladi.
5. **Stavka hajmi:** Kelly mezonining 1/4 qismi (`f = 0.25 × (p×k − 1)/(k − 1)`), bankning maksimal 2% i bilan cheklanadi. Boshlanishda oddiy **flat 1 unit** tavsiya etiladi.

### 4-qadam: Gemini — kontekst va izoh (faqat EV filtrdan o'tganlar, 0–10 ta)
* **Bitta so'rov**, barcha nomzodlar JSON ko'rinishida yuboriladi. Javob `response_schema` bilan qat'iy formatda qaytadi.
* LLM **ehtimollikni o'zgartirmaydi**. U faqat:
  * Raqamlarda ko'rinmaydigan xavflarni belgilaydi (asosiy o'yinchi jarohati, rotatsiya, motivatsiya yo'qligi, derbi);
  * `verdict: "tasdiq" | "ehtiyot" | "rad"` beradi;
  * 1–2 gaplik izoh yozadi.
* **Prompt namunasi (system):**
  > "Sen sport tahlilchisisan. Senga statistik model allaqachon hisoblagan nomzod stavkalar beriladi (p_model, koeffitsient, EV, forma, jarohatlar). Ehtimollikni qayta hisoblama. Har bir nomzod uchun faqat raqamlarda aks etmagan sifatli omillarni bahola: jarohat va diskvalifikatsiyalar, rotatsiya xavfi, turnir motivatsiyasi. Agar jiddiy salbiy omil bo'lsa `rad`, noaniq bo'lsa `ehtiyot`, aks holda `tasdiq` qaytar. Izohni o'zbek tilida, 2 gapdan oshirmay, faqat berilgan ma'lumotlarga tayanib yoz. Ma'lumot yetarli bo'lmasa, buni ochiq ayt. To'qib chiqarma."
* **Chiqish sxemasi:**
  ```json
  { "fixture_id": 123, "verdict": "tasdiq", "risk_flags": ["rotatsiya"], "izoh": "..." }
  ```
* Promptga **"Tajriba konteksti"** bloki qo'shiladi: o'tgan natijalar asosidagi shu liga, bozor va jamoa statistikasi hamda tegishli saboqlar (6.13-bo'lim).
* Javob `pydantic` bilan tekshiriladi. Xato bo'lsa, 1 marta qayta so'raladi, keyin LLM izohisiz (faqat raqamlar bilan) yuboriladi. Tizim LLM ga qaram bo'lmaydi.

### 5-qadam: Telegramga yetkazish
* **Ertalabki xabar (09:15):** "Bugungi nomzodlar" (o'yin, bozor, koeff., p_model, EV, tavsiya unit, izoh). Signal bo'lmasa: *"Bugun value topilmadi"*. Bu ham foydali natija.
* **Pre-match yangilanish (kick-off − 60 daq):** `fixtures/lineups` va joriy koeffitsient olinadi, EV qayta hisoblanadi → "✅ Kuchda" yoki "❌ Bekor (koeff. tushdi / asosiy o'yinchi yo'q)".
* Bot imkoniyatlari batafsil **6-bo'limda** keltirilgan.
* Formatlash: `parse_mode=HTML`.

### 6-qadam: Natijalarni tekshirish va hisobot (har kuni 08:00)
* Kechagi signallar `fixtures?ids=id1-id2-...` (20 tagacha bitta so'rovda) orqali tekshiriladi.
* Hisoblanadigan metrikalar:
  * **ROI / Yield**: asosiy foyda ko'rsatkichi;
  * **CLV**: signal koeffitsienti yopilish koeffitsientidan yuqori bo'lgan holatlar ulushi. Kam namunada (100–200 stavka) ham modelning haqiqiy ustunligini eng yaxshi ko'rsatadi;
  * **Brier score**: `p_model` qanchalik kalibrlanganini o'lchaydi;
  * Win Rate: faqat qo'shimcha ma'lumot sifatida.
* Haftalik hisobot Telegramga avtomatik yuboriladi.

---

## 5. Ko'p Sport Turlari Arxitekturasi

Har bir sport turi alohida **adapter** sifatida ulanadi. Pipeline, bot, baza va hisobotlar barcha sportlar uchun umumiy, faqat ma'lumot manbai va statistik model sportga xos bo'ladi.

```python
class SportAdapter(Protocol):
    sport: str                                   # "football", "hockey", ...
    def fetch_fixtures(self, date) -> list[Fixture]: ...
    def fetch_odds(self, fixtures) -> list[OddsSnapshot]: ...
    def refresh_history(self, league_ids) -> None: ...
    def predict(self, fixture) -> dict[Market, float]: ...   # bozor → ehtimollik
    def markets(self) -> list[Market]: ...
```

| Sport | Manba | Statistik model | Asosiy bozorlar | Bosqich |
|---|---|---|---|---|
| ⚽ Futbol | API-Football | Poisson / Dixon-Coles | 1X2, Ikki natija, Total, BTTS, Fora | MVP |
| 🏒 Xokkey | API-Hockey | Poisson (asosiy vaqt) | 1X2, Total, Puck line | 2-bosqich |
| 🏀 Basketbol | API-Basketball | Reyting + temp, ochkolarning normal taqsimoti | G'olib, Fora, Total | 2-bosqich |
| 🏐 Voleybol | API-Volleyball | Set ehtimolligi modeli | G'olib, Setlar totali | 3-bosqich |
| 🎾 Tennis | Alohida manba kerak | Elo (qoplama bo'yicha) | G'olib, Geymlar totali | 3-bosqich |

* API-Sports oilasidagi har bir sport API si bitta kalit bilan ishlaydi, lekin limitlar **sport bo'yicha alohida** hisoblanishi mumkin (tarifni tekshiring).
* Har bir sport alohida backtestdan o'tmaguncha botda faqat "🧪 Test rejimi" belgisi bilan ko'rsatiladi.

---

## 6. Telegram Bot Imkoniyatlari

### 6.1. Asosiy menyu (doimiy klaviatura)
```
[📊 Tahlil]           [🔍 Maxsus tahlil]
[💾 Shablonlarim]     [⚡ Bugungi signallar]
[⭐ Sevimlilar]        [🎟 Mening stavkalarim]
[📈 Hisobot]          [⚙️ Sozlamalar]
[ℹ️ Holat / Yordam]
```

### 6.2. 📊 Tahlil (sana oralig'i bilan)
Asosiy tahlil oqimi. Bosqichma-bosqich inline tugmalar orqali tanlanadi, har bir qadamda `[⬅️ Orqaga]` va `[⏭ Standart]` tugmalari bor:

1. **📅 Sana oralig'i:**
   `Bugun` `Ertaga` `Dam olish kunlari` `3 kun` `7 kun` `📅 Oraliq tanlash`
   * "Oraliq tanlash" bosilganda inline kalendar chiqadi: boshlanish va tugash sanasi tanlanadi.
   * Matn bilan ham kiritish mumkin: `25.10 - 31.10` yoki `25.10.2026-31.10.2026`.
   * Kelajak oralig'i ko'pi bilan **14 kun** (bukmeker koeffitsientlari odatda 1–2 hafta oldin chiqadi).
2. **🏅 Sport turi:** ⚽ Futbol / 🏒 Xokkey / 🏀 Basketbol / … / 🌐 Hammasi
3. **🎯 O'tish ehtimolligi (minimal):** `50%` `60%` `70%` `80%` `90%` `✏️ O'zim kiritaman`
   * Tanlash bilan birga minimal EV ham so'raladi (standart: sozlamalardagi qiymat). 80% dan yuqorida 6.6-bo'limdagi ogohlantirish ko'rsatiladi.
4. **🔎 Qamrov (ixtiyoriy):**
   * `🌐 Hamma o'yinlar` (standart, sozlamalardagi ligalar bo'yicha)
   * `🏆 Ligalar`: ✅ checkbox ro'yxat
   * `👕 Jamoalar`: nom yozib qidiriladi, bir nechta jamoa qo'shiladi (`Arsenal, Inter, Barcelona`), oraliqdagi ularning barcha o'yinlari olinadi
   * `⚽ Aniq o'yinlar`: oraliqdagi o'yinlar **kunlar bo'yicha guruhlangan** checkbox ro'yxat sifatida chiqadi (sahifalash bilan: `◀️ 1/4 ▶️`), keraklilari belgilanadi
5. **🔢 O'yinlar soni:** `3` `5` `10` `15` `Hammasi` yoki `✏️ O'zim kiritaman` (oraliq bo'yicha jami yoki kuniga)
6. **📊 Bozorlar:** 1X2, Total, BTTS, Fora… (ko'p tanlov)
7. **↕️ Saralash:** EV / ehtimollik / sana va vaqt bo'yicha

Tanlovlar oxirida tasdiqlash xabari chiqadi:
```
📋 So'rov: 25.10–31.10 · ⚽ Futbol · ≥ 60% · EV ≥ 5%
Qamrov: Arsenal, Inter · Bozorlar: 1X2, Total · Soni: hammasi
Topilgan o'yinlar: 4 · Taxminiy API sarfi: 6 so'rov
[▶️ Boshlash] [✏️ O'zgartirish] [💾 Shablon sifatida saqlash]
```

**Tezkor buyruq** (menyusiz, bir qatorda):
`/tahlil 25.10-31.10 futbol 60% Arsenal,Inter`
Berilmagan parametrlar sozlamalardagi standart qiymatlardan olinadi.

**Oraliq tahlilining xususiyatlari:**
* Natijalar **kunlar bo'yicha guruhlab** yuboriladi (`📅 Shanba, 25.10 — 3 ta signal`). Ko'p bo'lsa, sahifalanadi.
* 3 kundan uzoq o'yinlar uchun tarkib va jarohat ma'lumotlari hali to'liq emas. Shuning uchun ularning ishonch darajasi avtomatik bir pog'ona pasaytiriladi va `⏳ Dastlabki tahlil` belgisi qo'yiladi.
* Oraliqdagi signallar uchun **avtomatik qayta hisoblash** rejalashtiriladi: o'yindan 24 soat va (sozlamaga ko'ra) 60 daqiqa oldin. O'zgarish bo'lsa, bot xabar beradi.
* **O'tgan sana oralig'i** ham tanlanishi mumkin (retro-tahlil). Bunda bot model o'sha o'yinlar uchun nima bergan bo'lardi va natija qanday bo'lganini ✅/❌ bilan ko'rsatadi. Modelga qanchalik ishonish mumkinligini tekshirish uchun foydali (faqat bazada mavjud o'yinlar, API sarfi deyarli yo'q).

**💾 Shablonlar va obuna:**
* Tez-tez ishlatiladigan so'rovni nom bilan saqlash mumkin: masalan, *"Dam olish kunlari · APL · 60%+"*. U keyin bitta tugma bilan ishga tushadi.
* Shablonga **jadval** biriktirish mumkin: masalan, *"har juma 10:00 da shu shablon bo'yicha tahlil yubor"*.

Natija har bir signal uchun alohida karta sifatida keladi:
```
⚽ Arsenal — Chelsea   🕒 20:00  🏆 Premier League
🎯 Tanlov: Total 2.5 dan ko'p   @ 1.85
📊 Ehtimollik: 61% (adolatli koeff. 1.64)
💹 EV: +12.9%   🛡 Ishonch: Yuqori
💰 Tavsiya: 1.5 unit
📝 Ikki jamoaning so'nggi 5 o'yinidagi xG o'rtachasi 3.1. Chelsea himoyachisi jarohatlangan.

[🔄 Qayta hisoblash] [📋 Batafsil] [✅ Stavka qildim] [🙈 Yashirish]
```
Agar tanlangan son bo'yicha shartlarga mos o'yin yetarli bo'lmasa, bot shuni ochiq aytadi: *"Shartlarga 3 ta o'yin mos keldi (siz 10 ta so'ragansiz)"*. Shartga to'g'ri kelmaydigan o'yin ro'yxatni to'ldirish uchun qo'shilmaydi.

### 6.3. 🔍 Maxsus tahlil (aniq jamoa yoki o'yin)
* **Jamoa nomi bo'yicha qidiruv:** `Real` deb yozilsa → "Real Madrid, Real Sociedad, Real Betis…" tugmalari chiqadi (noaniq qidiruv, lotin/kirill va xatolarni ham topadi). Tanlangan jamoaning yaqin 3 ta o'yini ko'rsatiladi.
* **O'yinni to'g'ridan-to'g'ri yozish:** `/tahlil Arsenal - Chelsea` yoki oddiy matn `Arsenal Chelsea`.
* **Ro'yxatdan bir nechtasini tanlash:** bugungi o'yinlar ✅ checkbox tugmalari bilan chiqadi, belgilanganlar `[▶️ Tahlil qilish]` tugmasi bilan birdaniga tahlil qilinadi.
* Maxsus tahlil **EV filtrisiz** ishlaydi: o'yin qiymatsiz bo'lsa ham, barcha bozorlar bo'yicha ehtimolliklar ko'rsatiladi va *"Value topilmadi"* deb belgilanadi.

### 6.4. 🔄 Qayta hisoblash
* **Bitta signal:** kartadagi `[🔄 Qayta hisoblash]` tugmasi.
* **Butun so'rov:** `/qayta` yoki natijalar ostidagi `[🔄 Hammasini yangilash]`. Bu bugungi kun, sana oralig'i yoki shablon natijalarining hammasini qayta hisoblaydi.
* Qayta hisoblashda yangi koeffitsientlar, jarohatlar va (chiqqan bo'lsa) tarkiblar olinadi. Bot **farqni** ko'rsatadi:
  ```
  🔄 Arsenal — Chelsea yangilandi (14:32)
  Koeff.: 1.85 → 1.72   EV: +12.9% → +4.9%
  Holat: ⚠️ Chegaradan pastga tushdi, tavsiya etilmaydi
  ```
* Har bir hisob versiyasi saqlanadi (`signal_versions`), keyin "qaysi versiya to'g'ri chiqdi" tahlil qilinadi.
* **Himoya:** bitta o'yin uchun 10 daqiqada 1 marta. Kunlik API limitining 30% i qo'lda so'rovlar uchun zaxirada turadi. Limit tugasa, keshlangan ma'lumot *"09:00 holatiga"* degan belgi bilan ko'rsatiladi.

### 6.5. ⚙️ Sozlamalar
| Sozlama | Qiymatlar | Standart |
|---|---|---|
| Sport turlari | ko'p tanlov | Futbol |
| Ligalar | sport bo'yicha ro'yxat | Top-10 liga |
| Bozorlar | 1X2, Total, BTTS, Fora… | 1X2, Total |
| Kunlik signal soni (maks.) | 1–30 | 10 |
| **Minimal ehtimollik** | 30–95% | 50% |
| Minimal EV | 0–20% | 5% |
| Koeffitsient diapazoni | masalan 1.40–3.00 | 1.40–3.00 |
| Risk profili (tayyor shablon) | Konservativ / Balanslangan / Agressiv | Balanslangan |
| Yuborish vaqti | soat | 09:00 |
| Pre-match yangilanish | o'chiq / 30 / 60 / 90 daqiqa | 60 daq. |
| Tinch soatlar | masalan 00:00–08:00 | yoqilgan |
| Vaqt mintaqasi | — | Asia/Tashkent |
| Bank hajmi va unit | summa | — |

**Risk profillari:**
| Profil | Min. ehtimollik | Min. EV | Koeff. |
|---|---|---|---|
| 🛡 Konservativ | 60% | 3% | 1.30–2.00 |
| ⚖️ Balanslangan | 45% | 5% | 1.50–3.00 |
| 🔥 Agressiv | 30% | 8% | 2.00–5.00 |

### 6.6. Ehtimollik va ishonch darajasi (90%+ masalasi)
Botda ikki xil ko'rsatkich bor va ular aralashtirilmaydi:
1. **Ehtimollik (%):** model hisoblagan natija ehtimolligi. Foydalanuvchi minimal chegarani belgilay oladi, masalan `≥ 90%`.
2. **Ishonch darajasi (Yuqori / O'rta / Past):** hisobning o'ziga qanchalik ishonish mumkinligi. U quyidagilardan tuziladi:
   * ma'lumot sifati (o'yinlar soni, xG mavjudligi, tarkib chiqqanmi);
   * model va bozor bir-biriga qanchalik mos;
   * Gemini kontekst bahosi (`tasdiq` / `ehtiyot`);
   * shu liga va bozor bo'yicha modelning o'tgan aniqligi (kalibratsiya).

⚠️ **Chegara 80–90% dan yuqori qo'yilganda** bot quyidagi ogohlantirishni bir marta ko'rsatadi:
> 90% ehtimollik odatda 1.03–1.10 koeffitsientga to'g'ri keladi. Bunday tanlovlar kam uchraydi (kuniga 0–3 ta) va deyarli har doim **manfiy EV** ga ega: bukmeker marjasi sababli 1.07 koeffitsientda 1 ta mag'lubiyatni qoplash uchun 14 ta g'alaba kerak. Tavsiya: ehtimollik 55–70% + EV ≥ 5%.

Bot foydalanuvchi tanlovini bajaradi, lekin `EV < 0` bo'lgan signallarni *"❗ Qiymatsiz"* deb belgilaydi.

### 6.7. ⭐ Sevimlilar (Watchlist)
* Jamoa yoki ligani sevimlilarga qo'shish (`[⭐]` tugmasi yoki `/kuzat Barcelona`).
* Sevimli jamoaning o'yini bo'lsa, u kunlik xabarga **har doim** kiritiladi (EV past bo'lsa ham, belgi bilan).
* Ixtiyoriy bildirishnomalar: tarkib chiqqanda, koeffitsient keskin o'zgarganda, o'yin yakunlanganda.

### 6.8. 📋 Batafsil o'yin kartasi
* Forma (so'nggi 5 o'yin: ✅✅➖❌✅), uy/mehmon statistikasi, H2H (so'nggi 5 uchrashuv)
* xG, gol o'rtachalari, jarohatlar va diskvalifikatsiyalar
* **Eng ehtimolli 3 ta hisob** (masalan, 2:1 – 11%, 1:1 – 10%, 2:0 – 9%)
* Barcha bozorlar jadvali: model ehtimolligi | adolatli koeff. | bukmeker koeff. | EV
* Koeffitsient harakati (ertalab → hozir)

### 6.9. 🎟 Mening stavkalarim va bank
* `[✅ Stavka qildim]` bosilganda haqiqiy koeffitsient va summa so'raladi (standart: signal qiymati).
* O'yin tugagach natija avtomatik hisoblanadi: foyda/zarar, bank balansi.
* `/bank`: joriy balans, oylik P/L, bank o'zgarishi grafigi (PNG rasm).
* **Mas'uliyatli o'yin:** kunlik/haftalik zarar limiti. Limitga yetilganda bot ogohlantiradi va o'sha kun yangi signal yubormaydi.

### 6.10. 📈 Hisobot
* Davr: 7 kun / 30 kun / hammasi / o'zim tanlayman.
* Kesimlar: sport, liga, bozor, risk profili, ishonch darajasi.
* Ko'rsatkichlar: signallar soni, ROI, Yield, CLV, Win Rate, eng uzun mag'lubiyat seriyasi.
* **Kalibratsiya jadvali:** *"60–70% deb berilgan signallar haqiqatda 64% holatda o'tdi"*. Bot berayotgan foizlarga qanchalik ishonish mumkinligini shu jadval ko'rsatadi.
* `/eksport`: barcha signallar va stavkalar CSV faylda.

### 6.11. 🧩 Ekspress tuzuvchi (ixtiyoriy)
* Kunlik signallardan 2–4 tasini tanlab ekspress yig'ish. Umumiy ehtimollik va EV hisoblanadi.
* Bir o'yindagi o'zaro bog'liq bozorlar (masalan, W1 + Total ko'p) birlashtirilmaydi.
* Ogohlantirish: 3 ta 70% lik tanlovdan tuzilgan ekspressning o'tish ehtimolligi atigi 34%.

### 6.12. ℹ️ Tizim va xavfsizlik
* `/holat`: API limitlari qoldig'i (sport bo'yicha), oxirgi yangilanish vaqti, oxirgi xatolar.
* **Kirish cheklovi:** bot faqat ruxsat berilgan Telegram `user_id` lar uchun ishlaydi (whitelist). Begonalar javob olmaydi.
* Tizimdagi xatolar (API ishlamay qoldi, Gemini javob bermadi) adminga avtomatik yuboriladi.
* `/yordam`: barcha buyruqlar va atamalar (EV, CLV, unit) qisqa izohi.

### 6.13. ✅ Natijalarni belgilash va o'z-o'zini yaxshilash (Feedback loop)

**A. Natijani belgilash**
* O'yin tugagach natija API orqali **avtomatik** aniqlanadi va karta yangilanadi: `✅ O'tdi` / `❌ O'tmadi` / `↩️ Qaytarildi (void)`.
* Har bir yakunlangan kartada qo'lda tuzatish tugmalari bor: `[✅ O'tdi] [❌ O'tmadi] [↩️ Void] [💸 Cashout]`. Ular API natija bermagan, o'yin bekor qilingan yoki siz erta yopgan holatlar uchun.
* `/natijalar`: natijasi hali aniqlanmagan signallar ro'yxati, bir tugma bilan belgilanadi.
* Ertalabki xabar boshida kechagi yakun chiqadi: *"Kecha: 6 ta signal · 4 ✅ · 2 ❌ · +2.3 unit · CLV +3.1%"*.
* Ixtiyoriy izoh: `[💬 Izoh]` tugmasi bilan *"tarkibda asosiy hujumchi yo'q edi"* kabi kuzatuvni yozib qo'yish mumkin. U keyingi tahlilda kontekst sifatida ishlatiladi.

**B. Natijalar modelni qanday yaxshilaydi.** Natijalar 4 qatlamda ishlatiladi. Asosiy yaxshilanish 1- va 2-qatlamda: LLM o'z-o'zidan "o'rganmaydi", u faqat berilgan matnni o'qiydi.

| # | Qatlam | Nima qiladi | Qachon |
|---|---|---|---|
| 1 | **Kalibratsiya** | Model "60%" desa-yu, haqiqatda 52% o'tsa, keyingi ehtimolliklar avtomatik to'g'rilanadi (isotonic / Platt regressiya). | Har hafta, ≥ 200 signaldan keyin |
| 2 | **Segment filtri** | Liga × bozor × koeff. diapazoni kesimida ROI va CLV hisoblanadi. Doimiy manfiy segmentlar (masalan, "Seriya A, Total ko'p") uchun EV chegarasi oshiriladi yoki ular vaqtincha o'chiriladi. | Har hafta, segmentda ≥ 50 signal |
| 3 | **Gemini uchun "tajriba konteksti"** | Promptga shu liga, bozor va jamoa bo'yicha **qisqa statistik xulosa** qo'shiladi (pastda namuna). | Har bir tahlilda |
| 4 | **Mag'lubiyat tahlili (post-mortem)** | Har bir ❌ uchun Gemini sababni toifalaydi: `omadsizlik` (xG bo'yicha model to'g'ri edi) / `ma'lumot yetishmadi` (jarohat, rotatsiya) / `model xatosi`. Faqat qaytariladigan xatolar "saboq" sifatida saqlanadi. | Har kuni, natijalar tekshiruvidan keyin |

**3-qatlam: promptga qo'shiladigan blok namunasi** (xom tarix emas, ixcham xulosa, ≈ 150–300 token):
```
TAJRIBA KONTEKSTI (so'nggi 90 kun):
- Premier League · Total 2.5 ko'p: 48 signal, ROI +6.2%, kalibratsiya: model +3 p.p. ortiqcha baholaydi
- Chelsea ishtirokidagi signallar: 9 ta, 3 ✅ / 6 ❌. 4 tasi "rotatsiya" sababli
- Tegishli saboqlar:
  • Yevrokubokdan 3 kun oldin Chelsea asosiy tarkibni dam oldiradi (2 marta tasdiqlangan)
- Foydalanuvchi izohi (12.10): "Chelsea himoyasi yangi murabbiy bilan kuchaydi"
```
* Saboqlar jamoa, liga va bozor bo'yicha **teglanadi**. Promptga faqat shu o'yinga tegishlilari qo'shiladi (eng ko'pi 5 ta). Butun tarix yuborilmaydi: token tejaladi va LLM chalg'imaydi.
* Bir marta uchragan holat "saboq" emas. Saboq kamida **2 marta** takrorlangandan keyin faollashadi va 90 kundan keyin eskiradi.

**C. Xavfsizlik qoidalari (ortiqcha moslashuvning oldini olish)**
* **Barcha signallar** natijasi hisobga olinadi, faqat siz stavka qilganlari emas. Aks holda tanlov xatosi (selection bias) paydo bo'ladi.
* Kichik namunada xulosa chiqarilmaydi (yuqoridagi minimal sonlar). Segment statistikasi umumiy o'rtachaga qarab "yumshatiladi" (shrinkage).
* 3–5 ta ketma-ket mag'lubiyat model xatosi degani emas, bu oddiy tebranish. Asosiy ko'rsatkich: **CLV va kalibratsiya**, qisqa muddatli Win Rate emas.
* Har bir o'zgarish (yangi kalibratsiya, o'chirilgan segment) jurnalga yoziladi va `/ozgarishlar` buyrug'i bilan ko'rinadi. Natija yomonlashsa, oldingi versiyaga qaytarish mumkin.
* Haftalik hisobotda **"model nimani o'rgandi"** bo'limi chiqadi: kalibratsiya o'zgarishi, o'chirilgan yoki qayta yoqilgan segmentlar, yangi saboqlar.

### 6.14. Keyingi bosqich g'oyalari
* Live (o'yin davomida) tahlil. API so'rovlari juda ko'p ketadi, shuning uchun pullik tarifda.
* Ovozli yoki matnli savol: *"Bugun Real Madrid yutadimi?"*. Gemini savolni tushunib, tegishli tahlil funksiyasini chaqiradi (function calling).
* Kunlik xulosani rasm (infografika) shaklida yuborish.

---

## 7. Ma'lumotlar Bazasi (minimal sxema)

| Jadval | Asosiy maydonlar |
|---|---|
| `fixtures` | id, sport, league_id, season, date, home_id, away_id, home_score, away_score, home_xg, away_xg, status |
| `odds_snapshots` | fixture_id, bookmaker, market, selection, odds, taken_at (ertalab / qayta hisob / pre-match / closing) |
| `signals` | id, user_id, fixture_id, market, selection, odds, p_model, ev, confidence, stake_units, llm_verdict, llm_note, source (kunlik / maxsus / sevimli), status (active / cancelled / hidden), created_at |
| `signal_versions` | signal_id, version, odds, p_model, ev, reason (qayta hisob / pre-match), created_at |
| `results` | signal_id, outcome (win / loss / void / cashout), source (api / manual), profit_units, closing_odds, clv, user_note |
| `postmortems` | signal_id, category (omadsizlik / ma'lumot yetishmadi / model xatosi), reason, tags (jamoa, liga, bozor), created_at |
| `lessons` | id, text, tags, confirmations, status (kutilmoqda / faol / eskirgan), first_seen, last_seen |
| `segment_stats` | sport, league_id, market, odds_bucket, n, roi, clv, calib_bias, ev_threshold, enabled, updated_at |
| `calibration_versions` | version, method, params, n_samples, brier_before, brier_after, active, created_at |
| `users` | user_id, is_admin, timezone, bank, unit, created_at |
| `user_settings` | user_id, sports, leagues, markets, max_signals, min_prob, min_ev, odds_min, odds_max, risk_profile, send_time, prematch_min, quiet_hours |
| `watchlist` | user_id, type (team / league), sport, entity_id, notify_flags |
| `user_bets` | id, user_id, signal_id, odds_taken, stake, outcome, profit, placed_at |
| `saved_queries` | id, user_id, name, date_mode (bugun / ertaga / dam olish / N kun / oraliq), date_from, date_to, sports, min_prob, min_ev, scope (all / leagues / teams / fixtures), scope_ids, markets, max_signals, sort, schedule_cron |
| `scheduled_recalcs` | signal_id, run_at, kind (24 soat / pre-match), done |
| `api_usage` | date, provider, sport, requests_used, reserved_manual |

---

## 8. Loyiha Tuzilmasi

```
ai-tahlilchi/
├── src/app/
│   ├── config.py          # .env, standart sozlamalar, risk profillari
│   ├── clients/           # api_sports.py, odds_api.py, gemini.py
│   ├── sports/            # base.py (SportAdapter), football.py, hockey.py, basketball.py
│   ├── models/            # poisson.py, normal_points.py, elo.py, devig.py, staking.py, confidence.py
│   ├── pipeline/          # refresh.py, daily.py, custom.py, recalc.py, prematch.py, settle.py
│   ├── db/                # schema.py, repo.py
│   └── bot/
│       ├── handlers/      # menu.py, daily.py, custom.py, recalc.py, settings.py, watchlist.py, bets.py, report.py
│       ├── keyboards.py   # inline va reply klaviaturalar
│       ├── formatters.py  # signal kartasi, batafsil karta, hisobot
│       └── middleware.py  # whitelist, rate limit
├── tests/                 # model, EV va formatlash uchun unit testlar
├── scripts/backtest.py    # o'tgan mavsumlar ustida modelni sinash
├── Dockerfile
└── .env.example
```

---

## 9. API So'rovlari Byudjeti (bitta sport, kunlik)

| Vazifa | So'rov |
|---|---|
| Liga mavsum ma'lumotlarini yangilash (10 liga) | 10 |
| Bugungi o'yinlar | 1 |
| Koeffitsientlar (liga bo'yicha, sahifalash) | 10–15 |
| Jarohatlar (faqat nomzodlar) | 3–5 |
| Pre-match tarkiblar (faqat signallar) | 0–10 |
| Kechagi natijalar | 1–2 |
| **Avtomatik jami** | **≈ 25–43** |
| Qo'lda: qayta hisoblash, maxsus tahlil va sana oralig'i | ≈ 30 gacha (zaxira) |
| **Umumiy** | **≈ 55–73** (limit: 100) |

Limitlar `api_usage` jadvali va so'rovlar orasidagi kechikish (rate limiter) orqali nazorat qilinadi. Bir xil ma'lumot 10 daqiqa ichida qayta so'ralmaydi (kesh).

**Sana oralig'ini tejamkor yuklash:**
* O'yinlar ro'yxati: `fixtures?league=..&season=..&from=..&to=..`. Oraliq uzunligidan qat'i nazar, liga bo'yicha 1 so'rov (ko'pincha 0–3 bosqichdagi mavsum keshidan olinadi, API sarfi yo'q).
* Jamoalar bo'yicha qamrov: `fixtures?team=..&from=..&to=..`, ya'ni jamoa bo'yicha 1 so'rov.
* Koeffitsientlar faqat **filtrdan o'tgan** o'yinlar uchun so'raladi va kelajakdagi kunlar uchun 6 soat keshlanadi.
* Jarohat va tarkiblar faqat 0–3 kun ichidagi o'yinlar uchun olinadi.
* Tahlil boshlanishidan oldin **taxminiy API sarfi** ko'rsatiladi. Limit yetmasa, bot oraliqni qisqartirish yoki qamrovni toraytirishni taklif qiladi.

---

## 10. Xarajatlar

* **Gemini API:** kuniga 1–2 kichik so'rov (≈ 3–5 ming token). Bepul tarif (Free Tier) yetarli. **$0**. *Eslatma: bepul tarifda Google ma'lumotlardan model yaxshilash uchun foydalanishi mumkin.*
* **API-Football:** bepul tarif 100 so'rov/kun. ⚠️ **Muhim:** bepul tarifda **joriy mavsumga kirish cheklangan bo'lishi mumkin** (faqat o'tgan mavsumlar). Ishni boshlashdan oldin tekshiring. Joriy mavsum ishlamasa, backtest uchun bepul tarifdan, real ish uchun pullik tarifdan (≈ $19/oy) foydalaniladi.
* **The Odds API (ixtiyoriy):** bepul tarif ≈ 500 so'rov/oy. Kuniga 10–15 so'rov bilan yetadi.
* **Qo'shimcha sportlar:** har bir sport API si uchun bepul tarif bilan sinash mumkin. Real ish uchun har biriga alohida pullik tarif kerak bo'lishi mumkin.
* **VPS:** ≈ $4–6/oy (Hetzner va shunga o'xshash), 2 GB RAM. SQLite va bitta konteyner uchun eng kichik server yetarli.
* **Jami:** $4–6/oy (bepul API lar bilan), ≈ $25/oy (futbol, pullik ma'lumot bilan), har bir qo'shimcha sport uchun yana ≈ $10–20/oy.

---

## 11. Bosqichma-bosqich Ishga Tushirish (Roadmap)

1. **1-hafta: Ma'lumotlar qatlami.** API klientlar, `SportAdapter` interfeysi (futbol), kesh, SQLite sxema, so'rov hisoblagichi.
2. **2-hafta: Model + Backtest.** Poisson va de-vig moduli, ishonch darajasi, o'tgan 1–2 mavsum ustida backtest (ROI, Brier, kalibratsiya). **Backtestda musbat natija chiqmasa, keyingi bosqichga o'tilmaydi.** Avval model yaxshilanadi.
3. **3-hafta: Bot yadrosi.** Asosiy menyu, 📊 Tahlil oqimi (sana oralig'i va inline kalendar, sport, ehtimollik, qamrov, son, bozor), `/tahlil` tezkor buyrug'i, signal kartasi, ⚙️ Sozlamalar va risk profillari, whitelist.
4. **4-hafta: Interaktiv funksiyalar.** 🔍 Maxsus tahlil (jamoa qidiruvi, checkbox ro'yxat), 🔄 Qayta hisoblash (farq ko'rsatish, limit himoyasi), 📋 Batafsil karta, Gemini integratsiyasi.
5. **5-hafta: Kuzatuv va hisob.** ⭐ Sevimlilar, 💾 shablonlar va jadvalli obuna, avtomatik qayta hisoblash (24 soat va pre-match), retro-tahlil, 🎟 Mening stavkalarim, ✅ natijalarni belgilash (avtomatik va qo'lda), 📈 Hisobot va kalibratsiya jadvali.
6. **Paper trading davomida: Feedback loop.** Post-mortem va saboqlar darhol yig'ila boshlaydi. Segment filtri ≈ 50 signaldan, kalibratsiya ≈ 200 signaldan keyin yoqiladi.
7. **6–9-hafta: Paper trading.** Signallar real pul tikmasdan kuzatiladi, kamida 150–200 ta signal to'planadi, CLV va ROI tahlil qilinadi.
8. **Keyin:** xokkey va basketbol adapterlari (har biri alohida backtest bilan), ekspress tuzuvchi, CLV barqaror musbat bo'lsa minimal stavkalar bilan real foydalanish.

---

## 12. Xulosa va Ogohlantirishlar

* **Bot-detection xavfi deyarli yo'q**, chunki stavka qo'lda qilinadi. Lekin bukmekerlar doimiy yutayotgan akkauntlarga **stavka limiti** qo'yishi mumkin. Bu xavf saqlanib qoladi.
* **Huquqiy jihat:** O'z mamlakatingizda litsenziyaga ega bo'lmagan bukmekerlardan foydalanish qonunga zid bo'lishi mumkin. Platformani tanlashda buni tekshiring.
* **Bukmeker marjasi (5–8%)** sababli ko'pchilik modellar uzoq muddatda zarar ko'radi. Shuning uchun backtest va paper trading bosqichlari majburiy.
* **Bank boshqaruvi:** faqat yo'qotishga tayyor bo'lgan mablag', bitta stavka bankning 1–2% idan oshmasin, yo'qotishni "qaytarib olish" uchun stavkani oshirish mumkin emas.
* Loyiha muhandislik malakasini (API integratsiyasi, statistik modellashtirish, LLM structured output, scheduling, backtesting) oshirish uchun juda yaxshi pet-project. Uning eng katta qiymati shu.
* AI signallari faqat tavsiya xarakteriga ega. Kafolatlangan foyda yo'q.
