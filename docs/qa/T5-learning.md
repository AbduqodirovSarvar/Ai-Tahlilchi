# QA hisoboti: T5 o'rganish halqasi (F8.5 segmentlar, F8.6 kalibratsiya)

- **Tekshirilgan diff:** `63c8035..bfc4cbb` (models/segments.py, models/calibration.py, pipeline/learning.py, overlay.py, hooklar, db/repo/learning.py, Alembic 0008, scheduler)
- **Rol:** code-reviewer + test-engineer (birlashgan o'tish). Ishlab chiqarish kodi (`src/`, `scripts/`) o'zgartirilmagan; xatolar qat'iy `xfail` testlar bilan mixlangan.
- **Qo'shilgan testlar:** `tests/models/test_learning_qa.py`, `tests/pipeline/test_learning_qa.py`, `tests/db/test_migration_0008_qa.py`
- **Xulosa:** **Darvozadan shartli o'tadi** (pastga qarang). Mexanika to'g'ri va ehtiyotkor, lekin statistik darvozalar haftalik takroriy tekshiruvlarni hisobga olmaydi: bu "shovqinga moslashish" xavfi.

## 1. Topilmalar

### 🟠 T5-QA-1 — Segment darvozasi nol-ustunlikli segmentlarni yiliga ~9–23% hollarda "tuzatadi"
- **Joy:** `src/app/models/segments.py:227-248` (`_estimate`), `:338-348` (`_significantly_negative`, `_severely_negative`), `:389-403` (`_from_active`).
- **Sabab:** har bir haftalik tekshiruv 95% (har tomon 2.5%) intervalini ishlatadi, lekin bir segment yil davomida ~30 marta (n ≥ 50 dan keyin) qayta tekshiriladi (ketma-ket qarashlar, tuzatishsiz). Foiz-bootstrap kam bloklarda intervalni torroq beradi. Segmentlar ko'p (liga × bozor × 5 oraliq), shuning uchun ko'p taqqoslash muammosi bor. CLV kanali (`or`) ikkinchi imkoniyat qo'shadi.
- **Simulyatsiya (52 hafta, koeffitsient 2.0, ROI haqiqatda 0; 20 segment, global o'rtacha umumiy):**

| Stsenariy | Signal/hafta | Segmentlar | Kamida bitta RAISE/DISABLE | 3+ marta holat o'zgargan |
|---|---|---|---|---|
| ROI = 0 | 3 | 240 | **8.7%** | 6.7% |
| ROI = 0 | 8 | 240 | **12.5%** | 9.6% |
| ROI = 0, CLV shovqini N(0; 0.05) | 3 | 240 | **23.3%** (RAISE) | 0.4% |
| ROI = 0, bitta segmentli tizim (shrinkage yo'q) | 5 | 20 | 25% | 15% |

- **Xavf:** 300 segmentli tizimda yiliga o'nlab yaxshi (aniqrog'i neytral) segmentlar o'chiriladi. Maqsad ("kam") bajarilmagan.
- **Tuzatish:** (a) 99–99.5% interval yoki alpha-spending (kuzatuvlar soniga qarab), (b) DISABLE/RAISE uchun ikki ketma-ket haftada ahamiyatlilik, (c) CLV `or` o'rniga `and` (ROI va CLV birga) yoki CLV ni faqat qo'shimcha dalil sifatida, (d) segmentlar soniga Bonferroni/FDR yoki kamida "bir haftada eng ko'pi bilan N ta o'zgarish" siyosati. Test: `test_zero_edge_segments_are_rarely_tightened_within_a_year` (strict xfail; tuzatilgach xfail olib tashlanadi).

### 🟠 T5-QA-2 — O'chirish → 60 kun → sinov → darhol qayta o'chirish sikli
- **Joy:** `segments.py:375-386` (`_from_disabled`) + `:406-421` (`_from_raised`: `_severely_negative` `_held` dan OLDIN tekshiriladi, `:409`).
- **Stsenariy:** segment DISABLED bo'ladi. 60 kundan keyin RAISED (sinov, `since=now`). Statistika esa **butun tarix** ustida (eski yomon natijalar o'chirilmagan), sinov davrida yangi ma'lumot kelmagan, shuning uchun keyingi dushanba `SEVERE_NEGATIVE` yana o'chiradi. Natija: 60 kun o'chiq, 7 kun "sinov", yana o'chiq. Sinov hech qachon baholanmaydi. Bu holat o'tkazilgan simulyatsiyada nol-ustunlikli segmentlarning 7–10% ida 3+ holat o'zgarishi sifatida ko'rindi (gisterezis maqsadiga zid).
- **Tuzatish:** sinovga qaytganda statistika oynasini `state_since` dan boshlash (yoki eski ma'lumotni vaznlash), sinov davomida faqat sinov davridagi ma'lumot bilan baholash; yoki kamida `_held` ni `DISABLE` dan oldin tekshirish.
- **Testlar:** `test_a_reenabled_segment_gets_a_real_probation_not_an_immediate_second_disable`, `test_zero_edge_segments_do_not_change_state_back_and_forth` (strict xfail).

### 🟠 T5-QA-3 — Kalibratsiya darvozasi: mukammal kalibrlangan modelga yiliga ~25% ehtimol bilan "shovqin xaritasi" beriladi
- **Joy:** `src/app/models/calibration.py:253-258` (`_gain_lower_bound`, 95% bir tomonlama), `:312-317` (`_reason`).
- **Sabab:** xuddi shu: har haftalik refit alohida testlanadi, 52 qarash. Hold-out (oxirgi 30%) har hafta siljiydi.
- **Simulyatsiya (p ∈ U(0.45; 0.75), hit-rate = p, haftalik refit, 52 hafta):**

| Haqiqiy og'ish | Signal/hafta | Faollashtirish ulushi (≤52 hafta) | Medianа hafta |
|---|---|---|---|
| 0 (mukammal) | 20 | **22.5%** | 31 |
| 0 (mukammal) | 50 | **27.5%** | 18 |
| 0.08 | 20 | 85% | 26 |
| 0.08 | 50 | 100% | 13 |
| 0.05 | 50 | 70% | 21 |
| *Taqqos:* ishonch 0.995, og'ish 0 | 20 / 50 | **2.5% / 2.5%** | |
| *Taqqos:* ishonch 0.995, og'ish 0.08 | 20 / 50 | 25% / 65% | 30 / 36 |

- **Oqibat:** zararli emas, balki foydasiz (shovqin xaritasi o'z-o'zidan Brier'ni biroz yomonlashtiradi) va undan keyin `activate` natijasida faol xarita almashadi; qo'lda qaytarilmaguncha qoladi.
- **Tuzatish:** `ONE_SIDED_CONFIDENCE` ni 0.995 ga ko'tarish (yuqoridagi taqqos: soxta ulush 2.5%), yoki ketma-ket 2 hafta o'tish talabi; shuningdek, yangi xarita faqat **hozirgi faol xaritadan** yaxshiroq bo'lsagina almashtirilsin.
- **Test:** `test_a_calibrated_model_is_almost_never_given_a_calibration_map` (strict xfail, slow). Quvvat testi (`..._overconfident_model_is_calibrated_within_a_year`) o'tadi.

### 🟠 T5-QA-4 — Push (VOID) natijalar tashlab yuboriladi: P(win) shartli chastota bilan solishtiriladi
- **Joy:** `src/app/db/repo/learning.py:27,117` (`LEARNING_OUTCOMES` VOID ni chiqaradi), `src/app/pipeline/learning.py:178-181`.
- **Stsenariy:** Total 2.0 (butun chiziq): model mukammal, P(over)=0.4, P(push)=0.2, P(under)=0.4. Push signallar tashlansa, kuzatilgan "hit" chastotasi 0.5, model esa 0.4 deydi. Kalibratsiya 0.4 → 0.5 xaritasini "o'rganadi", gate o'tadi (n ≥ ~1600 da) va barcha shunday bozorlarda ehtimollar sun'iy oshiriladi → sun'iy EV. Shu kabi ROI uchun push (0 daromad) tashlansa, |ROI| `1/(1−P(push))` marta oshiriladi.
- **Tuzatish:** kalibratsiyada push namunalarni hit=False deb emas, balki (a) butun chiziqli/push li bozorlarni kalibratsiyadan chiqarish, yoki (b) mos ehtimollik sifatida `p/(1−p_push)` solishtirish. ROI uchun push ni 0 daromad bilan hisobga qo'shish (`settlement_returns` allaqachon `PUSH` ni biladi).
- **Test:** `test_a_perfect_model_on_a_push_market_is_not_recalibrated` (strict xfail, slow).

### 🟠 T5-QA-5 — Qo'lda kalibratsiya rollback'i keyingi haftada bekor bo'ladi
- **Joy:** `src/app/pipeline/learning.py:313-363` (`_apply_calibration`), `:391-404`. Segment rollback `MANUAL_HOLD=30` kun muzlatadi, kalibratsiya esa yo'q.
- **Stsenariy:** odam faollashuvni qaytaradi; keyingi dushanba yangi ma'lumot bilan `n_samples` o'zgaradi, yangi versiya gate'dan o'tadi va yana faollashadi. (Bir xil ma'lumotda esa qayta faollashmaydi: `:324-328`.)
- **Tuzatish:** `learning_changes` dan oxirgi `calibration_rolled_back` vaqtini o'qib, `MANUAL_HOLD` davomida faollashtirmaslik (versiya saqlanadi, faqat `active` qo'yilmaydi).
- **Test:** `test_a_manual_calibration_rollback_survives_the_next_weekly_run` (strict xfail, slow).

### 🟡 T5-QA-6 — Eski o'zgarishni rollback qilish yangi faol kalibratsiyani o'chiradi
- **Joy:** `learning.py:391-404` (`_rollback_calibration`: `set_active_calibration(session, change.before["version"])`), segmentlar uchun `:407-447`.
- **Stsenariy:** v1 faollashdi (A), keyin v2 (B). A ni rollback qilish v2 ni (hali qaytarilmagan) o'chiradi va `None` qo'yadi. Segmentlarda ham eski `before` bilan yangi holat ustidan yoziladi.
- **Tuzatish:** faqat **oxirgi** qaytarilmagan o'zgarishni qaytarishga ruxsat berish (yoki `after.version` hozirgi faol versiyaga teng bo'lishini tekshirish).
- **Test:** `test_rolling_back_an_old_activation_does_not_switch_off_a_newer_one` (strict xfail, slow).

### 🟡 T5-QA-7 — Buzilgan `params` (dict emas) overlay qaytish mexanizmidan o'tib ketadi
- **Joy:** `src/app/pipeline/overlay.py:93` (`record.params.get`), `:133` (`except` ro'yxatida `AttributeError` yo'q).
- **Stsenariy:** JSON ustunda ro'yxat yoki satr bo'lsa, `AttributeError` ko'tariladi, `analyze.py` da `ctx.learning.load()` try'dan tashqarida, butun tahlil yiqiladi. Hujjatda va'da: "o'rganish muammosi tahlilni to'xtatmasligi kerak".
- **Tuzatish:** `AttributeError` (yoki umuman `Exception` + log) ni ushlash yoki `isinstance(params, dict)` tekshiruvi.
- **Test:** `test_a_calibration_row_with_non_dict_params_falls_back_to_the_raw_model` (strict xfail, `raises=AttributeError`).

### 🟡 Boshqa eslatmalar (testlanmagan yoki hujjatlashtirilgan)
- **Retro tahlil:** `analyze.py:297` retro kartalarga ham bugungi segment/kalibratsiya qo'llanadi (o'rganish ma'lumotiga retro kirmaydi, shuning uchun ifloslanish yo'q, lekin "o'tmishdagi model nima derdi" ko'rsatkichi kelajak bilimi bilan aralashadi).
- **Haftalik job muvaffaqiyatsizlikdan keyin:** ledger backoff dan keyin qayta uriniladi, lekin cron faqat haftada bir marta ishlaydi, demak vaqtinchalik xatoda hafta yo'qoladi (job tick'siz). `/holat` da ko'rsatilishi kerak.
- **`passed_gate`:** `active_calibration` (`db/repo/learning.py:224`) `passed_gate` ni tekshirmaydi; 0008 dan oldin faol bo'lgan qator (hozirda yo'q) `passed_gate=0` bilan ham ishlatilardi (migratsiya testida ko'rsatilgan).
- **Shrinkage va interval:** interval shrunk nuqtaviy bahoning bootstrapi (`segments.py:242-248`), ya'ni orqa taqsimot oralig'i emas. Global o'rtacha `g` manfiy bo'lsa (bukmeker marjasi, ~−4%) hamma segmentlar uchun `high < 0` osonlashadi. Bu mutlaq mezonning natijasi (haqiqatda manfiy ROI), lekin ADR da aytilmagan.
- **Prior kuchi k=30:** asosli (50 signalda 62.5% o'z o'rtachasi), lekin ROI dispersiyasi (koeffitsient 3.0 da sd ≈ 1.4, n=50 da se ≈ 0.2) bilan solishtirganda shrinkage hal qiluvchi emas; qaror intervalga bog'liq. Hujjatlashtirish yetarli.
- **Global qamrov:** o'rganish barcha foydalanuvchilar bo'yicha global (bir hodisa = eng kichik `signal_id`), ADR 0013 §1 da yozilgan; bir foydalanuvchili bot uchun mantiqli. Foydalanuvchi o'z `min_ev` dan past bo'lmagan segment chegarasi: `max` ishlatiladi (`selection.py:52` + `overlay.allows`), to'g'ri.

## 2. QA mezonlari

| # | Mezon | Natija | Dalil |
|---|---|---|---|
| 1 | Shrinkage formulasi qo'lda hisobga mos | ✅ | `test_shrinkage_and_roi_hand_computed_example`: A=−0.15625, B=−0.09375 |
| 2 | Interval ma'lumot diapazonida, tartiblangan | ✅ | hypothesis `test_interval_never_lies_outside_the_data_range` (60 misol) |
| 3 | Bootstrap deterministik va kiritish tartibiga bog'liq emas | ✅ | mavjud `test_stats_are_deterministic_and_order_independent`, seed = (policy.seed, crc32(kalit)); blok = o'yin kuni (UTC), to'g'ri birlik |
| 4 | Min namuna (50 / 200 / hold-out 60) | ✅ | mavjud testlar; kodda `segments.py:443`, `calibration.py:281` |
| 5 | Streak yaxshi segmentni o'chirmaydi | ✅ | mavjud `test_a_bad_streak_in_a_good_segment_triggers_nothing` |
| 6 | Nol-ustunlikli segment soxta harakati past | ❌ | 8.7–12.5% (CLV bilan 23%), T5-QA-1 |
| 7 | Haqiqatan manfiy segment oqilona muddatda topiladi | ✅ | ROI −20%: 92% segment, median 17-hafta (3 signal/hafta); ROI −10%: 52% (3/hafta) va 81% (8/hafta), median 15–20 hafta; ROI −5%: 37% (5/hafta) |
| 8 | Gisterezis: haftama-hafta chayqalish yo'q | ❌ | 3+ o'zgarish 7–10%, T5-QA-2 |
| 9 | Ko'p taqqoslash uchun tuzatish | ❌ | yo'q (na Bonferroni, na ketma-ket alpha), T5-QA-1/3 |
| 10 | PAV to'g'riligi (sklearn oracle) | ✅ | 25 ta tasodifiy to'plam (n ≤ 300, takroriy `p`), qadam balandliklari `IsotonicRegression` bilan mos; `test_isotonic_blocks_match_the_sklearn_oracle` |
| 11 | Walk-forward, sizib chiqish yo'q | ✅ | `test_perturbing_anything_settled_at_or_after_the_cutoff_changes_nothing` (5 seed, cutoff dagi namuna ham chiqarilgan); hold-out yorliqlarini teskari qilish darvozani yiqitadi: `test_the_holdout_labels_decide_the_gate_not_the_fit` |
| 12 | Darvoza (bir tomonlama CI) to'g'ri hisoblanadi | ✅ (bitta qarashda) / ❌ (52 qarashda) | kod `calibration.py:253-258` to'g'ri; ketma-ket qarashlarda soxta faollashuv 22–27%, T5-QA-3 |
| 13 | [0.01; 0.99] qisish, komplementar renormalizatsiya | ✅ | mavjud testlar + hypothesis |
| 14 | Osiyo natija qayta masshtablash | ✅ | yangi hypothesis `test_asian_rescale_hits_the_target_and_keeps_a_distribution` (80 misol): yig'indi 1, win+half_win = maqsad |
| 15 | `p_raw` ikki marta kalibrlanmaydi | ✅ | o'rganish `p_raw ?? p_model` o'qiydi (`repo/learning.py:144`); mavjud `test_calibration_trains_on_raw_probabilities...`; recalc `p_raw` ni saqlaydi (diff `recalc.py`) |
| 16 | Push natijalar to'g'ri hisoblanadi | ❌ | T5-QA-4 |
| 17 | Rollback to'g'ri va jurnalga yoziladi | ✅ (oxirgi o'zgarish) / ❌ (eski o'zgarish, qo'lda qaytarish saqlanishi) | mavjud testlar; T5-QA-5/6 |
| 18 | Segment filtri faqat avtomatik tavsiyalarga | ✅ | kod `analyze.py:290-298` (`include_no_value` yoki `watched` bo'lsa filtrsiz); mavjud custom testi. Kuzatuv (watched) holati faqat kod o'qish bilan tasdiqlandi |
| 19 | `learning.load()` xatosi xom modelga qaytaradi | ✅ (SQL/validatsiya xatolari) / ❌ (`AttributeError`) | mavjud 2 test; T5-QA-7 |
| 20 | Haftalik job idempotent | ✅ | `test_two_simultaneous_runs_in_one_week_do_exactly_one_review` (gather: bittasi `skipped`); mavjud takroriy test |
| 21 | Tranzaksiya: o'rtada to'xtasa qisman yozuv yo'q, qayta urinish mumkin | ✅ | `test_a_crash_after_the_segments_leaves_no_partial_week_and_can_be_retried` (segment/jurnal/versiya bo'sh; backoffdan keyin muvaffaqiyatli) |
| 22 | Retro chiqarilgan, yashirin kiritilgan, tavsiya koeffitsienti | ✅ | mavjud test + `test_learning_uses_the_recommended_odds_not_the_current_ones` (2.5 olinadi, `None` bo'lsa 1.9) |
| 23 | Dushanba 09:00 Asia/Tashkent; qayta ishga tushirishda bir marta ro'yxatga olinadi | ✅ | `test_the_learning_job_is_registered_once_across_restarts` (3 hayot sikli, bitta job-store; 5 ta job id, `learning` = dushanba 09:00 mahalliy); UTC 04:00 = bir xil sana, 52 hafta uchun kalitlar noyob |
| 24 | Alembic 0008 up/down ma'lumot bilan, `alembic check` | ✅ | `tests/db/test_migration_0008_qa.py`: segment (o'chirilgan) va faol kalibratsiya saqlanadi, `passed_gate=0`, `check` toza, down/up qayta |

## 3. Simulyatsiya uslubi
- Segment: koeffitsient 2.0, `p` ∈ {0.5; 0.475; 0.45; 0.4}, 52 haftalik `compute_segment_stats` + `segment_action` (holat `since` bilan yuritiladi), blok = tasodifiy kun, 20 segment bir dunyoda (umumiy `g`), 3–12 dunyo. Skriptlar vaqtinchalik, repoga kiritilmagan; qayta ishlab chiqarish: `tests/models/test_learning_qa.py::_simulate_segments` va `_calibration_world`.
- Repoga kiritilgan testlar kichraytirilgan (4 dunyo × 20 segment; 20 / 8 dunyo kalibratsiya) va `@pytest.mark.slow`.
- Cheklov: simulyatsiya soddalashtirilgan (barcha segmentlar bir xil `n`, mustaqil, bir koeffitsient); real tizimda signallar kam bo'lsa soxta ulush pasayadi (kam qarash), lekin quvvat ham pasayadi.

## 4. Tavsiya etilgan tuzatish tartibi
1. T5-QA-4 (push) va T5-QA-7: kichik, mahalliy tuzatishlar.
2. T5-QA-2 (sinov davri statistikasi) va T5-QA-5 (kalibratsiya hold).
3. T5-QA-1/3: bardoshli parametrlar (99.5% / ketma-ket 2 hafta) va tuzatishdan keyin simulyatsiya testlaridan `xfail` ni olib tashlash.

## 5. Yakuniy xulosa
**Darvozadan shartli o'tadi.** Matematik yadro (PAV, shrinkage formulasi, sizib chiqish himoyasi, `p_raw`, idempotentlik, migratsiya, scheduler) to'g'ri va mustaqil oracle bilan tasdiqlangan. Lekin T5-QA-1…QA-5 sababli tizim real foydalanishda shovqin ustiga harakat qilishi mumkin (nol-ustunlikli segmentlarning ~9–23% i bir yilda tuzatiladi; mukammal modelga ~25% ehtimol bilan soxta xarita beriladi; push bozorlarda sun'iy "kalibratsiya"). Ular yumshoq ishlaydi (kamida 50 signal, EV chegarasi ko'tariladi, rollback bor), shuning uchun merge to'sig'i emas, lekin T5-QA-4 (push) va T5-QA-1/3 (alpha) ni ishga tushirishdan oldin tuzatish tavsiya etiladi.

## 6. To'liq darvoza (shu branchda)
- `pytest --cov=app --cov-branch --cov-fail-under=85`: **2884 passed, 8 xfailed** (9m24s, chiqish kodi 0; 8 xfail = T5-QA-1…7 ning mixlangan testlari).
- `ruff check .`: toza. `ruff format --check .`: 342 fayl toza. `mypy src`: 162 fayl, xato yo'q.
- `git diff main -- src scripts`: bo'sh (ishlab chiqarish kodi o'zgarmagan).
- Sekin testlar (`@pytest.mark.slow`): segment va kalibratsiya simulyatsiyalari, push va rollback integratsiya testlari.
