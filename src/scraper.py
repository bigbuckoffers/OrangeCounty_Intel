"""
scraper.py — Orange County FL Automated Motivated Seller Scraper
Fetches CSVs + enriches Lis Pendens with property addresses from PDFs.
"""
import json, logging, os, csv, io, requests, time, re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import pdfplumber

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_URL    = "https://selfservice.or.occompt.com"
SEARCH_URL  = f"{BASE_URL}/ssweb/searchPost/DOCSEARCH2950S1"
RESULTS_URL = f"{BASE_URL}/ssweb/search/DOCSEARCH2950S1"
CSV_URL     = f"{BASE_URL}/ssweb/viewSearchResultsReport/DOCSEARCH2950S1/CSV"
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

# Only these doc types get PDF enrichment
ENRICH_DOC_TYPES = {"Lis Pendens", "Judgment"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "Referer": RESULTS_URL,
}

@dataclass
class Lead:
    document_number:   str = ""
    file_date:         str = ""
    grantor:           str = ""
    grantee:           str = ""
    legal_description: str = ""
    document_type:     str = ""
    seller_score:      int = 0
    distress_flags:    list = field(default_factory=list)
    property_address:  str = ""
    mailing_address:   str = ""
    tax_id:            str = ""
    action_date:       str = ""
    case_summary:      str = ""
    amount_owed:       str = ""
    doc_id:            str = ""
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
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        resp = session.get(RESULTS_URL, timeout=30)
        log.info("Session init: %d", resp.status_code)
        session.cookies.set("disclaimerAccepted", "true")
    except Exception as e:
        log.error("Session init failed: %s", e)
    return session

def search_and_get_data(session, doc_type, doc_code, doc_label):
    """POST search, get HTML for doc_id mapping, get CSV for lead data."""
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
        "field_selfservice_documentTypes-holderInput":   doc_code,
        "field_selfservice_documentTypes-holderValue":   doc_label,
        "field_selfservice_documentTypes-containsInput": "Contains Any",
        "field_selfservice_documentTypes":               "",
        "field_UseAdvancedSearch":                       "",
    }
    try:
        resp = session.post(SEARCH_URL, data=payload, timeout=30)
        log.info("Search POST: %d | %d bytes", resp.status_code, len(resp.content))
    except Exception as e:
        log.error("Search POST failed: %s", e)
        return {}, None

    time.sleep(2)

    # Collect all HTML pages for doc_id mapping
    all_html = ""
    page = 1
    while True:
        try:
            page_resp = session.get(f"{RESULTS_URL}?page={page}", timeout=30)
            html = page_resp.text
            all_html += html
            # Check if there's a next page
            if f'page={page+1}' not in html and 'Next' not in html:
                break
            # Safety: max 20 pages
            if page >= 20:
                break
            page += 1
            time.sleep(1)
        except Exception as e:
            log.error("Page %d fetch failed: %s", page, e)
            break

    # Parse doc_id mapping from HTML
    doc_id_map = {}
    try:
        soup = BeautifulSoup(all_html, "html.parser")
        for li in soup.find_all("li", class_="ss-search-row"):
            doc_id = li.get("data-documentid", "")
            h1 = li.find("h1")
            if h1 and doc_id:
                text = h1.get_text(separator=" ").strip()
                match = re.search(r'\b(\d{10,})\b', text)
                if match:
                    doc_id_map[match.group(1)] = doc_id
        log.info("Parsed %d doc_id mappings", len(doc_id_map))
    except Exception as e:
        log.error("HTML parse failed: %s", e)

    # Get CSV
    csv_text = None
    try:
        csv_resp = session.get(CSV_URL, timeout=30)
        if csv_resp.status_code == 200 and len(csv_resp.content) > 200:
            csv_text = csv_resp.text
            log.info("CSV: %d bytes", len(csv_resp.content))
        else:
            log.warning("CSV empty or error: %d", csv_resp.status_code)
    except Exception as e:
        log.error("CSV fetch failed: %s", e)

    return doc_id_map, csv_text

def parse_csv_text(csv_text, doc_type, base_score):
    leads = []
    lines = csv_text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if '"Document #"' in line or 'Document #' in line:
            header_idx = i
            break
    if header_idx is None:
        log.error("No CSV header found")
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    for row in reader:
        row = {k: v.strip().strip('"') if v else "" for k, v in row.items()}
        doc_num = (row.get("Document #") or row.get("Document") or "").strip().strip('"')
        if not doc_num or not doc_num.isdigit():
            continue
        dtype = row.get("Description") or doc_type
        score, flags = score_lead(dtype, base_score)
        leads.append(Lead(
            document_number=doc_num,
            file_date=row.get("Recording Date", ""),
            grantor=row.get("Grantor", ""),
            grantee=row.get("Grantee", ""),
            legal_description=row.get("Legal", ""),
            document_type=dtype,
            seller_score=score,
            distress_flags=flags,
        ))
    return leads

def get_pdf_text(session, doc_id, doc_num):
    """Fetch document page to find PDF URL, then download and extract text."""
    try:
        # Get the document page
        doc_page_url = f"{BASE_URL}/ssweb/document/{doc_id}?search=DOCSEARCH2950S1"
        resp = session.get(doc_page_url, timeout=30)
        if resp.status_code != 200:
            return None

        # Find queryId in the page source
        query_id = None
        for pattern in [
            r'queryId["\s:=]+([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})',
            r'"queryId"\s*:\s*"([^"]+)"',
            r'queryId=([a-f0-9\-]{36})',
        ]:
            m = re.search(pattern, resp.text)
            if m:
                query_id = m.group(1)
                break

        if query_id:
            pdf_url = f"{BASE_URL}/ssweb/document-image-pdfs/{doc_id}/{query_id}/{doc_num}.pdf?allowDownload=true&index=1"
        else:
            pdf_url = f"{BASE_URL}/ssweb/document-image-pdfs/{doc_id}/{doc_num}.pdf?allowDownload=true&index=1"

        pdf_resp = session.get(pdf_url, timeout=45)
        if pdf_resp.status_code != 200 or len(pdf_resp.content) < 500:
            log.warning("PDF not available for %s (%s)", doc_num, doc_id)
            return None

        with pdfplumber.open(io.BytesIO(pdf_resp.content)) as pdf:
            text = ""
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        return text

    except Exception as e:
        log.error("PDF error for %s: %s", doc_num, e)
        return None

def parse_pdf_enrichment(pdf_text, doc_type):
    """
    Extract property address, mailing address, tax ID, action date,
    case summary, and amount owed from PDF text.
    Only returns data we are confident about.
    """
    result = {
        "property_address": "",
        "mailing_address":  "",
        "tax_id":           "",
        "action_date":      "",
        "case_summary":     "",
        "amount_owed":      "",
    }

    if not pdf_text:
        return result

    # Normalize text — collapse multiple spaces/newlines
    text = re.sub(r'\s+', ' ', pdf_text).strip()

    # ── PROPERTY ADDRESS ──────────────────────────────────────────────────────
    # Patterns used in FL foreclosure/judgment docs, in order of reliability
    # We look for the actual property address, NOT attorney/court addresses
    # Attorney addresses usually follow "whose address is" right after a name/bar number
    # Property addresses follow "COMMONLY KNOWN AS", "a/k/a", "located at", "Address:"

    # Words that indicate it's NOT a property address (attorney/court/notary)
    NOT_PROPERTY = [
        'attorney', 'esquire', 'esq.', 'law firm', 'law office', 'law, p.a',
        'p.o. box', 'po box', 'suite', 'bar no', 'florida bar',
        'clerk of court', 'comptroller', 'judicial circuit',
        'notary public', 'commission expires',
        'return receipt', 'certified mail',
    ]

    property_patterns = [
        # "COMMONLY KNOWN AS: 123 Main St, Orlando, FL 32801"
        r'COMMONLY KNOWN AS[:\s]+([0-9][^\n,]{5,50},\s*[A-Za-z\s]+,\s*FL\s+\d{5})',
        # "a/k/a 527 Vereen Drive, Maitland, FL 32751"
        r'a/k/a\s+([0-9][^\n,]{5,50},\s*[A-Za-z\s]+,\s*FL\s+\d{5})',
        # "also known as 123 Main St"
        r'also known as\s+([0-9][^\n,]{5,50},\s*[A-Za-z\s]+,\s*FL\s+\d{5})',
        # "Address: 123 Main St, Orlando, FL 32801"
        r'Address:\s+([0-9][^\n,]{5,50},\s*[A-Za-z\s]+,\s*FL\s+\d{5})',
        # "property located at 123 Main St"
        r'property located at\s+([0-9][^\n,]{5,50},\s*[A-Za-z\s]+,\s*FL\s+\d{5})',
        # "real property located at"
        r'real property located at\s+([0-9][^\n,]{5,50},\s*[A-Za-z\s]+,\s*FL\s+\d{5})',
        # "located at 123 Main St"  
        r'located at[:\s]+([0-9][^\n,]{5,50},\s*[A-Za-z\s]+,\s*FL\s+\d{5})',
    ]

    for pattern in property_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            addr = m.group(1).strip().rstrip('.,;')
            addr_lower = addr.lower()
            # Verify it's not an attorney/notary/court address
            if not any(skip in addr_lower for skip in NOT_PROPERTY):
                # Verify it looks like a real street address (starts with number)
                if re.match(r'^\d+\s+\w', addr):
                    result["property_address"] = addr
                    # For Lis Pendens and foreclosure judgments,
                    # the property IS the mailing address (owner lives there)
                    result["mailing_address"] = addr
                    break

    # ── TAX / PARCEL ID ───────────────────────────────────────────────────────
    tax_patterns = [
        r'TAX ID[:\s]+([0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9\-]{4,})',
        r'Tax ID[:\s]+([0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9\-]{4,})',
        r'Parcel\s+(?:ID|No|Number)[:\s#]*([0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9\-]{4,})',
        r'parcel id[:\s]+([0-9\-]{10,})',
    ]
    for pattern in tax_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            result["tax_id"] = m.group(1).strip()
            break

    # ── ACTION DATE ───────────────────────────────────────────────────────────
    date_patterns = [
        r'DATED[:\s]+([A-Z][a-z]+ \d{1,2},? \d{4})',
        r'Dated[:\s]+([A-Z][a-z]+ \d{1,2},? \d{4})',
        r'DATED this ([A-Z][a-z]+ \d{1,2},? \d{4})',
        r'this (\d{1,2}(?:st|nd|rd|th) day of [A-Z][a-z]+,? \d{4})',
        r'DONE AND ORDERED[^,]*,\s*([A-Z][a-z]+,? [A-Z][a-z]+,? Florida,? [A-Z][a-z]+ \d{1,2},? \d{4})',
    ]
    for pattern in date_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            result["action_date"] = m.group(1).strip()
            break

    # ── AMOUNT OWED ───────────────────────────────────────────────────────────
    # Look for total judgment amount, total due, principal balance
    amount_patterns = [
        # "TOTAL AMOUNT OF TAX LIEN: $363.23"
        r'TOTAL AMOUNT OF TAX LIEN[:\s]+\$?([\d,]+\.?\d*)',
        # "total is $12,345.67"
        r'[Tt]otal\s+(?:amount\s+)?(?:due|owed|is)[:\s]+\$?([\d,]+\.?\d*)',
        # "sum of $12,345.67"
        r'sum of \$?([\d,]+\.?\d*)',
        # "in the amount of $X"
        r'in the amount of \$?([\d,]+\.?\d*)',
        # "TOTAL DUE: $X" from tax lien tables
        r'TOTAL DUE[:\s]+\$?([\d,]+\.?\d*)',
        # Judgment amounts: "judgment in the amount of"
        r'judgment[^$]{0,50}\$\s*([\d,]+\.?\d*)',
        # "unpaid $9,216.90"
        r'unpaid\s+\$?([\d,]+\.?\d*)',
        # "current amount due is $X"
        r'current amount due is \$?([\d,]+\.?\d*)',
        # "principal balance of $X"
        r'principal balance of \$?([\d,]+\.?\d*)',
    ]
    for pattern in amount_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            amount_str = m.group(1).replace(',', '')
            try:
                amount = float(amount_str)
                if amount > 0:
                    result["amount_owed"] = f"${amount:,.2f}"
                    break
            except ValueError:
                continue

    # ── CASE SUMMARY ─────────────────────────────────────────────────────────
    # For Lis Pendens: what is the foreclosure about?
    # For Judgments: what type of judgment?
    summary_patterns = [
        r'(foreclose\s+a\s+(?:Mortgage|Lien)[^.]{0,250}\.)',
        r'(foreclosure action[^.]{0,250}\.)',
        r'(NOTICE OF LIS PENDENS[^.]{0,300}\.)',
        r'(NOTICE OF ATTORNEY\'S CHARGING LIEN[^.]{0,200}\.)',
        r'(NOTICE OF TAX LIEN[^.]{0,200}\.)',
        r'(FINAL SUMMARY JUDGMENT[^.]{0,200}\.)',
        r'(AGREED FINAL JUDGMENT[^.]{0,200}\.)',
        r'(CONSTRUCTION LIEN[^.]{0,200}\.)',
    ]
    for pattern in summary_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            summary = re.sub(r'\s+', ' ', m.group(1).strip())
            result["case_summary"] = summary[:300]
            break

    return result

def should_enrich(lead):
    """Only enrich Lis Pendens and Judgments that are property-related."""
    dt = lead.document_type.lower() if isinstance(lead, Lead) else lead.get("document_type", "").lower()
    return "lis pendens" in dt or "judgment" in dt

def enrich_new_leads(session, leads, doc_id_map):
    """Fetch PDFs for new leads and enrich with property data."""
    enriched = 0
    for i, lead in enumerate(leads):
        doc_num = lead.document_number
        doc_id  = doc_id_map.get(doc_num, "")

        if not doc_id:
            log.debug("No doc_id for %s — skipping enrichment", doc_num)
            continue

        if not should_enrich(lead):
            log.debug("Skipping enrichment for %s (%s)", doc_num, lead.document_type)
            continue

        log.info("[%d/%d] Enriching %s | %s | %s",
                 i+1, len(leads), doc_num, lead.document_type, doc_id)

        pdf_text = get_pdf_text(session, doc_id, doc_num)

        if pdf_text:
            data = parse_pdf_enrichment(pdf_text, lead.document_type)
            lead.property_address = data["property_address"]
            lead.mailing_address  = data["mailing_address"]
            lead.tax_id           = data["tax_id"]
            lead.action_date      = data["action_date"]
            lead.case_summary     = data["case_summary"]
            lead.amount_owed      = data["amount_owed"]
            lead.doc_id           = doc_id
            if data["property_address"]:
                log.info("  ✓ Address: %s", data["property_address"])
            if data["amount_owed"]:
                log.info("  ✓ Amount:  %s", data["amount_owed"])
            enriched += 1
        else:
            lead.doc_id = doc_id

        # Be polite to the server
        time.sleep(2)

    log.info("Enriched %d / %d leads", enriched, len(leads))
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

    new_leads    = []
    all_doc_maps = {}

    for doc_type, doc_code, base_score in TARGET_DOC_TYPES:
        session = make_session()
        try:
            doc_id_map, csv_text = search_and_get_data(session, doc_type, doc_code, doc_type)
            all_doc_maps.update(doc_id_map)

            if csv_text:
                leads = parse_csv_text(csv_text, doc_type, base_score)
                log.info("Got %d leads for %s", len(leads), doc_type)
                new_leads.extend(leads)
            else:
                log.error("No CSV data for %s", doc_type)
        except Exception as e:
            log.error("Error on %s: %s", doc_type, e)
        time.sleep(3)

    # Load existing and find truly new leads
    existing     = load_existing(OUTPUT_PATH)
    existing_nums = {
        l["document_number"] if isinstance(l, dict) else l.document_number
        for l in existing
    }
    truly_new = [l for l in new_leads if l.document_number not in existing_nums]
    log.info("New leads to process: %d", len(truly_new))

    # Enrich only new leads that are Lis Pendens or Judgments
    if truly_new:
        session = make_session()
        truly_new = enrich_new_leads(session, truly_new, all_doc_maps)

    # Merge into existing
    merged = list(existing)
    seen   = set(existing_nums)
    added  = 0
    for lead in truly_new:
        doc_num = lead.document_number
        if doc_num not in seen:
            merged.append(lead)
            seen.add(doc_num)
            added += 1

    merged.sort(
        key=lambda l: l["seller_score"] if isinstance(l, dict) else l.seller_score,
        reverse=True
    )

    log.info("Added: %d | Total: %d", added, len(merged))
    save_json(merged)
    log.info("Done.")

if __name__ == "__main__":
    main()
