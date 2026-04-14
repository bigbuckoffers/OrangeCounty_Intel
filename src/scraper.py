"""
scraper.py — Orange County FL Automated Motivated Seller Scraper
Uses Selenium to control a real Chrome browser to download CSVs automatically.
"""
import json, logging, os, time, csv, io, shutil
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

BASE_URL = "https://selfservice.or.occompt.com/ssweb/search/DOCSEARCH2950S1"
OUTPUT_PATH    = "data/output.json"
DASHBOARD_PATH = "dashboard/index.html"

END_DATE   = datetime.today()
START_DATE = END_DATE - timedelta(days=7)
DATE_START = START_DATE.strftime("%m/%d/%Y")
DATE_END   = END_DATE.strftime("%m/%d/%Y")

TARGET_DOC_TYPES = [
    ("Lis Pendens", 30),
    ("Lien",        15),
    ("Judgment",    15),
    ("Probate Court Paper", 20),
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
    if "tax deed" in dt:    flags.append("tax_delinquency"); score = max(score,30)
    if "lien" in dt:        flags.append("multiple_liens")
    if "judgment" in dt:    flags.append("judgment")
    if "probate" in dt:     flags.append("probate")
    if "domestic" in dt:    flags.append("divorce_bankruptcy")
    return min(score,100), flags

def make_driver(download_dir):
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_experimental_option("prefs", {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "safebrowsing.enabled": True,
    })
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(30)
    return driver

def wait_for_download(download_dir, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = [f for f in os.listdir(download_dir) if f.lower().endswith(".csv")]
        if files:
            time.sleep(2)
            return os.path.join(download_dir, files[0])
        time.sleep(1)
    raise TimeoutError("Download timed out")

def parse_csv(filepath, doc_type, base_score):
    leads = []
    with open(filepath, encoding="utf-8-sig", errors="replace") as f:
        content = f.read()
    lines = content.splitlines()
    header_idx = 0
    for i, line in enumerate(lines):
        if "Document #" in line:
            header_idx = i
            break
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    for row in reader:
        doc_num = (row.get("Document #") or "").strip().strip('"')
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

def scrape_doc_type(driver, download_dir, doc_type, base_score):
    log.info("Scraping: %s", doc_type)
    for f in os.listdir(download_dir):
        os.remove(os.path.join(download_dir, f))
    try:
        driver.get(BASE_URL)
        wait = WebDriverWait(driver, 20)

        # Enter start date
        date_inputs = wait.until(EC.presence_of_all_elements_located(
            (By.CSS_SELECTOR, "input[placeholder*='mm/dd/yyyy'], input[id*='Date']")
        ))
        if len(date_inputs) >= 1:
            date_inputs[0].clear()
            date_inputs[0].send_keys(DATE_START)
        if len(date_inputs) >= 2:
            date_inputs[1].clear()
            date_inputs[1].send_keys(DATE_END)

        # Click search
        search_btn = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(text(),'Search')] | //input[@value='Search']")
        ))
        driver.execute_script("arguments[0].click();", search_btn)
        time.sleep(3)

        # Click the doc type filter in sidebar
        try:
            filter_el = driver.find_element(By.XPATH,
                f"//*[contains(@class,'filter') or contains(@class,'sidebar')]//*[contains(text(),'{doc_type}')]"
            )
            driver.execute_script("arguments[0].click();", filter_el)
            time.sleep(2)
        except NoSuchElementException:
            log.warning("Filter for %s not found, trying to export all", doc_type)

        # Click print/export button then Export as CSV
        try:
            # Find the print/AZ button
            print_btn = wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, ".print-btn, [class*='print'], [title*='Print'], button.az")
            ))
            driver.execute_script("arguments[0].click();", print_btn)
            time.sleep(1)
        except Exception:
            # Try finding by icon or text
            btns = driver.find_elements(By.XPATH, "//*[contains(@class,'icon') or contains(@title,'Export') or contains(@title,'Print')]")
            for b in btns:
                try:
                    driver.execute_script("arguments[0].click();", b)
                    time.sleep(0.5)
                    break
                except Exception:
                    continue

        # Click "Export as CSV"
        csv_btn = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//*[contains(text(),'Export as CSV') or contains(text(),'CSV')]")
        ))
        driver.execute_script("arguments[0].click();", csv_btn)

        filepath = wait_for_download(download_dir, timeout=30)
        leads = parse_csv(filepath, doc_type, base_score)
        log.info("  Got %d leads", len(leads))
        return leads

    except Exception as e:
        log.error("Error scraping %s: %s", doc_type, e)
        # Save screenshot for debugging
        try:
            driver.save_screenshot(f"error_{doc_type.replace(' ','_')}.png")
        except Exception:
            pass
        return []

def save_json(leads):
    os.makedirs("data", exist_ok=True)
    payload = {"generated_at": datetime.utcnow().isoformat()+"Z", "total_records": len(leads), "date_range": f"{DATE_START} to {DATE_END}", "leads": [asdict(l) for l in leads]}
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    log.info("JSON saved -> %s (%d records)", OUTPUT_PATH, len(leads))

def save_dashboard(leads):
    os.makedirs("dashboard", exist_ok=True)
    html = open("dashboard/index.html").read() if os.path.exists("dashboard/index.html") else ""
    # Dashboard loads from JSON dynamically - no changes needed
    log.info("Dashboard uses dynamic JSON loading - no update needed")

def main():
    log.info("=== OC Motivated Seller Scraper ===")
    log.info("Date range: %s to %s", DATE_START, DATE_END)
    download_dir = os.path.abspath("downloads")
    os.makedirs(download_dir, exist_ok=True)
    driver = make_driver(download_dir)
    all_leads, seen = [], set()
    try:
        for doc_type, base_score in TARGET_DOC_TYPES:
            leads = scrape_doc_type(driver, download_dir, doc_type, base_score)
            for lead in leads:
                if lead.document_number not in seen:
                    seen.add(lead.document_number)
                    all_leads.append(lead)
            time.sleep(2)
    finally:
        driver.quit()
        if os.path.exists(download_dir):
            shutil.rmtree(download_dir)
    all_leads.sort(key=lambda l: l.seller_score, reverse=True)
    log.info("Total unique leads: %d", len(all_leads))
    save_json(all_leads)
    save_dashboard(all_leads)
    log.info("Done.")

if __name__ == "__main__":
    main()
