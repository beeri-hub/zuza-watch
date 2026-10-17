#!/usr/bin/env python3
"""שומר חיצוני של ZUZA — רץ ב-GitHub Actions (.github/workflows/watchdog.yml).

Second, independent safety layer ("who watches the watchdog"):
  a) skips quietly during the Shabbat / Yom Tov freeze (Asia/Jerusalem);
  b) checks that the Apps Script "ZUZA Watchdog" is alive
     (env WATCHDOG_URL + WATCHDOG_KEY; missing -> warning only);
  c) runs the site smoke checks (scripts/site_smoke.py) for zuza, beeri, u4b;
  d) writes a Hebrew summary to $GITHUB_STEP_SUMMARY.
Exit 1 on any failure -> GitHub emails the repo owner.

Usage:
  python3 scripts/external_watch.py [--at 2026-10-16T16:00]   (--at = Israel local time, for tests)
Python 3 stdlib only. The key is never printed.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import site_smoke  # noqa: E402

TZ = ZoneInfo("Asia/Jerusalem")
ROOT = os.path.dirname(HERE)
CAL = os.path.join(ROOT, "calendar", "holidays.json")
FREEZE_START = time(15, 0)
FREEZE_END = time(21, 0)
MAX_AGE_MIN = 45          # watchdog runs every 10 min; older than this = it stopped
POST_FREEZE_GRACE_MIN = 60
GROUPS = ["zuza", "beeri", "u4b"]
IS_GHA = os.environ.get("GITHUB_ACTIONS") == "true"


# ---------- freeze ----------
def freeze_windows(now, notes):
    """All freeze windows near `now`: (start, end, label)."""
    wins = []
    d = now.date()
    for back in (0, 7):  # this week's and last week's Shabbat
        friday = d - timedelta(days=(d.weekday() - 4) % 7 + back)
        wins.append((datetime.combine(friday, FREEZE_START, TZ),
                     datetime.combine(friday + timedelta(days=1), FREEZE_END, TZ), "שבת"))
    try:
        with open(CAL, encoding="utf-8") as f:
            items = json.load(f)["items"]
        days = sorted({date.fromisoformat(i["date"]): i.get("hebrew") or i.get("title", "חג")
                       for i in items if i.get("yomtov")}.items())
        run = []
        for day, name in days + [(None, None)]:
            if run and (day is None or day != run[-1][0] + timedelta(days=1)):
                wins.append((datetime.combine(run[0][0] - timedelta(days=1), FREEZE_START, TZ),
                             datetime.combine(run[-1][0], FREEZE_END, TZ), run[0][1]))
                run = []
            if day is not None:
                run.append((day, name))
    except (OSError, ValueError, KeyError) as e:
        notes.append("⚠️ לא הצלחתי לקרוא את לוח החגים (calendar/holidays.json): %s — בודק רק שבת" % e)
    return wins


def frozen_now(now, wins):
    for s, e, label in wins:
        if s <= now < e:
            return label
    return None


def minutes_since_freeze_end(now, wins):
    ended = [e for s, e, _ in wins if e <= now]
    return (now - max(ended)).total_seconds() / 60 if ended else None


# ---------- watchdog ----------
def check_watchdog(now, wins):
    """Returns (fail_msgs, warn_msgs, info_lines)."""
    url = os.environ.get("WATCHDOG_URL", "").strip()
    key = os.environ.get("WATCHDOG_KEY", "").strip()
    if not url or not key:
        return [], ["⚠️ השומר הראשי (Apps Script) עוד לא מחובר — חסרים WATCHDOG_URL/WATCHDOG_KEY. "
                    "בודק רק את האתרים."], []

    def clean(s):
        s = str(s)
        return s.replace(key, "***").replace(urllib.parse.quote(key, safe=""), "***")

    sep = "&" if "?" in url else "?"
    full = "%s%sk=%s&f=json" % (url, sep, urllib.parse.quote(key, safe=""))
    req = urllib.request.Request(full, headers={"User-Agent": site_smoke.UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return ["השומר הראשי לא עונה (קוד %d)" % e.code], [], []
    except Exception as e:
        return ["השומר הראשי לא עונה (%s)" % clean(e)[:150]], [], []
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except ValueError:
        return ["השומר הראשי ענה בתשובה לא תקינה (לא JSON) — אולי נדרשת התחברות או שהפריסה נשברה"], [], []

    fails, warns, info = [], [], []
    age = data.get("ageMin")
    info.append("השומר הראשי: ריצה אחרונה %s, לפני %s דקות%s" % (
        clean(data.get("lastRun", "לא ידוע")), age if age is not None else "?",
        " (במצב הקפאה)" if data.get("frozen") else ""))
    try:
        age_f = float(age)
    except (TypeError, ValueError):
        age_f = None
    if age_f is None:
        fails.append("השומר הראשי לא מדווח מתי רץ לאחרונה (ageMin חסר)")
    elif age_f > MAX_AGE_MIN:
        since = minutes_since_freeze_end(now, wins)
        msg = "השומר הראשי הפסיק לרוץ — הריצה האחרונה לפני %d דקות" % int(age_f)
        if since is not None and since < POST_FREEZE_GRACE_MIN:
            warns.append("⚠️ " + msg + " (סוף הקפאה לפני %d דקות — לא נחשב תקלה עדיין)" % int(since))
        else:
            fails.append(msg)

    def fmt(x):
        if isinstance(x, dict):
            return clean(x.get("name_he") or x.get("name") or x.get("url") or json.dumps(x, ensure_ascii=False))
        return clean(x)

    for d in data.get("down") or []:
        warns.append("⚠️ השומר הראשי מדווח שנפל: %s" % fmt(d))
    for w in data.get("warn") or []:
        warns.append("⚠️ השומר הראשי מזהיר: %s" % fmt(w))
    if data.get("beats"):
        info.append("דופק: %s" % clean(json.dumps(data["beats"], ensure_ascii=False))[:300])
    return fails, warns, info


# ---------- main ----------
def parse_now(argv):
    if "--at" in argv:
        i = argv.index("--at")
        dt = datetime.fromisoformat(argv[i + 1])
        return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
    return datetime.now(TZ)


def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def main(argv):
    now = parse_now(argv)
    stamp = now.strftime("%d.%m.%Y %H:%M")
    notes = []
    wins = freeze_windows(now, notes)
    label = frozen_now(now, wins)
    if label:
        msg = "⏸️ הקפאה (%s) — %s שעון ישראל. לא בודק כלום, יוצא בשקט." % (label, stamp)
        print(msg)
        write_summary(["## שומר חיצוני של ZUZA", "", msg])
        return 0

    w_fail, w_warn, w_info = check_watchdog(now, wins)
    results = []
    for g in GROUPS:
        results += site_smoke.run(g)
    site_fail = [r for r in results if not r["ok"]]
    fails = w_fail + ["%s: %s" % (r["name_he"], r["reason"]) for r in site_fail]

    if fails:
        head = "🔴 בקרת ZUZA: " + (fails[0] if len(fails) == 1 else "%d תקלות — %s" % (len(fails), fails[0]))
    elif w_warn:
        head = "🟡 בקרת ZUZA: %d הדפים עונים, יש %d אזהרות" % (len(results), len(w_warn))
    else:
        head = "🟢 בקרת ZUZA: הכל תקין (%d דפים)" % len(results)

    body = [head, "בדיקה: %s שעון ישראל" % stamp]
    body += notes + w_warn + w_info + ["❌ " + f for f in w_fail]
    body += [site_smoke.line(r) for r in results]
    if fails:
        body.append("מה לעשות: לפתוח את האתר בדפדפן. אם הוא באמת למטה — להחזיר את הפריסה הקודמת "
                    "ב-Cloudflare Pages, או לבקש מסוכן לטפל.")
    print("\n".join(body))
    if IS_GHA:
        for f in fails:
            print("::error title=בקרת ZUZA::%s" % f)
        for w in notes + w_warn:
            print("::warning title=בקרת ZUZA::%s" % w)

    md = ["## שומר חיצוני של ZUZA", "", "**%s**" % head, "", "בדיקה: %s שעון ישראל" % stamp, ""]
    md += ["- " + x for x in notes + w_warn + w_info + ["❌ " + f for f in w_fail]]
    md += ["- " + site_smoke.line(r) for r in results]
    write_summary(md)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
