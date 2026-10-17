# zuza-watch — השומר החיצוני של ZUZA

ריפו ציבורי קטן שמריץ כל שעתיים (GitHub Actions, חינם לריפו ציבורי) בדיקה שהאתרים
zuzadoor.com, beeritzori.com ו-u4balance.com עונים, ושהשומר הראשי (Apps Script) חי.
אין כאן שום דבר עסקי. המקור של הקבצים: `beeri-hub/zuza-hq` (`scripts/external_watch.py`,
`scripts/site_smoke.py`, `scripts/site_smoke.json`, `calendar/holidays.json`) — מי שמשנה שם, מעתיק לכאן.

Secrets (מגדירים פעם אחת): `WATCHDOG_URL`, `WATCHDOG_KEY`. בלעדיהם נבדקים רק האתרים.
