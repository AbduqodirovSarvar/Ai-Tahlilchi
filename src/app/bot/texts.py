"""All user-facing bot texts (Uzbek, Latin script). Handlers never inline strings.

Messages are sent with ``parse_mode=HTML``: static texts here must not contain a raw ``<``
or ``&``; values substituted into ``{placeholders}`` are escaped by the formatters.
"""

from typing import Final

from app.domain import MarketType, Selection, Sport
from app.services.interfaces import (
    ConfidenceLevel,
    FormResult,
    ResultMark,
    RiskProfile,
    SortKey,
    WatchKind,
    WatchNotification,
)

# --------------------------------------------------------------------------- main menu
MENU_ANALYSIS: Final = "📊 Tahlil"
MENU_CUSTOM: Final = "🔍 Maxsus tahlil"
MENU_TEMPLATES: Final = "💾 Shablonlarim"
MENU_TODAY: Final = "⚡ Bugungi signallar"
MENU_WATCHLIST: Final = "⭐ Sevimlilar"
MENU_BETS: Final = "🎟 Mening stavkalarim"
MENU_REPORT: Final = "📈 Hisobot"
MENU_SETTINGS: Final = "⚙️ Sozlamalar"
MENU_HELP: Final = "ℹ️ Holat / Yordam"
MENU_PLACEHOLDER_HINT: Final = "Menyudan bo'limni tanlang 👇"

WELCOME: Final = (
    "👋 <b>AI Tahlilchi</b> ga xush kelibsiz!\n\n"
    "Men sport o'yinlarini statistik model va sun'iy intellekt yordamida tahlil qilib, "
    "<b>qiymatli</b> (EV musbat) tanlovlarni topaman.\n\n"
    "📊 <b>Tahlil</b> — sana oralig'i bo'yicha qadam-baqadam tahlil\n"
    "⚙️ <b>Sozlamalar</b> — standart chegaralar va risk profili\n"
    "ℹ️ <b>Yordam</b> — buyruqlar va atamalar\n\n"
    "⚠️ Bu maslahat tizimi: kafolat yo'q, mas'uliyat bilan o'ynang."
)

HELP: Final = (
    "ℹ️ <b>Yordam</b>\n\n"
    "<b>Buyruqlar</b>\n"
    "/start — bosh menyu\n"
    "/tahlil — tezkor tahlil, masalan:\n"
    "<code>/tahlil 25.10-31.10 futbol 60% Arsenal,Inter</code>\n"
    "<code>/tahlil Arsenal - Chelsea</code> — maxsus tahlil (EV filtrisiz); retro: "
    "<code>/tahlil kecha</code>\n"
    "/kuzat — sevimlilar: <code>/kuzat Barcelona</code>\n"
    "/bank — bank, stavkalar va grafik\n"
    "/hisobot — natijalar hisoboti; /eksport — CSV (signallar va stavkalar)\n"
    "/ekspress — 2–4 signaldan ekspress hisoblash\n"
    "/qayta — bugungi signallarni qayta hisoblash (yoki <code>/qayta ertaga</code>, "
    "<code>/qayta 3kun</code>)\n"
    "/natijalar — natijasi aniqlanmagan signallarni bir bosishda belgilash\n"
    "/ozgarishlar — model o'zgarishlari jurnali va orqaga qaytarish\n"
    "/holat — tizim holati: API limitlari, oxirgi yangilanish, xatolar\n"
    "/sozlamalar — sozlamalar\n"
    "/bekor — joriy amalni bekor qilish\n"
    "/yordam — shu yordam\n\n"
    "<b>Atamalar</b>\n"
    "• <b>Ehtimollik</b> — model hisoblagan natija ehtimolligi (%).\n"
    "• <b>Ishonch</b> (Yuqori / O'rta / Past) — hisobning o'ziga qanchalik ishonish mumkinligi: "
    "ma'lumot sifati, model va bozor mosligi.\n"
    "• <b>EV</b> (kutilgan qiymat) — uzoq muddatda 1 birlik stavkadan kutiladigan foyda. "
    "EV +5% = har 100 dan o'rtacha +5.\n"
    "• <b>Adolatli koeff.</b> — model ehtimolligiga mos koeffitsient (1 / ehtimollik).\n"
    "• <b>Unit</b> — bankning bir ulushi (masalan, 1%). Tavsiya unit da beriladi.\n"
    "• <b>CLV</b> — olingan koeffitsient yopilish koeffitsientidan qanchalik yaxshi. "
    "Musbat CLV — to'g'ri yo'ldasiz degani.\n"
    "• ⏳ <b>Dastlabki tahlil</b> — o'yinga 3 kundan ko'p bor: tarkib va jarohatlar hali "
    "noma'lum, ishonch bir pog'ona past.\n"
    "• ❗ <b>Qiymatsiz</b> — EV manfiy: tavsiya etilmaydi."
)

UNKNOWN_TEXT: Final = "🤔 Buni tushunmadim. Menyudan bo'lim tanlang yoki /yordam ni bosing."
USE_BUTTONS: Final = (
    "👆 Iltimos, yuqoridagi tugmalardan birini tanlang (yoki /bekor — bekor qilish)."
)
CANCELLED: Final = "❌ Bekor qilindi. Menyudan yangi amal tanlang."
NOTHING_TO_CANCEL: Final = "Bekor qilinadigan amal yo'q. Menyudan bo'lim tanlang."

# --------------------------------------------------------------------------- errors
STALE_BUTTON: Final = "⌛ Bu tugma eskirgan, qaytadan boshlang."
STALE_BUTTON_HINT: Final = (
    "⌛ Bu tugma eskirgan (bot qayta ishga tushgan yoki oqim yakunlangan).\n"
    "Qaytadan boshlang: 📊 <b>Tahlil</b> yoki /start."
)
ALREADY_RUNNING: Final = "⏳ Tahlil allaqachon bajarilmoqda, kuting. Qotib qolsa, /bekor yuboring."
RATE_LIMITED: Final = "⏳ Juda tez! Bir necha soniya kutib, qayta urinib ko'ring."
COOLDOWN: Final = "⏳ {minutes} daqiqadan keyin qayta urinib ko'ring."
BUDGET_EXCEEDED: Final = (
    "💸 API limiti yetmaydi: bu so'rov uchun {needed} ta so'rov kerak, bugun qo'lda "
    "so'rovlar uchun {available} ta qoldi.\n"
    "Sana oralig'ini qisqartiring yoki qamrovni toraytiring (ligalar, jamoalar)."
)
NOT_FOUND: Final = "🔎 Topilmadi: ma'lumot o'chirilgan yoki eskirgan. Menyudan qaytadan boshlang."
UNEXPECTED_ERROR: Final = (
    "⚠️ Kutilmagan xatolik yuz berdi. Birozdan keyin qayta urinib ko'ring yoki /start ni bosing."
)
VALIDATION_GENERIC: Final = "❗ Noto'g'ri qiymat. Tekshirib, qaytadan kiriting."
VALIDATION_BY_FIELD: Final[dict[str, str]] = {
    "date_to": (
        "📅 Sana oralig'i noto'g'ri: oyna ko'pi bilan {max_window} kun bo'lishi va bugundan "
        "{max_future} kundan uzoqqa chiqmasligi kerak. Boshqa oraliq tanlang."
    ),
    "date_from": "📅 Sana oralig'i to'liq emas: boshlanish va tugash sanasini tanlang.",
    "sports": "🏅 Kamida bitta sport turini tanlang.",
    "markets": "📊 Kamida bitta bozorni tanlang.",
    "scope_ids": "🔎 Qamrovda kamida bitta (ko'pi bilan {max_scope}) element tanlang.",
    "max_signals": "🔢 Signal soni ruxsat etilgan oraliqda bo'lishi kerak.",
    "min_probability": "🎯 Minimal ehtimollik {low}–{high}% oralig'ida bo'lishi kerak.",
    "min_ev": "💹 Minimal EV {low}–{high}% oralig'ida bo'lishi kerak.",
    "odds_min": "📈 Koeffitsient {low}–{high} oralig'ida bo'lishi kerak.",
    "odds_max": "📈 Koeffitsient diapazoni noto'g'ri: yuqori chegara pastkisidan katta bo'lsin.",
    "unit": "🎲 Unit bank summasidan oshmasligi kerak. Avval bankni kattalashtiring.",
    "bank": "💰 Bank summasi musbat son bo'lishi kerak.",
    "daily_loss_limit": "🛑 Kunlik zarar limiti musbat son bo'lishi kerak.",
    "weekly_loss_limit": "🛑 Haftalik zarar limiti musbat son bo'lishi kerak.",
    "timezone": "🌍 Vaqt mintaqasi noma'lum. Masalan: Asia/Tashkent.",
    "quiet_hours": "🌙 Tinch soatlarning boshlanishi va tugashi har xil bo'lishi kerak.",
    "prematch_minutes": "⏱ Pre-match yangilanish: o'chiq, 30, 60 yoki 90 daqiqa.",
    "signal_id": "⚽ Bu o'yin uchun amal hozir mumkin emas (o'yin boshlangan bo'lishi mumkin).",
    "stake": (
        "💵 Summani o'zingiz kiriting: unit sozlanmagan yoki bu signal uchun stavka tavsiya "
        "etilmagan. ✅ Stavka qildim → ✏️ Boshqa koeff./summa."
    ),
    "name": "💾 Shablon nomi 1–{max_name} belgidan iborat bo'lishi kerak.",
    "text": "🔤 Qidiruv uchun kamida {min_search} ta harf yozing.",
}

# --------------------------------------------------------------------------- common buttons
BTN_BACK: Final = "⬅️ Orqaga"
BTN_CANCEL: Final = "❌ Bekor qilish"
BTN_DEFAULT: Final = "⏭ Standart"
BTN_DONE: Final = "✅ Tayyor"
BTN_CUSTOM: Final = "✏️ O'zim kiritaman"
BTN_ALL: Final = "Hammasi"
BTN_PREV_PAGE: Final = "◀️"
BTN_NEXT_PAGE: Final = "▶️"
PAGE_INDICATOR: Final = "{page}/{pages}"
CHECKED: Final = "✅"
UNCHECKED: Final = "▫️"
DEFAULT_MARK: Final = " ⭐"

# --------------------------------------------------------------------------- labels
SPORT_LABELS: Final[dict[Sport, str]] = {
    Sport.FOOTBALL: "⚽ Futbol",
    Sport.HOCKEY: "🏒 Xokkey",
    Sport.BASKETBALL: "🏀 Basketbol",
    Sport.VOLLEYBALL: "🏐 Voleybol",
    Sport.TENNIS: "🎾 Tennis",
}
SPORT_ICONS: Final[dict[Sport, str]] = {
    Sport.FOOTBALL: "⚽",
    Sport.HOCKEY: "🏒",
    Sport.BASKETBALL: "🏀",
    Sport.VOLLEYBALL: "🏐",
    Sport.TENNIS: "🎾",
}
ALL_SPORTS: Final = "🌐 Hammasi"
EXPERIMENTAL_MARK: Final = " 🧪"
MARKET_LABELS: Final[dict[MarketType, str]] = {
    MarketType.MATCH_RESULT: "1X2",
    MarketType.DOUBLE_CHANCE: "Ikki natija",
    MarketType.TOTALS: "Total",
    MarketType.BTTS: "Ikkala jamoa gol uradi",
    MarketType.ASIAN_HANDICAP: "Fora",
}
SELECTION_HOME_WIN: Final = "{home} g'alabasi (1)"
SELECTION_DRAW: Final = "Durang (X)"
SELECTION_AWAY_WIN: Final = "{away} g'alabasi (2)"
SELECTION_DOUBLE: Final[dict[Selection, str]] = {
    Selection.HOME_OR_DRAW: "Ikki natija: 1X",
    Selection.HOME_OR_AWAY: "Ikki natija: 12",
    Selection.DRAW_OR_AWAY: "Ikki natija: X2",
}
SELECTION_OVER: Final = "Total {line} dan ko'p"
SELECTION_UNDER: Final = "Total {line} dan kam"
SELECTION_BTTS_YES: Final = "Ikkala jamoa gol uradi: Ha"
SELECTION_BTTS_NO: Final = "Ikkala jamoa gol uradi: Yo'q"
SELECTION_HANDICAP: Final = "Fora {team} ({line})"
CONFIDENCE_LABELS: Final[dict[ConfidenceLevel, str]] = {
    ConfidenceLevel.HIGH: "Yuqori",
    ConfidenceLevel.MEDIUM: "O'rta",
    ConfidenceLevel.LOW: "Past",
}
RESULT_LABELS: Final[dict[ResultMark, str]] = {
    ResultMark.WON: "✅ O'tdi",
    ResultMark.LOST: "❌ O'tmadi",
    ResultMark.VOID: "↩️ Qaytarildi",
    ResultMark.HALF_WON: "✅ Yarim o'tdi",
    ResultMark.HALF_LOST: "❌ Yarim o'tmadi",
    ResultMark.CASHOUT: "💸 Cashout",
}
SORT_LABELS: Final[dict[SortKey, str]] = {
    SortKey.EV: "💹 EV bo'yicha",
    SortKey.PROBABILITY: "🎯 Ehtimollik bo'yicha",
    SortKey.KICKOFF: "🕒 Sana va vaqt bo'yicha",
}
RISK_LABELS: Final[dict[RiskProfile, str]] = {
    RiskProfile.CONSERVATIVE: "🛡 Konservativ",
    RiskProfile.BALANCED: "⚖️ Balanslangan",
    RiskProfile.AGGRESSIVE: "🔥 Agressiv",
    RiskProfile.CUSTOM: "✏️ Moslashtirilgan",
}
WEEKDAYS: Final = ("Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba")
WEEKDAYS_SHORT: Final = ("Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya")
MONTHS: Final = (
    "Yanvar",
    "Fevral",
    "Mart",
    "Aprel",
    "May",
    "Iyun",
    "Iyul",
    "Avgust",
    "Sentabr",
    "Oktabr",
    "Noyabr",
    "Dekabr",
)
RANGE_DASH: Final = "–"
OFF: Final = "o'chiq"
NOT_SET: Final = "kiritilmagan"
DEFAULT_LEAGUES: Final = "standart ligalar"

# --------------------------------------------------------------------------- signal card
CARD_HEADER: Final = "{icon} <b>{fixture}</b>   🕒 {time}  🏆 {league}"
CARD_PICK: Final = "🎯 Tanlov: <b>{selection}</b>   @ <b>{odds}</b>"
CARD_PROBABILITY: Final = "📊 Ehtimollik: {probability} (adolatli koeff. {fair_odds})"
CARD_EV: Final = "💹 EV: {ev}   🛡 Ishonch: {confidence}"
CARD_STAKE: Final = "💰 Tavsiya: {stake} unit"
CARD_NO_STAKE: Final = "💰 Tavsiya: stavka qilinmaydi"
CARD_NOTE: Final = "📝 {note}"
CARD_RETRO: Final = "🕰 Retro: model o'sha paytda shuni bergan bo'lardi (tavsiya emas)"
CARD_PRELIMINARY: Final = "⏳ Dastlabki tahlil (tarkib va jarohatlar hali noma'lum)"
CARD_NEGATIVE_EV: Final = "❗ Qiymatsiz: EV manfiy, tavsiya etilmaydi"
CARD_NO_VALUE: Final = "❗ Qiymatsiz: chegaralaringizdan past"
CARD_WATCHLIST: Final = "⭐ Sevimli"
CARD_EXPERIMENTAL: Final = "🧪 Test rejimi (sport hali sinovdan o'tmagan)"
CARD_CAUTION: Final = "⚠️ AI ogohlantirishi: ehtiyot bo'ling"
CARD_RISK_FLAGS: Final = "⚠️ Xavflar: {flags}"
CARD_DATA_AS_OF: Final = "📦 Ma'lumot {time} holatiga"
CARD_RESULT: Final = "Natija: {result}"
CARD_ODDS_MOVED: Final = "📌 Tavsiya koeff.: {recommended} → hozir {current}"
CARD_HIDDEN: Final = "🙈 Yashirildi: {fixture}"
CARD_CANCELLED: Final = "🚫 Signal bekor qilingan"
BTN_RECALC: Final = "🔄 Qayta hisoblash"
BTN_DETAIL: Final = "📋 Batafsil"
BTN_BET: Final = "✅ Stavka qildim"
BTN_HIDE: Final = "🙈 Yashirish"
HIDDEN_ALERT: Final = "🙈 Signal yashirildi"

# --------------------------------------------------------------------------- results
WATCHLIST_HEADER: Final = "⭐ <b>Sevimlilar</b> — {count} ta (har doim ko'rsatiladi)"
WATCHLIST_HEADER_CONTINUED: Final = "⭐ <b>Sevimlilar</b> (davomi)"
RESULTS_WATCHLIST: Final = (
    "⭐ Sevimli jamoa/ligalaringizdan yana {count} ta o'yin alohida ko'rsatiladi "
    "(so'ralgan sondan tashqari)."
)
DAY_HEADER: Final = "📅 <b>{weekday}, {day}</b> — {count} ta signal"
DAY_HEADER_CONTINUED: Final = "📅 <b>{weekday}, {day}</b> (davomi)"
RESULTS_HEADER: Final = "📊 <b>Natijalar</b>: {window} · {count} ta signal · {fixtures} ta o'yin"
RESULTS_RETRO: Final = (
    "🕰 Retro-tahlil: o'tgan o'yinlar, model o'sha paytda nima bergan bo'lardi va natija ✅/❌."
)
RESULTS_RETRO_TALLY: Final = (
    "🧮 O'sha paytdagi tanlovlar: ✅ {won} · ❌ {lost} — o'tish ulushi {rate}{extra}."
)
RESULTS_RETRO_PENDING: Final = " · ⏳ {count} ta natijasiz"
RESULTS_SHORTFALL: Final = (
    "ℹ️ Shartlarga {matched} ta o'yin mos keldi (siz {requested} ta so'ragansiz). "
    "Ro'yxat shartga mos kelmagan o'yinlar bilan to'ldirilmaydi."
)
RESULTS_FAILED: Final = "⚠️ {count} ta o'yinni tahlil qilib bo'lmadi, ular o'tkazib yuborildi."
RESULTS_NO_PRICE: Final = (
    "💱 {count} ta o'yin: 1xBet narxi yo'q, shuning uchun baholanmadi "
    "(bukmeker koeffitsient chiqarmagan bo'lishi mumkin)."
)
RESULTS_STALE_PRICE: Final = (
    "⏱ {count} ta o'yin: 1xBet narxi eskirgan, shuning uchun baholanmadi. "
    "🔄 Qayta hisoblash bilan yangilab ko'ring."
)
RESULTS_DATA_STALE: Final = (
    "📦 Ma'lumot yangilanmadi — eski ma'lumot ishlatildi. Raqamlar eskirgan bo'lishi mumkin."
)
RESULTS_EMPTY: Final = (
    "🤷 <b>Value topilmadi.</b>\n"
    "{window} oralig'ida shartlaringizga mos qiymatli tanlov yo'q ({fixtures} ta o'yin "
    "tahlil qilindi).\n\n"
    "Nima qilish mumkin:\n"
    "• minimal ehtimollik yoki EV ni pasaytiring;\n"
    "• sana oralig'ini kengaytiring yoki boshqa bozorlarni qo'shing."
)
RESULTS_MORE: Final = "Ko'rsatildi: {shown}/{total}"
BTN_MORE: Final = "➡️ Yana {count} ta"
RESULTS_END: Final = "✅ Hammasi ko'rsatildi ({total} ta signal)."
PROGRESS: Final = "⏳ Tahlil qilinmoqda… {done}/{total}"
PROGRESS_STAGES: Final[dict[str, str]] = {
    "collecting": "⏳ Ma'lumot yig'ilmoqda…",
    "analysing": "⏳ Tahlil qilinmoqda… {done}/{total}",
    "reviewing": "⏳ AI tekshiruvi… {done}/{total}",
    "finished": "✅ Tahlil tugadi: {total} ta o'yin ko'rib chiqildi.",
}

# --------------------------------------------------------------------------- analysis wizard
WIZARD_DATES: Final = (
    "📅 <b>1/7 · Sana oralig'i</b>\n"
    "Tugmani tanlang yoki oraliqni yozing: <code>25.10 - 31.10</code> yoki "
    "<code>25.10.2026-31.10.2026</code>.\n"
    "Kelajak oralig'i ko'pi bilan {max_future} kun."
)
BTN_TODAY: Final = "Bugun"
BTN_TOMORROW: Final = "Ertaga"
BTN_WEEKEND: Final = "Dam olish kunlari"
BTN_NEXT_DAYS: Final = "{days} kun"
BTN_YESTERDAY: Final = "🕰 Kecha (retro)"
BTN_LAST_WEEK: Final = "🕰 O'tgan hafta (retro)"
BTN_CALENDAR: Final = "📅 Oraliq tanlash"
CALENDAR_PICK_START: Final = "📅 <b>Boshlanish sanasini</b> tanlang:"
CALENDAR_PICK_END: Final = (
    "📅 Boshlanish: <b>{start}</b>\n<b>Tugash sanasini</b> tanlang (shu kunni qayta bosing — "
    "bir kunlik oraliq):"
)
WIZARD_SPORT: Final = "🏅 <b>2/7 · Sport turi</b>"
WIZARD_PROBABILITY: Final = (
    "🎯 <b>3/7 · Minimal o'tish ehtimolligi</b>\nStandart (sozlamalardan): {default}"
)
WIZARD_PROBABILITY_INPUT: Final = (
    "✏️ Minimal ehtimollikni foizda yozing ({low}–{high}), masalan: <code>65</code>"
)
WIZARD_EV: Final = (
    "💹 <b>3/7 · Minimal EV</b>\nEhtimollik: {probability}. Standart EV (sozlamalardan): {default}"
)
WIZARD_EV_INPUT: Final = "✏️ Minimal EV ni foizda yozing ({low}–{high}), masalan: <code>4</code>"
HIGH_PROBABILITY_WARNING: Final = (
    "⚠️ <b>Diqqat: yuqori ehtimollik chegarasi</b>\n"
    "90% ehtimollik odatda 1.03–1.10 koeffitsientga to'g'ri keladi. Bunday tanlovlar kam "
    "uchraydi (kuniga 0–3 ta) va deyarli har doim <b>manfiy EV</b> ga ega: bukmeker marjasi "
    "sababli 1.07 koeffitsientda 1 ta mag'lubiyatni qoplash uchun 14 ta g'alaba kerak.\n"
    "Tavsiya: ehtimollik 55–70% + EV ≥ 5%."
)
WIZARD_SCOPE: Final = "🔎 <b>4/7 · Qamrov</b>\nQaysi o'yinlar tahlil qilinsin?"
BTN_SCOPE_ALL: Final = "🌐 Hamma o'yinlar"
BTN_SCOPE_LEAGUES: Final = "🏆 Ligalar"
BTN_SCOPE_TEAMS: Final = "👕 Jamoalar"
BTN_SCOPE_FIXTURES: Final = "⚽ Aniq o'yinlar"
WIZARD_LEAGUES: Final = "🏆 <b>Ligalarni belgilang</b> (tanlangan: {count})"
WIZARD_LEAGUES_EMPTY: Final = "🏆 Tanlangan sport uchun liga topilmadi. ⬅️ Orqaga qayting."
WIZARD_TEAMS: Final = (
    "👕 <b>Jamoalar</b> (tanlangan: {count})\n"
    "Jamoa nomini yozing, bir nechtasini vergul bilan: <code>Arsenal, Inter, Barcelona</code>"
)
WIZARD_TEAMS_NOT_FOUND: Final = "🔎 Topilmadi: {names}. Boshqacha yozib ko'ring."
WIZARD_TEAMS_TOO_MANY_NAMES: Final = (
    "Bir xabarda ko'pi bilan {max_names} ta jamoa nomi yozing. Qolganlarini alohida yuboring."
)
WIZARD_TEAMS_CHOOSE: Final = "👇 Kerakli jamoani belgilang (➕):"
WIZARD_FIXTURES: Final = (
    "⚽ <b>Aniq o'yinlarni belgilang</b> (tanlangan: {count})\nSahifa {page}/{pages}"
)
WIZARD_FIXTURES_EMPTY: Final = (
    "⚽ Bu oraliqda o'yin topilmadi. ⬅️ Orqaga qaytib sana yoki sportni o'zgartiring."
)
WIZARD_COUNT: Final = (
    "🔢 <b>5/7 · O'yinlar soni</b>\nNechta signal kerak? Standart: {default}\n"
    "📆 Kuniga: {per_day} (yoqilsa, son har bir kun uchun alohida)"
)
WIZARD_COUNT_INPUT: Final = "✏️ Signal sonini yozing (1–{high}), masalan: <code>12</code>"
BTN_PER_DAY: Final = "📆 Kuniga: {state}"
WIZARD_MARKETS: Final = "📊 <b>6/7 · Bozorlar</b>\nBir nechtasini belgilash mumkin."
WIZARD_SORT: Final = "↕️ <b>7/7 · Saralash</b>"
WIZARD_CONFIRM: Final = (
    "📋 <b>So'rov:</b> {window} · {sports} · ≥ {probability} · EV ≥ {ev}\n"
    "<b>Qamrov:</b> {scope} · <b>Bozorlar:</b> {markets} · <b>Soni:</b> {count}\n"
    "<b>Saralash:</b> {sort}\n"
    "Topilgan o'yinlar: {fixtures} · Taxminiy API sarfi: {requests} so'rov "
    "(zaxirada {remaining})"
)
WIZARD_RUN_SUMMARY: Final = "📋 <b>So'rov:</b> {window} · {sports} · ≥ {probability} · EV ≥ {ev}"
WIZARD_CONFIRM_RETRO: Final = "🕰 O'tgan sanalar: retro-tahlil (API sarfi deyarli yo'q)."
WIZARD_OVER_BUDGET: Final = (
    "⚠️ So'rov API limitidan oshadi: {requests} ta so'rov kerak, zaxirada {remaining} ta. "
    "✏️ O'zgartirish bilan sana oralig'ini qisqartiring yoki qamrovni toraytiring."
)
WIZARD_NO_FIXTURES: Final = "ℹ️ Bu shartlarga mos o'yin topilmadi. ✏️ O'zgartirib ko'ring."
BTN_START: Final = "▶️ Boshlash"
BTN_EDIT: Final = "✏️ O'zgartirish"
BTN_SAVE_TEMPLATE: Final = "💾 Shablon sifatida saqlash"
WIZARD_EDIT: Final = "✏️ <b>Qaysi parametrni o'zgartirasiz?</b>"
BTN_EDIT_DATES: Final = "📅 Sana"
BTN_EDIT_SPORT: Final = "🏅 Sport"
BTN_EDIT_PROBABILITY: Final = "🎯 Ehtimollik va EV"
BTN_EDIT_SCOPE: Final = "🔎 Qamrov"
BTN_EDIT_COUNT: Final = "🔢 Soni"
BTN_EDIT_MARKETS: Final = "📊 Bozorlar"
BTN_EDIT_SORT: Final = "↕️ Saralash"
TEMPLATE_NAME_PROMPT: Final = (
    "💾 Shablon nomini yozing (1–{max_name} belgi), masalan: <code>Dam olish · APL · 60%+</code>"
)
TEMPLATE_SAVED: Final = "💾 Shablon saqlandi: <b>{name}</b>"
WIZARD_STARTED: Final = "▶️ Tahlil boshlandi."
SCOPE_ALL_LABEL: Final = "hamma o'yinlar"
COUNT_ALL_LABEL: Final = "hammasi"
COUNT_PER_DAY_LABEL: Final = "{count} (kuniga)"
SCOPE_LIMIT: Final = "Ko'pi bilan {max_scope} ta tanlash mumkin"
SELECT_AT_LEAST_ONE: Final = "Kamida bittasini tanlang"
WINDOW_SINGLE: Final = "{day}"
WINDOW_RANGE: Final = "{start}–{end}"

# --------------------------------------------------------------------------- input parsing
PARSE_EMPTY: Final = "Qiymat bo'sh. Qaytadan yozing."
PARSE_NOT_NUMBER: Final = "«{value}» son emas. Masalan: <code>{example}</code>"
PARSE_PERCENT_RANGE: Final = "«{value}» noto'g'ri: {low}–{high}% oralig'ida bo'lishi kerak."
PARSE_COUNT_RANGE: Final = "«{value}» noto'g'ri: {low}–{high} oralig'ida bo'lishi kerak."
PARSE_BAD_DATE: Final = (
    "«{value}» — bunday sana yo'q. Format: <code>25.10</code> yoki <code>25.10.2026</code>."
)
PARSE_BAD_RANGE: Final = (
    "«{value}» ni sana oralig'i sifatida tushunmadim. Masalan: <code>25.10 - 31.10</code>."
)
PARSE_REVERSED_RANGE: Final = "Tugash sanasi boshlanishdan oldin bo'lishi mumkin emas."
PARSE_WINDOW_TOO_LONG: Final = "Oraliq juda uzun: ko'pi bilan {max_window} kun."
PARSE_TOO_OLD: Final = (
    "Oraliq juda eski: retro-tahlil bugundan ko'pi bilan {max_lookback} kun oldingi "
    "sanadan boshlanadi."
)
PARSE_TOO_FAR: Final = (
    "Oraliq juda uzoqda: bugundan ko'pi bilan {max_future} kun keyingi o'yinlar tahlil qilinadi "
    "(koeffitsientlar odatda 1–2 hafta oldin chiqadi)."
)
PARSE_BAD_TIME: Final = "«{value}» vaqt emas. Format: <code>09:00</code>."
PARSE_BAD_TIME_RANGE: Final = "«{value}» noto'g'ri. Format: <code>00:00-08:00</code>."
PARSE_BAD_ODDS_RANGE: Final = (
    "«{value}» noto'g'ri. Format: <code>1.40-3.00</code> "
    "({low}–{high}, pastki chegara yuqorisidan kichik)."
)
PARSE_BAD_MONEY: Final = (
    "«{value}» noto'g'ri summa. Musbat son yozing, masalan: <code>1000000</code>."
)
PARSE_BAD_TIMEZONE: Final = (
    "«{value}» — noma'lum vaqt mintaqasi. Masalan: <code>Asia/Tashkent</code>."
)
QUICK_DUPLICATE: Final = "«{value}»: {what} ikki marta berilgan. Bittasini qoldiring."
QUICK_AMBIGUOUS_NUMBER: Final = (
    "«{value}» — bu foizmi yoki sonmi? Ehtimollik uchun <code>{value}%</code>, "
    "signal soni uchun <code>{value}ta</code> deb yozing."
)
QUICK_WHAT_DATE: Final = "sana"
QUICK_WHAT_PROBABILITY: Final = "ehtimollik"
QUICK_WHAT_EV: Final = "EV"
QUICK_WHAT_COUNT: Final = "signal soni"
QUICK_TOO_LONG: Final = "So'rov juda uzun (ko'pi bilan {max_length} belgi)."
QUICK_TOO_MANY_TEAMS: Final = "Jamoalar juda ko'p: ko'pi bilan {max_scope} ta."
QUICK_ERROR: Final = (
    "❗ {problem}\n\n"
    "Namuna: <code>/tahlil 25.10-31.10 futbol 60% Arsenal,Inter</code>\n"
    "Qo'shimcha: <code>ev5%</code>, <code>10ta</code>, <code>bugun</code>, <code>ertaga</code>, "
    "<code>dam</code>, <code>3kun</code>; retro: <code>kecha</code>, <code>o'tgan hafta</code>."
)
QUICK_TEAM_NOT_FOUND: Final = "«{name}» jamoasi topilmadi. Nomini tekshiring."
QUICK_TEAM_AMBIGUOUS: Final = "«{name}» noaniq: {options}. Aniqroq yozing."

# --------------------------------------------------------------------------- settings
SETTINGS_TITLE: Final = "⚙️ <b>Sozlamalar</b>\nO'zgartirish uchun parametrni tanlang."
SETTINGS_ROWS: Final[dict[str, str]] = {
    "sports": "🏅 Sport turlari: {value}",
    "leagues": "🏆 Ligalar: {value}",
    "markets": "📊 Bozorlar: {value}",
    "max_signals": "🔢 Kunlik signal soni (maks.): {value}",
    "min_probability": "🎯 Minimal ehtimollik: {value}",
    "min_ev": "💹 Minimal EV: {value}",
    "odds": "📈 Koeffitsient diapazoni: {value}",
    "risk": "🛡 Risk profili: {value}",
    "send_time": "⏰ Yuborish vaqti: {value}",
    "prematch": "⏱ Pre-match yangilanish: {value}",
    "quiet": "🌙 Tinch soatlar: {value}",
    "timezone": "🌍 Vaqt mintaqasi: {value}",
    "bank": "💰 Bank: {value}",
    "unit": "🎲 Unit: {value}",
    "daily_loss": "🛑 Kunlik zarar limiti: {value}",
    "weekly_loss": "🛑 Haftalik zarar limiti: {value}",
}
SETTINGS_BUTTONS: Final[dict[str, str]] = {
    "sports": "🏅 Sportlar",
    "leagues": "🏆 Ligalar",
    "markets": "📊 Bozorlar",
    "max_signals": "🔢 Signal soni",
    "min_probability": "🎯 Ehtimollik",
    "min_ev": "💹 EV",
    "odds": "📈 Koeffitsient",
    "risk": "🛡 Risk profili",
    "send_time": "⏰ Yuborish vaqti",
    "prematch": "⏱ Pre-match",
    "quiet": "🌙 Tinch soatlar",
    "timezone": "🌍 Vaqt mintaqasi",
    "bank": "💰 Bank",
    "unit": "🎲 Unit",
    "daily_loss": "🛑 Kunlik limit",
    "weekly_loss": "🛑 Haftalik limit",
}
SETTINGS_PROMPTS: Final[dict[str, str]] = {
    "sports": "🏅 <b>Sport turlari</b> — belgilang (o'zgarish darhol saqlanadi):",
    "leagues": (
        "🏆 <b>Ligalar</b> — belgilang. Hech biri belgilanmasa, standart ligalar ishlatiladi:"
    ),
    "markets": "📊 <b>Bozorlar</b> — belgilang (o'zgarish darhol saqlanadi):",
    "max_signals": "🔢 <b>Kunlik signal soni</b> (1–{high}):",
    "min_probability": "🎯 <b>Minimal ehtimollik</b> ({low}–{high}%):",
    "min_ev": "💹 <b>Minimal EV</b> ({low}–{high}%):",
    "odds": "📈 <b>Koeffitsient diapazoni</b>:",
    "risk": (
        "🛡 <b>Risk profili</b> — tayyor chegaralar to'plami:\n"
        "🛡 Konservativ: ehtimollik ≥ 60%, EV ≥ 3%, koeff. 1.30–2.00\n"
        "⚖️ Balanslangan: ehtimollik ≥ 45%, EV ≥ 5%, koeff. 1.50–3.00\n"
        "🔥 Agressiv: ehtimollik ≥ 30%, EV ≥ 8%, koeff. 2.00–5.00"
    ),
    "send_time": "⏰ <b>Kunlik xabar yuborish vaqti</b>:",
    "prematch": "⏱ <b>O'yindan oldin qayta hisoblash</b>:",
    "quiet": "🌙 <b>Tinch soatlar</b> (bu vaqtda xabar yuborilmaydi):",
    "timezone": "🌍 <b>Vaqt mintaqasi</b> (vaqtlar shu bo'yicha ko'rsatiladi):",
    "bank": "💰 <b>Bank hajmi</b>:",
    "unit": "🎲 <b>Unit</b> (bir tavsiya birligi summasi, bankdan oshmaydi):",
    "daily_loss": "🛑 <b>Kunlik zarar limiti</b>:",
    "weekly_loss": "🛑 <b>Haftalik zarar limiti</b>:",
}
SETTINGS_INPUT_PROMPTS: Final[dict[str, str]] = {
    "max_signals": "✏️ Signal sonini yozing (1–{high}):",
    "min_probability": "✏️ Minimal ehtimollikni foizda yozing ({low}–{high}):",
    "min_ev": "✏️ Minimal EV ni foizda yozing ({low}–{high}):",
    "odds": "✏️ Diapazonni yozing, masalan: <code>1.40-3.00</code>",
    "send_time": "✏️ Vaqtni yozing, masalan: <code>08:30</code>",
    "quiet": "✏️ Oraliqni yozing, masalan: <code>23:00-07:00</code>",
    "timezone": "✏️ IANA vaqt mintaqasini yozing, masalan: <code>Europe/Moscow</code>",
    "bank": "✏️ Bank summasini yozing, masalan: <code>1000000</code>",
    "unit": "✏️ Unit summasini yozing, masalan: <code>10000</code>",
    "daily_loss": "✏️ Kunlik zarar limitini yozing, masalan: <code>50000</code>",
    "weekly_loss": "✏️ Haftalik zarar limitini yozing, masalan: <code>150000</code>",
}
SETTINGS_SAVED: Final = "✅ Saqlandi"
SETTINGS_CURRENT: Final = "Joriy qiymat: <b>{value}</b>"
PREMATCH_MINUTES: Final = "{minutes} daq."
BTN_OFF: Final = "🚫 O'chirish"
BTN_SETTINGS_BACK: Final = "⬅️ Sozlamalarga"

# --------------------------------------------------------------------------- service errors (v1.2)
BUSY: Final = (
    "⏳ Tahlil allaqachon bajarilmoqda. Tugashini kuting, so'ng qayta urinib ko'ring "
    "(ikki marta bosish API limitini ikki marta sarflamaydi)."
)
SERVICE_UNAVAILABLE: Final = (
    "🔌 Ma'lumot manbai hozir javob bermayapti ({component}). Sozlamalaringiz saqlangan: "
    "birozdan keyin qayta urinib ko'ring."
)

# --------------------------------------------------------------------------- recalculation (F6.7)
RECALC_STARTED: Final = "⏳ Qayta hisoblanmoqda…"
RECALC_DONE: Final = "✅ Tayyor."
RECALC_STAGES: Final[dict[str, str]] = {
    "collecting": "⏳ Yangi koeffitsientlar olinmoqda…",
    "analysing": "⏳ Qayta hisoblanmoqda… {done}/{total}",
    "reviewing": "⏳ AI tekshiruvi… {done}/{total}",
    "finished": "✅ Qayta hisoblash tugadi: {total} ta o'yin.",
}
RECALC_TITLE: Final = "🔄 <b>{fixture}</b> yangilandi ({time})"
UPDATE_TITLE_24H: Final = "⏰ <b>{fixture}</b> — o'yinga 24 soat qoldi, yangilandi ({time})"
UPDATE_TITLE_PREMATCH: Final = "🔔 <b>{fixture}</b> — o'yin yaqin, yangilandi ({time})"
DIFF_ODDS_EV: Final = "Koeff.: {odds_before} → {odds_after}   EV: {ev_before} → {ev_after}"
DIFF_PROBABILITY: Final = "Ehtimollik: {before} → {after}"
DIFF_STAKE: Final = "Tavsiya: {before} → {after} unit"
DIFF_CONFIDENCE: Final = "Ishonch: {before} → {after}"
DIFF_UNCHANGED: Final = "Raqamlar o'zgarmadi."
DIFF_STATUS: Final = "Holat: {status}"
DIFF_LINEUPS: Final = "📋 Tarkiblar e'lon qilingan."
DIFF_DATA_AS_OF: Final = "📦 Yangi ma'lumot olinmadi: raqamlar {time} holatiga."
VERDICT_LABELS: Final[dict[str, str]] = {
    "still_valid": "✅ Kuchda",
    "below_threshold": "⚠️ Chegaradan pastga tushdi, tavsiya etilmaydi",
    "cancelled": "❌ Signal bekor qilindi",
    "no_price": "⚠️ Bukmekerda yangi narx yo'q, raqamlar tasdiqlanmadi",
}
RECALC_BATCH_HEADER: Final = (
    "🔄 <b>Qayta hisoblash tugadi:</b> yangilandi {updated} ta, raqami o'zgargan {changed} ta."
)
RECALC_BATCH_EMPTY: Final = (
    "ℹ️ Qayta hisoblanadigan signal topilmadi. Avval 📊 Tahlil yoki ⚡ Bugungi signallar ni oching."
)
RECALC_BATCH_UNCHANGED: Final = "Hech bir signalning raqami o'zgarmadi."
RECALC_BATCH_COOLDOWN: Final = (
    "⏳ {count} ta signal yaqinda yangilangan (bitta o'yin {minutes} daqiqada bir marta)."
)
RECALC_BATCH_STARTED: Final = "⚽ {count} ta o'yin boshlangan: qayta hisoblanmadi."
RECALC_BATCH_FAILED: Final = (
    "⚠️ {count} ta signalni yangilab bo'lmadi. Keyinroq qayta urinib ko'ring."
)
RECALC_BATCH_MORE: Final = "…va yana {count} ta o'zgarish."
BTN_RECALC_ALL: Final = "🔄 Hammasini yangilash"
QAYTA_NOT_A_WINDOW: Final = "«{value}» sana oralig'i emas."
QAYTA_BAD_ARGUMENT: Final = (
    "❗ {problem}\n\n"
    "Namuna: <code>/qayta</code> (bugun), <code>/qayta ertaga</code>, <code>/qayta 3kun</code>, "
    "<code>/qayta 25.10-27.10</code>."
)

# --------------------------------------------------------------------------- notifications
DIGEST_TITLE: Final = "☀️ <b>Kunlik tahlil</b> — {weekday}, {day}"
DIGEST_TEMPLATE_TITLE: Final = "💾 <b>{name}</b> — {weekday}, {day}"
LOSS_LIMIT_PERIODS: Final[dict[str, str]] = {"day": "kunlik", "week": "haftalik"}
DIGEST_LOSS_LIMIT: Final = (
    "🛑 <b>Zarar limitiga yetildi</b>: {period} limit {limit}, zarar {loss}.\n"
    "Yangi signallar yuborilmaydi: bugun to'xtab turish tavsiya etiladi. Qaror sizniki: "
    "kerak bo'lsa, 📊 Tahlil orqali qo'lda tahlil qilishingiz mumkin."
)
RESULTS_LOSS_LIMIT: Final = (
    "🛑 Zarar limitiga yetgansiz ({period} limit {limit}, zarar {loss}). "
    "Tahlil baribir bajarildi: qaror sizniki, lekin to'xtab turish tavsiya etiladi."
)
DIGEST_YESTERDAY: Final = (
    "📆 <b>Kecha:</b> {signals} ta signal · {won} ✅ · {lost} ❌{extra} · {profit} unit{clv}"
)
DIGEST_YESTERDAY_VOID: Final = " · {count} ↩️"
DIGEST_YESTERDAY_PENDING: Final = " · {count} ⏳"
DIGEST_YESTERDAY_CLV: Final = " · CLV {clv}"
CANCELLED_TITLE: Final = "❌ <b>Bekor: {fixture}</b>"
CANCEL_REASONS: Final[dict[str, str]] = {
    "postponed": "o'yin qoldirildi",
    "cancelled": "o'yin bekor qilindi",
    "abandoned": "o'yin to'xtatildi",
}
CANCELLED_BODY: Final = (
    "Sabab: {reason}.\n"
    "🎯 Tanlov: {selection}\n"
    "Signal endi tavsiya etilmaydi. Stavka qilgan bo'lsangiz, bukmeker qoidalarini tekshiring "
    "(odatda stavka qaytariladi)."
)
WATCH_TITLES: Final[dict[str, str]] = {
    "lineups": "📋 Tarkiblar e'lon qilindi",
    "odds_move": "📉 Koeffitsient sezilarli o'zgardi",
    "result": "🏁 O'yin tugadi",
}
WATCH_HEADER: Final = "⭐ <b>Sevimli:</b> {title}\n{icon} <b>{fixture}</b>   🕒 {time}  🏆 {league}"
SETTLED_HEADER: Final = "{result} — <b>{fixture}</b>"
SETTLED_PICK: Final = "🎯 {selection}   @ {odds}"
SETTLED_PROFIT: Final = "💰 Natija: {profit} unit"
ADMIN_ALERT: Final = "🚨 <b>Tizim xatosi</b> · {component} · {time}\n{message}"

# --------------------------------------------------------------------------- today's signals
TODAY_HEADER: Final = "⚡ <b>Bugungi signallar</b> — {count} ta"
TODAY_EMPTY: Final = (
    "🤷 <b>Bugun faol signal yo'q.</b>\n"
    "Kunlik xabar sozlangan vaqtda keladi. Hozir tekshirish uchun 📊 Tahlil → «Bugun» ni tanlang."
)

# --------------------------------------------------------------------------- /holat (F6.9)
STATUS_TITLE: Final = "ℹ️ <b>Tizim holati</b>"
STATUS_MODE: Final[dict[str, str]] = {
    "fake": "🧪 Rejim: <b>namunaviy ma'lumot</b> (haqiqiy API ulanmagan)",
    "real": "🟢 Rejim: <b>haqiqiy ma'lumot</b>",
}
STATUS_QUOTA_HEADER: Final = "📡 <b>API limitlari</b> (bugun):"
STATUS_QUOTA_LINE: Final = (
    "• {provider}{sport}: ishlatilgan {used}/{limit} · qoldi {remaining}\n"
    "  qo'lda so'rovlar: {manual_used}/{manual_reserve} · qo'lda qoldi {manual_left}"
)
STATUS_LLM: Final = (
    "🤖 <b>AI (Gemini)</b> (bugun): so'rovlar {requests_used}/{requests_limit} · "
    "tokenlar {tokens_used}/{tokens_limit}"
)
STATUS_LLM_NONE: Final = "🤖 AI (Gemini): limit ma'lumoti yo'q."
STATUS_QUOTA_NONE: Final = "📡 API limitlari: ma'lumot yo'q."
STATUS_REFRESH: Final = "🔄 Ma'lumot oxirgi yangilangan: <b>{time}</b> ({age})"
STATUS_REFRESH_NEVER: Final = "🔄 Ma'lumot hali yangilanmagan."
STATUS_AGE_MINUTES: Final = "{minutes} daq. oldin"
STATUS_AGE_HOURS: Final = "{hours} soat oldin"
STATUS_AGE_DAYS: Final = "{days} kun oldin"
STATUS_AGE_NOW: Final = "hozirgina"
STATUS_JOBS_HEADER: Final = "🗓 <b>Oxirgi ishlar:</b>"
STATUS_JOB_LINE: Final = "• {mark} {name} — {time}{detail}"
STATUS_JOB_NEVER: Final = "hali ishlamagan"
STATUS_JOBS_NONE: Final = "🗓 Rejalashtirilgan ishlar haqida ma'lumot yo'q."
STATUS_ERRORS_HEADER: Final = "🧯 <b>Oxirgi xatolar:</b>"
STATUS_ERROR_LINE: Final = "• {time} · {component}: {message}"
STATUS_ERRORS_NONE: Final = "🧯 Oxirgi xatolar: yo'q ✅"
STATUS_MORE: Final = "…va yana {count} ta"
STATUS_FOOTER: Final = "Buyruqlar va atamalar: /yordam"

# --------------------------------------------------------------------------- minimal bets
BET_PROMPT: Final = (
    "✅ <b>Stavka qildim</b>\n"
    "{fixture}\n"
    "🎯 {selection}\n"
    "Signal koeffitsienti: <b>{odds}</b> · tavsiya: <b>{stake}</b>\n\n"
    "Shu qiymatlar bilan yozaymi yoki o'zingiz kiritasizmi?"
)
BET_STAKE_UNITS: Final = "{units} unit"
BET_STAKE_MONEY: Final = "{amount} ({units} unit)"
BTN_BET_OK: Final = "✅ Yozish: {odds}"
BTN_BET_CUSTOM: Final = "✏️ Boshqa koeff./summa"
BET_INPUT_PROMPT: Final = (
    "✏️ Haqiqatda olgan koeffitsientingizni (va ixtiyoriy summani) yozing:\n"
    "<code>1.90</code> yoki <code>1.90 50000</code>\n"
    "Summa yozilmasa, tavsiya etilgan summa olinadi."
)
BET_PLACED: Final = (
    "🎟 <b>Stavka yozildi</b>\n{fixture}\n🎯 {selection} @ <b>{odds}</b>\n💵 Summa: <b>{stake}</b>"
)
BET_LOSS_LIMIT: Final = (
    "🛑 Diqqat: zarar limitingizga yetdingiz. Qaror sizniki, lekin bugun to'xtab turish tavsiya "
    "etiladi."
)
BET_CANCELLED: Final = "❌ Stavka yozilmadi."
BET_FINISH_FLOW_FIRST: Final = "✏️ Avval joriy amalni tugating yoki /bekor ni bosing."
BET_BAD_INPUT: Final = (
    "«{value}» ni tushunmadim. Format: <code>1.90</code> yoki <code>1.90 50000</code>."
)
BET_BAD_ODDS: Final = "Koeffitsient {low}–{high} oralig'ida bo'lishi kerak."
BTN_BET_CANCEL: Final = "❌ Bekor qilish"
BETS_TITLE: Final = "🎟 <b>Mening stavkalarim</b>"
BETS_BALANCE: Final = "💰 Balans: <b>{balance}</b> (boshlang'ich bank + yopilgan stavkalar foydasi)"
BETS_BANK_UNSET: Final = "💰 Bank kiritilmagan: ⚙️ Sozlamalar → Bank."
BETS_MONTH_PROFIT: Final = "📅 Oy foydasi: {profit}"
BETS_OPEN_COUNT: Final = "🟡 Ochiq stavkalar: {count} (balansni kamaytirmaydi)"
BETS_LOSS_LIMIT: Final = "🛑 Zarar limitiga yetildi (bugun {today}, hafta {week})."
BETS_EMPTY: Final = (
    "Ochiq stavkalar yo'q.\nSignal kartasidagi «✅ Stavka qildim» tugmasi bilan stavka yozing."
)
BETS_LINE: Final = "• {fixture} — {selection} @ {odds} · {stake} · {date}"
BETS_MORE: Final = "…va yana {count} ta"
BET_SIDE_HOME: Final = "uy jamoasi"
BET_SIDE_AWAY: Final = "mehmon jamoasi"


# --------------------------------------------------------------------------- detail card (F7.2)
DETAIL_TITLE: Final = "📋 <b>{fixture}</b>\n🏆 {league} · 🕒 {time}"
DETAIL_SCORE: Final = "🏁 Hisob: <b>{home}:{away}</b>"
DETAIL_LINEUPS_CONFIRMED: Final = "✅ Tarkiblar e'lon qilingan"
DETAIL_LINEUPS_UNKNOWN: Final = "⏳ Tarkiblar hali e'lon qilinmagan"
DETAIL_FORM_HEADER: Final = "📈 <b>Forma</b> (so'nggi o'yinlar, eskidan yangiga)"
DETAIL_FORM_LINE: Final = "{team}: {form}"
DETAIL_NO_DATA: Final = "ma'lumot yo'q"
FORM_MARKS: Final[dict[FormResult, str]] = {
    FormResult.WIN: "✅",
    FormResult.DRAW: "➖",
    FormResult.LOSS: "❌",
}
DETAIL_STATS_HEADER: Final = "📊 <b>Statistika</b> (o'rtacha, o'z roli bo'yicha)"
DETAIL_STATS_LINE: Final = "{team} ({role}, {matches} o'yin): urgan {gf} · o'tkazgan {ga}{xg}"
DETAIL_STATS_XG: Final = " · xG {xg_for} / {xg_against}"
ROLE_HOME: Final = "uyda"
ROLE_AWAY: Final = "safarda"
DETAIL_H2H_HEADER: Final = "🤝 <b>Oldingi uchrashuvlar</b>"
DETAIL_H2H_EMPTY: Final = "🤝 Oldingi uchrashuvlar: ma'lumot yo'q"
DETAIL_H2H_LINE: Final = "{date} · {home} {home_score}:{away_score} {away}"
DETAIL_ABSENCES_HEADER: Final = "🩹 <b>Jarohat va diskvalifikatsiyalar</b>"
DETAIL_ABSENCES_NONE: Final = "🩹 Jarohat va diskvalifikatsiyalar: ma'lum emas"
DETAIL_ABSENCE_LINE: Final = "• {team}: {player} — {reason}"
DETAIL_SCORES_HEADER: Final = "🎯 <b>Eng ehtimolli hisoblar</b>"
DETAIL_SCORE_ITEM: Final = "{home}:{away} — {probability}"
DETAIL_MARKETS_HEADER: Final = "💹 <b>Barcha bozorlar</b> (model | adolatli koeff. | bukmeker | EV)"
DETAIL_MARKETS_COLUMNS: Final = "Tanlov               Model Adolatli  Bukm.      EV"
DETAIL_MARKETS_EMPTY: Final = "💹 Bozorlar jadvali: narxlar hali yo'q"
DETAIL_NO_PRICE: Final = "—"
DETAIL_MOVES_HEADER: Final = "📉 <b>Koeffitsient harakati</b> (ertalab → hozir)"
DETAIL_MOVE_LINE: Final = "• {selection}: {opening} → {current} ({change})"
DETAIL_AS_OF: Final = "📦 Ma'lumot {time} holatiga"
DETAIL_MORE: Final = "…va yana {count} ta"
SHORT_HOME: Final = "1"
SHORT_AWAY: Final = "2"
SHORT_DRAW: Final = "X"
SHORT_DOUBLE: Final[dict[Selection, str]] = {
    Selection.HOME_OR_DRAW: "1X",
    Selection.HOME_OR_AWAY: "12",
    Selection.DRAW_OR_AWAY: "X2",
}
SHORT_OVER: Final = "Ko'p {line}"
SHORT_UNDER: Final = "Kam {line}"
SHORT_BTTS_YES: Final = "Ikkalasi gol: ha"
SHORT_BTTS_NO: Final = "Ikkalasi gol: yo'q"
SHORT_HANDICAP: Final = "Fora {side} ({line})"
BTN_BACK_TO_CARD: Final = "⬅️ Kartaga qaytish"


# --------------------------------------------------------------------------- custom analysis (F7.1)
CUSTOM_PROMPT: Final = (
    "🔍 <b>Maxsus tahlil</b>\n"
    "Jamoa nomini yozing, masalan <code>Real</code>, yoki o'yinni: "
    "<code>Arsenal - Chelsea</code>. Lotin yoki kirill, xato yozilgan nom ham topiladi.\n"
    "Yoki bugungi/ertangi o'yinlardan tanlang.\n\n"
    "ℹ️ Bu tahlil EV filtrisiz ishlaydi: o'yin qiymatsiz bo'lsa ham barcha bozorlar "
    "ehtimolliklari ko'rsatiladi."
)
BTN_CUSTOM_TODAY: Final = "📅 Bugungi o'yinlar"
BTN_CUSTOM_TOMORROW: Final = "📆 Ertangi o'yinlar"
BTN_CUSTOM_RUN: Final = "▶️ Tahlil qilish ({count})"
BTN_CUSTOM_AGAIN: Final = "🔎 Qayta qidirish"
CUSTOM_TEAMS_FOUND: Final = "👕 <b>{query}</b> bo'yicha topildi. Jamoani tanlang:"
CUSTOM_NOT_FOUND: Final = (
    "🔎 «{query}» topilmadi. Boshqacha yozib ko'ring: to'liq nom yoki "
    "<code>Arsenal - Chelsea</code>."
)
CUSTOM_TEAM_FIXTURES: Final = (
    "👕 <b>{team}</b> — yaqin o'yinlar. Tahlil qilinadiganlarini belgilang:"
)
CUSTOM_NO_UPCOMING: Final = (
    "ℹ️ {team} uchun yaqin {days} kunda o'yin topilmadi. Boshqa jamoani yozing."
)
CUSTOM_FIXTURES_FOUND: Final = "⚽ <b>{query}</b> — topilgan o'yinlar. Belgilang:"
CUSTOM_DAY_FIXTURES: Final = "⚽ <b>{day}</b> o'yinlari. Tahlil qilinadiganlarini belgilang:"
CUSTOM_DAY_EMPTY: Final = "ℹ️ {day} uchun o'yin topilmadi. Boshqa kunni yoki jamoa nomini yozing."
CUSTOM_TRUNCATED: Final = "ℹ️ Faqat birinchi {count} ta o'yin ko'rsatildi."
CUSTOM_SELECTED: Final = "Tanlangan: {count} (ko'pi bilan {max})"
CUSTOM_LIMIT: Final = "Bir martada ko'pi bilan {max} ta o'yin tahlil qilinadi"
CUSTOM_SELECT_ONE: Final = "Avval kamida bitta o'yinni belgilang"
CUSTOM_RUNNING: Final = "⏳ Maxsus tahlil boshlandi…"
CUSTOM_ALREADY_RUNNING: Final = (
    "⏳ Maxsus tahlil allaqachon bajarilmoqda, kuting. Qotib qolsa, /bekor yuboring."
)
RESTART_RECOVERED: Final = (
    "🔄 Bot qayta ishga tushdi, shu sababli davom etayotgan tahlil to'xtadi. "
    "Natija kelmagan bo'lsa, tahlilni qaytadan ishga tushiring."
)
CUSTOM_RESULT_HEADER: Final = (
    "🔍 <b>Maxsus tahlil</b>: {fixtures} ta o'yin · {picks} ta tanlov (EV filtrisiz)"
)
CUSTOM_FIXTURE_HEADER: Final = "{icon} <b>{fixture}</b>   🕒 {time}  🏆 {league}"
CUSTOM_PICK_LINE: Final = "{mark} {selection}: {probability} · @{odds} · EV {ev}"
CUSTOM_MARK_VALUE: Final = "✅"
CUSTOM_MARK_NO_VALUE: Final = "▫️"
CUSTOM_MARK_NEGATIVE: Final = "❗"
CUSTOM_LEGEND: Final = "✅ — chegaralaringizdan o'tgan tanlov (karta pastda), ❗ — EV manfiy."
CUSTOM_NO_VALUE_NOTE: Final = (
    "ℹ️ <b>Value topilmadi</b>: bu o'yinda hech bir tanlov chegaralaringizdan o'tmadi. "
    "Yuqorida barcha bozorlar bo'yicha ehtimolliklar."
)
CUSTOM_MORE_PICKS: Final = "…va yana {count} ta tanlov"
CUSTOM_EMPTY_RESULT: Final = (
    "🤷 <b>Natija yo'q.</b> Tanlangan o'yinlar uchun bukmeker narxi topilmadi "
    "yoki o'yin boshlangan. Boshqa o'yinni tanlab ko'ring."
)


# --------------------------------------------------------------------------- watchlist (F7.3)
WATCH_TITLE: Final = "⭐ <b>Sevimlilar</b> — {count} ta"
WATCH_EMPTY: Final = (
    "⭐ <b>Sevimlilar</b>\n"
    "Hali hech narsa kuzatilmayapti.\n"
    "👕 Jamoa yoki 🏆 ligani qo'shing, yoki <code>/kuzat Barcelona</code> deb yozing."
)
WATCH_FOOTER: Final = (
    "Sevimli jamoa yoki liga o'yini kunlik xabarga <b>har doim</b> kiritiladi "
    "(EV past bo'lsa ham, ⭐ belgisi bilan)."
)
WATCH_ITEM_LINE: Final = "{icon} <b>{name}</b> — {flags}"
WATCH_FLAGS_NONE: Final = "bildirishnomasiz"
WATCH_KIND_ICONS: Final[dict[WatchKind, str]] = {WatchKind.TEAM: "👕", WatchKind.LEAGUE: "🏆"}
WATCH_NOTIFY_LABELS: Final[dict[WatchNotification, str]] = {
    WatchNotification.LINEUPS: "📋 Tarkib",
    WatchNotification.ODDS_MOVE: "📉 Koeffitsient",
    WatchNotification.RESULT: "🏁 Natija",
}
WATCH_ITEM_TITLE: Final = (
    "{icon} <b>{name}</b> ({sport})\n"
    "Qaysi bildirishnomalar kelsin? Tugmani bossangiz yoqiladi/o'chadi."
)
BTN_WATCH_ADD_TEAM: Final = "➕ Jamoa"
BTN_WATCH_ADD_LEAGUE: Final = "➕ Liga"
BTN_WATCH_REMOVE: Final = "🗑 Olib tashlash"
BTN_WATCH_BACK: Final = "⬅️ Sevimlilarga"
BTN_WATCH_CARD: Final = "⭐ Kuzatish"
WATCH_ASK_TEAM: Final = (
    "👕 Kuzatmoqchi bo'lgan jamoa nomini yozing, masalan <code>Barcelona</code>."
)
WATCH_PICK_TEAM: Final = "👕 «{query}» bo'yicha topildi. Jamoani tanlang:"
WATCH_PICK_LEAGUE: Final = "🏆 Kuzatmoqchi bo'lgan ligani tanlang:"
WATCH_NO_LEAGUES: Final = "🏆 Liga topilmadi. Sozlamalarda sport turini tekshiring."
WATCH_NOT_FOUND: Final = "🔎 «{query}» jamoasi topilmadi. Boshqacha yozib ko'ring."
WATCH_ADDED: Final = "⭐ <b>{name}</b> sevimlilarga qo'shildi."
WATCH_REMOVED: Final = "🗑 <b>{name}</b> sevimlilardan olib tashlandi."
WATCH_GONE: Final = "ℹ️ Bu element endi sevimlilarda yo'q."
WATCH_CARD_ASK: Final = "⭐ <b>{fixture}</b> — nimani kuzatamiz?"
WATCH_CARD_ON: Final = "✅ {name} (olib tashlash)"
WATCH_CARD_OFF: Final = "⭐ {name}"
WATCH_CARD_UNKNOWN: Final = "ℹ️ Katalogda topilmadi: {names}."
WATCH_STORED: Final = "Saqlandi"
WATCH_ADDED_ALERT: Final = "⭐ Sevimlilarga qo'shildi"
WATCH_REMOVED_ALERT: Final = "🗑 Sevimlilardan olib tashlandi"


# --------------------------------------------------------------------------- templates (F7.4)
TPL_TITLE: Final = "💾 <b>Shablonlarim</b> — {count} ta"
TPL_EMPTY: Final = (
    "💾 <b>Shablonlarim</b>\n"
    "Hali shablon yo'q.\n"
    "📊 Tahlil → tasdiqlash ekranida «💾 Shablon sifatida saqlash» tugmasini bosing."
)
TPL_LINE: Final = "• <b>{name}</b> — {summary}{schedule}"
TPL_SUMMARY: Final = "{dates} · {sports} · ≥ {probability} · EV ≥ {ev}"
TPL_SCHEDULE_SHORT: Final = " · 🗓 {days} {time}"
TPL_DATES: Final[dict[str, str]] = {
    "today": "bugun",
    "tomorrow": "ertaga",
    "weekend": "dam olish kunlari",
    "next_days": "{days} kun",
    "range": "{start}–{end}",
}
TPL_DETAIL: Final = (
    "💾 <b>{name}</b>\n"
    "📅 {dates} · {sports}\n"
    "🎯 ≥ {probability} · EV ≥ {ev}\n"
    "🔎 {scope} · 📊 {markets}\n"
    "🔢 {count} · ↕️ {sort}\n"
    "🗓 Jadval: {schedule}"
)
TPL_NO_SCHEDULE: Final = "yo'q"
TPL_SCHEDULE_TEXT: Final = "{days}, {time}"
TPL_UNKNOWN_ITEM: Final = "tanlangan element"
BTN_TPL_RUN: Final = "▶️ Ishga tushirish"
BTN_TPL_SCHEDULE: Final = "🗓 Jadval"
BTN_TPL_DELETE: Final = "🗑 O'chirish"
BTN_TPL_BACK: Final = "⬅️ Shablonlarga"
BTN_TPL_DELETE_YES: Final = "🗑 Ha, o'chirish"
BTN_TPL_DELETE_NO: Final = "↩️ Yo'q"
TPL_DELETE_ASK: Final = "🗑 <b>{name}</b> shabloni o'chirilsinmi? Qaytarib bo'lmaydi."
TPL_DELETED: Final = "🗑 Shablon o'chirildi: <b>{name}</b>"
TPL_SCHEDULE_TITLE: Final = (
    "🗓 <b>{name}</b> — jadval\n"
    "Kunlarni belgilang va vaqtni tanlang (mahalliy vaqt, {zone}).\n"
    "Tanlangan: <b>{days}</b> · <b>{time}</b>"
)
TPL_SCHEDULE_NOTE: Final = (
    "ℹ️ Jadval saqlanadi, lekin avtomatik ishga tushirish hozircha ulanmagan: "
    "tahlilni shablondan qo'lda ishga tushiring."
)
TPL_NO_DAYS: Final = "kun tanlanmagan"
BTN_TPL_SAVE_SCHEDULE: Final = "💾 Saqlash"
BTN_TPL_CLEAR_SCHEDULE: Final = "🚫 Jadvalni o'chirish"
BTN_TPL_TYPE_TIME: Final = "✏️ Vaqt kiritish"
TPL_TIME_PROMPT: Final = "✏️ Jadval vaqtini yozing, masalan: <code>08:30</code>"
TPL_SCHEDULE_SAVED: Final = "🗓 Jadval saqlandi: <b>{schedule}</b>."
TPL_SCHEDULE_CLEARED: Final = "🗓 <b>{name}</b>: jadval o'chirildi."
TPL_PICK_A_DAY: Final = "Kamida bitta kunni belgilang"
TPL_RUNNING: Final = "💾 Shablon: <b>{name}</b>"


# --------------------------------------------------------------------------- bank and bets (F7.5)
BANK_UNIT: Final = "🎲 Unit: <b>{unit}</b>"
BANK_UNIT_UNSET: Final = "🎲 Unit kiritilmagan: ⚙️ Bank va limitlar."
BANK_SETTLED: Final = "📊 Yopilgan: {count} ta · ✅ {won} · ❌ {lost} · ↩️ {void} · 💸 {cashout}"
BANK_PL: Final = "💰 Foyda/zarar (yopilganlar): <b>{profit}</b>{yield_part}"
BANK_YIELD: Final = " · stavkaga nisbatan {value}"
BANK_LIMITS: Final = "🛑 Zarar limiti: bugun {today}/{daily}, hafta {week}/{weekly}"
BANK_LIMIT_NOT_SET: Final = "belgilanmagan"
BTN_BANK_OPEN: Final = "🟡 Ochiq ({count})"
BTN_BANK_SETTLED: Final = "✅ Yopilgan"
BTN_BANK_CHART: Final = "📈 Bank grafigi"
BTN_BANK_SETUP: Final = "⚙️ Bank va limitlar"
BTN_BANK_HOME: Final = "⬅️ Stavkalarim"
BANK_OPEN_TITLE: Final = (
    "🟡 <b>Ochiq stavkalar</b> — {count} ta\nNatijani belgilash uchun stavkani tanlang."
)
BANK_SETTLED_TITLE: Final = "✅ <b>Yopilgan stavkalar</b> (oxirgi {count} ta)"
BANK_SETTLED_EMPTY: Final = "Yopilgan stavkalar hali yo'q."
BANK_SETTLED_LINE: Final = "• {fixture} — {selection} @ {odds} · {stake} → {result} {profit}"
BANK_BET_TITLE: Final = (
    "🎟 <b>{fixture}</b>\n"
    "🎯 {selection} @ <b>{odds}</b> · {stake}\n\n"
    "O'yin tugagach natijani qo'lda belgilashingiz mumkin. Belgi shu signaldagi barcha "
    "stavkalarga tegadi."
)
BTN_MARK_CASHOUT: Final = "💸 Cashout"
BANK_MARKED: Final = "✅ Natija belgilandi: {result}"
BANK_MARK_REFUSED: Final = "O'yin hali boshlanmagan: natijani belgilab bo'lmaydi."
BANK_MARK_RETRO: Final = "Bu retro-tahlil signali: unga natija belgilab bo'lmaydi."
BANK_MARK_CANCELLED: Final = "Signal bekor qilingan: natijani belgilab bo'lmaydi."
BANK_CASHOUT_PROMPT: Final = (
    "💸 Bukmeker sizga to'lagan <b>summani</b> yozing (stavkangiz: {stake}), "
    "masalan <code>200</code>. Foyda = to'langan summa − stavka; "
    "to'lov bo'lmagan bo'lsa <code>0</code> yozing."
)
BANK_CASHOUT_RANGE: Final = (
    "To'langan summa 0 dan {ceiling} gacha bo'lishi kerak "
    "(stavka yutganda to'lanadigan eng ko'p summa)."
)
BANK_NO_SIGNAL: Final = "Bu stavka signalga bog'lanmagan: natijani belgilab bo'lmaydi."
BANK_NO_CHART: Final = (
    "📈 Grafik uchun bank (⚙️ Bank va limitlar) va kamida bitta yopilgan stavka kerak."
)
BANK_CHART_CAPTION: Final = "📈 Bank: {first} → {last}"
BANK_SETUP_TITLE: Final = "⚙️ <b>Bank va limitlar</b>\nQaysi qiymatni o'zgartirasiz?"
BANK_LIMIT_ASK: Final = (
    "🛑 <b>Zarar limitiga yetdingiz</b> (bugun {today}, hafta {week}).\n"
    "Qaror sizniki, lekin bugun to'xtab turish tavsiya etiladi. Baribir stavka yozasizmi?"
)
BTN_BANK_FORCE: Final = "🛑 Baribir davom etaman"
BET_LIMIT_FORCED: Final = "🛑 Davom etasiz: qaror sizniki."
BANK_NOT_FOUND: Final = "ℹ️ Bu stavka topilmadi (o'chirilgan yoki yopilgan)."
CHART_BANK_TITLE: Final = "Bank o'zgarishi (kun oxiridagi balans)"
CHART_BANK_START: Final = "boshlang'ich bank"
CHART_CALIBRATION_TITLE: Final = "Kalibratsiya: model va'dasi va haqiqat"
CHART_CALIBRATION_X: Final = "Model ehtimolligi, %"
CHART_CALIBRATION_Y: Final = "Haqiqatda o'tgan, %"
CHART_YIELD_X: Final = "Yield, %"
CHART_SEGMENTS_TITLE: Final = "Yield: {group} bo'yicha"


# --------------------------------------------------------------------------- report (F7.6)
REPORT_TITLE: Final = "📈 <b>Hisobot</b> — {period}"
REPORT_PERIOD_ALL: Final = "hammasi ({end} gacha)"
REPORT_PERIOD_RANGE: Final = "{start}–{end}"
REPORT_COUNTS: Final = (
    "📊 Signallar: <b>{signals}</b> · yopilgan {settled}: ✅ {won} · ❌ {lost} · ↩️ {void}"
)
REPORT_PROFIT: Final = "💰 Foyda: <b>{profit}</b> unit · Yield: {yield_rate}"
REPORT_ROI: Final = "🎟 ROI (sizning stavkalaringiz): {roi}"
REPORT_ROI_NONE: Final = "🎟 ROI: shu davrda stavkangiz yo'q"
REPORT_CLV: Final = "📐 CLV: {clv} · koeffitsienti yopilishdan yaxshi: {beat} signalda"
REPORT_WINRATE: Final = "🎯 Win rate: {rate} · eng uzun mag'lubiyat seriyasi: {streak}"
REPORT_NONE: Final = "—"
REPORT_EMPTY: Final = (
    "🤷 Bu davrda tavsiya qilingan signal yo'q.\n"
    "Davrni kengaytiring (30 kun, hammasi) yoki avval 📊 Tahlil / ⚡ Bugungi signallar "
    "orqali signal oling."
)
REPORT_NO_SETTLED: Final = "⏳ Signallar bor, lekin hali birortasi yopilmagan: natijalarni kuting."
REPORT_CALIBRATION_HEADER: Final = (
    "📉 <b>Kalibratsiya</b> — bot bergan foizlarga qanchalik ishonish mumkin"
)
REPORT_CALIBRATION_COLUMNS: Final = "Model ehtimolligi   n   Va'da   Haqiqat"
REPORT_CALIBRATION_SENTENCE: Final = (
    "💡 {lower}–{upper}% deb berilgan signallar haqiqatda {actual} holatda o'tdi ({count} ta)."
)
REPORT_CALIBRATION_THIN: Final = (
    "ℹ️ Ma'lumot kam: {count} ta yopilgan signal ishonchli xulosa uchun yetarli emas."
)
REPORT_SEGMENTS_HEADER: Final = "🧩 <b>Kesim bo'yicha</b> ({group})"
REPORT_SEGMENTS_COLUMNS: Final = "Kesim                     n   Foyda  Yield    CLV   Win"
REPORT_MORE: Final = "…va yana {count} ta"
REPORT_GROUP_LABELS: Final[dict[str, str]] = {
    "": "kesimsiz",
    "sport": "sport",
    "league": "liga",
    "market": "bozor",
    "risk_profile": "risk profili",
    "confidence": "ishonch",
}
BTN_REPORT_PERIODS: Final[dict[str, str]] = {
    "7": "7 kun",
    "30": "30 kun",
    "all": "Hammasi",
    "c": "📅 O'zim",
}
BTN_REPORT_CHART: Final = "📊 Grafik"
BTN_REPORT_EXPORT: Final = "📤 CSV"
REPORT_RANGE_PROMPT: Final = (
    "📅 Davrni yozing: <code>01.10 - 21.10</code> (o'tgan sanalar, bugungacha)."
)
REPORT_RANGE_FUTURE: Final = (
    "Hisobot faqat bugungacha bo'lgan sanalar uchun: tugash sanasi bugundan keyin bo'lmasin."
)
REPORT_NO_CHART: Final = "📊 Grafik uchun kamida bitta yopilgan signal (yoki kesim) kerak."
REPORT_CHART_CALIBRATION: Final = "📉 Kalibratsiya: model va'dasi va haqiqat"
REPORT_CHART_SEGMENTS: Final = "🧩 Yield: {group} bo'yicha"
EXPORT_CAPTION_SIGNALS: Final = "📤 Signallar (CSV)"
EXPORT_CAPTION_BETS: Final = "📤 Stavkalar (CSV)"


# --------------------------------------------------------------------------- express (F7.8)
EXPRESS_PICK: Final = (
    "🧩 <b>Ekspress tuzuvchi</b>\n"
    "2–4 ta signalni belgilang (har biri boshqa o'yindan). Tanlangan: {count}"
)
EXPRESS_EMPTY: Final = (
    "🧩 Ekspress uchun kamida 2 ta faol signal kerak (turli o'yinlardan).\n"
    "Avval 📊 Tahlil yoki ⚡ Bugungi signallar orqali signal oling."
)
BTN_EXPRESS: Final = "🧩 Ekspress"
BTN_EXPRESS_CALC: Final = "🧮 Hisoblash ({count})"
BTN_EXPRESS_BACK: Final = "⬅️ Tanlashga qaytish"
BTN_EXPRESS_CLOSE: Final = "❌ Yopish"
EXPRESS_SAME_MATCH: Final = "Bir o'yindagi bozorlar birlashtirilmaydi: ular o'zaro bog'liq"
EXPRESS_LIMIT: Final = "Ekspressda ko'pi bilan {max} ta tanlov bo'ladi"
EXPRESS_NEED_TWO: Final = "Kamida {min} ta tanlov belgilang"
EXPRESS_RESULT_HEADER: Final = "🧩 <b>Ekspress</b> — {count} ta tanlov"
EXPRESS_LEG: Final = "{number}. {fixture}: {selection} @ {odds} ({probability})"
EXPRESS_TOTALS: Final = (
    "📈 Umumiy koeffitsient: <b>{odds}</b>\n"
    "🎯 Umumiy ehtimollik: <b>{probability}</b> (adolatli koeff. {fair_odds})\n"
    "💹 EV: <b>{ev}</b>"
)
EXPRESS_NEGATIVE: Final = "❗ Qiymatsiz: bu ekspressning EV si manfiy."
EXPRESS_YOURS: Final = (
    "⚠️ Sizning ekspressingiz: {count} ta tanlov, har birining ehtimolligi {low}–{high} "
    "→ hammasi birdaniga o'tish ehtimolligi atigi {probability}."
)
EXPRESS_WARNING: Final = (
    "⚠️ Eslatma: 3 ta 70% lik tanlovdan tuzilgan ekspressning o'tish ehtimolligi atigi 34%. "
    "Tanlov qancha ko'p bo'lsa, o'tish ehtimolligi shuncha tez kamayadi."
)
EXPRESS_REFUSED: Final = (
    "🧩 Bu ekspressni hisoblab bo'lmaydi: signal endi tavsiya qilinmaydi, o'yin boshlangan "
    "yoki bir o'yindan ikkita tanlov bor. Ro'yxatni yangilab, qaytadan tanlang."
)


# ------------------------------------------------------------ results marking (F8.1)
BTN_RESULT_NOTE: Final = "💬 Izoh"
BTN_RESULT_MARKS: Final[dict[str, str]] = {
    "w": "✅ O'tdi",
    "l": "❌ O'tmadi",
    "v": "↩️ Void",
    "c": "💸 Cashout",
}
RESULTS_PENDING_COMMAND_TITLE: Final = "🏁 <b>Natijasi kutilayotgan signallar</b> — {count} ta"
RESULTS_PENDING_HINT: Final = (
    "Natija API dan kelmagan signallar. Bir tugma bilan belgilang; o'yin bekor qilingan "
    "bo'lsa ↩️ Void, erta yopgan bo'lsangiz 💸 Cashout."
)
RESULTS_PENDING_LINE: Final = "{number}. {fixture} — {selection} @ {odds} · {kickoff}"
RESULTS_PENDING_EMPTY: Final = "✅ Natijasi kutilayotgan signal yo'q. Hammasi aniqlangan."
RESULT_MARKED: Final = "✅ Natija belgilandi: {result}"
RESULT_MARK_NOT_STARTED: Final = "O'yin hali boshlanmagan: natijani belgilab bo'lmaydi."
RESULT_MARK_RETRO: Final = "Bu retro-tahlil signali: unga natija belgilab bo'lmaydi."
RESULT_MARK_CANCELLED: Final = "Signal bekor qilingan: natijani belgilab bo'lmaydi."
RESULT_SIGNAL_GONE: Final = "ℹ️ Bu signal topilmadi (o'chirilgan yoki boshqa foydalanuvchiniki)."
RESULT_CASHOUT_PROMPT: Final = (
    "💸 <b>{fixture}</b>\nCashout natijasini <b>unit</b> da yozing: tavsiya etilgan stavkaga "
    "nisbatan (foyda musbat, zarar manfiy), masalan <code>0.8</code> yoki <code>-0.4</code>.\n"
    "Bukmeker to'lagan summa bo'yicha yopmoqchi bo'lsangiz, 🎟 Mening stavkalarim orqali "
    "summani yozing."
)
RESULT_CASHOUT_RANGE: Final = "Cashout natijasi {low} dan {high} gacha unit bo'lishi kerak."
RESULT_NOTE_PROMPT: Final = (
    "💬 <b>{fixture}</b>\nIzohingizni yozing (eng ko'pi {limit} belgi), masalan: "
    "<i>tarkibda asosiy hujumchi yo'q edi</i>. U keyingi tahlilda kontekst sifatida ishlatiladi."
)
RESULT_NOTE_SAVED: Final = "💬 Izoh saqlandi: «{note}»"
RESULT_NOTE_TOO_LONG: Final = "Izoh juda uzun: {limit} belgidan oshmasin ({length} belgi yozildi)."
RESULT_NOTE_EMPTY: Final = "Izoh bo'sh bo'lishi mumkin emas. Matn yozing yoki /bekor."


# ------------------------------------------------------------ learning journal (F8.8)
LEARN_TITLE: Final = "🧠 <b>Model o'zgarishlari</b> — {total} ta"
LEARN_MODES: Final[dict[str, str]] = {
    "off": "⚪ O'rganish o'chirilgan: model hech narsani o'zgartirmaydi.",
    "shadow": (
        "👁 <b>Kuzatuv rejimi</b>: model nimani o'zgartirishni hisoblaydi va yozib boradi, "
        "lekin tahlilga hali qo'llamaydi."
    ),
    "apply": "✅ <b>Qo'llash rejimi</b>: ro'yxatdagi o'zgarishlar tahlilga ta'sir qiladi.",
}
LEARN_EMPTY: Final = "Hozircha o'zgarish yo'q. Model har dushanba natijalarni ko'rib chiqadi."
LEARN_KINDS: Final[dict[str, str]] = {
    "segment_raised": "📈 Segment chegarasi oshirildi",
    "segment_disabled": "⛔ Segment o'chirildi",
    "segment_relaxed": "🔓 Segment qayta yoqildi",
    "calibration_activated": "🎯 Kalibratsiya yoqildi",
    "calibration_rolled_back": "↩️ Kalibratsiya qaytarildi",
    "segment_rolled_back": "↩️ Segment qaytarildi",
}
LEARN_LINE: Final = "<b>#{id}</b> {kind} · {date}"
LEARN_DETAIL_SEGMENT: Final = "   <code>{subject}</code>{samples}{floor}"
LEARN_DETAIL_CALIBRATION: Final = "   versiya {version}{samples}"
LEARN_DETAIL_SAMPLES: Final = " · namuna {samples}"
LEARN_DETAIL_FLOOR: Final = " · EV chegarasi {floor}"
LEARN_REASON: Final = "   sabab: <code>{reason}</code>"
LEARN_STATE_APPLIED: Final = "   ✅ qo'llangan"
LEARN_STATE_SHADOW: Final = "   👁 kuzatuv rejimi: shunday qilgan bo'lardi"
LEARN_STATE_ROLLED_BACK: Final = "   ↩️ qaytarilgan"
BTN_LEARN_UNDO: Final = "↩️ Qaytarish #{id}"
BTN_LEARN_UNDO_YES: Final = "✅ Ha, qaytarish"
BTN_LEARN_UNDO_NO: Final = "⬅️ Yo'q"
BTN_LEARN_BACK: Final = "⬅️ Ro'yxatga"
LEARN_UNDO_ASK: Final = (
    "↩️ <b>#{id}</b> o'zgarishni qaytarasizmi?\n\n{line}\n\n"
    "Qaytarilgan o'zgarishni model {days} kun davomida qayta o'zgartirmaydi "
    "(qo'lda ushlab turish)."
)
LEARN_UNDO_OUTCOMES: Final[dict[str, str]] = {
    "rolled_back": "↩️ Qaytarildi. Model bu o'zgarishni {days} kun davomida qayta qo'llamaydi.",
    "not_found": "ℹ️ Bu o'zgarish topilmadi.",
    "already_rolled_back": "ℹ️ Bu o'zgarish allaqachon qaytarilgan.",
    "not_undoable": "ℹ️ Bu yozuvni qaytarib bo'lmaydi.",
    "stale": (
        "⚠️ Bu o'zgarishdan keyin holat yana o'zgargan: qaytarish yangi holatni eskisi bilan "
        "almashtirib yuborar edi, shuning uchun bajarilmadi. Avval eng yangi o'zgarishni "
        "qaytaring."
    ),
}
LEARN_SUMMARY_TITLE: Final = "🧠 <b>Model nimani o'rgandi</b> — hafta {week}"
LEARN_SUMMARY_NONE: Final = "Bu hafta model hech narsani o'zgartirmadi."
LEARN_SUMMARY_COUNTS: Final = (
    "Kalibratsiya: {calibration} · o'chirilgan segment: {disabled} · chegarasi oshirilgan: "
    "{raised} · qayta yoqilgan: {relaxed}"
)
LEARN_SUMMARY_MORE: Final = "…va yana {count} ta"
LEARN_SUMMARY_FOOTER: Final = "Batafsil va orqaga qaytarish: /ozgarishlar"
STATUS_LEARNING_HEADER: Final = "🧠 <b>Model o'rganishi:</b>"
STATUS_LEARNING_MODE: Final[dict[str, str]] = {
    "off": "• rejim: o'chirilgan",
    "shadow": "• rejim: kuzatuv (hisoblaydi, tahlilga qo'llamaydi)",
    "apply": "• rejim: qo'llash (tahlilga ta'sir qiladi)",
}
STATUS_LEARNING_JOBS: Final[dict[str, str]] = {
    "learning": "haftalik o'rganish",
    "lessons": "saboqlar",
    "postmortem": "mag'lubiyat tahlili",
}
STATUS_LEARNING_JOB_LINE: Final = "• {mark}{name} — {time}"
STATUS_LEARNING_MISSED: Final = (
    "⚠️ Haftalik o'rganish bu hafta ishlamagan (hafta yo'qolgan): model yangilanmayapti. "
    "Serverni tekshiring."
)
