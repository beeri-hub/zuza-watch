#!/usr/bin/env python3
"""בדיקת עשן לאתרים של ZUZA — רצה מיד אחרי כל פריסה.

Usage:
  python3 scripts/site_smoke.py [zuza|beeri|u4b|all] [--json]

Rules for agents: run this right after every deploy (`npm run deploy:cf`,
deploy-cf.sh, wrangler ...). Exit 1 = the site is broken -> ROLL BACK
(Cloudflare Pages: redeploy the previous deployment) and report.

Targets live in scripts/site_smoke.json (add URLs there, not here).
Python 3 stdlib only. Exit: 0 = all OK, 1 = something failed, 2 = usage/config error.
"""
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "site_smoke.json")
UA = "ZUZA-smoke/1.0 (+https://zuzadoor.com)"
TIMEOUT = 20
RETRY_WAIT = 5


def load_targets(group="all", config=CONFIG):
    with open(config, encoding="utf-8") as f:
        sites = json.load(f)["sites"]
    if group == "all":
        return [t for g in sites for t in sites[g]]
    if group not in sites:
        raise KeyError(group)
    return list(sites[group])


def bust(url):
    """Add ?smoke=<ts> only when the URL has no query string."""
    if urllib.parse.urlsplit(url).query:
        return url
    return url + ("?smoke=%d" % int(time.time()))


def _reason_for_exception(e):
    if isinstance(e, urllib.error.URLError):
        e = e.reason if not isinstance(e.reason, str) else e
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "הדף לא נטען תוך %d שניות" % TIMEOUT
    if isinstance(e, ssl.SSLError) or "CERTIFICATE" in str(e).upper():
        return "בעיה בתעודת האבטחה של האתר (SSL)"
    if isinstance(e, socket.gaierror):
        return "שם האתר לא נמצא (בעיית DNS)"
    if isinstance(e, ConnectionRefusedError):
        return "השרת סירב לחיבור"
    return "אין חיבור לאתר (%s)" % str(e)[:120]


def _status_reason(code, expect):
    if code == 404:
        return "הדף לא נמצא (404)"
    if code in (401, 403):
        return "הגישה לדף חסומה (%d)" % code
    if code >= 500:
        return "שגיאת שרת (%d)" % code
    return "קוד תשובה %d במקום %d" % (code, expect)


def _fetch_once(target):
    url = target["url"]
    expect = int(target.get("expect_status", 200))
    req = urllib.request.Request(bust(url), headers={
        "User-Agent": UA,
        "Cache-Control": "no-cache",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            code = r.status
            body = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
    except Exception as e:  # network / timeout / SSL / DNS
        return {"ok": False, "status": None, "reason": _reason_for_exception(e),
                "ms": int((time.time() - t0) * 1000)}
    ms = int((time.time() - t0) * 1000)
    if code != expect:
        return {"ok": False, "status": code, "reason": _status_reason(code, expect), "ms": ms}
    missing = [s for s in target.get("contains", []) if s not in body]
    if missing:
        return {"ok": False, "status": code, "ms": ms,
                "reason": "הדף נטען אבל חסר בו הטקסט «%s» — כנראה עלה דף שגוי או ריק" % missing[0]}
    return {"ok": True, "status": code, "reason": "", "ms": ms}


def check(target):
    res = _fetch_once(target)
    attempts = 1
    if not res["ok"]:
        time.sleep(RETRY_WAIT)
        res = _fetch_once(target)
        attempts = 2
    res.update({"url": target["url"], "name_he": target.get("name_he", target["url"]),
                "attempts": attempts})
    return res


def run(group="all"):
    targets = load_targets(group)
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(check, targets))


def line(res):
    if res["ok"]:
        return "✅ תקין · %s · %s" % (res["name_he"], res["url"])
    return "❌ %s: %s · %s" % (res["name_he"], res["reason"], res["url"])


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    as_json = "--json" in argv
    group = args[0] if args else "all"
    try:
        results = run(group)
    except KeyError:
        print("❌ קבוצה לא מוכרת: %s (אפשר: zuza, beeri, u4b, all)" % group)
        return 2
    except (OSError, ValueError) as e:
        print("❌ לא הצלחתי לקרוא את scripts/site_smoke.json: %s" % e)
        return 2
    failed = [r for r in results if not r["ok"]]
    if as_json:
        print(json.dumps({"ok": not failed, "group": group, "results": results},
                         ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(line(r))
        if failed:
            print("🔴 %d מתוך %d דפים נכשלו. אם זה אחרי פריסה — להחזיר את הגרסה הקודמת ולדווח."
                  % (len(failed), len(results)))
        else:
            print("🟢 כל %d הדפים תקינים." % len(results))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
