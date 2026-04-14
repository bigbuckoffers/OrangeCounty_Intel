"""
scraper.py — Orange County FL Automated Motivated Seller Scraper
Uses Selenium to do the search, then grabs session cookies to download CSV directly.
"""
import json, logging, os, time, csv, io, requests
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

BASE_URL    = "https://selfservice.or.occompt.com/ssweb/search/DOCSEARCH2950S1"
CSV_URL     = "https://selfservice.or.occompt.com/ssweb/viewSearchResultsReport/DOCSEARCH2950S1/CSV"
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

def dump_page_info(driver, label):
    """Log all input fields found on page for debugging."""
    log.info("=== PAGE DUMP: %s ===", label)
    log.info("URL: %s", driver.current_url)
    log.info("Title: %s", driver.title)
    inputs = driver.find_elements(By.TAG_NAME, "input")
    log.info("Found %d input elements:", len(inputs))
    for i, el in enumerate(inputs):
        try:
            log.info("  input[%d]: type=%s id=%s name=%s placeholder=%s class=%s visible=%s",
                i, el.get_attribute("type"), el.get_attribute("id"),
                el.get_attribute("name"), el.get_attribute("placeholder"),
                el.get_attribute("class"), el.is_displayed())
        except Exception:
            pass
    buttons = driver.find_elements(By.TAG_NAME, "button")
    log.info("Found %d button elements:", len(buttons))
    for i, b in enumerate(buttons[:10]):
        try:
            log.info("  button[%d]: text=%s id=%s class=%s visible=%s",
                i, b.text[:50], b.get_attribute("id"),
                b.get_attribute("class"), b.is_displayed())
        except Exception:
            pass

def accept_disclaimer(driver, wait):
    try:
        accept_btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(text(),'Accept') or contains(text(),'agree') or contains(text(),'Continue') or contains(text(),'OK') or contains(text(),'I Agree')]")
        ))
        driver.execute_script("arguments[0].click();", accept_btn)
        log.info("Accepted disclaimer")
        time.sleep(2)
    except Exception:
        pass

def fill_input_js(driver, element, value):
    """Fill input using JavaScript to bypass any framework bindings."""
    driver.execute_script("""
        var el = arguments[0];
        var val = arguments[1];
        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        nativeInputValueSetter.call(el, val);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
    """, element, value)

def do_search(driver, doc_type):
    log.info("Loading search page for: %s", doc_type)
    driver.get(BASE_URL)
    time.sleep(4)

    # Set cookie
    try:
        driver.add_cookie({"name": "disclaimerAccepted", "value": "true",
                          "domain": "selfservice.or.occompt.com"})
    except Exception:
        pass

    accept_disclaimer(driver, None)
    time.sleep(2)

    # Dump all inputs for debugging
    dump_page_info(driver, f"before_fill_{doc_type}")

    # Get ALL input elements
    all_inputs = driver.find_elements(By.TAG_NAME, "input")
    visible_inputs = [el for el in all_inputs if el.is_displayed()]
    text_inputs = [el for el in visible_inputs if el.get_attribute("type") in ("text", "date", "", None)]

    log.info("Visible inputs: %d, Text inputs: %d", len(visible_inputs), len(text_inputs))

    filled = False

    # Try by placeholder
    for el in all_inputs:
        ph = (el.get_attribute("placeholder") or "").lower()
        nm = (el.get_attribute("name") or "").lower()
        eid = (el.get_attribute("id") or "").lower()
        if any(x in ph or x in nm or x in eid for x in ["start", "from", "begin", "recordingstart", "datestart"]):
            try:
                fill_input_js(driver, el, DATE_START)
                log.info("Filled start date in: id=%s name=%s", el.get_attribute("id"), el.get_attribute("name"))
                filled = True
            except Exception as e:
                log.warning("Could not fill start: %s", e)

        if any(x in ph or x in nm or x in eid for x in ["end", "to", "thru", "recordingend", "dateend"]):
            try:
                fill_input_js(driver, el, DATE_END)
                log.info("Filled end date in: id=%s name=%s", el.get_attribute("id"), el.get_attribute("name"))
            except Exception as e:
                log.warning("Could not fill end: %s", e)

    # Fallback: use first two visible text inputs
    if not filled and len(text_inputs) >= 2:
        log.info("Fallback: filling first two visible text inputs")
        fill_input_js(driver, text_inputs[0], DATE_START)
        fill_input_js(driver, text_inputs[1], DATE_END)
        filled = True

    if not filled:
        log.error("COULD NOT FILL ANY DATE INPUTS for %s", doc_type)
        driver.save_screenshot(f"debug_nodates_{doc_type.replace(' ','_')}.png")
        return False

    time.sleep(1)

    # Click search button
    search_clicked = False
    # Try multiple strategies
    for xpath in [
        "//button[contains(translate(text(),'SEARCH','search'),'search')]",
        "//input[@type='submit']",
        "//button[@type='submit']",
        "//button[contains(@class,'search')]",
        "//input[@value='Search']",
        "//button[contains(@id,'search') or contains(@id,'Search')]",
    ]:
        try:
            btn = driver.find_element(By.XPATH, xpath)
            if btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                log.info("Clicked search button via: %s", xpath)
                search_clicked = True
                break
        except Exception:
            continue

    if not search_clicked:
        # Last resort: find any visible button and click it
        btns = driver.find_elements(By.TAG_NAME, "button")
        for b in btns:
            if b.is_displayed() and b.text.strip():
                log.info("Last resort: clicking button with text: %s", b.text)
                driver.execute_script("arguments[0].click();", b)
                search_clicked = True
                break

    if not search_clicked:
        log.error("Could not find search button for %s", doc_type)
        driver.save_screenshot(f"debug_nosearchbtn_{doc_type.replace(' ','_')}.png")
        return False

    log.info("Waiting for results...")
    time.sleep(6)

    # Try clicking sidebar filter
    try:
        filter_el = driver.find_element(By.XPATH,
            f"//*[contains(@class,'filter') or contains(@class,'facet') or contains(@class,'sidebar') or contains(@class,'refine')]//*[contains(text(),'{doc_type}')]"
        )
        driver.execute_script("arguments[0].click();", filter_el)
        log.info("Clicked sidebar filter: %s", doc_type)
        time.sleep(3)
    except NoSuchElementException:
        log.info("No sidebar filter for %s", doc_type)

    dump_page_info(driver, f"after_search_{doc_type}")
    return True

def get_csv_via_cookies(driver):
    selenium_cookies = driver.get_cookies()
    session = requests.Session()
    for c in selenium_cookies:
        session.cookies.set(c["name"], c["value"])
    session.cookies.set("disclaimerAccepted", "true")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": BASE_URL,
        "Accept": "text/csv,text/html,*/*",
    }
    log.info("Fetching CSV...")
    resp = session.get(CSV_URL, headers=headers, timeout=30)
    log.info("CSV status: %d | size: %d bytes | content-type: %s",
             resp.status_code, len(resp.content),
             resp.headers.get("content-type","?"))
    log.info("CSV preview: %s", resp.text[:300])

    if resp.status_code == 200 and len(resp.content) > 200:
        return resp.text
    return None

def parse_csv_text(csv_text, doc_type, base_score):
    leads = []
    lines = csv_text.splitlines()
    header_idx = 0
    for i, line in enumerate(lines):
        if "Document" in line and ("Grantor" in line or "Recording" in line):
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

def load_existing(path):
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
            try:
                success = do_search(driver, doc_type)
                if success:
                    csv_text = get_csv_via_cookies(driver)
                    if csv_text:
                        leads = parse_csv_text(csv_text, doc_type, base_score)
                        log.info("Got %d leads for %s", len(leads), doc_type)
                        new_leads.extend(leads)
                    else:
                        log.error("No CSV data for %s", doc_type)
            except Exception as e:
                log.error("Error on %s: %s", doc_type, e)
            time.sleep(3)
    finally:
        driver.quit()

    existing = load_existing(OUTPUT_PATH)
    existing_nums = {l["document_number"] if isinstance(l, dict) else l.document_number for l in existing}

    merged = list(existing)
    added = 0
    for lead in new_leads:
        if lead.document_number not in existing_nums:
            merged.append(lead)
            existing_nums.add(lead.document_number)
            added += 1

    def get_score(l):
        return l["seller_score"] if isinstance(l, dict) else l.seller_score
    merged.sort(key=get_score, reverse=True)

    log.info("New leads added: %d | Total: %d", added, len(merged))
    save_json(merged)
    log.info("Done.")

if __name__ == "__main__":
    main()
Also — go to .github/workflows/scraper.yml and paste it here so I can check it too. The workflow needs to upload the debug screenshots as artifacts so we can actually see what Chrome is seeing on that page.# .github/workflows/scraper.yml
# ─────────────────────────────────────────────────────────────────────────────
# Automated Motivated Seller Lead Scraper — Orange County, FL
# Runs on a schedule (daily at 6 AM ET) and on manual dispatch.
# Outputs are committed back to the repository so the dashboarpastedhere is the raw9:04 AMThe workflow is missing two critical things:

Chrome isn't being installed — it relies on whatever Chrome happens to be on the Ubuntu runner, which may not match the chromedriver
Debug screenshots aren't being uploaded so we can't see what the browser sees

Here's the updated scraper.yml — replace the whole file with this:
yamlname: Scrape OC Motivated Seller Leads

on:
  schedule:
    - cron: "0 11 * * *"
  workflow_dispatch:

permissions:
  contents: write

jobs:
  scrape:
    name: Scrape & Generate Dashboard
    runs-on: ubuntu-latest
    timeout-minutes: 30

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"

      - name: Install Chrome
        run: |
          wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | sudo apt-key add -
          echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" | sudo tee /etc/apt/sources.list.d/google-chrome.list
          sudo apt-get update
          sudo apt-get install -y google-chrome-stable
          google-chrome --version

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install requests beautifulsoup4 lxml selenium webdriver-manager

      - name: Create output directories
        run: mkdir -p data dashboard

      - name: Run scraper
        run: python src/scraper.py
        env:
          PYTHONUNBUFFERED: "1"

      - name: Validate outputs
        run: |
          python - <<'EOF'
          import json, sys, os
          path = "data/output.json"
          if not os.path.exists(path):
              print("ERROR: data/output.json not found"); sys.exit(1)
          with open(path) as f:
              data = json.load(f)
          n = data.get("total_records", 0)
          print(f"✓ JSON valid — {n} records")
          if not os.path.exists("dashboard/index.html"):
              print("ERROR: dashboard/index.html not found"); sys.exit(1)
          print("✓ Dashboard HTML present")
          EOF

      - name: Commit results
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/output.json dashboard/index.html
          git diff --cached --quiet || git commit -m "chore: auto-update leads $(date -u '+%Y-%m-%d %H:%M UTC')"
          git push
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      - name: Upload artifacts and debug screenshots
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: lead-output-${{ github.run_id }}
          path: |
            data/output.json
            dashboard/index.html
            debug_*.png
          retention-days: 30
