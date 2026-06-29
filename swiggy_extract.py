"""
Swiggy Business Metrics — multi-account scraper.
Logs into each Swiggy Partner account (client), opens Business Reports -> Outlet
Level Details, and for each RID reads the metrics in DD's spec across the needed
date ranges. Per-RID rows are parsed and upserted to Supabase (history by day).

Credentials:
  SWIGGY_ACCOUNTS  (preferred) = JSON list, e.g.
      [{"label":"KOSTRA","user":"9991112222","pass":"****"},
       {"label":"Casa Dona","user":"8887776666","pass":"****"}]
  OR single account fallback: SWIGGY_USER + SWIGGY_PASS
Optional:
  SUPABASE_URL + SUPABASE_SERVICE_KEY  -> writes to table swiggy_metrics
  HEADLESS=false  -> headed; SHOT_DEBUG=1 -> screenshot every account (default: first only)

Output -> ./swiggy_probe_out/ (scrape.json, scrape.csv, screenshots, result.txt)
"""
import os, sys, time, pathlib, json, re

LOGIN_URL   = "https://partner.swiggy.com/login"
METRICS_URL = "https://partner.swiggy.com/food/business-metrics"
OUT = pathlib.Path("swiggy_probe_out"); OUT.mkdir(exist_ok=True)
HEADLESS  = os.environ.get("HEADLESS", "true").lower() != "false"
SHOT_DEBUG = os.environ.get("SHOT_DEBUG", "") not in ("", "0", "false")

# ---------------- scrape scope ----------------
# Efficient default: headline Net Sales at all 3 ranges, every other metric at MTD.
# Set ALL_AT_ALL=True for every metric at all 3 ranges (~3x slower per account).
ALL_AT_ALL     = False
HEADLINE       = "Net Sales"
HEADLINE_DATES = ["Yesterday", "This Week", "This Month"]   # Previous Day, Weekly, MTD
PRIMARY_DATE   = "This Month"                                # MTD for everything else
METRICS = [
    "Net Sales", "Delivered Orders", "Net AOV", "Restaurant Cancelled Orders", "Cancelled Order Loss",
    "CPC Driven Sales", "CPC Orders", "Total CPC Spends", "CPC Menu Visits", "ROAS", "CPC Ads Depth",
    "CBA Driven Sales", "CBA Orders", "Total CBA Spends", "Ad Impressions", "CBA Menu Visits",
    "Avg Cost Per Impressions", "CBA Ads Depth",
    "Sales via Discounts", "Discounted Orders %", "Discount Given by Restaurant(RDGMV)",
    "Discount GMV %", "Restaurant Discount Per Order(RDPO)",
    "Online Availability %", "Kitchen Prep Time", "Food Ready Accuracy (MFR)", "Delayed Orders (> 10 mins)",
]
if ALL_AT_ALL:
    WORK = [(d, m) for d in HEADLINE_DATES for m in METRICS]
else:
    WORK = [(d, HEADLINE) for d in HEADLINE_DATES] + [(PRIMARY_DATE, m) for m in METRICS if m != HEADLINE]

# ---------------- accounts ----------------
ACCOUNTS = []
_raw = os.environ.get("SWIGGY_ACCOUNTS")
if _raw:
    try:
        for a in json.loads(_raw):
            ACCOUNTS.append({"label": (a.get("label") or a.get("user")), "user": a["user"], "pass": a["pass"]})
    except Exception as e:
        sys.exit(f"SWIGGY_ACCOUNTS is not valid JSON: {e}")
elif os.environ.get("SWIGGY_USER") and os.environ.get("SWIGGY_PASS"):
    ACCOUNTS.append({"label": "default", "user": os.environ["SWIGGY_USER"], "pass": os.environ["SWIGGY_PASS"]})
else:
    sys.exit("Set SWIGGY_ACCOUNTS (JSON list) or SWIGGY_USER + SWIGGY_PASS.")

from playwright.sync_api import sync_playwright

log_lines = []
def log(m): print(m); log_lines.append(str(m))

SHOT_PREFIX = ""
def shot(page, name, force=False):
    if not (SHOT_DEBUG or force):
        return
    p = OUT / f"{SHOT_PREFIX}{name}.png"
    try: page.screenshot(path=str(p), full_page=True); log(f"  saved {p}")
    except Exception as e: log(f"  (screenshot {name} failed: {e})")

def first_visible(page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                return loc
        except Exception:
            pass
    return None

def parse_row(raw):
    parts = [p.strip() for p in raw.split("|")]
    name = parts[0] if parts else ""
    rid = None; locality = ""
    ridx = next((i for i, p in enumerate(parts) if p.startswith("RID:")), None)
    if ridx is not None:
        m = re.search(r"RID:\s*(\d+),?\s*(.*)", parts[ridx])
        if m: rid = m.group(1); locality = m.group(2).strip()
        rest = parts[ridx + 1:]
    else:
        rest = parts[1:]
    val = None; currency = False; no_data = False; is_pct = False
    delta = None; compare = ""
    if rest:
        v = rest[0]
        if v in ("-", ""):
            no_data = True
        else:
            if "₹" in v: currency = True
            vv = v.replace("₹", "").replace(",", "").strip()
            if vv.endswith("%"): is_pct = True; vv = vv[:-1].strip()
            try: val = float(vv)
            except Exception: pass
    if len(rest) >= 2 and rest[1].endswith("%"):
        try: delta = float(rest[1].replace("%", "").strip())
        except Exception: pass
    for p in rest[1:]:
        if p.startswith("vs "): compare = p
    return {"name": name, "rid": rid, "locality": locality, "value": val,
            "currency": currency, "is_pct": is_pct, "no_data": no_data,
            "delta_pct": delta, "compare": compare, "raw": raw}

def push_supabase(rows):
    url = os.environ.get("SUPABASE_URL"); key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log("[SB] Supabase: skipped (SUPABASE_URL / SUPABASE_SERVICE_KEY not set)"); return
    import urllib.request, urllib.error
    from datetime import datetime, timezone, timedelta
    cap = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()  # IST date
    payload = []
    for p in rows:
        if not p.get("rid"): continue
        payload.append({
            "captured_on": cap, "account": p.get("account"), "rid": p["rid"],
            "outlet_name": p["name"], "locality": p["locality"],
            "metric": p["metric"], "period": p["date"],
            "value": p["value"], "is_currency": p["currency"], "is_pct": p["is_pct"],
            "delta_pct": p["delta_pct"], "direction": (p["direction"] or None),
            "sentiment": (p.get("sentiment") or None),
            "compare_label": (p["compare"] or None), "no_data": p["no_data"],
        })
    if not payload:
        log("[SB] Supabase: nothing to write"); return
    body = json.dumps(payload).encode("utf-8")
    endpoint = url.rstrip("/") + "/rest/v1/swiggy_metrics?on_conflict=captured_on,rid,metric,period"
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "apikey": key, "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            log(f"[SB] Supabase: upserted {len(payload)} rows (HTTP {r.status})")
    except urllib.error.HTTPError as e:
        log(f"[SB] Supabase HTTP {e.code}: {e.read().decode()[:400]}")
    except Exception as e:
        log(f"[SB] Supabase error: {e}")


def run_account(browser, acct, do_shots):
    """Login + scrape one account. Returns (rows, status_str). Never raises."""
    global SHOT_PREFIX
    label = acct["label"]; user = acct["user"]; pwd = acct["pass"]
    SHOT_PREFIX = re.sub(r"[^A-Za-z0-9]+", "_", label)[:24] + "_"
    rows = []
    ctx = browser.new_context(accept_downloads=True)
    page = ctx.new_page()
    try:
        # ---------- LOGIN ----------
        log(f"[{label}] [1] Opening login")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        uf = first_visible(page, [
            'input[type=tel]', 'input[name*="mobile" i]', 'input[name*="phone" i]',
            'input[name*="email" i]', 'input[name*="user" i]', 'input[type=email]', 'input[type=text]'
        ])
        if uf:
            log(f"[{label}] [2] identifier"); uf.fill(user)
            cont = first_visible(page, ["button:has-text('Continue')", "button:has-text('Next')",
                "button:has-text('Proceed')", "button:has-text('Login')", "button[type=submit]"])
            if cont:
                try: cont.click(); time.sleep(3)
                except Exception as e: log(f"  continue click failed: {e}")
        pf = first_visible(page, ['input[type=password]'])
        if not pf:
            tog = first_visible(page, ["a:has-text('password')", "button:has-text('password')",
                "*:has-text('Login with password')", "*:has-text('Use password')", "text=/password/i"])
            if tog:
                log(f"[{label}] [3] password toggle")
                try: tog.click(); time.sleep(2)
                except Exception as e: log(f"  toggle failed: {e}")
                pf = first_visible(page, ['input[type=password]'])
        if pf:
            log(f"[{label}] [4] password"); pf.fill(pwd)
            sub = first_visible(page, ["button:has-text('Login')", "button:has-text('Sign in')",
                "button:has-text('Submit')", "button:has-text('Continue')", "button[type=submit]"])
            if sub:
                try: sub.click(); log(f"[{label}] [5] submitted")
                except Exception as e: log(f"  submit failed: {e}")
        else:
            log(f"[{label}] [4] NO password field — login changed or OTP wall."); shot(page, "login_fail", force=True)
            return rows, "login_no_password_field"

        def home_ready():
            for sel in ["text=/Growth Boosters/i", "text=/All Benefits/i", "text=/Run discounts/i",
                        "a:has-text('Reports')", "text=/Growth home/i"]:
                try:
                    if page.locator(sel).first.count() and page.locator(sel).first.is_visible():
                        return True
                except Exception: pass
            return False
        for _ in range(35):
            if home_ready(): break
            time.sleep(1)
        time.sleep(4)
        if do_shots: shot(page, "07_post_login")
        if not home_ready():
            log(f"[{label}] LOGIN FAILED (home not detected) — likely OTP/invalid creds.")
            shot(page, "login_fail", force=True)
            return rows, "login_failed"

        # ---------- NAV to Business Metrics ----------
        log(f"[{label}] [6] -> Business Metrics")
        def metrics_loaded():
            for sel in ["text=/Net Sales/i", "text=/Business Reports/i", "text=/Download Report/i"]:
                try:
                    if page.locator(sel).first.count(): return True
                except Exception: pass
            return False
        reached = False
        nav = first_visible(page, ['a[href*="business-metrics"]', 'a[href*="reports" i]',
            "nav a:has-text('Reports')", "a:has-text('Reports')", "text=/^Reports$/i", "*:has-text('REPORTS')"])
        if nav:
            try:
                nav.click()
                for _ in range(25):
                    if metrics_loaded(): reached = True; break
                    time.sleep(1)
            except Exception as e:
                log(f"  Reports nav click failed: {e}")
        if not reached:
            try:
                page.goto(METRICS_URL, wait_until="domcontentloaded", timeout=60000)
                for _ in range(25):
                    if metrics_loaded(): reached = True; break
                    time.sleep(1)
            except Exception as e:
                log(f"  direct nav failed: {e}")
        time.sleep(2)
        if do_shots: shot(page, "10_business_metrics")
        if not reached:
            log(f"[{label}] Business Metrics not detected."); shot(page, "metrics_fail", force=True)
            return rows, "metrics_not_loaded"

        # wait for real data
        for _ in range(25):
            try:
                b = page.inner_text("body")
                if ("₹" in b) or ("vs last" in b.lower()): break
            except Exception: pass
            time.sleep(1)

        # ---------- OUTLET LEVEL DETAILS ----------
        ol = first_visible(page, ["text=/See Outlet Level Data/i", "*:has-text('Outlet Level Data')"])
        if not ol:
            log(f"[{label}] 'See Outlet Level Data' not found."); shot(page, "no_outlet_link", force=True)
            return rows, "no_outlet_level"
        ol.click()
        for _ in range(20):
            try:
                if page.locator("text=/Outlet Level Details/i").first.count(): break
            except Exception: pass
            time.sleep(1)
        time.sleep(2)
        if do_shots: shot(page, "13_outlet_level")

        cur = {"m": "Net Sales"}  # currently-selected metric (pill label)

        def set_date(label_):
            pill = first_visible(page, [
                "text=/^Today$/", "text=/^Yesterday$/", "text=/^This [Ww]eek$/",
                "text=/^Last [Ww]eek$/", "text=/^This [Mm]onth$/", "text=/^Custom$/",
                "*:has-text('Today')",
            ])
            if not pill:
                log(f"   date pill not found ({label_})"); return False
            try:
                pill.click(); time.sleep(1.2)
                opt = page.get_by_text(label_, exact=False).first
                if opt.count():
                    opt.click(); time.sleep(3); return True
                log(f"   date option '{label_}' not found"); page.keyboard.press("Escape")
            except Exception as e:
                log(f"   set_date {label_} error: {e}")
            return False

        def set_metric(label_):
            opener = None
            try:
                c = page.get_by_text(cur["m"], exact=True).first
                if c.count(): opener = c
            except Exception: pass
            if opener is None:
                opener = first_visible(page, [f"*:has-text('{cur['m']}')"])
            if not opener:
                log(f"   filters opener not found (current={cur['m']})"); return False
            try:
                opener.click(); time.sleep(1.5)
                lab = page.get_by_text(label_, exact=True)
                if lab.count():
                    lab.last.click(); time.sleep(0.5)
                else:
                    log(f"   metric '{label_}' not in panel")
                ap = first_visible(page, ["button:has-text('Apply')", "*:has-text('Apply')"])
                if ap:
                    ap.click(); time.sleep(3); cur["m"] = label_; return True
                log("   Apply not found"); page.keyboard.press("Escape")
            except Exception as e:
                log(f"   set_metric {label_} error: {e}")
            return False

        def read_rows():
            out = []
            try:
                rws = page.locator('[class*="ListItemContainer-business-metrics-mfe"]')
                n = rws.count()
                if n == 0:
                    anchors = page.locator("text=/RID:/")
                    for i in range(anchors.count()):
                        try:
                            t = anchors.nth(i).locator("xpath=ancestor::*[self::div][2]").inner_text().strip()
                        except Exception:
                            t = ""
                        out.append((t.replace("\n", " | "), "", ""))
                    return out
                for i in range(n):
                    row = rws.nth(i)
                    try:
                        txt = row.inner_text().strip().replace("\n", " | ")
                    except Exception:
                        txt = ""
                    if "RID" not in txt:
                        continue
                    direction = ""; sentiment = ""
                    try:
                        img = row.locator("img").first
                        if img.count():
                            s = (img.get_attribute("src") or "")
                            fname = s.split("?")[0].rstrip("/").split("/")[-1].lower()
                            if any(k in fname for k in ["up", "increase", "rise", "uptrend", "arrow_up", "caret_up"]):
                                direction = "up"
                            elif any(k in fname for k in ["down", "decrease", "fall", "drop", "decline", "arrow_down", "caret_down"]):
                                direction = "down"
                            if "green" in fname: sentiment = "good"
                            elif "red" in fname: sentiment = "bad"
                    except Exception:
                        pass
                    out.append((txt, direction, sentiment))
            except Exception as e:
                log(f"   read_rows error: {e}")
            return out

        from collections import OrderedDict
        date_metrics = OrderedDict()
        for d, m in WORK:
            date_metrics.setdefault(d, [])
            if m not in date_metrics[d]:
                date_metrics[d].append(m)

        for d, mets in date_metrics.items():
            dok = set_date(d)
            log(f"[{label}] [8] date='{d}' set={dok} ({len(mets)} metrics)")
            for m in mets:
                mok = set_metric(m)
                for txt, direction, sentiment in read_rows():
                    p = parse_row(txt)
                    p["account"] = label; p["date"] = d; p["metric"] = m
                    p["direction"] = direction; p["sentiment"] = sentiment
                    rows.append(p)
            if do_shots: shot(page, f"scrape_{d.replace(' ', '_')}")
        log(f"[{label}] scraped {len(rows)} rows")
        return rows, "ok"
    except Exception as e:
        log(f"[{label}] UNHANDLED error: {e}")
        try: shot(page, "error", force=True)
        except Exception: pass
        return rows, f"error: {e}"
    finally:
        try: ctx.close()
        except Exception: pass


# ============================ MAIN ============================
log(f"=== Swiggy scrape: {len(ACCOUNTS)} account(s), {len(WORK)} (date,metric) reads each ===")
all_rows = []
summary = []
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=HEADLESS)
    for i, acct in enumerate(ACCOUNTS):
        do_shots = SHOT_DEBUG or (i == 0)
        rows, status = run_account(browser, acct, do_shots)
        all_rows.extend(rows)
        summary.append((acct["label"], len(rows), status))
        try:
            push_supabase(rows)   # incremental: persist each account so a mid-fleet crash keeps prior progress
        except Exception as e:
            log(f"[SB] {acct['label']} push error: {e}")
        time.sleep(5)  # gentle gap between accounts
    browser.close()

# write artifacts
with open(OUT / "scrape.json", "w", encoding="utf-8") as f:
    json.dump(all_rows, f, ensure_ascii=False, indent=2)
import csv
with open(OUT / "scrape.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["account", "date", "metric", "rid", "name", "locality", "value",
                "currency", "is_pct", "delta_pct", "direction", "sentiment", "compare", "no_data"])
    for p in all_rows:
        w.writerow([p.get("account"), p["date"], p["metric"], p["rid"], p["name"], p["locality"], p["value"],
                    p["currency"], p["is_pct"], p["delta_pct"], p["direction"],
                    p.get("sentiment", ""), p["compare"], p["no_data"]])
log(f"[9] wrote {len(all_rows)} rows -> scrape.json + scrape.csv")

log("=== SUMMARY ===")
for lbl, cnt, st in summary:
    log(f"  {lbl}: {cnt} rows  [{st}]")
fails = [s for s in summary if s[2] != "ok"]
if fails:
    log(f"  !! {len(fails)} account(s) did not complete cleanly: " + ", ".join(f"{l} ({st})" for l, c, st in fails))

(OUT / "result.txt").write_text("\n".join(log_lines), encoding="utf-8")
log(f"\nArtifacts in: {OUT.resolve()}")