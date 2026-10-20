#!/usr/bin/env python3
"""שומר התשלום של ZUZA — Checkout guard (הבעלים, 20.10.2026, פריט 13).

The owner: "automatically, when there's a problem, close the part of the site
that has the problem so people don't hit it. Must constantly verify that the
payment buttons work." Runs every ~10 minutes in GitHub Actions
(.github/workflows/checkout-guard.yml).

Each run:
  1. Asks zuzadoor.com which Hyp payment links its pay buttons use right now
     (/.netlify/functions/pay-links — read from the same code as the buttons).
  2. Opens every link the way a visitor would (GET only, nothing is paid) and
     checks it is a real Hyp payment form for the right amount. A failure is
     re-checked twice within the run (after 20 s and 40 s).
  3. Writes status.json (kit / gift: "ok" | "paused"). The site reads it via
     /.netlify/functions/watch-status and swaps a paused pay button for a
     short notice with an email field. Fail-open on the site side.
       pause:  Hyp says the link is invalid (error page)       -> 1 run
               Hyp does not answer / server error               -> 2 runs in a row
       reopen: the links work                                    -> 2 runs in a row
     "Blocked" answers (403/429/Cloudflare challenge) count as "don't know":
     nothing changes, so a block on GitHub's servers never closes the shop.
  4. Checks /.netlify/functions/count (the orders sheet). 3 failures in a row =
     orders are not being written (shipping addresses!) -> alert, no pause
     (owner, 18.10.2026: a sheet outage must not block a sale).
  5. Every change -> one email to beeri@ and office@ (via the site's
     /.netlify/functions/watch-alert, authenticated with this run's GitHub
     OIDC token — no secret). During the Shabbat / Yom Tov freeze the guard
     still checks and still pauses, but the email waits for the end of the
     freeze (constitution §6). If the email can't be sent, the run fails, and
     GitHub emails the repo owner instead.
  6. Once a week it refreshes `heartbeat` in status.json, so GitHub never
     disables this repo's schedules for inactivity (60 days).

Env: GUARD_SITE (default https://zuzadoor.com), GUARD_MODE = run | dry-run |
test-alert, GUARD_AT = Israel local time for tests (2026-10-23T16:00).
Python 3 stdlib only.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from external_watch import TZ, freeze_windows, frozen_now  # noqa: E402

ROOT = os.path.dirname(HERE)
STATUS_PATH = os.path.join(ROOT, "status.json")
SITE = os.environ.get("GUARD_SITE", "https://zuzadoor.com").rstrip("/")
MODE = os.environ.get("GUARD_MODE", "run")
AUDIENCE = "zuzadoor-watch-alert"
UA = "Mozilla/5.0 (compatible; zuza-checkout-guard/1.0; +https://github.com/beeri-hub/zuza-watch)"
RETRY_WAITS = (20, 40)
PAUSE_AFTER = {"broken": 1, "down": 2}
REOPEN_AFTER = 2
SHEET_ALERT_AFTER = 3
UNKNOWN_FAIL_AFTER = 3
AREAS = {"kit": "ערכת ZUZA (עמוד המוצר בעברית)", "gift": "מתנה לפיתוח (/rnd)"}
IS_GHA = os.environ.get("GITHUB_ACTIONS") == "true"
RUN_URL = ("%s/%s/actions/runs/%s" % (os.environ.get("GITHUB_SERVER_URL", ""),
                                       os.environ.get("GITHUB_REPOSITORY", ""),
                                       os.environ.get("GITHUB_RUN_ID", ""))) if IS_GHA else "(הרצה מקומית)"


def now_il():
    at = os.environ.get("GUARD_AT")
    return datetime.fromisoformat(at).replace(tzinfo=TZ) if at else datetime.now(TZ)


def fmt(dt):
    return dt.strftime("%d.%m.%Y %H:%M")


# ---------- http ----------
def get(url, timeout=25):
    """(status, text, error). text decoded as cp1255 (Hyp) with utf-8 fallback."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "he,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read() or b"", e.code
    except Exception as e:  # timeout, DNS, TLS, reset
        return None, "", "%s: %s" % (type(e).__name__, e)
    for enc in ("utf-8", "cp1255"):
        try:
            return status, raw.decode(enc), None
        except UnicodeDecodeError:
            pass
    return status, raw.decode("latin-1"), None


def get_json(url, timeout=20):
    status, text, err = get(url, timeout)
    if err or status != 200:
        return None, err or "HTTP %s" % status
    try:
        return json.loads(text), None
    except ValueError as e:
        return None, "JSON: %s" % e


# ---------- checks ----------
def classify(status, text, err, amount):
    """ok | broken | down | blocked, plus a short Hebrew reason."""
    if err:
        return "down", "אין תשובה מ-Hyp (%s)" % err[:80]
    if status in (403, 429) or "challenge-platform" in text or "Just a moment" in text:
        return "blocked", "Hyp חסם את הבודק (HTTP %s) — לא ידוע" % status
    if status >= 500:
        return "down", "שגיאת שרת ב-Hyp (HTTP %s)" % status
    # A payment form = the page works. (The amount is part of Hyp's signature:
    # a link with a wrong amount gets Hyp's error page, not a form.)
    low = text.lower()
    if status == 200 and ('id="cc_form"' in low or ("<form" in low and "yaadpay" in low)):
        shown = amount is None or ("%.2f" % amount) in text
        return "ok", "טופס תשלום תקין" + ("" if shown else " (הסכום לא זוהה בעמוד)")
    if "שגיאה" in text:
        i = text.find("שגיאה")
        snippet = " ".join(text[i:i + 60].replace("<", " <").split())
        snippet = snippet.split("<")[0].strip()
        return "broken", "Hyp מציג שגיאה במקום טופס תשלום (\"%s\")" % snippet
    return "broken", "Hyp לא מציג טופס תשלום (HTTP %s)" % status


def check_links(links):
    """links: [{key, label, url, amount}] -> {key: (verdict, reason)}; retries failures."""
    results = {}
    todo = list(links)
    for attempt in range(len(RETRY_WAITS) + 1):
        if attempt:
            time.sleep(RETRY_WAITS[attempt - 1])
        nxt = []
        for ln in todo:
            st, text, err = get(ln["url"])
            results[ln["key"]] = classify(st, text, err, ln["amount"])
            if results[ln["key"]][0] != "ok":
                nxt.append(ln)
        todo = nxt
        if not todo:
            break
    return results


def area_verdict(keys, results):
    vs = [results[k][0] for k in keys]
    if not vs:
        return "n/a"
    for v in ("broken", "down"):
        if v in vs:
            return v
    if "ok" in vs:
        return "ok"
    return "unknown"


# ---------- status ----------
def load_status():
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def normalize(s):
    s.setdefault("kit", "ok")
    s.setdefault("gift", "ok")
    s.setdefault("since", {})
    s.setdefault("reason", {})
    s.setdefault("streak", {})
    for a in AREAS:
        s["streak"].setdefault(a, {"bad": 0, "good": REOPEN_AFTER})
        s["since"].setdefault(a, "")
        s["reason"].setdefault(a, "")
    s.setdefault("sheet", {"bad": 0, "alerted": False})
    s.setdefault("unknown", 0)
    s.setdefault("pending", [])
    s.setdefault("send_failures", 0)
    s.setdefault("heartbeat", "")
    return s


def save_status(s):
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
        f.write("\n")


def git_commit(message):
    def run(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True)
    run("config", "user.name", "zuza-checkout-guard")
    run("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    run("add", "status.json")
    if run("diff", "--cached", "--quiet").returncode == 0:
        return True
    if run("commit", "-m", message).returncode != 0:
        return False
    for _ in range(3):
        if run("push").returncode == 0:
            return True
        run("pull", "--rebase", "-X", "theirs")
    return False


# ---------- alerts ----------
def oidc_token():
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    tok = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not tok:
        raise RuntimeError("no OIDC (permissions: id-token: write?)")
    req = urllib.request.Request(url + "&audience=" + AUDIENCE, headers={"Authorization": "bearer " + tok})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())["value"]


def send_alert(subject, text, test=False):
    body = json.dumps({"subject": subject, "text": text, "test": test}).encode()
    req = urllib.request.Request(SITE + "/.netlify/functions/watch-alert", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA,
                                          "Authorization": "Bearer " + oidc_token()})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return True, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return False, "HTTP %s %s" % (e.code, e.read().decode(errors="replace")[:200])
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


FOOTER = [
    "",
    "—",
    "איך זה עובד: השומר ב-GitHub (beeri-hub/zuza-watch) בודק כל 10 דקות את עמודי התשלום של Hyp,",
    "וסוגר או פותח את הכפתור באתר לבד. לסגור או לפתוח ידנית: ב-status.json שם לשנות את \"kit\" או \"gift\"",
    "ל-\"paused\" / \"ok\" ולהוסיף \"manual\": true (אחרת השומר מחזיר). להחזיר לאוטומטי: למחוק את \"manual\".",
    "הריצה: %s",
]


def pause_text(area, when, rows):
    return [
        "מה קרה: עמוד התשלום של Hyp לא עובד, אז האתר סגר לבד את כפתור התשלום — %s." % AREAS[area],
        "מאז: %s (שעון ישראל)." % fmt(when),
        "",
        "מה הלקוחות רואים עכשיו: במקום כפתור התשלום — \"ההזמנות מושהות לכמה שעות\" ושדה להשאיר מייל.",
        "מי שמשאיר מייל — מגיע אליכם במייל נפרד, וגם לגיליון הלידים.",
        "",
        "מה נבדק:",
        *rows,
        "",
        "מה המערכת עושה: בודקת שוב כל 10 דקות. כשהעמוד עובד פעמיים ברצף, הכפתור חוזר לבד ותקבלו מייל.",
        "",
        "מה לעשות אם זה לא חוזר תוך כמה שעות:",
        "1. להיכנס לממשק Hyp ולבדוק שהמסוף של U4B פעיל ושאין הודעה על תקלה.",
        "2. להעתיק את המייל הזה ל-Claude ולבקש: \"תבדוק למה התשלום של ZUZA סגור\".",
    ]


def evaluate(s, now, alerts, log):
    """One full run of the checks. Mutates s, appends (subject, lines) to alerts."""
    links_data, err = get_json(SITE + "/.netlify/functions/pay-links")
    prev_unknown = s["unknown"]
    if not links_data or not links_data.get("ok"):
        s["unknown"] = min(prev_unknown + 1, UNKNOWN_FAIL_AFTER)
        log.append("⚠️ לא הצלחתי לקבל את קישורי התשלום מהאתר (%s). לא משנה כלום." % err)
        # Fail the run (GitHub emails the owner) once, when it reaches the limit.
        return prev_unknown < UNKNOWN_FAIL_AFTER <= s["unknown"] and "pay-links unavailable %d runs" % UNKNOWN_FAIL_AFTER
    links = []
    for k in links_data.get("kit", []):
        links.append({"key": "kit-%s" % k["qty"], "area": "kit", "url": k["url"], "amount": k.get("amount"),
                      "label": "%s יח' (₪%s)" % (k["qty"], k.get("amount"))})
    for g in links_data.get("gift", []):
        sym = "₪" if g["currency"] == "ILS" else "$"
        links.append({"key": "gift-%s" % g["currency"], "area": "gift", "url": g["url"], "amount": g.get("amount"),
                      "label": "מתנה %s%s" % (sym, g.get("amount"))})
    results = check_links(links)
    for ln in links:
        v, why = results[ln["key"]]
        log.append("%s %s — %s" % ("✅" if v == "ok" else "❌", ln["label"], why))

    unknown_any = False
    for area in AREAS:
        keys = [ln["key"] for ln in links if ln["area"] == area]
        rows = ["  • %s: %s" % (ln["label"], results[ln["key"]][1]) for ln in links if ln["area"] == area]
        v = area_verdict(keys, results)
        st = s["streak"][area]
        log.append("אזור %s: %s (מצב באתר: %s)" % (area, v, s[area]))
        if s.get("manual"):
            # Someone set kit/gift by hand ("manual": true) — check, report, don't touch.
            log.append("✋ manual=true ב-status.json — השומר לא משנה את %s" % area)
            continue
        if v in ("broken", "down"):
            st["bad"] = min(st["bad"] + 1, 3)
            st["good"] = 0
            if s[area] != "paused" and st["bad"] >= PAUSE_AFTER[v]:
                s[area] = "paused"
                s["since"][area] = now.isoformat(timespec="minutes")
                s["reason"][area] = "; ".join(results[k][1] for k in keys if results[k][0] != "ok")[:300]
                alerts.append(("סגרתי אוטומטית את התשלום באתר — %s" % AREAS[area], pause_text(area, now, rows)))
        elif v in ("ok", "n/a"):
            st["good"] = min(st["good"] + 1, REOPEN_AFTER)
            st["bad"] = 0
            if s[area] == "paused" and (st["good"] >= REOPEN_AFTER or v == "n/a"):
                since = s["since"][area]
                s[area] = "ok"
                s["since"][area] = now.isoformat(timespec="minutes")
                s["reason"][area] = ""
                alerts.append(("התשלום באתר חזר לעבוד — %s" % AREAS[area], [
                    "עמוד התשלום של Hyp עובד שוב (נבדק פעמיים ברצף), והכפתור באתר חזר לבד — %s." % AREAS[area],
                    "היה סגור מ-%s עד %s (שעון ישראל)." % (fmt(datetime.fromisoformat(since)) if since else "?", fmt(now)),
                    "",
                    "מה לעשות: לבדוק במייל אם מישהו השאיר כתובת בזמן הסגירה, ולכתוב לו שאפשר להזמין.",
                ]))
        else:
            unknown_any = True
    s["unknown"] = min(s["unknown"] + 1, UNKNOWN_FAIL_AFTER) if unknown_any else 0

    # the orders sheet (no pause — alert only)
    cnt, cerr = get_json(SITE + "/.netlify/functions/count")
    sheet_ok = bool(cnt and cnt.get("ok"))
    sh = s["sheet"]
    log.append("%s גיליון ההזמנות (count): %s" % ("✅" if sheet_ok else "❌", "תקין" if sheet_ok else (cerr or "ok:false")))
    if sheet_ok:
        if sh["alerted"]:
            alerts.append(("גיליון ההזמנות חזר לעבוד", [
                "האתר שוב מצליח לקרוא ולכתוב בגיליון ההזמנות (%s)." % fmt(now),
                "מה לעשות: לבדוק ב-Hyp אם היו תשלומים בזמן התקלה, ולוודא שלכל אחד יש שורה וכתובת למשלוח בגיליון.",
            ]))
        sh.update(bad=0, alerted=False)
    else:
        sh["bad"] = min(sh["bad"] + 1, SHEET_ALERT_AFTER)
        if sh["bad"] >= SHEET_ALERT_AFTER and not sh["alerted"]:
            sh["alerted"] = True
            alerts.append(("האתר לא מצליח לרשום הזמנות בגיליון", [
                "מה קרה: כבר חצי שעה האתר לא מצליח לדבר עם גיליון ההזמנות (Apps Script).",
                "מה זה אומר: התשלום עצמו עובד, ו-Hyp שולח קבלה. אבל הכתובת למשלוח שהקונה מילא באתר",
                "לא נרשמת בגיליון. הכפתור לא נסגר (החלטת בעלים 18.10: תקלה בגיליון לא עוצרת מכירה).",
                "",
                "מה לעשות:",
                "1. לבדוק ב-Hyp אם יש תשלומים חדשים, ולהתקשר לקונים לקבל כתובת.",
                "2. להעתיק את המייל הזה ל-Claude ולבקש: \"גיליון ההזמנות של zuzadoor לא עונה\".",
            ]))
    return prev_unknown < UNKNOWN_FAIL_AFTER <= s["unknown"] and "status unknown %d runs" % UNKNOWN_FAIL_AFTER


def main():
    now = now_il()
    notes = []
    wins = freeze_windows(now, notes)
    frozen = frozen_now(now, wins)

    if MODE == "test-alert":
        ok, info = send_alert("בדיקה: מנגנון ההתראות של שומר התשלום עובד",
                              "זו בדיקה בלבד (%s). אם המייל הגיע — השומר יכול להתריע לך. אין צורך לעשות כלום.\n%s"
                              % (fmt(now), RUN_URL), test=True)
        print("test alert:", ok, info)
        return 0 if ok else 1

    s = normalize(load_status())
    before = json.dumps(s, sort_keys=True)
    log, alerts = [], []
    fail_reason = evaluate(s, now, alerts, log)

    for subject, lines in alerts:
        s["pending"].append({"at": now.isoformat(timespec="minutes"), "subject": subject,
                             "text": "\n".join(lines + [l % RUN_URL if "%s" in l else l for l in FOOTER])})

    send_failed = False
    if s["pending"] and MODE == "run":
        if frozen:
            log.append("🕯️ הקפאה (%s): %d התראות ממתינות למוצאי שבת/חג." % (frozen, len(s["pending"])))
        else:
            p = s["pending"]
            if len(p) == 1:
                subject, text = p[0]["subject"], p[0]["text"]
            else:
                subject = "%d עדכונים משומר התשלום" % len(p)
                text = "\n\n".join("[%s] %s\n%s" % (fmt(datetime.fromisoformat(x["at"])), x["subject"], x["text"]) for x in p)
            ok, info = send_alert(subject, text)
            log.append(("📧 נשלח מייל: %s" if ok else "❌ המייל לא נשלח: %s") % (subject if ok else info))
            if ok:
                s["pending"], s["send_failures"] = [], 0
            else:
                s["send_failures"] = min(s["send_failures"] + 1, 99)
                send_failed = s["send_failures"] in (1, 6, 36)

    today = now.date().isoformat()
    if not s["heartbeat"] or (now.date() - datetime.fromisoformat(s["heartbeat"]).date()).days >= 7:
        s["heartbeat"] = today

    changed = json.dumps(s, sort_keys=True) != before
    if changed:
        s["updated"] = now.isoformat(timespec="minutes")
    print("\n".join(log))
    if MODE == "run" and changed:
        save_status(s)
        msg = "status: kit=%s gift=%s" % (s["kit"], s["gift"])
        if not git_commit(msg):
            print("❌ git push failed")
            send_failed = True
    if IS_GHA and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("## שומר התשלום — %s\n\n" % fmt(now) + "\n".join("- " + l for l in log) + "\n")
    if fail_reason:
        print("❌ " + fail_reason)
    return 1 if (fail_reason or send_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
