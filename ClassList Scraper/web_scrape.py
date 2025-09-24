# web_scrape.py
from __future__ import annotations
import csv, os, sys, time
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

CATALOG_URL = "https://academiccalendars.romcmaster.ca/content.php?catoid=58&navoid=12627"
START_PAGE = 2
END_PAGE = None     # set to int if auto-detect fails (e.g., 31)
OUTPUT_FILE = "course_names.csv"
HEADLESS = True    # set True once it's steady

LOGS = Path("logs"); LOGS.mkdir(exist_ok=True)

# top of file (constants)
UI_TIMEOUT = 3_000          # generic short wait for UI state
SLOW_TIMEOUT = 6_000        # only for truly slow bits
TYPE_DELAY = 40             # ms per keystroke


def safe_remove(p: str):
    try: os.remove(p)
    except FileNotFoundError: pass

def init_driver() -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    if HEADLESS:
        opts.add_argument("--headless=new")
    else:
        opts.add_argument("--start-maximized")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,2000")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)

def wait_css(page, css, timeout=25):
    return WebDriverWait(page, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, css))
    )

def detect_last_page_number(page) -> int | None:
    try:
        buttons = page.find_elements(By.CSS_SELECTOR, '[aria-label^="Page "]')
        mx = 0
        for b in buttons:
            aria = (b.get_attribute("aria-label") or "").strip()
            if aria.lower().startswith("page "):
                try: mx = max(mx, int(aria.split()[-1]))
                except ValueError: pass
        return mx or None
    except Exception:
        return None

def click_pagination_button(page, label_num: int):
    btn = page.find_element(By.CSS_SELECTOR, f'[aria-label="Page {label_num}"]')
    page.execute_script("arguments[0].click();", btn)

def scrape_names_on_page(driver, page_number: int, writer):
    # table rows for this page
    rows = driver.find_elements(
        By.XPATH,
        '//*[@id="table_block_n2_and_content_wrapper"]/table/tbody/tr[2]/td[2]/table/tbody/tr/td/table[2]/tbody/tr'
    )
    print(f"Page {page_number}: found {len(rows)} rows", flush=True)

    # skip first 2 and last 1 (spacers)
    for j in range(3, len(rows) - 1):
        row = rows[j]
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", row)
            time.sleep(0.03)

            # The course name is the anchor in the second cell
            # Try a few selectors to be safe across pages
            sel_options = [
                "td.width a",           # common on your HTML
                "td.width_85 a",
                "td:nth-child(2) a"
            ]
            link = None
            for sel in sel_options:
                try:
                    link = row.find_element(By.CSS_SELECTOR, sel)
                    if link.text.strip():
                        break
                except Exception:
                    continue
            if not link:
                raise Exception("NameLinkNotFound")

            name = link.text.strip()
            if not name:
                raise Exception("EmptyNameText")

            writer.writerow([name])
            print(f"✓ {name}", flush=True)

        except Exception as e:
            # dump artifacts for debugging this row if needed
            png = LOGS / f"page_{page_number}_row_{j}.png"
            html = LOGS / f"page_{page_number}_row_{j}.html"
            try: driver.save_screenshot(str(png))
            except Exception: pass
            try: html.write_text(row.get_attribute("outerHTML") or "", encoding="utf-8")
            except Exception: pass
            print(f"row {j}: failed ({e.__class__.__name__}) → saved {png.name} / {html.name}", flush=True)

def main():
    print(f"[{datetime.now():%H:%M:%S}] starting scrape of names only")
    safe_remove(OUTPUT_FILE)

    driver = init_driver()
    driver.get(CATALOG_URL)
    wait_css(driver, "#table_block_n2_and_content_wrapper")

    last_page = END_PAGE or detect_last_page_number(driver) or 31
    print(f"Using last page: {last_page}", flush=True)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Course Name"])

        for i in range(START_PAGE, last_page + 1):
            scrape_names_on_page(driver, i, writer)
            # next page button is labeled "Page i"
            try:
                click_pagination_button(driver, i)
                time.sleep(0.3)
                wait_css(driver, "#table_block_n2_and_content_wrapper")
            except Exception as e:
                print(f"could not click Page {i}: {e}", flush=True)
                break

    driver.quit()
    print(f"[{datetime.now():%H:%M:%S}] done. wrote {OUTPUT_FILE}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
