"""
scraper.py — Orange County FL Automated Motivated Seller Scraper
Pure requests-based scraper. No Selenium, no Chrome, no browser needed.
Uses the Tyler Technologies JSON API directly.
"""
import json, logging, os, csv, io, requests, time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SEARCH_URL  = "https://selfservice.or.occompt.com/ssweb/searchPost/DOCSEARCH2950S1"
RESULTS_URL = "https://selfservice.or.occompt.com/ssweb/search/DOCSEARCH2950S1"
CSV_URL     = "https://selfservice.or.occompt.com/ssweb/viewSearchResultsReport/DOCSEARCH2950S1/CSV"
OUTPUT_PATH = "data/output.json"

END_DATE   = datetime.today()
START_DATE = END_DATE - timedelta(days=7)
DATE_START = START_DATE.strftime("%m/%d/%Y")
DATE_END   = END_DATE.strftime("%m/%d/%Y")

TARGET_DOC_TYPES = [
    ("Lis Pendens",             "LP",   30),
    ("Lien",                    "LN",   15),
    ("Judgment",                "J",    15),
    ("Probate Court Paper",     "PRCP", 20),
    ("Domestic Relations Deed", "DRD",  10),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://selfservice.or.occompt.com",
    "Referer": "https://selfservice.or.occompt.com/ssweb/search/DOCSEARCH2950S1",
}

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

def make_session():
    """Create a requests session and initialize cookies by visiting the search page."""
    session = requests.Session()
    session.headers.update(HEADERS)
    
    log.info("Initializing session...")
    try:
        resp = session.get(RESULTS_URL, timeout=30)
        log.info("Session init status: %d", resp.status_code)
        session.cookies.set("disclaimerAccepted", "true")
        log.info("Cookies: %s", dict(session.cookies))
    except Exception as e:
        log.error("Session init failed: %s", e)
    
    return session

def search_and_get_csv(session, doc_type, doc_code, doc_label):
    """POST search for a specific doc type, then download CSV."""
    log.info("Searching for: %s (%s)", doc_type, doc_code)
    
    payload = {
        "field_RecordingDateID_DOT_StartDate": DATE_START,
        "field_RecordingDateID_DOT_EndDate":   DATE_END,
        "field_DocumentID":                    "",
        "field_BothNamesID-containsInput":     "Contains Any",
        "field_BothNamesID":                   "",
        "field_GrantorID-containsInput":       "Contains Any",
        "field_GrantorID":                     "",
        "field_GranteeID-containsInput":       "Contains Any",
        "field_GranteeID":                     "",
        "field_BookPageID_DOT_Book":           "",
        "field_BookPageID_DOT_Page":           "",
        "field_selfservice_documentTypes-holderInput":  doc_code,
        "field_selfservice_documentTypes-holderValue":  doc_label,
        "field_selfservice_documentTypes-containsInput": "Contains Any",
        "field_selfservice_documentTypes":     "",
        "field_UseAdvancedSearch":             "",
    }

    # POST the search
    try:
        resp = session.post(SEARCH_URL, data=payload, timeout=30)
        log.info("Search POST status: %d | size: %d bytes", resp.status_code, len(resp.content))
        log.info("Search response preview: %s", resp.text[:200])
    except Exception as e:
        log.error("Search POST failed: %s", e)
        return None

    time.sleep(2)

    # Download CSV using same session (session holds the search state)
    try:
        csv_resp = session.get(CSV_URL, timeout=30)
        log.info("CSV status: %d | size: %d bytes | type: %s",
                 csv_resp.status_code, len(csv_resp.content),
                 csv_resp.headers.get("content-type", "?"))
        log.info("CSV preview: %s", csv_resp.text[:300])
        
        if csv_resp.status_code == 200 and len(csv_resp.content) > 200:
            return csv_resp.text
        else:
            log.warning("CSV empty or error")
            return None
    except Exception as e:
        log.error("CSV download failed: %s", e)
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
    log.info("=== OC Motivated Seller Scraper (No-Browser Mode) ===")
    log.info("Date range: %s to %s", DATE_START, DATE_END)

    new_leads = []

    for doc_type, doc_code, base_score in TARGET_DOC_TYPES:
        # Each doc type gets its own fresh session to avoid state bleed
        session = make_session()
        try:
            csv_text = search_and_get_csv(session, doc_type, doc_code, doc_type)
            if csv_text:
                leads = parse_csv_text(csv_text, doc_type, base_score)
                log.info("Got %d leads for %s", len(leads), doc_type)
                new_leads.extend(leads)
            else:
                log.error("No CSV data for %s", doc_type)
        except Exception as e:
            log.error("Error on %s: %s", doc_type, e)
        time.sleep(2)

    # Merge with existing
    existing = load_existing(OUTPUT_PATH)
    existing_nums = {l["document_number"] if isinstance(l, dict) else l.document_number
                     for l in existing}
    merged = list(existing)
    added = 0
    for lead in new_leads:
        if lead.document_number not in existing_nums:
            merged.append(lead)
            existing_nums.add(lead.document_number)
            added += 1

    merged.sort(key=lambda l: l["seller_score"] if isinstance(l, dict) else l.seller_score,
                reverse=True)

    log.info("New leads added: %d | Total: %d", added, len(merged))
    save_json(merged)
    log.info("Done.")

if __name__ == "__main__":
    main()
