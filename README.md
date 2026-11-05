# Khaled 3.1.0 — Binance USDT-M Futures Demo

نسخة المصدر الموحدة على `main`، انطلاقًا من `ced0bcc` (3.0.3). **مسار التشغيل المعتمد هنا هو Demo فقط: `run_demo.py`.** يرفض Mainnet والعناوين غير المعتمدة. لم يُعدّل مستودع Final، ولم يُشغّل تداول أثناء التطوير.

الإصلاح مثبت بمحاكاة التنفيذ والاستعادة، وليس إعلان جاهزية حسابك أو ضمان ربح/حماية من فجوات السعر. [تقرير السبب المثبت والاختبارات والقيود](reports/DEMO_EXECUTION_3_1_0.md) و[سجل التغييرات](CHANGELOG.md).

## تثبيت جديد داخل Ubuntu على Termux

أوقف النسخة القديمة قبل تشغيل الجديدة على الحساب نفسه. **احتفظ بالمجلد القديم وruntime وسجل الأخطاء؛ لا تحذفهما ولا تستبدلهما.** لا تنسخ حالتهما عميانيًا إلى الإصدار الجديد. لا تشغّل تثبيتين على الحساب نفسه؛ قفل الملفات يمنع ازدواج العملية داخل مجلد الحالة نفسه فقط.

داخل Ubuntu في PRoot، باستخدام Python 3.11 أو أحدث:

```bash
apt update
apt install -y git python3 python3-venv
mkdir -p ~/khaled-installations
cd ~/khaled-installations
git clone --branch main --single-branch https://github.com/khaledhasneen1993/Khaled.git Khaled-3.1.0
cd Khaled-3.1.0
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

اترك `baseline.json` كما هو. القيمة الافتراضية `paper` تحفظ توافق أدوات Paper واللوحة؛ `run_demo.py` يختار `testnet` في ذاكرة عمليته فقط، دون تعديل الملف. `testnet` في الكود يعني Binance Futures **Demo** بالعناوين أدناه. لا تستخدم أوامر إعداد Mainnet أو مفاتيحه.

## الاتصال والمصادقة

**ألغِ مفتاح Demo الذي ظهر في الصورة، وأنشئ بديلًا قبل التشغيل الجديد.** لا ترفع مفتاحًا خاصًا أو رموز Telegram للمحادثة أو Git أو baseline أو السجلات.

أدخل المتغيرات محليًا دون وضع قيمها في سجل أوامر Bash:

```bash
read -r -s -p 'Demo API key: ' BINANCE_API_KEY; echo
export BINANCE_API_KEY
read -r -s -p 'Telegram bot token: ' TELEGRAM_BOT_TOKEN; echo
export TELEGRAM_BOT_TOKEN
read -r -s -p 'Telegram chat ID: ' TELEGRAM_CHAT_ID; echo
export TELEGRAM_CHAT_ID
```

اختر **طريقة توقيع واحدة فقط**. لـRSA، استخدم ملف RSA PEM بصيغة PKCS#8 مرتبطًا بالمفتاح العام المسجل في Binance Demo:

```bash
unset BINANCE_API_SECRET
export BINANCE_RSA_PRIVATE_KEY_PATH=/root/.binance-demo-keys/private.pem
chmod 600 "$BINANCE_RSA_PRIVATE_KEY_PATH"
```

أو لـHMAC:

```bash
unset BINANCE_RSA_PRIVATE_KEY_PATH
read -r -s -p 'Demo HMAC secret: ' BINANCE_API_SECRET; echo
export BINANCE_API_SECRET
```

يجب إعادة توفير متغيرات البيئة في جلسة التشغيل الجديدة. يمكن حفظها محليًا بملف خارج المستودع بصلاحية 0600 إن اخترت ذلك؛ لا تنشره. Telegram إلزامي للتشغيل، ويُختبر تسليم رسالة البدء قبل السماح بالدخول.

## الفحص ثم التشغيل يدويًا

استخدم حساب Demo مخصصًا، one-way وsingle-asset USDT. لا تشاركه مع تداول يدوي أو بوت آخر؛ المصالحة تفحص الحساب كله، ويمكن لمسار الأمان إغلاق المراكز غير المعروفة وإلغاء الأوامر اليتيمة عند التشغيل.

بعد مراجعة صلاحيات Futures وضوابط IP وتعطيل/عدم إتاحة السحب وتدوير المفتاح، سجّل مراجعتك محليًا مرة واحدة:

```bash
python tools/demo_setup.py
python tools/demo_preflight.py
```

الفحص يستخدم **GET فقط** واشتراك بيانات سوق WebSocket؛ لا يضبط رافعة ولا يرسل أمرًا ولا رسالة Telegram. يفحص الاتصال والتوقيع والرصيد الحالي ووضع الحساب وقراءة المراكز والأوامر العادية وAlgo والتنفيذات، ويعرض أعلى رافعة مرشحة للرمز والحجم. يمكن اختيار رمز آخر عبر `--symbol HUMAUSDT`.

نجاح القراءة لا يثبت صلاحية TRADE أو قبول الرافعة أو SL/TP؛ هذه الحدود تظهر صراحة في النتيجة. لا نستخدم `/sapi/v1/account/apiRestrictions` لإثبات الصلاحيات؛ نتيجة 404 على Demo ليست إثباتًا ولا استثناءً لـMainnet. إذا ظهرت مراكز/أوامر أو Kill Switch أو نيات معلقة، راجعها قبل البدء؛ نتيجة «صفر» التاريخية لا تصف الحساب الآن.

**الأمر التالي يبدأ التداول التجريبي فعليًا عندما تنفذه أنت:**

```bash
python -u run_demo.py
```

الاستئناف بالأمر نفسه يستعيد `runtime/testnet/state.json` والنيات وسجل التنفيذ والحماية. Ctrl+C يحاول الإغلاق الآمن ويحفظ قفل الأمان؛ لا يُصفّر القفل تلقائيًا. لا يُحذف runtime عند الإيقاف أو الخطأ. عند اختلاف إعدادات حالة قديمة، يلزم فحص/ترحيل واعٍ، وليس `--reset`.

بعد مراجعة حادث والتأكد من حالة Binance الحقيقية وخلو الحالة المطلوبة من المراكز، توجد أداة المراجعة اليدوية القديمة:

```bash
python tools/reset_safety.py --state runtime/testnet/state.json --reviewed
```

لا تشغّلها لتجاوز نية أمر مجهولة أو محاسبة غير مكتملة. لا تمسح `order_intents.json` أو `executions.json` أو سجلات الحوادث.

## السلوك المعتمد

| البند | التطبيق |
|---|---|
| الاستراتيجية | Cascade ±1.2%، volume spike 3.5، imbalance ±0.35، cooldown 8 ثوانٍ؛ لم تُضف فلاتر إشارة |
| الحجم | هامش مستهدف 10% من **equity الحقيقي الحالي** للحساب، أيًّا كان الرصيد؛ حتى 3 مراكز |
| الرافعة في Demo | أعلى عدد صحيح صالح وفق شرائح حساب الرمز والقيمة الاسمية بعد تقريب الكمية، مع تأكيد POST للرافعة وحد القيمة الاسمية؛ محفوظة لكل مركز |
| التعرض | مجموع هوامش المراكز برافعتها الفردية؛ حدود الاتجاه/الارتباط وباقي الحمايات محفوظة |
| ATR | period 14، stop ×1.5، TP ×2.5، min stop 0.008، min RR 1.5 |
| المخاطرة/التصفية | ميزانية مخططة 3% equity مع الرسوم؛ MMR وخصم الصيانة من الشريحة، buffer 0.003، وفحص سعر تصفية Binance الفعلي |
| Breakeven/Trailing | trigger 0.006، offset 0.001 أو تغطية الرسوم وفق المنطق الحالي، trailing ×1.2 ATR؛ الوقف لا يتراجع والإغلاق عبر مسار الخروج |
| Time Stop/التمويل | 45 دقيقة إذا الربح السعري أقل من 0.002؛ حماية التمويل الحالية محفوظة |
| SL/TP | أوامر Binance Algo reduce-only، مؤكدة؛ البديل يُنشأ ويُؤكد ويُحفظ قبل إزالة القديم |

`min_stop_pct` هو حد مسافة ATR الخام قبل قيود الأمان. قد تضيق المسافة عند الرافعة القصوى بسبب التصفية أو ميزانية الرسوم والمخاطرة؛ لا تُخفّض الحماية لزيادة الصفقات. يشرح التقرير التصحيح الرياضي لنموذج التصفية وخصم الصيانة. قد تُرفض صفقة إذا لم توجد مسافة وقف موجبة صالحة أو لم يسمح الهامش/حد Binance الأدنى بها؛ لا يزيد البوت الحجم لتجاوز الحد الأدنى. قيمة 10× العامة باقية لمحاكاة Paper والتوافق فقط، ولا تُستخدم لاختيار رافعة Demo أو إدارة جميع مراكزه.

## التنفيذ والاستعادة والمحاسبة

تُحفظ النية ذريًا قبل POST، ثم معرّفات الأمر واستجابته، ثم حقيقة التنفيذ. إذا ضاعت الاستجابة يُستعلم عن **نفس client order ID**؛ الغياب المؤقت لا يسمح بإعادة الدخول. لا يوجد وعد exactly-once للشبكة. قبل إعادة إغلاق مركز يُحسم الأمر السابق وتُقرأ الكمية المتبقية الفعلية؛ يبقى الغموض غير المحسوم مقفولًا للمراجعة.

تأكيد FILLED مستقل عن اكتمال `userTrades`. المركز المؤكد يُحفظ ويُحمى، وتبقى المحاسبة `pending` حتى تتطابق الكميات والرسوم والربح الحقيقي. القيم الحسابية المؤقتة في حقول توافق اللوحة ليست نتائج نهائية؛ لا يُصدر `exit_detail` نهائي قبل اكتمال بيانات الدخول والخروج. يُعاد جلب الصفحات وفق حدود baseline، وتُحفظ المجاميع العشرية الدقيقة دون مضاعفة السجل.

سجل `executions.json` مفهرس بالرمز ومعرّف أمر Binance، ويحفظ معلومات التنفيذ والمركز والمحاسبة. `state.json` يحفظ أيضًا بيانات الاستراتيجية، الإشارات، الوقف المتحرك والقفل. سجلات التشخيص تسمح بحقوق stage، order/client ID، نوع الخطأ، HTTP/code وإجراء الاستعادة؛ لا تُسجّل مفاتيح أو URLs موقّعة أو أجسام استثناءات.

غياب مركز من Binance لا يحذف بياناته المحلية قبل إسناد الخروج. فشل الحماية يمنع الدخول ويستخدم مسار الطوارئ؛ إذا تعذّر الاتصال أو الإغلاق تبقى الحماية المتاحة والقفل. لا ضمان ضد فجوات السعر والانزلاق والانقطاع أو اختلاف mark price وسعر تشغيل الوقف. التمويل ينعكس في equity، ولا يُوزّع حاليًا على صافي سجل كل صفقة. الرسوم بعملة غير USDT أو بيانات التنفيذ الناقصة تبقى معلقة للمراجعة.

## استعادة HUMA التاريخية دون أوامر تداول

لا تعتمد الأداة على أرقام التقرير كحالة حساب حالية. تقرأ أمر الدخول `247831476` والإغلاق `247831491` وتنفيذاتهما من Binance وتتحقق من الربح المؤكد `-1.06663200 USDT`. إذا تعارضت البيانات تتوقف؛ لا تغيّر الربح لتلائم نموذجًا محليًا.

```bash
python tools/recover_huma.py
```

راجع JSON المعروض وبصمة `preview_sha256`، ثم إن كانت النتيجة صحيحة نفّذ:

```bash
python tools/recover_huma.py --apply --expected-sha256 البصمة_المعروضة
```

الاستيراد المحلي إلى `runtime/testnet/recovered_history.json` لا يكرر السجل، ولا يمس Kill Switch أو حالة الاستراتيجية. ATR وTP/SL والرافعة والهامش التاريخية غير المحفوظة تبقى `null`. التاريخ المستورد منفصل عن دفتر مخاطرة التداول الجديد ومقاييسه، ولا يضاف مرة ثانية إلى PnL الحساب.

## اللوحة وPaper والاختبارات

واجهات `V2PaperHunter` و`PaperExchange` والناشر الحالي باقية. لتشغيل Demo مع اللوحة الحالية، وفّر `CASCADE_DASHBOARD_TOKEN` محليًا ثم استخدم `python -u run_demo.py --dashboard`. لا تغيّر هوية `khaled` أو أصول اللوحة.

`python -u run_bot.py` يبقى المسار القديم بحسب baseline (Paper افتراضيًا). أداة المقارنة `tools/run_comparison_dashboard.py --final-dir /مسار/Final` باقية للمقارنة الورقية فقط دون تعديل Final؛ ليست مشغّل Demo، ولا يلزم تشغيلها لتداول Demo.

```bash
python -m pip install pytest
python -m pytest -q
python tools/audit_secrets.py
```

آخر نتيجة موثقة: **121 اختبارًا و9 اختبارات فرعية**. كل الاختبارات تستخدم محاكاة دون مفاتيح حقيقية؛ اتصالات الشبكة محظورة داخل الاختبارات، وإعداد Paper معزول دون تعديل baseline على القرص.

## عقود Binance الرسمية التي روجعت

- [REST Demo والتوقيع HMAC/RSA ومعنى حالة الإرسال المجهولة](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info).
- [شرائح القيمة الاسمية والرافعة وMMR](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account).
- [أوامر السوق وAlgo والاستعلام والإلغاء والتنفيذات](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade).
- [مسار سوق WebSocket للشموع](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market).

REST المعتمد `https://demo-fapi.binance.com`، والشموع عبر `wss://demo-fstream.binance.com/market/stream`. يُرسل التنفيذ عبر REST؛ WebSocket هنا لبيانات السوق، وليس WebSocket order API. التحقق الفعلي من حساب Demo بعد تدوير المفتاح ومن قبول أوامره ما زال مطلوبًا من المشغّل؛ لم تُنفّذ أوامر لهذا الغرض أثناء التطوير.
