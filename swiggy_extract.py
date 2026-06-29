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

    # Detect login by PAGE CONTENT, not URL. Swiggy is a SPA: the URL can transiently
    # sit at /food/login or use hash routing (#!/login) while the dashboard is already up.
    log("[5b] Waiting for post-login render…")
    def logged_in_now():
        for sel in ["text=/Growth home/i", "text=/Growth Boosters/i", "text=/Logout/i",
                    "text=/Sign out/i", "text=/All Benefits/i"]:
            try:
                if page.locator(sel).first.count() and page.locator(sel).first.is_visible():
                    return True
            except Exception: pass
        u = page.url.lower()
        return ("/food/" in u) and not u.split("/food/", 1)[-1].lstrip("#!/").startswith("login")
    for _ in range(25):
        if logged_in_now(): break
        time.sleep(1)
    shot(page, "07_post_login")
    log(f"  authenticated={logged_in_now()}, URL now: {page.url}")

    # ---------- BUSINESS METRICS ----------
    # Navigate regardless — the session cookie carries us even if the URL looked like login.
    log(f"[6] Opening {METRICS_URL}")
    page.goto(METRICS_URL, wait_until="domcontentloaded", timeout=60000)
    metrics_ok = False
    for _ in range(25):
        try:
            if (page.locator("text=/Net Sales/i").first.count() or
                page.locator("text=/Business Reports/i").first.count()):
                metrics_ok = True; break
        except Exception: pass
        time.sleep(1)
    time.sleep(2)
    shot(page, "10_business_metrics")
    log(f"  URL now: {page.url}; metrics_loaded={metrics_ok}")
    if not metrics_ok:
        log("  (Net Sales/Business Reports text not detected — check 10_business_metrics.png; "
            "may be a render delay or layout change.)")

    # ---------- DOWNLOAD REPORT ----------
    dl_btn = first_visible(page, [
        "button:has-text('Download Report')", "a:has-text('Download Report')",
        "*:has-text('Download Report')", "button:has-text('Download')"
    ])
    if dl_btn:
        log("[7] Clicking 'Download Report'")
        downloaded = False
        try:
            with page.expect_download(timeout=45000) as di:
                dl_btn.click()
            dl = di.value
            dest = OUT / (dl.suggested_filename or "business_metrics_report.xlsx")
            dl.save_as(str(dest))
            log(f"  DOWNLOADED: {dest.name} ({dest.stat().st_size} bytes)")
            downloaded = True
        except Exception as e:
            log(f"  No direct download captured ({e}).")
        time.sleep(3)
        shot(page, "11_after_download_click")   # reveals any date/format dialog if one appeared
        if not downloaded:
            log("  -> a dialog may have appeared (date range / email). See 11_after_download_click.png")
    else:
        log("[7] 'Download Report' button not found — see 10_business_metrics.png")

    # ---------- SCOUT THE DATE SELECTOR (for Phase 2) ----------
    log("[8] Scouting the date control")
    date_ctrl = first_visible(page, [
        "text=/Today,/i", "text=/^Today/i", "*:has-text('Today, ')",
        "button:has-text('Today')", "*:has-text('Last Monday')"
    ])
    if date_ctrl:
        try:
            date_ctrl.click(); time.sleep(2); shot(page, "12_date_options")
            log("  clicked date header — see 12_date_options.png for available ranges")
        except Exception as e:
            log(f"  date click failed: {e}")
    else:
        log("  date header not found — check 10_business_metrics.png")

    (OUT/"result.txt").write_text("\n".join(log_lines), encoding="utf-8")
    log(f"\nArtifacts in: {OUT.resolve()}")
    ctx.close(); browser.close()