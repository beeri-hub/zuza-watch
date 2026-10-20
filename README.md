# zuza-watch — השומר החיצוני של ZUZA

ריפו ציבורי קטן שמריץ כל שעתיים (GitHub Actions, חינם לריפו ציבורי) בדיקה שהאתרים
zuzadoor.com, beeritzori.com ו-u4balance.com עונים, ושהשומר הראשי (Apps Script) חי.
אין כאן שום דבר עסקי. המקור של הקבצים: `beeri-hub/zuza-hq` (`scripts/external_watch.py`,
`scripts/site_smoke.py`, `scripts/site_smoke.json`, `calendar/holidays.json`) — מי שמשנה שם, מעתיק לכאן.

Secrets (מגדירים פעם אחת): `WATCHDOG_URL`, `WATCHDOG_KEY`. בלעדיהם נבדקים רק האתרים.

## שומר התשלום (20.10.2026)
`.github/workflows/checkout-guard.yml` + `scripts/checkout_guard.py`, כל ~10 דקות: פותח את עמודי התשלום של Hyp
שהאתר משתמש בהם (בלי לשלם) וכותב `status.json`. האתר קורא אותו (`/.netlify/functions/watch-status`) ומחליף
כפתור תשלום שבור בהודעה "ההזמנות מושהות לכמה שעות" + שדה מייל. עובד שוב פעמיים ברצף → הכפתור חוזר.
כל שינוי = מייל לבארי ול-office@ דרך `/.netlify/functions/watch-alert` באתר (מאומת ב-OIDC של GitHub, בלי סוד).
ידנית: ב-`status.json` לשנות `"kit"`/`"gift"` ולהוסיף `"manual": true`. `status.json` נכתב רק על ידי השומר — לא לערוך בזמן ריצה.
