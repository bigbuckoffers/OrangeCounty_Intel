"""
scraper.py — Orange County FL Automated Motivated Seller Scraper
Uses Selenium to do the search, then grabs session cookies to download CSV directly.
"""
import json, logging, os, time, csv, io, shutil, requests
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_URL   = "https://selfservice.or.occompt.com/ssweb/search/DOCSEARCH2950S1"
CSV_URL    = "https://selfservice.or.occompt.com/ssweb/viewSearchResultsReport/DOCSEARCH2950S1/CSV"
OUTPUT_PATH = "data/output.json"

END_DATE   = datetime.today()
START_DATE = END_DATE - timedelta(days=7)
DATE_START = START_DATE.strftime("%m/%d/%Y")
DATE_END   = END_DATE.strftime("%m/%d/%Y")

TARGET_DOC_TYPES = [
    ("Lis Pendens",             30),
    ("Lien",                    15),
    ("Judgment",                15),
    ("Probate Court Paper",     20),
    ("Domestic Relations Deed", 10),
]

@dataclass
class Lead:
    document_number:   str = ""
    file_date:         str = ""
    grantor:           str = ""
    grantee:           str = ""
    legal_description: str = ""
    property_address:  str = ""
    document_type:     str = ""
    seller_score:      int = 0
    distress_flags:    list = field(default_factory=list)
    scraped_at:        str = field(default_factory=lambda: datetime.utcnow().isoformat()+"Z")

def score_lead(doc_type, base_score):
    dt = doc_type.lower()
    flags, score = [], base_score
    if "lis pendens" in dt: flags.append("lis_pendens")
    if "tax deed"    in dt: flags.append("tax_delinquency"); score = max(score, 30)
    if "lien"        in dt: flags.append("multiple_liens")
    if "judgment"    in dt: flags.append("judgment")
    if "probate"     in dt: flags.append("probate")
    if "domestic"    in dt: flags.append("divorce_bankruptcy")
    return min(score, 100), flags

def make_driver():
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(30)
    return driver

def accept_disclaimer(driver, wait):
    """Accept any disclaimer/terms popup if present."""
    try:
        accept_btn = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(text(),'Accept') or contains(text(),'agree') or contains(text(),'Continue') or contains(text(),'OK')]")
        ), timeout=5)
        driver.execute_script("arguments[0].click();", accept_btn)
        log.info("Accepted disclaimer")
        time.sleep(2)
    except Exception:
        pass  # No disclaimer, continue

def do_search(driver, wait, doc_type):
    """Navigate to search page, fill dates, select doc type, submit search."""
    log.info("Loading search page...")
    driver.get(BASE_URL)
    time.sleep(3)

    # Accept disclaimer if shown
    accept_disclaimer(driver, wait)

    # Set disclaimerAccepted cookie just in case
    driver.add_cookie({"name": "disclaimerAccepted", "value": "true", "domain": "selfservice.or.occompt.com"})

    # Find date fields — try multiple selector strategies
    date_start_filled = False
    date_end_filled = False

    # Strategy 1: placeholder
    try:
        inputs = driver.find_elements(By.CSS_SELECTOR, "input[placeholder*='mm/dd/yyyy']")
        if len(inputs) >= 2:
            inputs[0].clear(); inputs[0].send_keys(DATE_START)
            inputs[1].clear(); inputs[1].send_keys(DATE_END)
            date_start_filled = date_end_filled = True
            log.info("Filled dates via placeholder selector")
    except Exception:
        pass

    # Strategy 2: id contains Date
    if not date_start_filled:
        try:
            start_el = driver.find_element(By.XPATH, "//input[contains(@id,'start') or contains(@id,'Start') or contains(@name,'start')]")
            end_el   = driver.find_element(By.XPATH, "//input[contains(@id,'end')   or contains(@id,'End')   or contains(@name,'end')]")
            start_el.clear(); start_el.send_keys(DATE_START)
            end_el.clear();   end_el.send_keys(DATE_END)
            date_start_filled = date_end_filled = True
            log.info("Filled dates via id/name selector")
        except Exception:
            pass

    # Strategy 3: all visible text inputs
    if not date_start_filled:
        try:
            inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='text']")
            visible = [i for i in inputs if i.is_displayed()]
            if len(visible) >= 2:
                visible[0].clear(); visible[0].send_keys(DATE_START)
                visible[1].clear(); visible[1].send_keys(DATE_END)
                date_start_filled = date_end_filled = True
                log.info("Filled dates via visible text inputs")
        except Exception:
            pass

    if not date_start_filled:
        log.error("Could not find date input fields!")
        driver.save_screenshot(f"debug_no_dates_{doc_type.replace(' ','_')}.png")
        return False

    # Select document type in search form if available
    try:
        # Look for a doc type dropdown or checkbox
        doc_select = driver.find_elements(By.XPATH,
            f"//select | //input[@type='checkbox'][contains(following-sibling::*,'{doc_type}')] | //label[contains(text(),'{doc_type}')]"
        )
        for el in doc_select:
            tag = el.tag_name.lower()
            if tag == "select":
                from selenium.webdriver.support.ui import Select
                sel = Select(el)
                try:
                    sel.select_by_visible_text(doc_type)
                    log.info("Selected doc type in dropdown: %s", doc_type)
                    break
                except Exception:
                    pass
            elif tag in ("input", "label"):
                driver.execute_script("arguments[0].click();", el)
                log.info("Clicked doc type checkbox/label: %s", doc_type)
                break
    except Exception:
        pass  # Will filter by sidebar after search

    # Click Search button
    try:
        search_btn = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(text(),'Search')] | //input[@value='Search'] | //button[@type='submit']")
        ))
        driver.execute_script("arguments[0].click();", search_btn)
        log.info("Clicked search button")
    except Exception as e:
        log.error("Could not find search button: %s", e)
        driver.save_screenshot(f"debug_no_searchbtn_{doc_type.replace(' ','_')}.png")
        return False

    # Wait for results to load
    time.sleep(5)

    # Try clicking sidebar filter for this doc type
    try:
        sidebar_filter = driver.find_element(By.XPATH,
            f"//*[contains(@class,'filter') or contains(@class,'facet') or contains(@class,'sidebar')]//*[contains(text(),'{doc_type}')]"
        )
        driver.execute_script("arguments[0].click();", sidebar_filter)
        log.info("Clicked sidebar filter for: %s", doc_type)
        time.sleep(3)
    except NoSuchElementException:
        log.info("No sidebar filter found for %s — exporting all results", doc_type)

    return True

def get_csv_via_cookies(driver):
    """Extract cookies from Selenium and use requests to download the CSV."""
    selenium_cookies = driver.get_cookies()
    session = requests.Session()

    for cookie in selenium_cookies:
        session.cookies.set(cookie["name"], cookie["value"])

    # Ensure disclaimerAccepted is set
    session.cookies.set("disclaimerAccepted", "true")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": BASE_URL,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    log.info("Requesting CSV from: %s", CSV_URL)
    response = session.get(CSV_URL, headers=headers, timeout=30)
    log.info("CSV response status: %d, size: %d bytes", response.status_code, len(response.content))

    if response.status_code == 200 and len(response.content) > 100:
        return response.text
    else:
        log.warning("CSV response was empty or error. Body: %s", response.text[:500])
        return None

def parse_csv_text(csv_text, doc_type, base_score):
    """Parse raw CSV text into Lead objects."""
    leads = []
    lines = csv_text.splitlines()

    # Find header row
    header_idx = 0
    for i, line in enumerate(lines):
        if "Document #" in line or "Document" in line:
            header_idx = i
            break

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    for row in reader:
        doc_num = (row.get("Document #") or row.get("Document") or "").strip().strip('"')
        if not doc_num or not doc_num.isdigit():
            continue
        dtype = (row.get("Description") or doc_type).strip()
        score, flags = score_lead(dtype, base_score)
        leads.append(Lead(
            document_number=doc_num,
            file_date=(row.get("Recording Date") or "").strip(),
            grantor=(row.get("Grantor") or "").strip(),
            grantee=(row.get("Grantee") or "").strip(),
            legal_description=(row.get("Legal") or "").strip(),
            property_address=(row.get("Legal") or "").strip(),
            document_type=dtype,
            seller_score=score,
            distress_flags=flags,
        ))
    return leads

def scrape_doc_type(driver, doc_type, base_score):
    log.info("=== Scraping: %s ===", doc_type)
    wait = WebDriverWait(driver, 20)

    success = do_search(driver, wait, doc_type)
    if not success:
        log.error("Search failed for %s", doc_type)
        return []

    csv_text = get_csv_via_cookies(driver)
    if not csv_text:
        log.error("No CSV data for %s", doc_type)
        driver.save_screenshot(f"debug_no_csv_{doc_type.replace(' ','_')}.png")
        return []

    leads = parse_csv_text(csv_text, doc_type, base_score)
    log.info("Got %d leads for %s", len(leads), doc_type)
    return leads

def load_existing(path):
    """Load existing leads so we don't lose historical data."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("leads", [])
    except Exception:
        return []

def save_json(leads):
    os.makedirs("data", exist_ok=True)
    payload = {
        "generated_at":  datetime.utcnow().isoformat() + "Z",
        "total_records": len(leads),
        "date_range":    f"{DATE_START} to {DATE_END}",
        "leads":         [asdict(l) if isinstance(l, Lead) else l for l in leads],
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    log.info("Saved %d records to %s", len(leads), OUTPUT_PATH)

def main():
    log.info("=== OC Motivated Seller Scraper ===")
    log.info("Date range: %s to %s", DATE_START, DATE_END)

    driver = make_driver()
    new_leads = []

    try:
        for doc_type, base_score in TARGET_DOC_TYPES:
            leads = scrape_doc_type(driver, doc_type, base_score)
            new_leads.extend(leads)
            time.sleep(3)
    finally:
        driver.quit()

    # Merge with existing data (keep history, no duplicates)
    existing = load_existing(OUTPUT_PATH)
    existing_nums = {l["document_number"] if isinstance(l, dict) else l.document_number for l in existing}

    merged = list(existing)
    added = 0
    for lead in new_leads:
        if lead.document_number not in existing_nums:
            merged.append(lead)
            existing_nums.add(lead.document_number)
            added += 1

    # Sort by score
    def get_score(l):
        return l["seller_score"] if isinstance(l, dict) else l.seller_score
    merged.sort(key=get_score, reverse=True)

    log.info("New leads added: %d | Total: %d", added, len(merged))
    save_json(merged)
    log.info("Done.")

if __name__ == "__main__":
    main()
