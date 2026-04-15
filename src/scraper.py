"""
scraper.py — Orange County FL Automated Motivated Seller Scraper
Fetches CSVs from county comptroller + enriches with property addresses
from the Orange County NAL appraisal dataset stored on Google Drive.
"""
import json, logging, os, csv, io, requests, time, re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_URL    = "https://selfservice.or.occompt.com"
SEARCH_URL  = f"{BASE_URL}/ssweb/searchPost/DOCSEARCH2950S1"
RESULTS_URL = f"{BASE_URL}/ssweb/search/DOCSEARCH2950S1"
CSV_URL     = f"{BASE_URL}/ssweb/viewSearchResultsReport/DOCSEARCH2950S1/CSV"
OUTPUT_PATH = "data/output.json"

NAL_GDRIVE_ID  = "1X1nZkK07FJV3BmUFHUFvpZA1hLEl4UP9"
NAL_LOCAL_PATH = "/tmp/NAL_orange.csv"

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
    owner_name:        str = ""
    assessed_value:    str = ""
    needs_enrichment:  bool = False
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

def download_nal_file():
    """Download the NAL appraisal file from Google Drive using gdown."""
    if os.path.exists(NAL_LOCAL_PATH):
        log.info("NAL file already downloaded")
        return True

    log.info("Downloading NAL file from Google Drive...")
    try:
        import gdown
        gdown.download(
            f"https://drive.google.com/uc?id={NAL_GDRIVE_ID}",
            NAL_LOCAL_PATH,
            quiet=False
        )
        size = os.path.getsize(NAL_LOCAL_PATH)
        log.info("NAL file downloaded: %d MB", size // (1024*1024))
        return size > 1000000
    except Exception as e:
        log.error("NAL download failed: %s", e)
        return False

def extract_subdivision(legal_desc):
    if not legal_desc:
        return ""
    legal = legal_desc.upper().strip()
    legal = re.sub(r'^(LOT|LOTS|UNIT|UNITS|PARCEL|TRACT|BLOCK)\s*[\w\d\-]+\s*', '', legal)
    legal = re.sub(r'^(LOT|LOTS|UNIT|UNITS|PARCEL|TRACT|BLOCK)\s*[\w\d\-]+\s*(BLOCK\s*[\w\d]+\s*)?', '', legal)
    legal = re.sub(r'\b\d{2}\s+\d{2}\s+\d{2}\s+[\d\s]+', '', legal)
    legal = re.sub(r'\s+(PHASE|PH|UNIT|SECTION|SEC)\s+[\w\d]+$', '', legal)
    legal = re.sub(r'\s+(PHASE|PH|UNIT|SECTION|SEC)\s+[\w\d]+\s+[\w\d]+$', '', legal)
    legal = legal.strip().strip(',').strip()
    if len(legal) < 3 or legal.isdigit():
        return ""
    return legal

def extract_lot_number(legal_desc):
    m = re.search(r'\bLOT\s+(\w+)', legal_desc.upper())
    if m:
        return f"LOT {m.group(1)}"
    m = re.search(r'\bUNIT\s+(\w+)', legal_desc.upper())
    if m:
        return f"UNIT {m.group(1)}"
    return ""

def load_nal_index():
    log.info("Building NAL index...")
    index = {}
    try:
        with open(NAL_LOCAL_PATH, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                phy_addr1 = (row.get("PHY_ADDR1") or "").strip()
                phy_addr2 = (row.get("PHY_ADDR2") or "").strip()
                phy_city  = (row.get("PHY_CITY")  or "").strip()
                phy_state = (row.get("PHY_STATE")  or "FL").strip()
                phy_zip   = (row.get("PHY_ZIPCD")  or "").strip()[:5]
                own_addr1 = (row.get("OWN_ADDR1") or "").strip()
                own_addr2 = (row.get("OWN_ADDR2") or "").strip()
                own_city  = (row.get("OWN_CITY")  or "").strip()
                own_state = (row.get("OWN_STATE") or "").strip()
                own_zip   = (row.get("OWN_ZIPCD") or "").strip()[:5]
                own_name  = (row.get("OWN_NAME")  or "").strip()
                s_legal   = (row.get("S_LEGAL")   or "").strip().upper()
                av_total  = (row.get("AV_NSD") or row.get("TV_NSD") or "").strip()

                if not phy_addr1 or not phy_city:
                    continue

                property_address = phy_addr1
                if phy_addr2:
                    property_address += f" {phy_addr2}"
                property_address += f", {phy_city}, {phy_state} {phy_zip}".strip()

                mailing_address = own_addr1
                if own_addr2:
                    mailing_address += f" {own_addr2}"
                if own_city:
                    mailing_address += f", {own_city}"
                if own_state:
                    mailing_address += f", {own_state}"
                if own_zip:
                    mailing_address += f" {own_zip}"
                mailing_address = mailing_address.strip() or property_address

                assessed = ""
                try:
                    av = int(av_total)
                    if av > 0:
                        assessed = f"${av:,}"
                except (ValueError, TypeError):
                    pass

                record = {
                    "property_address": property_address,
                    "mailing_address":  mailing_address,
                    "owner_name":       own_name,
                    "assessed_value":   assessed,
                    "s_legal":          s_legal,
                }

                subdiv = extract_subdivision(s_legal)
                if subdiv:
                    if subdiv not in index:
                        index[subdiv] = []
                    index[subdiv].append(record)

                count += 1
                if count % 100000 == 0:
                    log.info("Indexed %d records...", count)

        log.info("NAL index built: %d keys, %d records", len(index), count)
        return index
    except Exception as e:
        log.error("NAL index failed: %s", e)
        return {}

def match_lead_to_nal(lead, nal_index):
    legal = (lead.legal_description or "").upper().strip()
    if not legal:
        return None
    subdiv = extract_subdivision(legal)
    if not subdiv:
        return None
    lot = extract_lot_number(legal)
    if subdiv in nal_index:
        records = nal_index[subdiv]
        if lot:
            for rec in records:
                if lot in rec["s_legal"]:
                    return rec
        return records[0] if records else None
    for key in nal_index:
        if len(key) >= 8 and key in legal:
            records = nal_index[key]
            if lot:
                for rec in records:
                    if lot in rec["s_legal"]:
                        return rec
            return records[0] if records else None
    return None

def search_and_get_data(session, doc_type, doc_code, doc_label):
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
        return None
    time.sleep(2)
    try:
        csv_resp = session.get(CSV_URL, timeout=30)
        if csv_resp.status_code == 200 and len(csv_resp.content) > 200:
            log.info("CSV: %d bytes", len(csv_resp.content))
            return csv_resp.text
        log.warning("CSV empty: %d", csv_resp.status_code)
        return None
    except Exception as e:
        log.error("CSV fetch failed: %s", e)
        return None

def parse_csv_text(csv_text, doc_type, base_score):
    leads = []
    lines = csv_text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if '"Document #"' in line or 'Document #' in line:
            header_idx = i
            break
    if header_idx is None:
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

def enrich_leads_with_nal(leads, nal_index):
    matched = 0
    for lead in leads:
        result = match_lead_to_nal(lead, nal_index)
        if result:
            lead.property_address = result["property_address"]
            lead.mailing_address  = result["mailing_address"]
            lead.owner_name       = result["owner_name"]
            lead.assessed_value   = result["assessed_value"]
            lead.needs_enrichment = False
            matched += 1
        else:
            lead.needs_enrichment = True
    log.info("NAL matched %d / %d leads", matched, len(leads))
    return leads

def save_csv(leads, path="data/output.csv"):
    os.makedirs("data", exist_ok=True)
    fields = [
        "seller_score", "document_number", "file_date", "document_type",
        "grantor", "grantee", "legal_description",
        "property_address", "mailing_address", "owner_name", "assessed_value",
        "distress_flags", "needs_enrichment", "scraped_at"
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            row = asdict(lead) if isinstance(lead, Lead) else dict(lead)
            if isinstance(row.get("distress_flags"), list):
                row["distress_flags"] = ", ".join(row["distress_flags"])
            writer.writerow(row)
    log.info("CSV saved: %s", path)

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

    nal_index = {}
    if download_nal_file():
        nal_index = load_nal_index()
    else:
        log.warning("NAL unavailable — addresses will be missing")

    new_leads = []
    for doc_type, doc_code, base_score in TARGET_DOC_TYPES:
        session = make_session()
        try:
            csv_text = search_and_get_data(session, doc_type, doc_code, doc_type)
            if csv_text:
                leads = parse_csv_text(csv_text, doc_type, base_score)
                log.info("Got %d leads for %s", len(leads), doc_type)
                new_leads.extend(leads)
            else:
                log.error("No CSV for %s", doc_type)
        except Exception as e:
            log.error("Error on %s: %s", doc_type, e)
        time.sleep(3)

    if nal_index and new_leads:
        new_leads = enrich_leads_with_nal(new_leads, nal_index)

    existing = load_existing(OUTPUT_PATH)
    existing_nums = {
        l["document_number"] if isinstance(l, dict) else l.document_number
        for l in existing
    }
    merged = list(existing)
    added = 0
    seen = set(existing_nums)
    for lead in new_leads:
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
    save_csv(merged)
    log.info("Done.")

if __name__ == "__main__":
    main()
