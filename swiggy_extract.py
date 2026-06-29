#!/usr/bin/env python3
"""
Swiggy Business Metrics EXTRACT probe.

Builds on the proven login flow, then:
  1. opens https://partner.swiggy.com/food/business-metrics
  2. screenshots the page (so I can see the date selector + Download button)
  3. clicks "Download Report" and captures the downloaded file
  4. scouts the date control (clicks the "Today, ..." header, screenshots the options)

Output -> ./swiggy_probe_out/  (the downloaded report + screenshots + result.txt).
Run it the same way as the login probe (same GitHub workflow, just point `run:` at this file).
Credentials come from env vars: SWIGGY_USER, SWIGGY_PASS. HEADLESS defaults true.
"""
import os, sys, time, pathlib

LOGIN_URL   = "https://partner.swiggy.com/login"
METRICS_URL = "https://partner.swiggy.com/food/business-metrics"
OUT = pathlib.Path("swiggy_probe_out"); OUT.mkdir(exist_ok=True)

USER = os.environ.get("SWIGGY_USER")
PASS = os.environ.get("SWIGGY_PASS")
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
if not USER or not PASS:
    sys.exit("Set SWIGGY_USER and SWIGGY_PASS environment variables first.")

from playwright.sync_api import sync_playwright

log_lines = []
def log(m): print(m); log_lines.append(str(m))
def shot(page, name):
    p = OUT / f"{name}.png"
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

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=HEADLESS)
    ctx = browser.new_context(accept_downloads=True)
    page = ctx.new_page()

    # ---------- LOGIN (proven flow) ----------
    log(f"[1] Opening {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(3); log(f"  URL now: {page.url}")

    uf = first_visible(page, [
        'input[type=tel]', 'input[name*="mobile" i]', 'input[name*="phone" i]',
        'input[name*="email" i]', 'input[name*="user" i]', 'input[type=email]', 'input[type=text]'
    ])
    if uf:
        log("[2] Entering identifier"); uf.fill(USER)
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
            log("[3] Switching to password login")
            try: tog.click(); time.sleep(2)
            except Exception as e: log(f"  toggle failed: {e}")
            pf = first_visible(page, ['input[type=password]'])
    if pf:
        log("[4] Entering password"); pf.fill(PASS)
        sub = first_visible(page, ["button:has-text('Login')", "button:has-text('Sign in')",
            "button:has-text('Submit')", "button:has-text('Continue')", "button[type=submit]"])
        if sub:
            try: sub.click(); log("[5] Submitted login")
            except Exception as e: log(f"  submit failed: {e}")
    else:
        log("[4] No password field — login may have changed."); shot(page, "login_fail")

    # Wait for REAL logged-in content (not just URL) + a settle, so the SPA finishes booting
    # and persists its auth token. The URL alone is unreliable (it flickers through /login).
    log("[5b] Waiting for post-login render…")
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
    time.sleep(4)  # let the loading spinner clear and the auth token persist
    shot(page, "07_post_login")
    log(f"  home_ready={home_ready()}, URL now: {page.url}")

    # ---------- GO TO BUSINESS METRICS (in-app nav, NOT a hard reload) ----------
    # A hard goto() to the deep URL re-boots the SPA and bounces to login.
    # Clicking the in-app "Reports" link is a client-side route and keeps the session.
    log("[6] Navigating to Reports / Business Metrics (in-app click)")
    def metrics_loaded():
        for sel in ["text=/Net Sales/i", "text=/Business Reports/i", "text=/Download Report/i"]:
            try:
                if page.locator(sel).first.count(): return True
            except Exception: pass
        return False
    reached = False
    nav = first_visible(page, [
        'a[href*="business-metrics"]', 'a[href*="reports" i]',
        "nav a:has-text('Reports')", "a:has-text('Reports')",
        "text=/^Reports$/i", "*:has-text('REPORTS')"
    ])
    if nav:
        try:
            nav.click()
            for _ in range(25):
                if metrics_loaded(): reached = True; break
                time.sleep(1)
        except Exception as e:
            log(f"  Reports nav click failed: {e}")
    else:
        log("  Reports nav link not found on home — see 07_post_login.png")
    if not reached:
        log("  In-app nav didn't reach metrics; trying direct URL as fallback")
        try:
            page.goto(METRICS_URL, wait_until="domcontentloaded", timeout=60000)
            for _ in range(25):
                if metrics_loaded(): reached = True; break
                time.sleep(1)
        except Exception as e:
            log(f"  direct nav failed: {e}")
    time.sleep(2)
    shot(page, "10_business_metrics")
    log(f"  URL now: {page.url}; metrics_loaded={reached}")
    if not reached:
        log("  (Business Metrics not detected — see 10_business_metrics.png.)")

    import json, re
    # Wait for real data (avoid skeleton capture).
    log("[6b] Waiting for metric values to load")
    for _ in range(25):
        try:
            b = page.inner_text("body")
            if ("₹" in b) or ("vs last" in b.lower()): break
        except Exception: pass
        time.sleep(1)
    time.sleep(2); shot(page, "10_business_metrics")

    # ===================== SCRAPER (validation scope) =====================
    # Mechanics: metric = a "Filters" panel (pick one radio + Apply);
    # date = Today/Yesterday/This Week/Last Week/This Month/Custom;
    # rows = Name | RID | value | delta.  Expand DATES/METRICS once confirmed.
    # ---- scrape scope ----
    # Efficient default (matches the open decision): headline Net Sales at all 3 ranges,
    # every other metric at MTD only. Set ALL_AT_ALL=True to scrape every metric at all 3 ranges.
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
    current_metric = "Net Sales"
    shot_filters_done = {"v": False}

    def push_supabase(rows):
        url = os.environ.get("SUPABASE_URL"); key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not url or not key:
            log("[10] Supabase: skipped (SUPABASE_URL / SUPABASE_SERVICE_KEY not set)"); return
        import urllib.request, urllib.error
        from datetime import datetime, timezone, timedelta
        cap = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()  # IST date
        payload = []
        for p in rows:
            if not p.get("rid"): continue
            payload.append({
                "captured_on": cap, "rid": p["rid"], "outlet_name": p["name"],
                "locality": p["locality"], "metric": p["metric"], "period": p["date"],
                "value": p["value"], "is_currency": p["currency"], "is_pct": p["is_pct"],
                "delta_pct": p["delta_pct"], "direction": (p["direction"] or None),
                "compare_label": (p["compare"] or None), "no_data": p["no_data"],
            })
        body = json.dumps(payload).encode("utf-8")
        endpoint = url.rstrip("/") + "/rest/v1/swiggy_metrics?on_conflict=captured_on,rid,metric,period"
        req = urllib.request.Request(endpoint, data=body, method="POST", headers={
            "apikey": key, "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                log(f"[10] Supabase: upserted {len(payload)} rows (HTTP {r.status})")
        except urllib.error.HTTPError as e:
            log(f"[10] Supabase HTTP {e.code}: {e.read().decode()[:300]}")
        except Exception as e:
            log(f"[10] Supabase error: {e}")

    log("[7] Opening Outlet Level Details")
    ol = first_visible(page, ["text=/See Outlet Level Data/i", "*:has-text('Outlet Level Data')"])
    if not ol:
        log("  'See Outlet Level Data' not found — see 10_business_metrics.png")
    else:
        try:
            ol.click()
            for _ in range(20):
                try:
                    if page.locator("text=/Outlet Level Details/i").first.count(): break
                except Exception: pass
                time.sleep(1)
            time.sleep(2); shot(page, "13_outlet_level")

            def set_date(label):
                # the date pill is NOT a <button> (button-only selectors failed last run);
                # use the text-regex pattern that opened the dropdown during discovery.
                pill = first_visible(page, [
                    "text=/^Today$/", "text=/^Yesterday$/", "text=/^This [Ww]eek$/",
                    "text=/^Last [Ww]eek$/", "text=/^This [Mm]onth$/", "text=/^Custom$/",
                    "*:has-text('Today')",
                ])
                if not pill:
                    log(f"   date pill not found ({label})"); return False
                try:
                    pill.click(); time.sleep(1.2)
                    opt = page.get_by_text(label, exact=False).first
                    if opt.count():
                        opt.click(); time.sleep(3); return True
                    log(f"   date option '{label}' not found"); page.keyboard.press("Escape")
                except Exception as e:
                    log(f"   set_date {label} error: {e}")
                return False

            def set_metric(label):
                global current_metric
                opener = None
                try:
                    c = page.get_by_text(current_metric, exact=True).first
                    if c.count(): opener = c
                except Exception: pass
                if opener is None:
                    opener = first_visible(page, [f"*:has-text('{current_metric}')"])
                if not opener:
                    log(f"   filters opener not found (current={current_metric})"); return False
                try:
                    opener.click(); time.sleep(1.5)
                    if not shot_filters_done["v"]:
                        shot(page, "23_filters_panel"); shot_filters_done["v"] = True
                    lab = page.get_by_text(label, exact=True)
                    if lab.count():
                        lab.last.click(); time.sleep(0.5)
                    else:
                        log(f"   metric '{label}' not in panel")
                    ap = first_visible(page, ["button:has-text('Apply')", "*:has-text('Apply')"])
                    if ap:
                        ap.click(); time.sleep(3); current_metric = label; return True
                    log("   Apply not found"); page.keyboard.press("Escape")
                except Exception as e:
                    log(f"   set_metric {label} error: {e}")
                return False

            def read_rows():
                out = []
                try:
                    rows = page.locator('[class*="ListItemContainer-business-metrics-mfe"]')
                    n = rows.count()
                    if n == 0:
                        anchors = page.locator("text=/RID:/")
                        for i in range(anchors.count()):
                            try:
                                t = anchors.nth(i).locator("xpath=ancestor::*[self::div][2]").inner_text().strip()
                            except Exception:
                                t = ""
                            out.append((t.replace("\n", " | "), ""))
                        return out
                    for i in range(n):
                        row = rows.nth(i)
                        try:
                            txt = row.inner_text().strip().replace("\n", " | ")
                        except Exception:
                            txt = ""
                        if "RID" not in txt:
                            continue
                        direction = ""
                        try:
                            img = row.locator("img").first
                            if img.count():
                                a = (img.get_attribute("alt") or "")
                                s = (img.get_attribute("src") or "")
                                fname = s.split("?")[0].rstrip("/").split("/")[-1].lower()
                                blob = (a.lower() + " " + fname)
                                if any(k in blob for k in ["up", "increase", "rise", "uptrend", "ascend", "arrow_up", "caret_up"]):
                                    direction = "up"
                                elif any(k in blob for k in ["down", "decrease", "fall", "drop", "decline", "downtrend", "descend", "arrow_down", "caret_down"]):
                                    direction = "down"
                                if read_rows.logc < 4:
                                    log(f"   ARROW[{read_rows.logc}] alt='{a}' file='{fname}' src='{s}'"); read_rows.logc += 1
                        except Exception:
                            pass
                        out.append((txt, direction))
                except Exception as e:
                    log(f"   read_rows error: {e}")
                return out
            read_rows.logc = 0

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

            from collections import OrderedDict
            import csv
            date_metrics = OrderedDict()
            for d, m in WORK:
                date_metrics.setdefault(d, [])
                if m not in date_metrics[d]:
                    date_metrics[d].append(m)

            data = []
            for d, mets in date_metrics.items():
                dok = set_date(d)
                log(f"[8] date='{d}' set={dok} ({len(mets)} metrics)")
                for m in mets:
                    mok = set_metric(m)
                    rws = read_rows()
                    log(f"   metric='{m}' set={mok} rows={len(rws)}")
                    for txt, direction in rws:
                        p = parse_row(txt)
                        p["date"] = d; p["metric"] = m; p["direction"] = direction
                        data.append(p)
                        log(f"     DATA | {d} | {m} | rid={p['rid']} val={p['value']} cur={p['currency']} pct={p['is_pct']} d%={p['delta_pct']} dir={direction} {p['compare']}")
                shot(page, f"scrape_{d.replace(' ', '_')}")

            with open(OUT / "scrape.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            with open(OUT / "scrape.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["date", "metric", "rid", "name", "locality", "value",
                            "currency", "is_pct", "delta_pct", "direction", "compare", "no_data"])
                for p in data:
                    w.writerow([p["date"], p["metric"], p["rid"], p["name"], p["locality"], p["value"],
                                p["currency"], p["is_pct"], p["delta_pct"], p["direction"], p["compare"], p["no_data"]])
            log(f"[9] wrote {len(data)} rows -> scrape.json + scrape.csv")
            try:
                push_supabase(data)
            except Exception as e:
                log(f"[10] Supabase wrapper error: {e}")
        except Exception as e:
            log(f"  scraper failed: {e}")

    (OUT/"result.txt").write_text("\n".join(log_lines), encoding="utf-8")
    log(f"\nArtifacts in: {OUT.resolve()}")
    ctx.close(); browser.close()