#!/usr/bin/env python3
"""
Swiggy Partner login PROBE — diagnostic only.

Goal: find out whether a headless PASSWORD login from THIS machine (your Oracle VM)
logs straight into the dashboard, or gets challenged with an OTP / captcha / device check.
It does NOT scrape anything. It logs in once, screenshots each step, prints a verdict.

Run it on the Oracle VM (open internet) — NOT a laptop — so the result reflects the real
IP/environment the automation would run from. (A laptop may be a "trusted device" and pass
even if the server gets challenged.)

Setup (once):
    pip install playwright
    playwright install chromium

Run:
    export SWIGGY_USER='98xxxxxxxx'     # the mobile / email / restaurant-id you log in with
    export SWIGGY_PASS='your-password'
    export HEADLESS=true                # 'false' to watch a real browser if the VM has a display
    python3 swiggy_login_probe.py

Output -> ./swiggy_probe_out/  (screenshots + result.txt). Send those back.
Credentials are read from env vars on purpose — do NOT hardcode them or commit them anywhere.
"""
import os, sys, time, pathlib

LOGIN_URL = "https://partner.swiggy.com/login"   # edit if your actual login page differs
OUT = pathlib.Path("swiggy_probe_out"); OUT.mkdir(exist_ok=True)

USER = os.environ.get("SWIGGY_USER")
PASS = os.environ.get("SWIGGY_PASS")
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"

if not USER or not PASS:
    sys.exit("Set SWIGGY_USER and SWIGGY_PASS environment variables first.")

from playwright.sync_api import sync_playwright
log_lines = []
def log(m):
    print(m); log_lines.append(str(m))

def shot(page, name):
    p = OUT / f"{name}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
        log(f"  saved {p}")
    except Exception as e:
        log(f"  (screenshot {name} failed: {e})")

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
    ctx = browser.new_context()
    page = ctx.new_page()

    log(f"[1] Opening {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)
    shot(page, "01_landing")
    log(f"  URL now: {page.url}")

    # Many flows ask for mobile / id first. Fill the first text-like field.
    user_field = first_visible(page, [
        'input[type=tel]', 'input[name*="mobile" i]', 'input[name*="phone" i]',
        'input[name*="email" i]', 'input[name*="user" i]', 'input[type=email]', 'input[type=text]'
    ])
    if user_field:
        log("[2] Entering username / identifier")
        user_field.fill(USER)
        shot(page, "02_user_filled")
        cont = first_visible(page, [
            "button:has-text('Continue')", "button:has-text('Next')",
            "button:has-text('Proceed')", "button:has-text('Login')",
            "button:has-text('Sign in')", "button[type=submit]"
        ])
        if cont:
            try:
                cont.click(); time.sleep(3); shot(page, "03_after_continue")
            except Exception as e:
                log(f"  continue click failed: {e}")
    else:
        log("[2] No obvious username field found — inspect 01_landing.png")

    # If the page defaults to OTP, look for a 'login with password' toggle.
    pwd_field = first_visible(page, ['input[type=password]'])
    if not pwd_field:
        pwd_toggle = first_visible(page, [
            "a:has-text('password')", "button:has-text('password')",
            "*:has-text('Login with password')", "*:has-text('Use password')",
            "text=/password/i"
        ])
        if pwd_toggle:
            log("[3] Switching to 'login with password'")
            try:
                pwd_toggle.click(); time.sleep(2); shot(page, "04_password_mode")
            except Exception as e:
                log(f"  toggle click failed: {e}")
            pwd_field = first_visible(page, ['input[type=password]'])

    if pwd_field:
        log("[4] Entering password")
        pwd_field.fill(PASS)
        shot(page, "05_password_filled")
        submit = first_visible(page, [
            "button:has-text('Login')", "button:has-text('Sign in')",
            "button:has-text('Submit')", "button:has-text('Continue')",
            "button[type=submit]"
        ])
        if submit:
            try:
                submit.click(); log("[5] Submitted login")
            except Exception as e:
                log(f"  submit click failed: {e}")
        time.sleep(6)
        shot(page, "06_after_submit")
        log(f"  URL now: {page.url}")
    else:
        log("[4] No password field appeared — login may be OTP-only on this page. Check screenshots.")

    # verdict
    body = ""
    try:
        body = page.inner_text("body").lower()
    except Exception:
        pass
    url = page.url.lower()

    otp_field = first_visible(page, [
        'input[name*="otp" i]', 'input[autocomplete="one-time-code"]',
        "input[maxlength='1']", "input[maxlength='4']", "input[maxlength='6']"
    ])
    otp_words = any(w in body for w in
        ["otp", "one time password", "one-time password", "verification code", "enter the code"])
    captcha = any(w in body for w in
        ["captcha", "recaptcha", "are you a robot", "verify you are human"])
    logged_in = any(w in url for w in ["dashboard", "home", "business", "/food", "outlet"]) or \
                any(w in body for w in ["business metrics", "orders today", "logout", "sign out", "payout"])

    log("\n================ VERDICT ================")
    if captcha:
        log("RESULT: CHALLENGED — CAPTCHA. Unattended login will be hard here.")
    elif otp_field or otp_words:
        log("RESULT: CHALLENGED — OTP step-up. Password ALONE is not enough from this machine.")
    elif logged_in:
        log("RESULT: LOGGED IN — password login went straight through. Auto-login is viable.")
    else:
        log("RESULT: UNKNOWN — could not classify. Read 06_after_submit.png + result.txt.")
    log("========================================")

    (OUT / "result.txt").write_text("\n".join(log_lines), encoding="utf-8")
    log(f"\nFull log + screenshots in: {OUT.resolve()}")
    ctx.close(); browser.close()
