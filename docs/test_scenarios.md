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
| 8.4 | `/holat` yoki `ℹ️ Holat / Yordam` | Rejim (🧪 namunaviy / 🟢 haqiqiy), API limitlari (ishlatilgan/limit, qoldi, qo'lda so'rovlar ishlatilgan/zaxira va qo'lda qoldi), AI (Gemini) so'rov va token sarfi, oxirgi yangilanish vaqti va "N soat oldin", oxirgi ishlar ✅/❌, oxirgi xatolar (≤ 5) yoki "yo'q ✅"; oxirida /yordam havolasi |
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
| 9.8 | `🎟 Mening stavkalarim` (to'liq UI: 15-bo'lim) | Balans (boshlang'ich bank + yopilgan stavkalar foydasi), oy foydasi, ochiq stavkalar soni ("balansni kamaytirmaydi"), ochiq stavkalar ro'yxati; bo'sh holat: "Ochiq stavkalar yo'q…" |

## 10. Avtomatik xabarlar (Notifier, `APP_SERVICES=real`)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 10.1 | Sozlangan vaqtda kunlik xabar | Sarlavha ("☀️ Kunlik tahlil — Payshanba, 22.10"), "📆 Kecha: 6 ta signal · 4 ✅ · 2 ❌ · +2.3 unit", natijalar xulosasi ([🔄 Hammasini yangilash]), kunlar bo'yicha kartalar (⭐ blok oxirida); signal yo'q bo'lsa "Value topilmadi"; bukmeker narxi yo'q o'yinlar ("N ta o'yin: 1xBet narxi yo'q"), eskirgan narx va "Ma'lumot yangilanmadi — eski ma'lumot" ogohlantirishi; qayta hisoblangan kartada "📌 Tavsiya koeff.: 1.85 → hozir 1.72" |
| 10.2 | Tinch soatlarda kunlik xabar | Tinch soat tugagach yuboriladi; 24 soat/pre-match xabarlari tashlanadi (pipeline qo'llaydi) |
| 10.3 | 24 soat / pre-match yangilanishi | Kunlik xabarga javob (reply) sifatida farq xabari (⏰ / 🔔), kartadagi tugmalar bilan |
| 10.4 | O'yin qoldirildi/bekor qilindi | "❌ Bekor: …" sabab bilan, javob sifatida |
| 10.5 | Natija yopildi | "✅ O'tdi — …" / "❌ O'tmadi — …" va foyda unit da |
| 10.6 | Foydalanuvchi botni bloklagan | Xabar yuborilmaydi, pipeline yiqilmaydi (logda `notify_user_blocked_bot`); 19-bo'lim |
| 10.7 | Tizim xatosi (API, Gemini) | Har bir admin'ga "🚨 Tizim xatosi · komponent · vaqt" |


## 11. 🔍 Maxsus tahlil (F7.1)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 11.1 | `🔍 Maxsus tahlil` | Taklif matni, [📅 Bugungi o'yinlar] [📆 Ertangi o'yinlar] [❌ Bekor qilish]; "EV filtrisiz" izohi |
| 11.2 | `Real` yozish | "Real Madrid, Real Sociedad, Real Betis…" tugmalari (jamoa va liga); lotin/kirill va xato yozilgan nom (`arsenl`) ham topiladi |
| 11.3 | Jamoani tanlash | Shu jamoaning yaqin ≤ 3 o'yini ✅ checkbox bilan, eng yaqini oldindan belgilangan; [▶️ Tahlil qilish (N)] |
| 11.4 | `/tahlil Arsenal - Chelsea` yoki oddiy matn `Arsenal Chelsea` | Topilgan o'yin(lar) ro'yxati (belgilanmagan); `/tahlil 25.10 - 31.10` esa oddiy tahlil tasdiqlash ekraniga o'tadi |
| 11.5 | ▶️ Tahlil qilish | Progress xabari, keyin: sarlavha, har o'yin uchun **barcha bozorlar** ehtimolliklari (✅ chegaradan o'tdi / ▫️ / ❗ EV manfiy), o'tgan tanlovlar uchun to'liq karta; hech biri o'tmasa "Value topilmadi" |
| 11.6 | Bir martada 10 tadan ko'p o'yin | Alert "ko'pi bilan 10 ta"; hech narsa belgilanmagan holda ▶️ — alert |
| 11.7 | Tahlil davomida yana ▶️ yoki tugma | "⏳ allaqachon bajarilmoqda"; xato bo'lsa (limit, manba) sabab ko'rsatiladi va tanlov ekrani qaytadi |
| 11.8 | Eski xabardagi tugma / restartdan keyin | "⌛ Bu tugma eskirgan" |
| 11.9 | `<b>x&y` kabi matn | Escape qilingan, HTML buzilmaydi |

## 12. 📋 Batafsil karta (F7.2)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 12.1 | Kartadagi [📋 Batafsil] | Karta shu xabarda batafsil kartaga aylanadi: forma `✅✅➖❌✅`, uy/mehmon statistikasi (+xG), so'nggi uchrashuvlar, jarohat/diskvalifikatsiya, eng ehtimolli 3 hisob, barcha bozorlar jadvali (model / adolatli / bukmeker / EV), koeffitsient harakati, "📦 Ma'lumot … holatiga" |
| 12.2 | [⬅️ Kartaga qaytish] | Asl karta joriy raqamlar va tugmalar bilan |
| 12.3 | Tugagan o'yin | "🏁 Hisob: 2:1" tarkib o'rniga |
| 12.4 | Juda uzun nomlar / ko'p bozor | Karta bitta xabarga sig'adi ("…va yana N ta") |

## 13. ⭐ Sevimlilar (F7.3)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 13.1 | `⭐ Sevimlilar` (bo'sh) | Tushuntirish va [➕ Jamoa] [➕ Liga] |
| 13.2 | `/kuzat Barcelona` | Aniq topilsa darhol qo'shiladi va bildirishnoma tugmalari chiqadi; noaniq nomda (`Real`) tanlash ro'yxati; topilmasa xabar; takroran — dublikat yo'q |
| 13.3 | ➕ Jamoa → nom yozish; ➕ Liga → ro'yxatdan | Qo'shiladi, element ekrani ochiladi |
| 13.4 | [📋 Tarkib] [📉 Koeffitsient] [🏁 Natija] | Har biri yoqiladi/o'chadi (✅/▫️), ro'yxatda ko'rinadi |
| 13.5 | [🗑 Olib tashlash]; boshqa joyda o'chirilgan elementning tugmasi | Ro'yxat qaytadi; "Bu element endi sevimlilarda yo'q" |
| 13.6 | Kartadagi [⭐ Kuzatish] | Uy jamoasi, mehmon jamoasi, liga tugmalari (karta id lari bo'yicha, qidiruvsiz); ikkinchi bosish — olib tashlaydi |
| 13.7 | Sevimli jamoa o'yini tahlilda | Karta "⭐ Sevimli" belgisi bilan, har doim kiritiladi |
| 13.8 | Ikkinchi foydalanuvchi | Ro'yxati alohida |

## 14. 💾 Shablonlarim (F7.4)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 14.1 | Wizard tasdiqlash → 💾 → nom | Shablon saqlanadi; `💾 Shablonlarim` ro'yxatida qisqa xulosa (sana · sport · ≥ ehtimollik · EV) |
| 14.2 | Shablonni tanlash | Barcha parametrlar va jadval; [▶️ Ishga tushirish] [🗓 Jadval] [🗑 O'chirish] |
| 14.3 | ▶️ Ishga tushirish | Wizardning tasdiqlash ekrani shablon so'rovi bilan (nisbiy sana yangi davrga o'tadi, API sarfi ko'rinadi); ▶️ Boshlash → natijalar |
| 14.4 | 🗓 Jadval: kunlar, vaqt (7–18 yoki ✏️ `08:30`), 💾 Saqlash | Jadval saqlanadi (ro'yxatda 🗓); kunsiz saqlash — alert; 🚫 — jadval o'chadi. **Eslatma: avtomatik ishga tushirish pipeline'da hali ulanmagan** (matnda aytilgan) |
| 14.5 | 🗑 → tasdiq | O'chadi; "Yo'q" — o'zgarmaydi |
| 14.6 | Boshqa joyda o'chirilgan shablon tugmasi | "Topilmadi" xabari |
| 14.7 | 90 kundan eski oraliqli shablon | "Oraliq juda eski…" xabari |

## 15. 🎟 Mening stavkalarim — to'liq (F7.5)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 15.1 | `🎟 Mening stavkalarim` yoki `/bank` | Balans, unit, oy foydasi, ochiq stavkalar, yopilgan hisobi (✅ ❌ ↩️ 💸), umumiy foyda va yield, limitlar; [🟡 Ochiq] [✅ Yopilgan] [📈 Bank grafigi] [⚙️ Bank va limitlar] |
| 15.2 | ⚙️ Bank va limitlar | Bank, unit, kunlik va haftalik limit muharrirlari (sozlamalar bilan bir xil) |
| 15.3 | 🟡 Ochiq → stavka → [✅ O'tdi] [❌ O'tmadi] [↩️ Qaytarildi] | O'yin boshlangan bo'lsa natija belgilanadi, balans yangilanadi; boshlanmagan bo'lsa alert |
| 15.4 | [💸 Cashout] → to'langan summa (masalan `200`) | Foyda = summa − stavka (20.12 ga qarang); `abc`, `-5`, juda katta son — xato va qadam ochiq; ⬅️ — chiqish |
| 15.5 | ✅ Yopilgan | Natija va foyda bilan oxirgi stavkalar |
| 15.6 | 📈 Bank grafigi | PNG rasm (kun oxiridagi balans, boshlang'ich bank chizig'i); bank yoki yopilgan stavka yo'q — tushuntirish |
| 15.7 | Zarar limitiga yetilgach ✅ Stavka qildim | "🛑 Zarar limitiga yetdingiz…" va [🛑 Baribir davom etaman] / [❌]; faqat tasdiqdan keyin odatiy so'rov |

## 16. 📈 Hisobot va eksport (F7.6)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 16.1 | `📈 Hisobot` yoki `/hisobot` | 7 kunlik hisobot: signallar, ROI, yield, CLV, win rate, eng uzun mag'lubiyat seriyasi, kalibratsiya jadvali va "60–70% deb berilgan signallar haqiqatda 64% holatda o'tdi" jumlasi (n ≥ 10 bo'lsa) |
| 16.2 | [30 kun] [Hammasi] | Davr almashadi, tanlangani ✅ |
| 16.3 | [📅 O'zim] → `01.10 - 21.10` | Oraliq hisoboti; noto'g'ri sana / kelajak sana — xato, qadam ochiq; kesim tugmalari oralig'ni saqlaydi |
| 16.4 | Kesim: sport, liga, bozor, risk profili, ishonch | Kesim jadvali (enumlar o'zbekcha), ≤ 15 qator |
| 16.5 | 📊 Grafik | Kalibratsiya PNG, kesim tanlangan bo'lsa yield PNG; ma'lumot yo'q — tushuntirish |
| 16.6 | `/eksport` yoki 📤 CSV | Ikkita fayl: `signals.csv` va `bets.csv` |
| 16.7 | Retro tahlil signallari | Hisobotga kirmaydi |

## 17. Retro-tahlil (F7.7)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 17.1 | Sana qadamida [🕰 Kecha (retro)] / [🕰 O'tgan hafta (retro)]; `/tahlil kecha`, `/tahlil o'tgan hafta` | Tasdiqlash ekrani "🕰 O'tgan sanalar: retro-tahlil" |
| 17.2 | Natija | "🕰 Retro-tahlil…", "🧮 O'sha paytdagi tanlovlar: ✅ N · ❌ M — o'tish ulushi X%"; kartada "Natija: ✅/❌", 🔄 va ✅ Stavka tugmalari yo'q |
| 17.3 | `/tahlil 01.07-05.07` (> 90 kun), 31 kundan uzun oraliq | "juda eski" / "juda uzun" va qoida (90 / 31 kun) |

## 18. 🧩 Ekspress (F7.8)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 18.1 | `/ekspress` yoki ⚡ Bugungi signallar ostidagi [🧩 Ekspress] | Faol signallar ro'yxati (koeffitsienti bilan); signal < 2 bo'lsa tushuntirish |
| 18.2 | Belgilash | 2–4 tadan [🧮 Hisoblash]; bir o'yindan ikkinchi tanlov — alert "bog'liq"; beshinchi — alert |
| 18.3 | 🧮 Hisoblash | Tanlovlar, umumiy koeffitsient, umumiy ehtimollik (+ adolatli koeff.), EV (manfiy bo'lsa ❗), "3 ta 70% lik tanlovdan … atigi 34%" ogohlantirishi |
| 18.4 | ⬅️ / ❌ | Tanlovga qaytadi / yopiladi |

## 19. Yetkazib bo'lmaydigan foydalanuvchi (shartnoma v1.6)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 19.1 | Foydalanuvchi botni bloklagan, kunlik xabar vaqti | Bir marta urinish (`notify_user_blocked_bot`), pipeline kalitni yakunlaydi: 5 marta qayta urinish ham, adminga kunlik ogohlantirish ham yo'q; 24 soatdan keyin yana urinadi |


## 20. 🏁 Natijalarni belgilash (F8.1)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 20.1 | O'yin boshlangan signal kartasi (batafsil kartadan ⬅️, hisobot xabari) | Kartada `[✅ O'tdi] [❌ O'tmadi]` / `[↩️ Void] [💸 Cashout]` / `[💬 Izoh]`; boshlanmagan o'yinda yo'q; retro, tavsiya qilinmagan va bekor qilingan kartada yo'q |
| 20.2 | [✅ O'tdi] | Karta shu xabarda yangilanadi: "Natija belgilandi" va natija belgisi; signaldagi barcha stavkalar yopiladi; tugmalar qoladi (tuzatish uchun) |
| 20.3 | API natijasi kelgan kartada qarama-qarshi belgi | Qo'lda belgi ustun; keyingi avtomatik hisob-kitob uni o'zgartirmaydi |
| 20.4 | O'yin boshlanmagan signalga tugma (eski xabar) | Alert "O'yin hali boshlanmagan…"; retro signal — "retro-tahlil signali…"; bekor qilingan — "Signal bekor qilingan…" |
| 20.5 | `/natijalar` | Natijasi aniqlanmagan, o'yini tugagan signallar: raqamlangan ro'yxat, har biriga tugmalar; bo'sh bo'lsa "Natijasi kutilayotgan signal yo'q" |
| 20.6 | Ro'yxatda ✅ / ❌ / ↩️ | Signal ro'yxatdan chiqadi (shu sahifa yangilanadi), "Natija belgilandi" |
| 20.7 | 6 tadan ko'p signal | ◀️ n/m ▶️; har bir signalga yetib borish mumkin |
| 20.8 | [💸 Cashout] (signal) → `0.8` | Tavsiya etilgan stavkaga nisbatan unit; `abc` — xato, `500` — "-100 dan 100 gacha", holat ochiq; `/bekor` — so'rov unutiladi, kechikkan raqam hech narsani belgilamaydi |
| 20.9 | [💬 Izoh] → matn | "Izoh saqlandi" (HTML belgilar escape qilingan); bo'sh yoki 500 belgidan uzun — xato, so'rov ochiq; aynan 500 — qabul |
| 20.10 | Tahlil ishlayotganda 💬 yoki 💸 | Alert "Tahlil allaqachon bajarilmoqda"; himoya o'chmaydi |
| 20.11 | Boshqa foydalanuvchining tugmasi (soxta `rs:` callback) | "Topilmadi"; hech narsa o'zgarmaydi, hech qanday holat ochilmaydi |
| 20.12 | 🎟 Stavkalarim → ochiq stavka → [💸 Cashout] | "Bukmeker sizga to'lagan **summani** yozing (stavkangiz: …)". `200` (stavka 250) → foyda **−50**; `0` — to'liq yo'qotish; `-5`, `abc`, 10¹² dan katta — xato; bir signaldagi boshqa stavkalar shu daromad foizi bilan yopiladi |

## 21. Kunlik xabar va qo'lda tahlil banneri (F8.2)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 21.1 | Ertalabki xabar | Sarlavha + "Kecha: 6 ta signal · 4 ✅ · 2 ❌ · +2.3 unit · CLV +3.1%" (kechagi signal bo'lmasa blok yo'q) |
| 21.2 | Jadvalli shablon xabari | Sarlavha `💾 Shablon nomi — …`, "Kecha" bloki yo'q |
| 21.3 | Zarar limiti yetilgan kun | Bitta "🛑 Zarar limitiga yetildi: kunlik/haftalik limit …, zarar …" xabari; bo'sh "0 signal" yo'q |
| 21.4 | Limit yetilganda 📊 Tahlil yoki 🔍 Maxsus tahlil | Tahlil bajariladi, natija boshida "Zarar limitiga yetgansiz…" banneri; retro oynada banner yo'q |
| 21.5 | Kartadagi ⭐ | Jamoa/liga tugmalari karta id lari bilan (katalog qidiruvi yo'q); id yo'q kartada kamroq tugma |

## 22. `/ozgarishlar` va model jurnali (F8.8)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 22.1 | `/ozgarishlar` (jurnal bo'sh) | Rejim satri (⚪ o'chiq / 👁 kuzatuv / ✅ qo'llash) va "Hozircha o'zgarish yo'q" |
| 22.2 | Jurnal bor | Yangi → eski; har yozuv: tur, sana, segment/versiya, namuna, sabab; `👁 kuzatuv rejimi: shunday qilgan bo'lardi` yoki `✅ qo'llangan` yoki `↩️ qaytarilgan` |
| 22.3 | 10 tadan ko'p | ◀️ n/m ▶️; har bir yozuvga yetib borish mumkin |
| 22.4 | Qo'llangan yozuvda [↩️ Qaytarish #N] | Kuzatuv (applied=0) va qaytarilgan yozuvda tugma yo'q |
| 22.5 | [↩️ Qaytarish #N] | Tasdiq ekrani: yozuv + "model N kun qayta o'zgartirmaydi"; [✅ Ha, qaytarish] / [⬅️ Yo'q] (hech narsa o'zgarmaydi) |
| 22.6 | ✅ Ha | "Qaytarildi…"; ro'yxatda `↩️ qaytarilgan`; ikki marta bosish — "allaqachon qaytarilgan" |
| 22.7 | Yozuvdan keyin holat o'zgargan (yangi kalibratsiya/segment) | `STALE`: tushuntirish, hech narsa yozilmaydi; eng yangi o'zgarishni qaytaring |
| 22.8 | Yozuv ro'yxatdan yo'qolgan/boshqa sahifaga o'tgan eski tugma | "Bu tugma eskirgan" |
| 22.9 | Dushanba 09:00 dan keyin | Har bir foydalanuvchiga bitta "🧠 Model nimani o'rgandi — hafta …" (rejim, sanoq, yozuvlar; ≤ 8 ta batafsil, qolgani "…va yana N ta"); `off` da yuborilmaydi; tinch soatda o'tkazib yuboriladi; qayta ishga tushirish dublikat yaratmaydi |
| 22.10 | `/holat` | "🧠 Model o'rganishi": rejim, oxirgi `learning`/`lessons`/`postmortem` ishlari; haftalik ish 8 kundan ko'p ishlamagan bo'lsa "⚠️ … hafta yo'qolgan" |

## 23. T5 bot tuzatishlari (QA F-1..F-10)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 23.1 | Tahlil ishlayotganda bot qayta ishga tushadi | Ishga tushganda "Bot qayta ishga tushdi… tahlilni qaytadan boshlang" xabari; 🔍/▶️ qotib qolmaydi |
| 23.2 | Tahlil ishlayotganda ⭐, `/kuzat`, ⚙️, 💾, 📈, 🧩 ochiladi | "Allaqachon bajarilmoqda" himoyasi saqlanadi; `/bekor` — chiqish yo'li, matnda aytilgan |
| 23.3 | 24 jamoa sevimlida / 25 shablon | Sahifalar (10 tadan); 21-chi va keyingisini ochish, bildirishnomani o'zgartirish, o'chirish; elementdan ⬅️ shu sahifaga qaytaradi; mavjud bo'lmagan sahifa — oxirgisi |
| 23.4 | 105 ta yopilgan stavka | Bank ekranida foyda/yopilgan soni **hammasi** bo'yicha (balans bilan mos); 101-chi ochiq stavka ham ochiladi va yopiladi |
| 23.5 | `/eksport` | Fayllar UTF-8 BOM bilan; `=`/`+`/`-`/`@` bilan boshlangan liga/jamoa nomi oldida `'`; raqamlar o'zgarmagan (`-50`) |
| 23.6 | Liga nomi `$…$` bo'lgan hisobot grafigi | Grafik chiziladi (xato yo'q) |
| 23.7 | Zarari bir sentdan kam | "0" (hech qachon "-0") |
| 23.8 | 4096 belgidan uzun ro'yxat (tugmadan tahrir) | Birinchi qism tahrir, qolgani yangi xabar(lar)da, tugmalar oxirgisida; hech narsa kesilmaydi |
| 23.9 | Ekspress: −10% EV oyoq + 0% EV oyoq | Ekspress EV −10% (0% emas); bir o'yindan ikki tanlov hamon rad etiladi |

## 24. 🗄 Baza (`/baza`) va 🔄 Bazani yangilash (T7-D)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 24.1 | `/baza` yoki menyudagi "🗄 Baza" | Oxirgi yangilanish (mahalliy vaqt + "N soat oldin"), hajm (MB), o'yinlar oralig'i, jadvallar bo'yicha qatorlar, "Vaqtlar Asia/Tashkent bo'yicha"; signallar qatorida "(faqat sizniki)" |
| 24.2 | Baza eskirgan (`data_stale`) | Overview'da "⚠️ Baza eskirgan…" ogohlantirishi |
| 24.3 | Baza bo'sh | "Baza bo'sh. Admin '🔄 Bazani yangilash' ni bosing"; faqat yangilash tugmasi |
| 24.4 | 🏆 Ligalar | Raqamlangan ro'yxat: nomi, davlat, o'yinlar soni, kelgusi, eng erta—eng kech sana; 10 tadan sahifa; raqam tugmasi ligani ochadi |
| 24.5 | Liga → o'yinlar | Kun sarlavhalari, "HH:MM Uy — Mehmon", liga, holat/hisob (✅ tugagan 2:1, ⏳, 🔴), 💱 narxi bor belgisi; ◀️ n/m ▶️ |
| 24.6 | Kun filtri: Bugun / Ertaga / Hammasi | Faqat shu mahalliy kun o'yinlari; faol tugmada ●; bo'sh kun — "Bu filtr bo'yicha o'yin yo'q…" |
| 24.7 | 🗓 Sana | Kalendar faqat bazadagi o'yinlar oralig'ida; kun tanlansa ro'yxat shu kunga; ⬅️ Orqaga ro'yxatga |
| 24.8 | UTC 20:00 da boshlanadigan o'yin (Toshkent) | Ro'yxatda ertasi kun sarlavhasi ostida 01:00; kartada "01:00 (Asia/Tashkent) · UTC 20:00" |
| 24.9 | O'yin raqami | Karta: liga, mahalliy+UTC vaqt, holat, hisob/xG, har bukmeker bozorining oxirgi koeffitsienti va olingan vaqti, "Mening signallarim"; juda ko'p narx bo'lsa "…yana N ta" |
| 24.10 | 💱 Narxlar tarixi | Yangisi birinchi, 15 tadan sahifa; ⬅️ kartaga qaytadi |
| 24.11 | 👥 Jamoalar, raqam | Jamoa o'yinlari ro'yxati; ⬅️ Jamoalarga |
| 24.12 | 🔎 Qidirish → matn | 1 harf: "kamida 2 ta harf"; 65+ belgi: "oshmasin"; topilmasa "… topilmadi. Boshqa nom yozing"; `/bekor` rejimdan chiqaradi |
| 24.13 | ⚽ Hamma o'yinlar | Barcha ligalar, kun bo'yicha, filtr va sahifalash ishlaydi |
| 24.14 | Yangi `/baza` ochib, eski xabardagi tugmani bosish; bot qayta ishga tushgandan keyin tugma | Alert "Bu ekran eskirgan… /baza"; jim yiqilmaydi |
| 24.15 | Sahifa/kun tugmasini ikki marta tez bosish | Natija bir xil, xato yo'q |
| 24.16 | 🔄 Bazani yangilash (admin) | Darhol "⏳ Baza yangilanmoqda…", so'ng o'sha xabar: o'yin/natija/narx soni, API so'rovlari, soniya; o'tkazib yuborilgan qadamlar ko'rsatiladi |
| 24.17 | Yangilash ketayotganda yana bosish | Alert "Yangilash allaqachon ketmoqda" |
| 24.18 | 30 daqiqa ichida qayta; limit yetmaydi; manba ishlamaydi; admin emas | Mos matnlar: "yana N daqiqadan keyin", "kerak ≈N, qoldi M. Hech narsa yuborilmadi", "manba ishlamayapti", "faqat administrator uchun" |

## 25. 📡 So'rovlar (`/sorovlar`) — admin (T7-D)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 25.1 | `/sorovlar` yoki "📡 So'rovlar" | So'nggi 24 soat: provayder bo'yicha so'rov, xato, keshdan, bloklangan, kvota; operatsiyalar bo'yicha qatorlar; filtr tugmalari. Admin bo'lmasa "faqat administrator uchun" |
| 25.2 | Filtr: Hammasi / API-Football / Gemini / Faqat xatolar | "12.10 12:41 · fixtures · ✅ · 230 ms" (mahalliy vaqt); 💾 kesh, ❌ xato (HTTP kodi), 🚫 blok belgilari; so'rov boshi (qirqilgan, escape) |
| 25.3 | Raqam tugmasi | Batafsil: vaqt (sekund bilan), operatsiya, holat, davomiylik, kvota, hajm, so'rov va javob `<pre>` bloklarida; xato matni |
| 25.4 | Matn 3500 belgidan uzun | Ekranda faqat boshi + "To'liq matn: fayl"; [📄 To'liq matnni fayl sifatida] `.txt` yuboradi (to'liq so'rov+javob+xato) |
| 25.5 | Jurnalda qirqilgan matn | "✂️ Jurnalda matn qirqilgan" belgisi |
| 25.6 | Hamma xabar | ≤ 4096 belgi; HTML buzilmaydi (`<`, `&` escape) |
| 25.7 | Bo'sh jurnal / bo'sh filtr | "Hali so'rov yozilmagan…" / "Bu filtr bo'yicha so'rov yo'q" |
| 25.8 | O'chirilgan (30 kundan eski) so'rov tugmasi | "Topilmadi" xabari |

## 26. Eskirgan baza tahlil oqimlarida (T7-D)
| # | Qadam | Kutilgan natija |
|---|---|---|
| 26.1 | ▶️ Boshlash, `/qayta`, 🔄 karta tugmasi, 🔍 Maxsus tahlil, shablon, ⚡ bugungi — baza eski/yo'q | "Baza yangilanmagan (oxirgi: 20.10 20:00 — mahalliy vaqt)…"; hech narsa hisoblanmaydi; izoh ostida [🔄 Bazani yangilash]; tasdiq ekrani qaytariladi |
| 26.2 | Tasdiq ekrani, baza eski | Ogohlantirish + [🔄 Bazani yangilash]; ▶️ Boshlash yashirin; ✏️ ishlaydi |
| 26.3 | Baza hali hech yangilanmagan | "Baza hali yangilanmagan…" |
| 26.4 | Izohdagi [🔄 Bazani yangilash] | Alohida "⏳" xabar, so'ng natija; asl ekran tugmalari saqlanadi |
