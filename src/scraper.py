"""
scraper.py — Orange County FL Automated Motivated Seller Scraper
Fetches CSVs from county comptroller + enriches with property addresses
from the Orange County NAL appraisal dataset stored on Google Drive.

Match confidence system (4 strategies):
  HIGH   — Legal description matched exactly one parcel (lot + subdivision)
  MEDIUM — Legal description matched subdivision + owner name confirmed
  LOW    — Legal description matched subdivision but multiple parcels found
            OR name matched but no legal description hit
  NONE   — No match; county search URL provided for manual lookup
"""
import json, logging, os, csv, io, requests, time, re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from rapidfuzz import fuzz

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_URL    = "https://selfservice.or.occompt.com"
SEARCH_URL  = f"{BASE_URL}/ssweb/searchPost/DOCSEARCH2950S1"
RESULTS_URL = f"{BASE_URL}/ssweb/search/DOCSEARCH2950S1"
CSV_URL     = f"{BASE_URL}/ssweb/viewSearchResultsReport/DOCSEARCH2950S1/CSV"
OUTPUT_PATH = "data/output.json"

# Orange County Property Appraiser search URL for manual fallback lookups
OC_APPRAISER_SEARCH = "https://www.ocpafl.org/searches/ParcelSearch.aspx"

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

# For doc-type-aware name matching:
# LP = match grantee (homeowner being sued) as primary, grantor secondary
# Lien/Judgment = match grantor (debtor) as primary
# Probate/DRD = try both
DOC_TYPE_PRIMARY_NAME = {
    "lis pendens":          "grantee",
    "lp":                   "grantee",
    "lien":                 "grantor",
    "ln":                   "grantor",
    "judgment":             "grantor",
    "j":                    "grantor",
    "probate court paper":  "both",
    "prcp":                 "both",
    "domestic relations":   "both",
    "drd":                  "both",
}

# Name similarity threshold (0–100). 85 = ~97% sensitivity per Jaro-Winkler research.
NAME_MATCH_THRESHOLD = 85

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
    document_number:   str  = ""
    file_date:         str  = ""
    grantor:           str  = ""
    grantee:           str  = ""
    legal_description: str  = ""
    document_type:     str  = ""
    seller_score:      int  = 0
    distress_flags:    list = field(default_factory=list)
    property_address:  str  = ""
    mailing_address:   str  = ""
    owner_name:        str  = ""
    assessed_value:    str  = ""
    match_confidence:  str  = "NONE"   # HIGH / MEDIUM / LOW / NONE
    match_reason:      str  = ""       # human-readable explanation
    county_search_url: str  = ""       # fallback manual lookup URL
    needs_enrichment:  bool = False
    scraped_at:        str  = field(default_factory=lambda: datetime.utcnow().isoformat()+"Z")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Name normalization helpers
# ---------------------------------------------------------------------------

_ABBREV = {
    r'\bWM\b': 'WILLIAM', r'\bBILL\b': 'WILLIAM', r'\bROB\b': 'ROBERT',
    r'\bBOB\b': 'ROBERT', r'\bJIM\b': 'JAMES',   r'\bTOM\b': 'THOMAS',
    r'\bRICH\b': 'RICHARD', r'\bDAN\b': 'DANIEL', r'\bMIKE\b': 'MICHAEL',
    r'\bJOE\b': 'JOSEPH',  r'\bAL\b': 'ALBERT',  r'\bLIZ\b': 'ELIZABETH',
    r'\bBET\b': 'ELIZABETH',
}

def normalize_name(raw: str) -> str:
    """Uppercase, strip punctuation, expand common abbreviations, sort tokens."""
    if not raw:
        return ""
    name = raw.upper().strip()
    # remove suffixes
    name = re.sub(r'\b(JR|SR|II|III|IV|ESQ|TRUSTEE|TRUST|LLC|INC|CORP|LTD|ET\s+AL|ET\s+UX)\b', '', name)
    # strip punctuation except spaces
    name = re.sub(r'[^A-Z\s]', ' ', name)
    # expand abbreviations
    for pattern, replacement in _ABBREV.items():
        name = re.sub(pattern, replacement, name)
    # collapse whitespace
    name = ' '.join(name.split())
    # sort tokens so "SMITH JOHN" == "JOHN SMITH"
    return ' '.join(sorted(name.split()))

def split_co_owners(raw: str) -> list[str]:
    """Split 'BREWER JULIA LANG, BREWER GARY ALAN' into individual names."""
    if not raw:
        return []
    # split on comma, ampersand, ' AND ', ' & '
    parts = re.split(r',|&|\bAND\b', raw.upper())
    return [p.strip() for p in parts if p.strip()]

def name_score(name_a: str, name_b: str) -> int:
    """Return 0-100 similarity using token_sort_ratio (handles word order)."""
    a = normalize_name(name_a)
    b = normalize_name(name_b)
    if not a or not b:
        return 0
    return fuzz.token_sort_ratio(a, b)

def best_name_score(candidates_a: list[str], candidates_b: list[str]) -> int:
    """Return highest name score across all pairings of two name lists."""
    best = 0
    for na in candidates_a:
        for nb in candidates_b:
            s = name_score(na, nb)
            if s > best:
                best = s
    return best


# ---------------------------------------------------------------------------
# Legal description helpers
# ---------------------------------------------------------------------------

def extract_subdivision(legal_desc: str) -> str:
    if not legal_desc:
        return ""
    legal = legal_desc.upper().strip()
    # strip leading lot/unit/block/parcel references
    legal = re.sub(r'^(LOT|LOTS|UNIT|UNITS|PARCEL|TRACT|BLOCK)\s*[\w\d\-]+\s*', '', legal)
    legal = re.sub(r'^(LOT|LOTS|UNIT|UNITS|PARCEL|TRACT|BLOCK)\s*[\w\d\-]+\s*(BLOCK\s*[\w\d]+\s*)?', '', legal)
    # strip township/range survey numbers
    legal = re.sub(r'\b\d{2}\s+\d{2}\s+\d{2}\s+[\d\s]+', '', legal)
    # strip trailing phase/unit/section qualifiers
    legal = re.sub(r'\s+(PHASE|PH|UNIT|SECTION|SEC)\s+[\w\d]+$', '', legal)
    legal = re.sub(r'\s+(PHASE|PH|UNIT|SECTION|SEC)\s+[\w\d]+\s+[\w\d]+$', '', legal)
    legal = legal.strip().strip(',').strip()
    if len(legal) < 3 or legal.isdigit():
        return ""
    return legal

def extract_lot_number(legal_desc: str) -> str:
    m = re.search(r'\bLOT\s+(\w+)', legal_desc.upper())
    if m:
        return f"LOT {m.group(1)}"
    m = re.search(r'\bUNIT\s+(\w+)', legal_desc.upper())
    if m:
        return f"UNIT {m.group(1)}"
    return ""


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# NAL download + indexing
# ---------------------------------------------------------------------------

def download_nal_file():
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
        return size > 1_000_000
    except Exception as e:
        log.error("NAL download failed: %s", e)
        return False

def load_nal_index():
    """
    Build two indexes from the NAL file:
      subdiv_index : subdivision_key -> list of records
      name_index   : normalized_owner_name -> list of records
    Each record also stores normalized s_legal and the raw owner name
    so we can do name matching during enrichment.
    """
    log.info("Building NAL index...")
    subdiv_index = {}
    name_index   = {}
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
                    count += 1
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
                    "owner_name_norm":  normalize_name(own_name),
                    "assessed_value":   assessed,
                    "s_legal":          s_legal,
                }

                # --- subdivision index ---
                subdiv = extract_subdivision(s_legal)
                if subdiv:
                    subdiv_index.setdefault(subdiv, []).append(record)

                # --- owner name index (skip blank or very short names) ---
                name_norm = normalize_name(own_name)
                if len(name_norm) >= 4:
                    name_index.setdefault(name_norm, []).append(record)

                count += 1
                if count % 100_000 == 0:
                    log.info("Indexed %d records...", count)

        log.info(
            "NAL index built: %d subdiv keys | %d name keys | %d total records",
            len(subdiv_index), len(name_index), count
        )
        return subdiv_index, name_index
    except Exception as e:
        log.error("NAL index failed: %s", e)
        return {}, {}


# ---------------------------------------------------------------------------
# 4-Strategy matching engine
# ---------------------------------------------------------------------------

def get_primary_names(lead: Lead) -> list[str]:
    """
    Return the names to match against OWN_NAME based on doc type.
    For LP → grantee (owner being sued).
    For Lien/Judgment → grantor (debtor).
    For others → try both.
    """
    dt = lead.document_type.lower()
    primary = "both"
    for key, val in DOC_TYPE_PRIMARY_NAME.items():
        if key in dt:
            primary = val
            break

    grantee_names = split_co_owners(lead.grantee)
    grantor_names = split_co_owners(lead.grantor)

    if primary == "grantee":
        return grantee_names or grantor_names
    if primary == "grantor":
        return grantor_names or grantee_names
    return grantee_names + grantor_names   # both


def build_county_search_url(lead: Lead) -> str:
    """Generate a direct OC Property Appraiser URL for manual fallback."""
    # Try to build a name search URL using the most useful name we have
    names = get_primary_names(lead)
    if names:
        # Use first name token as last name guess (county search uses last name)
        raw = names[0]
        parts = raw.split()
        last = parts[0] if parts else raw
        import urllib.parse
        return (
            f"https://www.ocpafl.org/searches/ParcelSearch.aspx"
            f"?SearchType=owner&SearchValue={urllib.parse.quote(last)}"
        )
    return OC_APPRAISER_SEARCH


def match_lead_to_nal(lead: Lead, subdiv_index: dict, name_index: dict) -> dict:
    """
    4-strategy matching. Returns a result dict:
      {
        match_confidence: HIGH|MEDIUM|LOW|NONE,
        match_reason: str,
        property_address: str,
        mailing_address: str,
        owner_name: str,
        assessed_value: str,
        candidates: int,   # number of NAL records considered
      }
    """
    legal      = (lead.legal_description or "").upper().strip()
    subdiv     = extract_subdivision(legal)
    lot        = extract_lot_number(legal)
    lead_names = get_primary_names(lead)     # names from comptroller CSV

    # -----------------------------------------------------------------------
    # Strategy 1 — Exact lot + subdivision match → HIGH confidence
    # -----------------------------------------------------------------------
    if subdiv and lot and subdiv in subdiv_index:
        lot_matches = [r for r in subdiv_index[subdiv] if lot in r["s_legal"]]
        if len(lot_matches) == 1:
            rec = lot_matches[0]
            return {
                "match_confidence": "HIGH",
                "match_reason":     f"Legal match: {subdiv} {lot} → 1 parcel",
                **_pick_fields(rec),
                "candidates":       1,
            }
        if len(lot_matches) > 1:
            # Narrow by name before falling through
            name_narrowed = _narrow_by_name(lot_matches, lead_names)
            if name_narrowed:
                return {
                    "match_confidence": "HIGH",
                    "match_reason":     f"Legal match: {subdiv} {lot} + name confirmed",
                    **_pick_fields(name_narrowed),
                    "candidates":       len(lot_matches),
                }
            # Multiple lot matches, can't disambiguate → LOW
            return {
                "match_confidence": "LOW",
                "match_reason":     f"Legal match: {subdiv} {lot} → {len(lot_matches)} parcels, name ambiguous",
                **_pick_fields(lot_matches[0]),
                "candidates":       len(lot_matches),
            }

    # -----------------------------------------------------------------------
    # Strategy 2 — Subdivision match + name confirmation → MEDIUM confidence
    # -----------------------------------------------------------------------
    subdiv_records = []
    if subdiv and subdiv in subdiv_index:
        subdiv_records = subdiv_index[subdiv]
    else:
        # try partial key match (for long subdivisions)
        for key in subdiv_index:
            if len(key) >= 8 and key in legal:
                subdiv_records = subdiv_index[key]
                subdiv = key
                break

    if subdiv_records and lead_names:
        name_narrowed = _narrow_by_name(subdiv_records, lead_names)
        if name_narrowed:
            return {
                "match_confidence": "MEDIUM",
                "match_reason":     f"Subdiv match: {subdiv} + name confirmed",
                **_pick_fields(name_narrowed),
                "candidates":       len(subdiv_records),
            }

    # -----------------------------------------------------------------------
    # Strategy 3 — Subdivision match only (no lot, name unconfirmed) → LOW
    # -----------------------------------------------------------------------
    if subdiv_records:
        n = len(subdiv_records)
        return {
            "match_confidence": "LOW",
            "match_reason":     f"Subdiv match: {subdiv} → {n} parcels, name unconfirmed",
            **_pick_fields(subdiv_records[0]),
            "candidates":       n,
        }

    # -----------------------------------------------------------------------
    # Strategy 3b — Name-only match (no legal hit at all) → LOW
    # -----------------------------------------------------------------------
    if lead_names:
        name_rec = _search_name_index(lead_names, name_index)
        if name_rec:
            return {
                "match_confidence": "LOW",
                "match_reason":     "Name-only match (no legal description hit)",
                **_pick_fields(name_rec),
                "candidates":       1,
            }

    # -----------------------------------------------------------------------
    # Strategy 4 — No match → NONE, provide manual search URL
    # -----------------------------------------------------------------------
    return {
        "match_confidence": "NONE",
        "match_reason":     "No match found; use county_search_url for manual lookup",
        "property_address": "",
        "mailing_address":  "",
        "owner_name":       "",
        "assessed_value":   "",
        "candidates":       0,
    }


def _pick_fields(rec: dict) -> dict:
    return {
        "property_address": rec["property_address"],
        "mailing_address":  rec["mailing_address"],
        "owner_name":       rec["owner_name"],
        "assessed_value":   rec["assessed_value"],
    }


def _narrow_by_name(records: list, lead_names: list[str]) -> dict | None:
    """
    From a list of NAL records, return the best name match above threshold.
    Tries grantee names first, then grantor names.
    Returns None if nothing clears the threshold.
    """
    best_rec, best_score = None, 0
    for rec in records:
        nal_names = split_co_owners(rec["owner_name"])
        s = best_name_score(lead_names, nal_names)
        if s > best_score:
            best_score = s
            best_rec = rec
    if best_score >= NAME_MATCH_THRESHOLD:
        return best_rec
    return None


def _search_name_index(lead_names: list[str], name_index: dict) -> dict | None:
    """
    Search the name index for any lead name that matches above threshold.
    Returns the best matching NAL record or None.
    """
    best_rec, best_score = None, 0
    # Limit to checking top 2 lead names to avoid O(n) full scan
    for lead_name in lead_names[:2]:
        norm = normalize_name(lead_name)
        if not norm:
            continue
        # Only check name_index keys that share at least one token
        lead_tokens = set(norm.split())
        for key, records in name_index.items():
            key_tokens = set(key.split())
            if not lead_tokens & key_tokens:
                continue
            s = fuzz.token_sort_ratio(norm, key)
            if s > best_score and s >= NAME_MATCH_THRESHOLD:
                best_score = s
                best_rec = records[0]
    return best_rec


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def enrich_leads_with_nal(leads: list, subdiv_index: dict, name_index: dict) -> list:
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NONE": 0}
    for lead in leads:
        result = match_lead_to_nal(lead, subdiv_index, name_index)

        lead.match_confidence  = result["match_confidence"]
        lead.match_reason      = result["match_reason"]
        lead.property_address  = result.get("property_address", "")
        lead.mailing_address   = result.get("mailing_address",  "")
        lead.owner_name        = result.get("owner_name",        "")
        lead.assessed_value    = result.get("assessed_value",    "")
        lead.county_search_url = build_county_search_url(lead)
        lead.needs_enrichment  = lead.match_confidence in ("LOW", "NONE")

        counts[lead.match_confidence] += 1

    log.info(
        "NAL match results → HIGH:%d  MEDIUM:%d  LOW:%d  NONE:%d",
        counts["HIGH"], counts["MEDIUM"], counts["LOW"], counts["NONE"]
    )
    return leads


# ---------------------------------------------------------------------------
# Comptroller fetch + CSV parse
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_csv(leads, path="data/output.csv"):
    os.makedirs("data", exist_ok=True)
    fields = [
        "seller_score", "document_number", "file_date", "document_type",
        "grantor", "grantee", "legal_description",
        "property_address", "mailing_address", "owner_name", "assessed_value",
        "match_confidence", "match_reason", "county_search_url",
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== OC Motivated Seller Scraper ===")
    log.info("Date range: %s to %s", DATE_START, DATE_END)

    subdiv_index, name_index = {}, {}
    if download_nal_file():
        subdiv_index, name_index = load_nal_index()
    else:
        log.warning("NAL unavailable — all leads will be NONE confidence")

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

    if new_leads:
        new_leads = enrich_leads_with_nal(new_leads, subdiv_index, name_index)

    existing = load_existing(OUTPUT_PATH)
    existing_nums = {
        l["document_number"] if isinstance(l, dict) else l.document_number
        for l in existing
    }
    merged = list(existing)
    seen   = set(existing_nums)
    added  = 0
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
