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
from playwright.sync_api import sync_playwright

log_lines = []
def log(m): print(m); log_lines.append(str(m))

def write_result_and_exit(code):
    try: (OUT / "result.txt").write_text("\n".join(log_lines), encoding="utf-8")
    except Exception: pass
    sys.exit(code)

# ---------------- accounts (tolerant parser) ----------------
ACCOUNTS = []
_raw = os.environ.get("SWIGGY_ACCOUNTS")
if _raw and _raw.strip():
    try:
        parsed = json.loads(_raw)
    except Exception as e:
        log(f"[FATAL] SWIGGY_ACCOUNTS is not valid JSON: {e}")
        log("        Check for: curly quotes instead of straight \", trailing comma after last }, "
            "unescaped \" or \\ in a password, or stray line breaks. Validate at jsonlint.com.")
        write_result_and_exit(1)
    if not isinstance(parsed, list):
        log("[FATAL] SWIGGY_ACCOUNTS must be a JSON list:  [ {\"label\":..,\"user\":..,\"pass\":..}, ... ]")
        write_result_and_exit(1)
    for i, a in enumerate(parsed):
        if not isinstance(a, dict):
            log(f"[skip] account[{i}] is not an object — ignored"); continue
        u, p = a.get("user"), a.get("pass")
        if not u or not p:
            log(f"[skip] account[{i}] (label={a.get('label','?')}) missing user/pass — ignored"); continue
        ACCOUNTS.append({"label": str(a.get("label") or u), "user": str(u), "pass": str(p)})
    log(f"[accounts] parsed {len(ACCOUNTS)} valid of {len(parsed)} entries in SWIGGY_ACCOUNTS")
elif os.environ.get("SWIGGY_USER") and os.environ.get("SWIGGY_PASS"):
    ACCOUNTS.append({"label": "default", "user": os.environ["SWIGGY_USER"], "pass": os.environ["SWIGGY_PASS"]})
else:
    log("[FATAL] No credentials: set SWIGGY_ACCOUNTS (JSON list) or SWIGGY_USER + SWIGGY_PASS.")
    write_result_and_exit(1)

if not ACCOUNTS:
    log("[FATAL] SWIGGY_ACCOUNTS parsed but contained no usable accounts (all entries skipped above).")
    write_result_and_exit(1)


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

        # ---------- OUTLET LEVEL DETAILS (tolerant) ----------
        # Some logins (single outlet, or ones that skip city/outlet selection) never show
        # the "See Outlet Level Data" link — the data is already on the page. Handle any
        # selection prompt, click the drill-down if present, and DO NOT bail if it's absent.
        def clear_selection_prompts():
            for sel in ["button:has-text('All Outlets')", "button:has-text('Select All')",
                        "button:has-text('All Cities')", "text=/^Select All Outlets$/i",
                        "button:has-text('Apply')", "button:has-text('Continue')", "button:has-text('Proceed')"]:
                try:
                    el = page.locator(sel).first
                    if el.count() and el.is_visible():
                        el.click(); time.sleep(1.2)
                except Exception:
                    pass

        clear_selection_prompts()
        ol = first_visible(page, ["text=/See Outlet Level Data/i", "*:has-text('Outlet Level Data')"])
        if ol:
            try:
                ol.click()
                for _ in range(20):
                    if page.locator("text=/Outlet Level Details/i").first.count(): break
                    time.sleep(1)
                time.sleep(2)
            except Exception as e:
                log(f"[{label}] outlet-level click failed: {e}")
            clear_selection_prompts()
        else:
            log(f"[{label}] no drill-down link — will scrape current page (likely single-outlet).")
            shot(page, "no_outlet_link", force=True)
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

        def _rid_of(t):
            m = re.search(r"RID:\s*(\d+)", t); return m.group(1) if m else None

        def _scroll_handle():
            try:
                return page.evaluate_handle("""() => {
                  const first=document.querySelector('[class*=\"ListItemContainer-business-metrics-mfe\"]');
                  if(!first) return null;
                  let el=first.parentElement;
                  while(el){
                    const s=getComputedStyle(el);
                    if((s.overflowY==='auto'||s.overflowY==='scroll') && el.scrollHeight>el.clientHeight+6) return el;
                    el=el.parentElement;
                  }
                  return null;
                }""")
            except Exception:
                return None

        def read_rows(diag=False):
            seen = {}; order = []
            def arrow(row):
                direction=""; sentiment=""
                try:
                    img=row.locator("img").first
                    if img.count():
                        s=(img.get_attribute("src") or "")
                        fname=s.split("?")[0].rstrip("/").split("/")[-1].lower()
                        if any(k in fname for k in ["up","increase","rise","uptrend","arrow_up","caret_up"]): direction="up"
                        elif any(k in fname for k in ["down","decrease","fall","drop","decline","arrow_down","caret_down"]): direction="down"
                        if "green" in fname: sentiment="good"
                        elif "red" in fname: sentiment="bad"
                except Exception: pass
                return direction, sentiment
            def snap():
                try:
                    rws=page.locator('[class*="ListItemContainer-business-metrics-mfe"]')
                    n=rws.count()
                except Exception:
                    n=0; rws=None
                for i in range(n):
                    row=rws.nth(i)
                    try: txt=row.inner_text().strip().replace("\n"," | ")
                    except Exception: continue
                    if "RID" not in txt: continue
                    rid=_rid_of(txt)
                    if not rid or rid in seen: continue
                    d,se=arrow(row)
                    seen[rid]=(txt,d,se); order.append(rid)
            # expand any explicit "load more / show all" control first
            for _ in range(10):
                btn=first_visible(page, ["button:has-text('Load more')","button:has-text('Show more')",
                                         "button:has-text('View all')","text=/show all outlets/i","text=/view all outlets/i"])
                if not btn: break
                try: btn.click(); time.sleep(1.0)
                except Exception: break
            handle=_scroll_handle()
            snap(); start_n=len(seen); stagnant=0; steps=0
            for _ in range(120):
                steps+=1
                try:
                    if handle:
                        at_bottom=page.evaluate("(el)=>{el.scrollTop=Math.min(el.scrollTop+el.clientHeight*0.85, el.scrollHeight); return el.scrollTop+el.clientHeight>=el.scrollHeight-6;}", handle)
                    else:
                        at_bottom=page.evaluate("()=>{window.scrollBy(0, Math.round(window.innerHeight*0.85)); return (window.innerHeight+Math.round(window.scrollY))>=document.body.scrollHeight-6;}")
                except Exception:
                    at_bottom=True
                time.sleep(0.45)
                before=len(seen); snap()
                if len(seen)==before: stagnant+=1
                else: stagnant=0
                if at_bottom and stagnant>=2: break
            try:
                if handle: page.evaluate("(el)=>el.scrollTop=0", handle)
                else: page.evaluate("()=>window.scrollTo(0,0)")
                time.sleep(0.3)
            except Exception: pass
            if diag:
                mode = "internal-scroll" if handle else "window/none"
                virt = "YES" if len(seen)>start_n else "no"
                log(f"   [rows] scroll={mode} first_paint={start_n} after_scroll={len(seen)} virtualized={virt} steps={steps}")
            return [(seen[r][0],seen[r][1],seen[r][2]) for r in order]

        # ---------- CALENDAR PROBE (PROBE_CUSTOM=1) ----------
        if os.environ.get("PROBE_CUSTOM"):
            log(f"[{label}] === CALENDAR PROBE ===")
            try:
                # open date pill, click Custom to reveal the calendar
                pill = first_visible(page, [
                    "text=/^Today$/", "text=/^Yesterday$/", "text=/^This [Ww]eek$/",
                    "text=/^Last [Ww]eek$/", "text=/^This [Mm]onth$/", "*:has-text('Today')",
                ])
                if pill: pill.click(); time.sleep(1.2)
                shot(page, "C1_date_open", force=True)
                cust = page.get_by_text("Custom", exact=False).first
                if cust.count(): cust.click(); time.sleep(2)
                shot(page, "C2_custom_calendar", force=True)

                dump = page.evaluate("""() => {
                  const pick = el => el ? el.outerHTML : null;
                  const months=/(January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{4}/;
                  let hdr=null;
                  for (const el of document.querySelectorAll('div,span,p,button,h1,h2,h3,h4,h5')) {
                    if (el.children.length===0 && months.test((el.textContent||'').trim())) { hdr=el; break; }
                  }
                  let cal=hdr;
                  for (let i=0;i<7 && cal;i++){ if (cal.querySelectorAll('button,td,[role=\\"gridcell\\"]').length>15) break; cal=cal.parentElement; }
                  const dlg=document.querySelector('[role=\\"dialog\\"]');
                  // sample a few day cells with their tag/class/text/disabled
                  let cells=[];
                  if (cal){
                    const cand=[...cal.querySelectorAll('button,td,[role=\\"gridcell\\"],div,span')]
                      .filter(e=>/^\\d{1,2}$/.test((e.textContent||'').trim()) && e.children.length===0);
                    cells=cand.slice(0,45).map(e=>({tag:e.tagName,cls:e.className,txt:e.textContent.trim(),
                      dis:e.getAttribute('disabled')!=null||e.getAttribute('aria-disabled')==='true',
                      opacity:getComputedStyle(e).opacity}));
                  }
                  const inputs=[...document.querySelectorAll('input')].map(i=>({ph:i.placeholder,val:i.value,ro:i.readOnly}));
                  const btns=[...document.querySelectorAll('button')].map(b=>b.textContent.trim()).filter(t=>t).slice(0,40);
                  return {header:hdr?hdr.textContent.trim():null, calendarHTML:pick(cal), dialogHTML:pick(dlg),
                          cells, inputs, buttons:btns};
                }""")

                (OUT/"custom_calendar.html").write_text(dump.get("calendarHTML") or dump.get("dialogHTML") or "(none)", encoding="utf-8")
                (OUT/"custom_probe.json").write_text(json.dumps({k:v for k,v in dump.items() if k not in ("calendarHTML","dialogHTML")}, indent=2, ensure_ascii=False), encoding="utf-8")
                log(f"   header text: {dump.get('header')}")
                log(f"   inputs: {dump.get('inputs')}")
                log(f"   buttons: {dump.get('buttons')}")
                log(f"   sample cells ({len(dump.get('cells') or [])}): {json.dumps((dump.get('cells') or [])[:8])}")
                log(f"   saved custom_calendar.html + custom_probe.json")

                # best-effort: click prev-chevron once + click a mid-month day, to observe behaviour
                try:
                    chev = first_visible(page, ["button[aria-label*='prev' i]", "button[aria-label*='previous' i]",
                                                "[class*='prev']", "text=/^‹$/", "text=/^</"])
                    if chev: chev.click(); time.sleep(1); shot(page, "C3_after_prev_chevron", force=True); log("   clicked a prev-chevron candidate")
                except Exception as e: log(f"   chevron click skipped: {e}")
                try:
                    day = page.get_by_text("15", exact=True).first
                    if day.count(): day.click(); time.sleep(1); shot(page, "C4_after_day15", force=True); log("   clicked a '15' candidate")
                except Exception as e: log(f"   day click skipped: {e}")
            except Exception as e:
                log(f"   PROBE error: {e}"); shot(page, "C_error", force=True)
            return rows, "probe_custom_done"

        def dump_dom(tag):
            try: (OUT / f"{tag}.html").write_text(page.content(), encoding="utf-8")
            except Exception: pass
            try: (OUT / f"{tag}.txt").write_text(page.inner_text("body"), encoding="utf-8")
            except Exception: pass
            shot(page, tag, force=True)

        # summary-tile label -> canonical metric (single-outlet pages show tiles, not a row list)
        TILE_SYNONYMS = [
            ("Net Sales", ["net sales"]),
            ("Delivered Orders", ["delivered orders", "delivered order", "delivered"]),
            ("Net AOV", ["net aov", "aov", "average order value"]),
            ("Restaurant Cancelled Orders", ["restaurant cancelled orders", "cancelled orders", "cancelled order"]),
            ("Cancelled Order Loss", ["cancelled order loss", "cancellation loss"]),
            ("Kitchen Prep Time", ["kitchen prep time", "avg prep time", "average prep time", "prep time", "kpt"]),
            ("Online Availability %", ["online availability", "online %", "online"]),
            ("Food Ready Accuracy (MFR)", ["food ready accuracy", "mfr", "food ready"]),
            ("Delayed Orders (> 10 mins)", ["delayed orders", "delayed order"]),
            ("Sales via Discounts", ["sales via discounts", "discount sales"]),
            ("Total CPC Spends", ["total cpc spends", "cpc spends", "cpc spend"]),
            ("CPC Driven Sales", ["cpc driven sales", "cpc sales"]),
            ("Total CBA Spends", ["total cba spends", "cba spends", "cba spend"]),
            ("Ad Impressions", ["ad impressions", "impressions"]),
        ]
        def _val_from(s):
            s = (s or "").strip()
            m = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", s)
            if m: return float(m.group(1).replace(",", "")), True, False
            m = re.search(r"([\d,]+(?:\.\d+)?)\s*%", s)
            if m: return float(m.group(1).replace(",", "")), False, True
            m = re.search(r"([\d,]+(?:\.\d+)?)\s*min", s, re.I)
            if m: return float(m.group(1).replace(",", "")), False, False
            m = re.fullmatch(r"([\d,]+(?:\.\d+)?)", s)
            if m: return float(m.group(1).replace(",", "")), False, False
            return None, False, False
        def read_tiles(d):
            try: body = page.inner_text("body")
            except Exception: return []
            rm = re.search(r"RID[:\s]*([0-9]{3,})", body)
            rid = rm.group(1) if rm else ""
            lines = [l.strip() for l in body.split("\n") if l.strip()]
            out = []; used = set()
            for i, l in enumerate(lines):
                low = l.lower()
                for canon, syns in TILE_SYNONYMS:
                    if canon in used: continue
                    hit = any(low == sy or low.startswith(sy + " ") or low == sy + ":" for sy in syns)
                    if not hit: continue
                    val = None; isc = False; isp = False
                    tail = l[len(l):]  # nothing; value usually on following line(s)
                    for j in range(i, min(i + 3, len(lines))):
                        cand = lines[j] if j > i else tail
                        v, c, p = _val_from(cand)
                        if v is None and j > i:
                            v, c, p = _val_from(lines[j])
                        if v is not None:
                            val, isc, isp = v, c, p; break
                    if val is not None:
                        out.append({"rid": rid, "name": label, "locality": "", "value": val,
                                    "currency": isc, "is_pct": isp, "delta_pct": None, "compare": "",
                                    "no_data": False, "account": label, "date": d, "metric": canon,
                                    "direction": "", "sentiment": ""})
                        used.add(canon)
            return out

        from collections import OrderedDict
        date_metrics = OrderedDict()
        for d, m in WORK:
            date_metrics.setdefault(d, [])
            if m not in date_metrics[d]:
                date_metrics[d].append(m)

        # ---- mode detection: is there an outlet row list, or a single-outlet tile page? ----
        set_date("This Month")
        single_mode = len(read_rows(diag=True)) == 0
        acct_rids = set()
        if single_mode:
            safe = re.sub(r"[^A-Za-z0-9]+", "_", label)[:24]
            log(f"[{label}] MODE: single-outlet/tile (no row list) — dumping layout as single_{safe}.*")
            dump_dom(f"single_{safe}")
            for d in ["Yesterday", "This Week", "This Month"]:
                dok = set_date(d)
                trows = read_tiles(d)
                for p in trows:
                    if p.get("rid"): acct_rids.add(p["rid"])
                rows.extend(trows)
                log(f"[{label}] [tiles] date='{d}' set={dok} -> {len(trows)} metrics")
            if not rows:
                log(f"[{label}] tile read empty — layout dumped for fixing.")
                return rows, "single_dumped"
            log(f"[{label}] OUTLETS SEEN: {len(acct_rids)} distinct RIDs -> {sorted(acct_rids)}")
            log(f"[{label}] scraped {len(rows)} rows (single-outlet)")
            return rows, "ok_single"

        for d, mets in date_metrics.items():
            dok = set_date(d)
            log(f"[{label}] [8] date='{d}' set={dok} ({len(mets)} metrics)")
            for mi, m in enumerate(mets):
                mok = set_metric(m)
                for txt, direction, sentiment in read_rows(diag=(mi == 0)):
                    p = parse_row(txt)
                    p["account"] = label; p["date"] = d; p["metric"] = m
                    p["direction"] = direction; p["sentiment"] = sentiment
                    if p.get("rid"): acct_rids.add(p["rid"])
                    rows.append(p)
            if do_shots: shot(page, f"scrape_{d.replace(' ', '_')}")
        log(f"[{label}] OUTLETS SEEN: {len(acct_rids)} distinct RIDs -> {sorted(acct_rids)}")
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