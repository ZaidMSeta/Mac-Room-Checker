# fast_api_scrape.py
# Setup:
#   python -m venv .venv && source .venv/bin/activate
#   pip install playwright lxml
#   python -m playwright install chromium
#
# Run:
#   python fast_api_scrape.py

import asyncio, re, sqlite3
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from lxml import etree as ET
from playwright.async_api import async_playwright, Page

COURSE_CODES_FILE = Path("course_codes.txt")
DB_FILE  = Path("mytimetable.db")
SKIP_LOG = Path("skipped_courses.txt")

BASE = "https://mytimetable.mcmaster.ca"
TERM_QUERY = "3202530"  # 2025 Fall as you’ve been using

TIMEZONE_ID = "America/Toronto"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125 Safari/537.36")

MAX_CONCURRENCY = 6      # bump to 8–10 if stable
REQ_TIMEOUT_MS  = 3500   # per-request timeout inside the page

DAY_MAP = {"1":"Sun","2":"Mon","3":"Tue","4":"Wed","5":"Thu","6":"Fri","7":"Sat"}
COURSE_LINE_RE = re.compile(r"^\s*([A-Za-z]+)\s+([0-9A-Za-z]+)\s*$")
LOC_SPLIT_RE = re.compile(r"^\s*([A-Z][A-Z0-9-]{1,6})\s+([A-Za-z0-9-]{1,10})\s*$")

def minutes_to_hhmm(m: int) -> str:
    m = int(m); return f"{m//60:02d}:{m%60:02d}"

def ensure_db():
    con = sqlite3.connect(DB_FILE); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS courses(
      course_key TEXT PRIMARY KEY, code TEXT, number TEXT, title TEXT, term_label TEXT, raw_term TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS selections(
      selection_key TEXT PRIMARY KEY, course_key TEXT, variant_va TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS blocks(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      selection_key TEXT, block_type TEXT, sec_no TEXT,
      location TEXT, building TEXT, room TEXT,
      instruction_mode TEXT, is_online INTEGER,
      timeblock_id TEXT, day_num INTEGER, day_name TEXT,
      start_min INTEGER, end_min INTEGER, start_time TEXT, end_time TEXT)""")
    # add missing columns for older DBs
    for col, typ in [("location","TEXT"),("building","TEXT"),("room","TEXT"),
                     ("instruction_mode","TEXT"),("is_online","INTEGER DEFAULT 0")]:
        try: cur.execute(f"ALTER TABLE blocks ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError: pass
    con.commit(); con.close()

def parse_location_parts(location: str) -> Tuple[Optional[str], Optional[str]]:
    if not location: return None, None
    m = LOC_SPLIT_RE.match(location.strip().upper())
    return (m.group(1), m.group(2)) if m else (None, None)

def parse_addcourse_xml(xml_text: str):
    root = ET.fromstring(xml_text.encode("utf-8"))
    classdata = root.find(".//classdata"); course_el = root.find(".//course")
    if course_el is None or classdata is None:
        err_node = root.find(".//errors"); msg = ""
        if err_node is not None:
            parts = []
            if (err_node.text or "").strip(): parts.append(err_node.text.strip())
            for e in err_node.findall(".//error"):
                t = (e.text or "").strip()
                if t: parts.append(t)
            msg = " | ".join(parts)
        raise ValueError(msg or "no_classdata_or_course")

    course = {
        "course_key": course_el.get("key"),
        "code": course_el.get("code"),
        "number": course_el.get("number"),
        "title": course_el.get("title") or "",
        "term_label": (classdata.find("./term").get("v") if classdata.find("./term") is not None else ""),
        "raw_term":   (classdata.find("./term").get("n") if classdata.find("./term") is not None else "")
    }

    selections = []
    ONLINE_IM = {"V","PV","TV","ONL"}

    for uselection in course_el.findall("./uselection"):
        sel_el = uselection.find("./selection")
        if sel_el is None: continue
        sel = {"selection_key": sel_el.get("key"), "variant_va": sel_el.get("va") or "", "blocks": []}
        tb_map = {tb.get("id"): {"id": tb.get("id"), "day": tb.get("day"), "t1": tb.get("t1"), "t2": tb.get("t2")}
                  for tb in uselection.findall("./timeblock")}
        for blk in sel_el.findall("./block"):
            ids = (blk.get("timeblockids") or "").split(",")
            timeblocks = [tb_map[i] for i in ids if i in tb_map]
            loc_raw = blk.get("location") or ""
            im_val = (blk.get("im") or "").strip().upper()
            building, room = parse_location_parts(loc_raw)
            is_online = 1 if (im_val in ONLINE_IM or loc_raw == "") else 0
            sel["blocks"].append({
                "type": blk.get("type"),
                "secNo": blk.get("secNo"),
                "location": loc_raw,
                "building": building,
                "room": room,
                "instruction_mode": im_val,
                "is_online": is_online,
                "timeblocks": timeblocks
            })
        selections.append(sel)

    return course, selections

def save_to_db(course, selections):
    con = sqlite3.connect(DB_FILE); cur = con.cursor()
    cur.execute("""INSERT OR REPLACE INTO courses(course_key,code,number,title,term_label,raw_term)
                   VALUES(?,?,?,?,?,?)""",
                (course["course_key"],course["code"],course["number"],course["title"],course["term_label"],course["raw_term"]))
    for sel in selections:
        cur.execute("""INSERT OR REPLACE INTO selections(selection_key,course_key,variant_va)
                       VALUES(?,?,?)""", (sel["selection_key"],course["course_key"],sel["variant_va"]))
        for blk in sel["blocks"]:
            for tb in blk["timeblocks"]:
                dn, t1, t2 = int(tb["day"]), int(tb["t1"]), int(tb["t2"])
                cur.execute("""INSERT INTO blocks(selection_key,block_type,sec_no,location,building,room,
                                                  instruction_mode,is_online,timeblock_id,day_num,day_name,
                                                  start_min,end_min,start_time,end_time)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (sel["selection_key"], blk["type"], blk["secNo"], blk["location"], blk["building"],
                             blk["room"], blk["instruction_mode"], blk["is_online"], tb["id"], dn,
                             DAY_MAP.get(str(dn), str(dn)), t1, t2, minutes_to_hhmm(t1), minutes_to_hhmm(t2)))
    con.commit(); con.close()

def log_skip(subj: str, num: str, reason: str):
    with SKIP_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{subj} {num}\t{reason}\n")

async def page_fetch_xml(page: Page, subj: str, num: str) -> Tuple[str, str, Optional[str]]:
    """
    Do the GET from inside the page with window.fetch to satisfy tz checks.
    Returns (status, reason, xml_or_None).
    """
    target = f"{subj}-{num}"
    # Build URL *in the page* so Date.now() is the page's clock
    js = """
    async ([base, term, target, timeoutMs]) => {
      const ts = Date.now();
      const url = `${base}/api/class-data?term=${term}&course_0_0=${encodeURIComponent(target)}&nouser=1&_=${ts}`;
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort("timeout"), timeoutMs);
      try {
        const res = await fetch(url, { method: "GET", credentials: "include", signal: ctrl.signal });
        if (!res.ok) return ["skip", `http_${res.status}`, null];
        const text = await res.text();
        return ["ok", "", text];
      } catch (e) {
        return ["skip", (e && e.message) ? e.message : "fetch_error", null];
      } finally {
        clearTimeout(t);
      }
    }
    """
    status, reason, xml = await page.evaluate(js, [BASE, TERM_QUERY, target, REQ_TIMEOUT_MS])
    if status != "ok":
        return ("skip", reason, None)
    # quick sanity check; if it's just the error wrapper, bubble a reason
    if "<course " not in xml or "<classdata " not in xml:
        try:
            root = ET.fromstring(xml.encode("utf-8"))
            err_node = root.find(".//errors"); msg = ""
            if err_node is not None:
                parts = []
                if (err_node.text or "").strip(): parts.append(err_node.text.strip())
                for e in err_node.findall(".//error"):
                    t = (e.text or "").strip()
                    if t: parts.append(t)
                msg = " | ".join(parts)
            return ("skip", msg or "no_course_node", None)
        except Exception:
            return ("skip", "bad_xml", None)
    return ("ok", "", xml)

async def process_one(page: Page, subj: str, num: str, stats: Dict[str,int], sem: asyncio.Semaphore):
    async with sem:
        status, reason, xml = await page_fetch_xml(page, subj, num)
        if status == "ok" and xml:
            try:
                course, selections = parse_addcourse_xml(xml)
                if not selections:
                    log_skip(subj, num, "no_uselection"); stats["skipped"] += 1
                    print(f"  - {subj} {num}   skipped: no_uselection")
                    return
                save_to_db(course, selections)
                stats["saved"] += 1
                print(f"  ✓ {subj} {num}   selections={len(selections)}")
            except Exception as pe:
                stats["failed"] += 1
                log_skip(subj, num, f"parse_error:{str(pe)[:120]}")
                print(f"  ! {subj} {num}   parse_error: {pe}")
        else:
            stats["skipped"] += 1
            log_skip(subj, num, reason or "unknown")
            print(f"  - {subj} {num}   skipped: {reason or 'unknown'}")

def parse_course_line(line: str):
    m = COURSE_LINE_RE.match(line)
    return (m.group(1).upper(), m.group(2).upper()) if m else (None, None)

async def run():
    ensure_db()
    if SKIP_LOG.exists(): SKIP_LOG.unlink()

    if not COURSE_CODES_FILE.exists():
        print("Missing course_codes.txt"); return
    lines = [ln.strip() for ln in COURSE_CODES_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    pairs: List[Tuple[str,str]] = []
    for ln in lines:
        subj, num = parse_course_line(ln)
        if subj and num: pairs.append((subj, num))
        else: print(f"Skipping bad line: {ln}")
    print(f"Total inputs: {len(pairs)}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            timezone_id=TIMEZONE_ID, locale="en-CA", user_agent=USER_AGENT
        )
        page = await context.new_page()
        # Load once to set cookies/session/tz
        await page.goto(f"{BASE}/criteria.jsp", wait_until="domcontentloaded")

        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        stats = {"saved":0,"skipped":0,"failed":0}
        tasks = [process_one(page, s, n, stats, sem) for s, n in pairs]
        # chunked gather to keep memory stable with big lists
        for i in range(0, len(tasks), 1000):
            await asyncio.gather(*tasks[i:i+1000])

        await context.close(); await browser.close()

    print("\nSummary:")
    print(f"  Saved:   {stats['saved']}")
    print(f"  Skipped: {stats['skipped']}")
    print(f"  Failed:  {stats['failed']}")
    print(f"  DB: {DB_FILE.resolve()}")
    print(f"  Skips: {SKIP_LOG.resolve()}")

if __name__ == "__main__":
    asyncio.run(run())
