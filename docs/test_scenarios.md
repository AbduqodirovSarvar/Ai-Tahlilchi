# Qo'lda sinov ssenariylari (Telegram bot)

> Jonli Telegram'da bajariladi (token qo'shilgandan keyin). Hozir `APP_SERVICES=fake`: ma'lumot namunaviy (APL, La Liga, Seriya A, Bundesliga, KHL 🧪), API so'rovi yo'q.
> Har bir ssenariyda: kutilgan natija ✅, xato bo'lsa skrinshot va vaqt bilan `docs/qa/` ga yoziladi.
> Umumiy talablar har qadamda: har bir tugma ≤ 1 soniyada javob beradi (soat belgisi aylanib qolmaydi), har bir wizard qadamida `⬅️ Orqaga` va `❌ Bekor qilish` bor, matnlar o'zbek (lotin) tilida.

## 0. Kirish cheklovi (F6.1, decisions #4)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 0.1 | Whitelist'da bo'lmagan akkauntdan `/start` | Hech qanday javob yo'q |
| 0.2 | Admin botni guruhga qo'shib `/start` yozadi | Hech qanday javob yo'q |
| 0.3 | Admin shaxsiy chatda `/start` | Xush kelibsiz matni + 9 tugmali asosiy menyu (§6.1 tartibida) |
| 0.4 | Tugmalarni juda tez (≈ 10 ta/soniya) bosish | Bir marta "⏳ Juda tez!" xabari; keyin bot yana javob beradi |

## 1. Menyu, `/yordam`, `/bekor` (F6.2)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 1.1 | `/yordam` | Buyruqlar ro'yxati (`/qayta`, `/holat` bilan) va EV, CLV, unit, ishonch, ⏳, ❗ izohlari |
| 1.2 | `🔍 Maxsus tahlil`, `💾 Shablonlarim`, `⭐ Sevimlilar`, `📈 Hisobot` (F7) | Har biri: "🔜 … tez orada qo'shiladi" |
| 1.3 | Oddiy matn "salom" | "🤔 Buni tushunmadim…" |
| 1.4 | `/bekor` (hech narsa ochiq emas) | "Bekor qilinadigan amal yo'q" |
| 1.5 | 📊 Tahlil → `/bekor` | "❌ Bekor qilindi", wizard xabarining tugmalari yo'qoladi |

## 2. ⚙️ Sozlamalar (F6.3)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 2.1 | `⚙️ Sozlamalar` | 16 qatorli ko'rinish (standart: Futbol, 1X2+Total, 10, 45%, 5%, 1.50–3.00, Balanslangan, 09:00, 60 daq., 00:00–08:00, Asia/Tashkent, bank kiritilmagan) |
| 2.2 | 🏅 Sportlar → Xokkey 🧪 ni belgilash | "✅ Saqlandi", belgi qo'yiladi |
| 2.3 | Yagona sportni olib tashlash | Alert: "Kamida bitta sport turini tanlang", o'zgarmaydi |
| 2.4 | 🏆 Ligalar → Premier League | Belgilanadi; ⬅️ Sozlamalarga → "Ligalar: Premier League" |
| 2.5 | 📊 Bozorlar → BTTS qo'shish | Saqlanadi; xokkey yolg'iz tanlansa faqat 1X2 va Total ko'rinadi |
| 2.6 | 🎯 Ehtimollik → 90% | Saqlanadi, umumiy ko'rinish ustida §6.6 ogohlantirishi (bir marta) |
| 2.7 | 🎯 Ehtimollik → ✏️ → `150` | "30–95% oralig'ida bo'lishi kerak", qayta so'raladi |
| 2.8 | 💹 EV → ✏️ → `4,5` | Saqlanadi: 4% (4.5) |
| 2.9 | 📈 Koeffitsient → ✏️ → `3-1.5` | Format xatosi; `1.40-3.00` → saqlanadi, risk profili "Moslashtirilgan" |
| 2.10 | 🛡 Risk profili → Konservativ | 60% / 3% / 1.30–2.00 qo'llanadi |
| 2.11 | ⏰ Yuborish vaqti → ✏️ → `25:00`, keyin `08:30` | Avval xato, keyin saqlanadi |
| 2.12 | ⏱ Pre-match → o'chiq | "o'chiq" |
| 2.13 | 🌙 Tinch soatlar → ✏️ `23:00-07:00`; keyin 🚫 O'chirish | Saqlanadi; keyin "o'chiq" |
| 2.14 | 🌍 Vaqt mintaqasi → ✏️ `Mars/Olympus`, keyin `Europe/Moscow` | Xato; keyin saqlanadi, karta vaqtlari Moskva bo'yicha |
| 2.15 | 💰 Bank `1 000 000`, 🎲 Unit `2000000` | "Unit bank summasidan oshmasligi kerak" |
| 2.16 | 🎲 Unit `10000`, 🛑 Kunlik limit `50000`, keyin 🚫 | Saqlanadi; keyin "kiritilmagan" |
| 2.17 | ✏️ kiritish kutilayotganda `📊 Tahlil` bosiladi | Wizard ochiladi (menyu matni qiymat sifatida olinmaydi) |

## 3. 📊 Tahlil wizard (F6.4)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 3.1 | 📊 Tahlil | "1/7 · Sana oralig'i": Bugun, Ertaga, Dam olish kunlari, 3 kun, 7 kun, 📅 Oraliq tanlash |
| 3.2 | Bugun → ⚽ Futbol → 60% → 5% → 🌐 Hamma o'yinlar → 10 → ✅ Tayyor → 💹 EV | Tasdiqlash: "📋 So'rov: 21.10 · ⚽ Futbol · ≥ 60% · EV ≥ 5%", topilgan o'yinlar, API sarfi, [▶️ Boshlash] [✏️ O'zgartirish] [💾 …] |
| 3.3 | ▶️ Boshlash | Wizard xabari "▶️ Tahlil boshlandi", alohida "⏳ Tahlil qilinmoqda… n/m" xabari yangilanadi va "✅ Tahlil tugadi" bo'ladi; natijalar sarlavhasi, kunlar bo'yicha guruhlar, kartalar |
| 3.4 | ▶️ Boshlash ni tez ikki marta bosish | Ikkinchisiga "⏳ allaqachon bajarilmoqda" yoki "eskirgan" alerti; tahlil bir marta |
| 3.5 | 📅 Oraliq tanlash → oy ▶️/◀️ → boshlanish → tugash | Bugundan +13 kundan keyingi kunlar "·" (bosilmaydi); boshlanishdan oldingi kunlar ham; tanlangan kun [25] ko'rinishida |
| 3.6 | Sana qadamida `25.10 - 31.10`, `25.10.2026-31.10.2026`, `22.10` yozish | Qabul qilinadi |
| 3.7 | `31.02`, `salom`, `01.12-05.12` yozish | Mos ravishda: "bunday sana yo'q", "tushunmadim", "juda uzoqda" |
| 3.8 | O'tgan oraliq (masalan, kecha) | Tasdiqlashda "🕰 retro-tahlil"; kartalarda "🕰 Retro" va ✅/❌ natija, ✅ Stavka/🔄 tugmalari yo'q |
| 3.9 | 🎯 qadamida ✏️ → `150%`, `ko'p`, `65` | Xato, xato, keyin EV qadamiga o'tadi |
| 3.10 | 80% yoki 90% tanlash (birinchi marta) | EV qadami ustida §6.6 ogohlantirishi; keyingi safar chiqmaydi |
| 3.11 | ⏭ Standart (ehtimollik qadamida) | Sozlamalardagi qiymat olinadi |
| 3.12 | 🏆 Ligalar → hech narsa tanlamay ✅ Tayyor | Alert "Kamida bittasini tanlang"; liga tanlab ✅ → keyingi qadam |
| 3.13 | 👕 Jamoalar → `Arsenal, Real, Qwertyzz` | Arsenal qo'shiladi, Real uchun ➕ variantlar, "Topilmadi: «Qwertyzz»" |
| 3.14 | ⚽ Aniq o'yinlar (7 kun) | Kunlar sarlavhasi ("📅 Chorshanba, 21.10"), ◀️ 1/4 ▶️ sahifalash, belgilash saqlanadi |
| 3.15 | 🔢 qadamida 📆 Kuniga ni yoqish, ✏️ `0` keyin `12` | Belgi o'zgaradi; xato; keyin bozorlar qadami |
| 3.16 | Bozorlardan hammasini olib tashlab ✅ | Alert "Kamida bittasini tanlang" |
| 3.17 | Tasdiqlashda ✏️ O'zgartirish → ↕️ Saralash → Ehtimollik | To'g'ridan-to'g'ri tasdiqlash ekraniga qaytadi |
| 3.18 | 💾 Shablon sifatida saqlash → nom | "💾 Shablon saqlandi", tasdiqlash ekrani qayta chiqadi |
| 3.19 | Har bir qadamda ⬅️ Orqaga | Oldingi qadam (birinchi qadamda — bekor qilish) |
| 3.20 | Wizard o'rtasida botni qayta ishga tushirish va eski tugmani bosish | Alert "⌛ Bu tugma eskirgan, qaytadan boshlang" |
| 3.21 | Ikkinchi 📊 Tahlil ochib, birinchi xabardagi tugmani bosish | "eskirgan" alerti |
| 3.22 | Tugmali qadamda matn yozish | "👆 Iltimos, tugmalardan birini tanlang" |
| 3.23 | Byudjetdan oshadigan so'rov (real servisda) | Tasdiqlashda "⚠️ API limitidan oshadi", ▶️ Boshlash yo'q |
| 3.24 | Tahlil davomida (progress "⏳ …") boshqa tugmalar: ⚙️ Sozlamalar, eski natijadagi 🙈, "➡️ Yana" | Hammasi javob beradi (tahlil fon vazifasi); wizard tugmalari "⏳ allaqachon bajarilmoqda" oladi |
| 3.25 | Tahlil davomida 📊 Tahlil bilan yangi wizard boshlash, so'ng eski tahlil tugashi (yoki xato berishi) | Yangi wizard o'z holatida qoladi; eski natija/xato xabari alohida yuboriladi |
| 3.26 | Tahlil kutilmagan xato bilan tugasa (servis ishlamayapti) | Progress xabari yopiladi, "⚠️ Kutilmagan xatolik", tasdiqlash ekrani qaytadi, eski kartalardagi tugmalar ishlayveradi |

## 4. `/tahlil` tezkor buyrug'i (F6.5)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 4.1 | `/tahlil 25.10-31.10 futbol 60% Arsenal,Inter` | Tasdiqlash ekrani: shu oraliq, ≥ 60%, Qamrov: Arsenal, Inter |
| 4.2 | `/tahlil Arsenal,Inter 60% futbol 25.10-31.10` (boshqa tartib) | 4.1 bilan bir xil |
| 4.3 | `/tahlil ertaga` | Qolgan parametrlar sozlamalardan (45%, EV 5%) |
| 4.4 | `/tahlil` | Wizard 1-qadami |
| 4.5 | `/tahlil 60` | "bu foizmi yoki sonmi? 60% yoki 60ta" + namuna |
| 4.6 | `/tahlil bugun ertaga`, `/tahlil 60% 70%` | "ikki marta berilgan" |
| 4.7 | `/tahlil Real` | "«Real» noaniq: Real Madrid, Real Sociedad, …" |
| 4.8 | `/tahlil 150%`, `/tahlil ev30%`, `/tahlil 31.02` | Aniq xato + namuna |
| 4.9 | `/tahlil xokkey 3kun ev8% 5ta` | Xokkey 🧪, 3 kun, EV ≥ 8%, soni 5 |

## 5. Natijalar va karta (F6.6)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 5.1 | 3.3 dagi kartalar | §6.2 tartibi: sarlavha (vaqt foydalanuvchi mintaqasida), Tanlov @ koeff., Ehtimollik (adolatli koeff.), EV + Ishonch, Tavsiya, 📝 izoh; 3 kundan uzoq — "⏳ Dastlabki tahlil"; EV manfiy — "❗ Qiymatsiz" |
| 5.2 | Kunlar sarlavhasi | "📅 Shanba, 24.10 — 2 ta signal" (o'zbekcha hafta kuni) |
| 5.3 | 10+ signal | 5 tadan sahifa, "Ko'rsatildi: 5/10" + [➡️ Yana 5 ta]; oxirida "✅ Hammasi ko'rsatildi"; davomli kun "(davomi)" |
| 5.4 | Bot restartidan keyin [➡️ Yana] | "eskirgan" alerti |
| 5.5 | Signal soni so'ralgandan kam | "Shartlarga 3 ta o'yin mos keldi (siz 10 ta so'ragansiz)" |
| 5.6 | Juda qattiq shartlar (95%, EV 20%) | "🤷 Value topilmadi" va nima qilish bo'yicha maslahat |
| 5.7 | ⭐ sevimli jamoa o'yini (F7 dan keyin) | Alohida "⭐ Sevimlilar" bloki, son hisobiga kirmaydi |
| 5.8 | 🙈 Yashirish | Alert "🙈 Signal yashirildi", karta "🙈 Yashirildi: …" ga aylanadi |
| 5.9 | 📋 Batafsil (F7.2) | Alert "🔜 Bu imkoniyat tez orada qo'shiladi" (🔄 va ✅ endi ishlaydi: 7 va 9-bo'limlar) |
| 5.10 | Eski natijadagi [➡️ Yana] ni yangi tahlildan keyin bosish; [➡️ Yana] ni tez ikki marta bosish | Birinchisi "⌛ eskirgan" alerti; ikkinchisida sahifa bir marta yuboriladi |

## 6. Telegram cheklovlari (F6.10)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 6.1 | Juda uzun matn chiqaradigan holat (masalan, ko'p ligali sozlamalar) | Xabar ≤ 4096 belgili qismlarga bo'linadi, HTML buzilmaydi |
| 6.2 | Jamoa nomida `<`, `&` (masalan, `/tahlil A&B <X>`) | Matn to'g'ri ko'rinadi, HTML xatosi yo'q |
| 6.3 | Logda `telegram_flood_control` (ko'p xabar yuborilganda) | Bot kutib, bir marta qayta yuboradi; foydalanuvchi xatoni ko'rmaydi |

## 7. 🔄 Qayta hisoblash (F6.7)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 7.1 | Kartadagi [🔄 Qayta hisoblash] | Darhol toast "⏳ Qayta hisoblanmoqda…"; karta yangi raqamlar bilan tahrirlanadi; ostida javob sifatida farq xabari: `🔄 Arsenal — Chelsea yangilandi (14:32)` / `Koeff.: 1.85 → 1.72   EV: +12.9% → +4.9%` / `Holat: ⚠️ Chegaradan pastga tushdi, tavsiya etilmaydi` (o'zgargan ehtimollik, tavsiya va ishonch alohida satrda) |
| 7.2 | Shu tugmani 10 daqiqa ichida qayta bosish | "⏳ N daqiqadan keyin qayta urinib ko'ring." (N yuqoriga yaxlitlanadi) |
| 7.3 | Natijalar xabari ostidagi [🔄 Hammasini yangilash] | "⏳ Qayta hisoblanmoqda…" progress xabari tahrirlanadi, so'ng "✅ Tayyor." va xulosa: yangilandi / o'zgargan soni, o'zgarganlar ro'yxati (≤ 10), cooldown dagilar, boshlangan o'yinlar, xatolar |
| 7.4 | 7.3 ni darhol takrorlash | "yangilandi 0 ta" va "N ta signal yaqinda yangilangan (bitta o'yin 10 daqiqada bir marta)" |
| 7.5 | `/qayta`, `/qayta ertaga`, `/qayta 3kun`, `/qayta 25.10-27.10` | Oyna signallari qayta hisoblanadi; signal yo'q bo'lsa "Qayta hisoblanadigan signal topilmadi" |
| 7.6 | `/qayta Arsenal`, `/qayta 31.02`, `/qayta 60%` | "❗ …" + namuna (`/qayta`, `/qayta ertaga`, …); hech narsa hisoblanmaydi |
| 7.7 | Tahlil davom etayotganda ▶️ ni ikkinchi marta bosish / ikkinchi qurilmadan tahlil | "⏳ Tahlil allaqachon bajarilmoqda…" (BusyError), API sarfi ikki marta emas |
| 7.8 | API ishlamayotganda 🔄 yoki tahlil | "🔌 Ma'lumot manbai hozir javob bermayapti (…)…"; holat saqlanadi, tugmalar ishlayveradi |
| 7.9 | API limiti tugagach 🔄 | Raqamlar o'zgarmaydi, "📦 Yangi ma'lumot olinmadi: raqamlar 09:00 holatiga." |
| 7.10 | Bot restartidan keyin eski [🔄 Hammasini yangilash] (so'rov yo'qolgan) | "🔎 Topilmadi…" xabari (xato emas) |

## 8. ⚡ Bugungi signallar va `/holat` (F6.8, F6.9)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 8.1 | `⚡ Bugungi signallar` (yoki `/bugun`), signal yo'q | "🤷 Bugun faol signal yo'q…" va nima qilish bo'yicha maslahat |
| 8.2 | Tahlildan keyin shu tugma | "⚡ Bugungi signallar — N ta", kartalar (tugmalari bilan), [🔄 Hammasini yangilash]; yashirilganlar ko'rinmaydi |
| 8.3 | 6+ signalda [➡️ Yana] | Keyingi sahifa; bitta ommaviy so'rov (`get_signals`), yo'qolgan signal jim o'tkazib yuboriladi |
| 8.4 | `/holat` yoki `ℹ️ Holat / Yordam` | Rejim (🧪 namunaviy / 🟢 haqiqiy), API limitlari (ishlatilgan/limit, qoldi, qo'lda), oxirgi yangilanish vaqti va "N soat oldin", oxirgi ishlar ✅/❌, oxirgi xatolar (≤ 5) yoki "yo'q ✅"; oxirida /yordam havolasi |
| 8.5 | Xato matnida `<`, `&` (masalan, `<timeout>`) | `/holat` da to'g'ri ko'rinadi (escape qilingan) |

## 9. ✅ Stavka qildim va 🎟 Mening stavkalarim
| # | Qadam | Kutilgan natija |
|---|---|---|
| 9.1 | Kartadagi [✅ Stavka qildim] (unit sozlangan) | Yangi xabar: signal koeffitsienti va tavsiya (summa + unit); [✅ Yozish: 1.85] [✏️ Boshqa koeff./summa] [❌ Bekor qilish] |
| 9.2 | [✅ Yozish] | Xabar "🎟 Stavka yozildi" ga aylanadi, tugmalar yo'qoladi; zarar limiti bo'lsa "🛑 Diqqat…" qo'shiladi |
| 9.3 | [✅ Yozish] ni tez ikki marta bosish | Bitta stavka (🎟 ro'yxatida 1 ta) |
| 9.4 | [✏️ Boshqa…] → `1.95 50000` | Stavka 1.95 va 50 000 bilan yoziladi; summa yozilmasa — tavsiya etilgan summa |
| 9.5 | Noto'g'ri qiymat (`abc`, `0.5`) | Format ko'rsatiladi, qadam ochiq qoladi; `/bekor` yoki ❌ — chiqish |
| 9.6 | Unit sozlanmagan yoki tavsiya "stavka qilinmaydi" | [✅ Yozish] yo'q; summani o'zi kiritishi kerak ("💵 Summani o'zingiz kiriting…") |
| 9.7 | Boshqa oqim (wizard) ochiqligida [✏️ Boshqa…] | Alert "Avval joriy amalni tugating yoki /bekor" |
| 9.8 | `🎟 Mening stavkalarim` | Balans (boshlang'ich bank + yopilgan stavkalar foydasi), oy foydasi, ochiq stavkalar soni ("balansni kamaytirmaydi"), ochiq stavkalar ro'yxati; bo'sh holat: "Ochiq stavkalar yo'q…" |

## 10. Avtomatik xabarlar (Notifier, `APP_SERVICES=real`)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 10.1 | Sozlangan vaqtda kunlik xabar | Sarlavha ("☀️ Kunlik tahlil — Payshanba, 22.10"), "📆 Kecha: 6 ta signal · 4 ✅ · 2 ❌ · +2.3 unit", natijalar xulosasi ([🔄 Hammasini yangilash]), kunlar bo'yicha kartalar (⭐ blok oxirida); signal yo'q bo'lsa "Value topilmadi" |
| 10.2 | Tinch soatlarda kunlik xabar | Tinch soat tugagach yuboriladi; 24 soat/pre-match xabarlari tashlanadi (pipeline qo'llaydi) |
| 10.3 | 24 soat / pre-match yangilanishi | Kunlik xabarga javob (reply) sifatida farq xabari (⏰ / 🔔), kartadagi tugmalar bilan |
| 10.4 | O'yin qoldirildi/bekor qilindi | "❌ Bekor: …" sabab bilan, javob sifatida |
| 10.5 | Natija yopildi | "✅ O'tdi — …" / "❌ O'tmadi — …" va foyda unit da |
| 10.6 | Foydalanuvchi botni bloklagan | Xabar yuborilmaydi, pipeline yiqilmaydi (logda `notify_user_blocked_bot`) |
| 10.7 | Tizim xatosi (API, Gemini) | Har bir admin'ga "🚨 Tizim xatosi · komponent · vaqt" |
