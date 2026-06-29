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

    # ---------- MAP THE LIVE PAGE (Path B: scrape) ----------
    log("[7] Mapping date selector")
    dc = first_visible(page, ["text=/Today,/i", "*:has-text('Today, ')", "button:has-text('Today')"])
    if dc:
        try:
            dc.click(); time.sleep(2); shot(page, "12_date_options")
            log("  date options -> 12_date_options.png")
            page.keyboard.press("Escape"); time.sleep(1)
        except Exception as e: log(f"  date click failed: {e}")
    else:
        log("  date control not found")

    log("[8] Opening 'See Outlet Level Data'")
    ol = first_visible(page, ["text=/See Outlet Level Data/i", "*:has-text('Outlet Level Data')"])
    if ol:
        try:
            ol.click(); time.sleep(2); shot(page, "13_outlet_level")
            log("  outlet-level -> 13_outlet_level.png")
        except Exception as e: log(f"  outlet-level click failed: {e}")
    else:
        log("  'See Outlet Level Data' not found")

    log("[9] Sampling category tabs")
    for tab, fname in [("Operations", "14_operations"), ("Ads", "15_ads"), ("Funnel", "16_funnel")]:
        t = first_visible(page, [f'text="{tab}"'])
        if t:
            try:
                t.click(); time.sleep(2); shot(page, fname); log(f"  {tab} -> {fname}.png")
            except Exception as e: log(f"  {tab} tab click failed: {e}")
        else:
            log(f"  {tab} tab not found")

    # ---------- ADVANCE THE DOWNLOAD WIZARD ONE STEP (Path A: xlsx) ----------
    log("[10] Opening Download Report wizard")
    dl_btn = first_visible(page, ["button:has-text('Download Report')", "*:has-text('Download Report')"])
    if dl_btn:
        try:
            dl_btn.click(); time.sleep(2); shot(page, "17_brand_modal")
            log("  brand modal -> 17_brand_modal.png")
            cb = first_visible(page, ["input[type=checkbox]"])
            if cb:
                try: cb.check(); time.sleep(1)
                except Exception:
                    try: cb.click(); time.sleep(1)
                    except Exception as e: log(f"  brand select failed: {e}")
            cont = first_visible(page, ["button:has-text('Continue')", "button:has-text('CONTINUE')"])
            if cont:
                try:
                    cont.click(); time.sleep(3); shot(page, "18_wizard_step2")
                    log("  wizard step 2 -> 18_wizard_step2.png")
                except Exception as e: log(f"  continue failed: {e}")
            else:
                log("  Continue not found in modal")
        except Exception as e:
            log(f"  download wizard failed: {e}")
    else:
        log("  'Download Report' button not found")

    (OUT/"result.txt").write_text("\n".join(log_lines), encoding="utf-8")
    log(f"\nArtifacts in: {OUT.resolve()}")
    ctx.close(); browser.close()