# intercept_scrape_ui.py
# Setup:
#   python -m venv .venv && source .venv/bin/activate
#   pip install playwright lxml
#   python -m playwright install chromium
#
# Run:
#   python intercept_scrape_ui.py

import asyncio
import re
import sqlite3
from pathlib import Path
from lxml import etree as ET
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Error as PWError

# ---------- Config ----------
COURSE_CODES_FILE = Path("course_codes.txt")
DB_FILE  = Path("mytimetable.db")
LOG_DIR  = Path("logs")

BASE = "https://mytimetable.mcmaster.ca"
CRITERIA = f"{BASE}/criteria.jsp"

TERM_LINK_TEXT         = "Fall"
COMBO_NAME_WITH_DOTS   = "Select Course..."
COMBO_NAME_PLAIN       = "Select Course"
REMOVE_BUTTON_TEXT     = "Remove course"

# speed/timeout tuning (milliseconds)
UI_TIMEOUT   = 1_200
NAV_TIMEOUT  = 4_000
API_TIMEOUT  = 1_500   # wait for /api/class-data after Enter (kept short)
POLL_INTERVAL = 150
TYPE_DELAY   = 25

TIMEZONE_ID = "America/Toronto"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125 Safari/537.36")

# Vendor uses 1=Sun, 2=Mon, ... 7=Sat
DAY_MAP = {"1":"Sun","2":"Mon","3":"Tue","4":"Wed","5":"Thu","6":"Fri","7":"Sat"}
COURSE_LINE_RE = re.compile(r"^\s*([A-Za-z]+)\s+([0-9A-Za-z]+)\s*$")

# ---------- DB / XML helpers ----------
def minutes_to_hhmm(m: int) -> str:
    m = int(m)
    return f"{m//60:02d}:{m%60:02d}"

def ensure_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS courses (
        course_key TEXT PRIMARY KEY,
        code       TEXT,
        number     TEXT,
        title      TEXT,
        term_label TEXT,
        raw_term   TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS selections (
        selection_key TEXT PRIMARY KEY,
        course_key    TEXT,
        variant_va    TEXT,
        FOREIGN KEY(course_key) REFERENCES courses(course_key)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS blocks (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        selection_key TEXT,
        block_type    TEXT,
        sec_no        TEXT,
        room          TEXT,    -- <- room/location from XML
        timeblock_id  TEXT,
        day_num       INTEGER,
        day_name      TEXT,
        start_min     INTEGER,
        end_min       INTEGER,
        start_time    TEXT,
        end_time      TEXT,
        FOREIGN KEY(selection_key) REFERENCES selections(selection_key)
    )""")
    con.commit()
    con.close()

def parse_addcourse_xml(xml_text: str):
    root = ET.fromstring(xml_text.encode("utf-8"))
    classdata = root.find(".//classdata")
    course_el = root.find(".//course")
    if course_el is None or classdata is None:
        # include errors if present for debugging
        err_node = root.find(".//errors")
        err_msg = ""
        if err_node is not None:
            parts = [(err_node.text or "").strip()]
            for e in err_node.findall(".//error"):
                parts.append((e.text or "").strip())
            err_msg = " | ".join([p for p in parts if p])
        raise ValueError(f"No <classdata>/<course>. errors='{err_msg}'")

    course = {
        "course_key": course_el.get("key"),
        "code":       course_el.get("code"),
        "number":     course_el.get("number"),
        "title":      course_el.get("title") or "",
        "term_label": (classdata.find("./term").get("v") if classdata.find("./term") is not None else ""),
        "raw_term":   (classdata.find("./term").get("n") if classdata.find("./term") is not None else "")
    }

    selections = []
    for uselection in course_el.findall("./uselection"):
        selection_el = uselection.find("./selection")
        if selection_el is None:
            continue
        sel = {
            "selection_key": selection_el.get("key"),
            "variant_va":    selection_el.get("va") or "",
            "blocks": []
        }
        tb_map = {
            tb.get("id"): {
                "id": tb.get("id"),
                "day": tb.get("day"),
                "t1":  tb.get("t1"),
                "t2":  tb.get("t2"),
            } for tb in uselection.findall("./timeblock")
        }
        for blk in selection_el.findall("./block"):
            ids = (blk.get("timeblockids") or "").split(",")
            timeblocks = [tb_map[i] for i in ids if i in tb_map]
            sel["blocks"].append({
                "type":     blk.get("type"),
                "secNo":    blk.get("secNo"),
                "room":     blk.get("location") or "",  # <- room
                "timeblocks": timeblocks
            })
        selections.append(sel)

    return course, selections

def save_to_db(course, selections):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
    INSERT OR REPLACE INTO courses(course_key, code, number, title, term_label, raw_term)
    VALUES (?, ?, ?, ?, ?, ?)""",
        (course["course_key"], course["code"], course["number"], course["title"],
         course["term_label"], course["raw_term"])
    )
    for sel in selections:
        cur.execute("""
        INSERT OR REPLACE INTO selections(selection_key, course_key, variant_va)
        VALUES (?, ?, ?)""",
            (sel["selection_key"], course["course_key"], sel["variant_va"])
        )
        for blk in sel["blocks"]:
            for tb in blk["timeblocks"]:
                dn, t1, t2 = int(tb["day"]), int(tb["t1"]), int(tb["t2"])
                cur.execute("""
                INSERT INTO blocks(selection_key, block_type, sec_no, room, timeblock_id,
                                   day_num, day_name, start_min, end_min, start_time, end_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (sel["selection_key"], blk["type"], blk["secNo"], (blk["room"] or None), tb["id"],
                     dn, DAY_MAP.get(str(dn), str(dn)),
                     t1, t2, minutes_to_hhmm(t1), minutes_to_hhmm(t2))
                )
    con.commit()
    con.close()

# ---------- UI helpers ----------
async def safe_snapshot(page, name, quiet=False):
    try:
        LOG_DIR.mkdir(exist_ok=True)
        await page.screenshot(path=str(LOG_DIR / f"{name}.png"), full_page=True)
    except PWError as e:
        if not quiet:
            print(f"  (snapshot skipped: {e})")

def parse_course_line(line: str):
    m = COURSE_LINE_RE.match(line)
    if not m:
        return None, None
    return m.group(1).upper(), m.group(2).upper()

async def get_combobox(page):
    for loc in [
        page.get_by_role("combobox", name=COMBO_NAME_WITH_DOTS),
        page.get_by_role("combobox", name=COMBO_NAME_PLAIN),
    ]:
        try:
            await loc.wait_for(state="visible", timeout=UI_TIMEOUT)
            return loc
        except Exception:
            pass
    for sel in ["#code_number", "input[aria-label*='Select Course' i]",
                "[role=combobox] input", "input[placeholder*='Select Course' i]"]:
        loc = page.locator(sel)
        try:
            await loc.wait_for(state="visible", timeout=UI_TIMEOUT)
            return loc
        except Exception:
            continue
    raise PWTimeout("Could not find the 'Select Course' combobox.")

async def click_term(page, name: str):
    try:
        await page.get_by_role("link", name=name).click(timeout=NAV_TIMEOUT)
    except Exception:
        pass

def api_predicate_for(subj: str, num: str):
    target = f"{subj}-{num}"
    def predicate(resp):
        if "/api/class-data" not in resp.url:
            return False
        u = resp.url
        return any(f"course_{i}_0={target}" in u for i in range(5))
    return predicate

async def remove_all_courses(page):
    """
    Clicks 'Remove course' until no such buttons remain,
    then waits for the left panel hint 'No Courses Selected' / zero results.
    """
    try:
        while True:
            btns = await page.get_by_role("button", name=REMOVE_BUTTON_TEXT).all()
            if not btns:
                break
            await btns[0].click(timeout=UI_TIMEOUT)
            await page.wait_for_timeout(80)
    except Exception:
        pass
    # best-effort confirmation the pane is clear
    for sel in ["text=No Courses Selected", "text=Select at least one course",
                "#noCoursesSelected", "#page_schedules_count:has-text('0 OF 0')"]:
        try:
            await page.locator(sel).first.wait_for(timeout=600)
            break
        except Exception:
            continue

async def try_add_course(page, subj: str, num: str):
    """
    Press Enter to add; then race:
      - API response -> ("saved", xml_text)
      - toast 'could not be' -> ("skip_not_found", None)
      - toast 'only available' -> ("skip_wrong_term", None)
    Else -> ("no_option", None)
    """
    await page.wait_for_selector("#page_criteria", timeout=UI_TIMEOUT)
    combo = await get_combobox(page)
    await combo.click()
    await combo.fill(f"{subj} {num}")
    await combo.press("Enter")

    api_task = asyncio.create_task(
        page.wait_for_event("response", predicate=api_predicate_for(subj, num), timeout=API_TIMEOUT)
    )
    could_not = page.get_by_text("could not be", exact=False)
    only_avail = page.get_by_text("only available", exact=False)

    steps = max(1, API_TIMEOUT // POLL_INTERVAL)
    for _ in range(steps):
        if await could_not.is_visible(timeout=0):
            if not api_task.done():
                api_task.cancel()
            return ("skip_not_found", None)
        if await only_avail.is_visible(timeout=0):
            if not api_task.done():
                api_task.cancel()
            return ("skip_wrong_term", None)
        if api_task.done():
            try:
                resp = await api_task
                return ("saved", await resp.text())
            except Exception:
                return ("no_option", None)
        await page.wait_for_timeout(POLL_INTERVAL)

    if not api_task.done():
        api_task.cancel()
        return ("no_option", None)
    try:
        resp = await api_task
        return ("saved", await resp.text())
    except Exception:
        return ("no_option", None)

# ---------- Main ----------
async def run():
    ensure_db()
    LOG_DIR.mkdir(exist_ok=True)

    if not COURSE_CODES_FILE.exists():
        print(f"Missing {COURSE_CODES_FILE}. Put lines like 'MATH 1A03' in it.")
        return

    lines = [ln.strip() for ln in COURSE_CODES_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    course_pairs = []
    for ln in lines:
        subj, num = parse_course_line(ln)
        if subj and num:
            course_pairs.append((subj, num))
        else:
            print(f"Skipping bad line: {ln}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # set True when happy
        context = await browser.new_context(
            timezone_id=TIMEZONE_ID, locale="en-CA", user_agent=USER_AGENT,
        )
        page = await context.new_page()
        page.set_default_timeout(UI_TIMEOUT)
        page.set_default_navigation_timeout(NAV_TIMEOUT)

        await page.goto(CRITERIA, wait_until="domcontentloaded")
        await click_term(page, TERM_LINK_TEXT)
        await page.wait_for_timeout(250)

        saved = skipped = failed = 0

        for subj, num in course_pairs:
            print(f"\n=== {subj} {num} ===")
            try:
                status, payload = await try_add_course(page, subj, num)
            except Exception as e:
                print(f"  ! Unexpected error: {e}")
                await safe_snapshot(page, f"{subj}-{num}_error", quiet=True)
                failed += 1
                # always clear before next course
                await remove_all_courses(page)
                continue

            if status == "saved" and payload:
                # raw xml for auditing
                (LOG_DIR / f"{subj}-{num}.xml").write_text(payload, encoding="utf-8")
                try:
                    course, selections = parse_addcourse_xml(payload)
                    if selections:
                        save_to_db(course, selections)
                        print(f"  ✓ Saved {subj} {num}: {len(selections)} selection(s)")
                        saved += 1
                    else:
                        print("  - No selections/timeblocks (empty XML).")
                        skipped += 1
                except Exception as pe:
                    print(f"  ! Parse error: {pe}")
                    await safe_snapshot(page, f"{subj}-{num}_parse_error", quiet=True)
                    failed += 1

            elif status == "skip_not_found":
                print('  - Skipped (UI: "could not be found").')
                skipped += 1

            elif status == "skip_wrong_term":
                print('  - Skipped (UI: "only available" – e.g., Winter-only).')
                skipped += 1

            elif status == "no_option":
                print("  - Skipped (no match in this term / no API fired).")
                skipped += 1

            else:
                print("  ! Unknown state; skipping.")
                failed += 1

            # ALWAYS remove any added course(s) before next loop
            await remove_all_courses(page)
            await page.wait_for_timeout(80)

        print("\nSummary:")
        print(f"  Saved:   {saved}")
        print(f"  Skipped: {skipped}")
        print(f"  Failed:  {failed}")
        print(f"  DB: {DB_FILE.resolve()}  |  XMLs: {LOG_DIR.resolve()}")

        await context.close()
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
