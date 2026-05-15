"""
scraper.py — Orange County FL Automated Motivated Seller Scraper

STACKING SYSTEM:
  After scraping all doc types, leads are grouped by normalized owner name.
  All distress signals for the same owner are combined into one stacked lead.
  Scores are summed (capped at 100). Multiple doc types show as stacked_flags.

  PROPERTY-LEVEL STACKING (post-merge):
  After merging new + existing leads, a second pass groups ALL leads by
  property address or parcel ID. This ensures the same property never
  appears as multiple separate records regardless of when filings were scraped.

MATCHING: 2-stage retrieval + weighted scoring
  HIGH   = score >= 85 AND parcel anchor AND strong subdiv (80%+ token overlap)
  MEDIUM = score 65-84
  LOW    = score 30-64
  NONE   = score < 30
"""
import json, logging, os, csv, io, requests, time, re, urllib.parse
from collections import defaultdict
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
    ("Tax Deed",                "TD",   35),
    ("Death Certificate",       "DC",   25),
    ("Notice of Commencement",  "NOC",  10),
]

DOC_TYPE_PRIMARY_NAME = {
    "lis pendens": "grantee",
    "lp":          "grantee",
    "lien":        "grantor",
    "ln":          "grantor",
    "judgment":    "grantor",
    "j":           "grantor",
    "probate":     "both",
    "prcp":        "both",
    "domestic":    "both",
    "drd":         "both",
    "tax deed":    "grantee",
    "td":          "grantee",
    "death":       "grantee",
    "dc":          "grantee",
    "notice":      "grantor",
    "noc":         "grantor",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "Referer": RESULTS_URL,
}

_LEGAL_ABBREV = [
    (r'\bBLK\b',    'BLOCK'),
    (r'\bSEC\b',    'SECTION'),
    (r'\bSUBD\b',   'SUBDIVISION'),
    (r'\bSUB\b',    'SUBDIVISION'),
    (r'\bADD\b',    'ADDITION'),
    (r'\bESTS\b',   'ESTATES'),
    (r'\bEST\b',    'ESTATES'),
    (r'\bHTS\b',    'HEIGHTS'),
    (r'\bHGTS\b',   'HEIGHTS'),
    (r'\bCONDM\b',  'CONDOMINIUM'),
    (r'\bCONDO\b',  'CONDOMINIUM'),
    (r'\bCOND\b',   'CONDOMINIUM'),
    (r'\bVIL\b',    'VILLAS'),
    (r'\bVLS\b',    'VILLAS'),
    (r'\b1ST\b',    'FIRST'),
    (r'\b2ND\b',    'SECOND'),
    (r'\b3RD\b',    'THIRD'),
    (r'\bPK\b',     'PARK'),
    (r'\bGDNS\b',   'GARDENS'),
    (r'\bGARD\b',   'GARDENS'),
    (r'\bLOT:\s*',  'LOT '),
    (r'\bUNIT:\s*', 'UNIT '),
    (r'\bBLOCK:\s*','BLOCK '),
]

_STOPWORDS = {
    'THE', 'OF', 'A', 'AN', 'AND', 'OR', 'IN', 'AT', 'TO', 'FOR',
    'PT', 'PB', 'PG', 'PLAT', 'BOOK', 'PAGE', 'THEREOF', 'THENCE',
    'BEARING', 'DEGREES', 'FEET', 'NORTH', 'SOUTH', 'EAST', 'WEST',
}

_METES_PATTERN = re.compile(
    r'\b(THE\s+[NSEW]\b|N\s*1/2|S\s*1/2|E\s*1/2|W\s*1/2|'
    r'NALF|NELF|SWLY|NWLY|SELY|NELY|HALF|SALF|EALF|WALF|'
    r'THEREOF|THENCE|BEARING|DEGREES|FEET\s+OF|'
    r'NE\s*1/4|NW\s*1/4|SE\s*1/4|SW\s*1/4|'
    r'LESS\s+AND\s+EXCEPT|COMMENC)\b',
    re.IGNORECASE
)

_RESORT_PATTERN = re.compile(
    r'\b(DISNEY|MARRIOTT|HILTON|SHERATON|WYNDHAM|WESTGATE|BLUEGREEN|'
    r'TIMESHARE|VISTANA|VACATION\s+CLUB|RESORT\s+CLUB|'
    r'GRAND\s+FLORIDIAN|ANIMAL\s+KINGDOM|WILDERNESS\s+LODGE|'
    r'BOARDWALK|SARATOGA|OLD\s+KEY\s+WEST)\b',
    re.IGNORECASE
)

_LENDER_NOISE = re.compile(
    r'\b(JPMORGAN|JMORGAN|PMORGAN|CHASE|BANK\s+OF|WELLS\s+FARGO|'
    r'CITIBANK|COUNTRYWIDE|NATIONSTAR|OCWEN|SETERUS|PHH\s+MORTGAGE|'
    r'QUICKEN|ROCKET\s+MORTGAGE|PENNYMAC|FREEDOM\s+MORTGAGE|'
    r'MORTGAGE\s+CORP|MORTGAGE\s+LLC|SERVICING|SERVICER|'
    r'FEDERAL\s+NATIONAL|FEDERAL\s+HOME|FANNIE\s+MAE|FREDDIE\s+MAC|'
    r'SECRETARY\s+OF\s+HOUSING|SECRETARY\s+OF|SECRETARY|'
    r'HOUSING\s+AND\s+UR|HUD\b|URBAN\s+DEVELOPMENT|'
    r'HOMEOWNERS\s+ASSOCIATION|HOA\b|COMMUNITY\s+ASSOCIATION)\b',
    re.IGNORECASE
)

_SPELLED_NUMBERS = {
    'ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX',
    'SEVEN', 'EIGHT', 'NINE', 'TEN', 'ELEVEN', 'TWELVE',
    'FIRST', 'SECOND', 'THIRD', 'FOURTH', 'FIFTH',
    'SIXTH', 'SEVENTH', 'EIGHTH', 'NINTH', 'TENTH',
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
    stacked:           bool = False
    stacked_docs:      list = field(default_factory=list)
    stacked_types:     list = field(default_factory=list)
    motivation_count:  int  = 1
    property_address:  str  = ""
    mailing_address:   str  = ""
    owner_name:        str  = ""
    assessed_value:    str  = ""
    match_confidence:  str  = "NONE"
    match_score:       int  = 0
    match_reason:      str  = ""
    county_search_url: str  = ""
    needs_enrichment:  bool = False
    parcel_id:         str  = ""
    nal_s_legal:       str  = ""
    prop_street:       str  = ""
    prop_city:         str  = ""
    prop_state:        str  = ""
    prop_zip:          str  = ""
    scraped_at:        str  = field(default_factory=lambda: datetime.utcnow().isoformat()+"Z")


def score_lead(doc_type, base_score):
    dt = doc_type.lower()
    flags, score = [], base_score
    if "lis pendens" in dt: flags.append("lis_pendens")
    if "tax deed"    in dt: flags.append("tax_deed"); score = max(score, 35)
    if "lien"        in dt: flags.append("lien")
    if "judgment"    in dt: flags.append("judgment")
    if "probate"     in dt: flags.append("probate")
    if "domestic"    in dt: flags.append("divorce")
    if "death"       in dt: flags.append("death_certificate")
    if "notice of"   in dt: flags.append("notice_of_commencement")
    return min(score, 100), flags


def normalize_owner_for_stacking(raw):
    if not raw:
        return ""
    text = raw.upper().strip()
    text = _LENDER_NOISE.sub('', text)
    text = re.sub(r'[^A-Z\s]', ' ', text)
    text = ' '.join(text.split())
    tokens = sorted(text.split())
    tokens = [t for t in tokens if len(t) >= 3]
    return ' '.join(tokens)


def clean_prop_addr(addr):
    """Normalize property address for dedup key — street number + street name only."""
    if not addr:
        return ""
    addr = addr.upper().strip()
    # Take only the first part before comma (street address)
    addr = addr.split(',')[0].strip()
    # Normalize whitespace
    addr = re.sub(r'\s+', ' ', addr).strip()
    return addr


def clean_parcel(pid):
    return re.sub(r'[-\s]', '', (pid or "")).strip()


def is_valid_parcel(pid):
    c = clean_parcel(pid)
    return bool(c and c.isdigit() and len(c) >= 10)


def get_lead_val(lead, key, default=""):
    """Get value from either a Lead dataclass or a dict."""
    if isinstance(lead, dict):
        return lead.get(key, default)
    return getattr(lead, key, default)


def set_lead_val(lead, key, val):
    """Set value on either a Lead dataclass or a dict."""
    if isinstance(lead, dict):
        lead[key] = val
    else:
        setattr(lead, key, val)


def merge_two_leads(primary, secondary):
    """
    Merge secondary lead's signals into primary.
    Primary keeps its address, owner, score etc.
    Secondary's doc numbers, flags, and types get added.
    Returns the updated primary.
    """
    # Combine distress flags
    p_flags = get_lead_val(primary, 'distress_flags', [])
    s_flags = get_lead_val(secondary, 'distress_flags', [])
    if isinstance(p_flags, str):
        p_flags = [f.strip() for f in p_flags.split(',') if f.strip()]
    if isinstance(s_flags, str):
        s_flags = [f.strip() for f in s_flags.split(',') if f.strip()]
    seen_flags = set(p_flags)
    for f in s_flags:
        if f not in seen_flags:
            p_flags.append(f)
            seen_flags.add(f)
    set_lead_val(primary, 'distress_flags', p_flags)

    # Combine stacked docs
    p_docs = get_lead_val(primary, 'stacked_docs', [])
    s_docs = get_lead_val(secondary, 'stacked_docs', [])
    if isinstance(p_docs, str):
        p_docs = [p_docs] if p_docs else []
    if isinstance(s_docs, str):
        s_docs = [s_docs] if s_docs else []
    p_doc_num = get_lead_val(primary, 'document_number', '')
    s_doc_num = get_lead_val(secondary, 'document_number', '')
    all_docs = list(dict.fromkeys([p_doc_num] + p_docs + [s_doc_num] + s_docs))
    all_docs = [d for d in all_docs if d]
    set_lead_val(primary, 'stacked_docs', all_docs)

    # Combine stacked types
    p_types = get_lead_val(primary, 'stacked_types', [])
    s_types = get_lead_val(secondary, 'stacked_types', [])
    if isinstance(p_types, str):
        p_types = [p_types] if p_types else []
    if isinstance(s_types, str):
        s_types = [s_types] if s_types else []
    p_doc_type = get_lead_val(primary, 'document_type', '')
    s_doc_type = get_lead_val(secondary, 'document_type', '')
    all_types = list(dict.fromkeys(p_types + s_types + [p_doc_type, s_doc_type]))
    all_types = [t for t in all_types if t]
    set_lead_val(primary, 'stacked_types', all_types)
    set_lead_val(primary, 'document_type', ' + '.join(all_types))

    # Mark as stacked
    set_lead_val(primary, 'stacked', True)

    # Motivation count
    p_count = get_lead_val(primary, 'motivation_count', 1)
    s_count = get_lead_val(secondary, 'motivation_count', 1)
    set_lead_val(primary, 'motivation_count', p_count + s_count)

    # Fill missing address fields from secondary if primary is missing them
    for field in ['property_address', 'prop_street', 'prop_city', 'prop_state',
                  'prop_zip', 'mailing_address', 'mail_street', 'mail_city',
                  'mail_state', 'mail_zip', 'owner_name', 'assessed_value',
                  'parcel_id', 'county_search_url']:
        if not get_lead_val(primary, field) and get_lead_val(secondary, field):
            set_lead_val(primary, field, get_lead_val(secondary, field))

    return primary


def stack_by_property(all_leads):
    """
    Fast property-level stacking — O(n) single pass.
    Groups ALL leads by parcel ID (primary) or property address (fallback).
    Ensures same property never appears as multiple rows.
    """
    log.info("=== Property-level stacking pass on %d leads ===", len(all_leads))

    by_parcel = {}  # parcel_id -> index in output
    by_addr   = {}  # normalized address -> index in output
    output    = []
    merged_count = 0

    for lead in all_leads:
        pid   = clean_parcel(get_lead_val(lead, 'parcel_id', ''))
        addr  = clean_prop_addr(get_lead_val(lead, 'property_address', ''))
        score = float(get_lead_val(lead, 'seller_score', 0) or 0)

        matched_idx = None

        # Check parcel ID first
        if is_valid_parcel(pid) and pid in by_parcel:
            matched_idx = by_parcel[pid]
        # Check address fallback
        elif addr and len(addr) > 8 and addr in by_addr:
            matched_idx = by_addr[addr]

        if matched_idx is not None:
            existing       = output[matched_idx]
            existing_score = float(get_lead_val(existing, 'seller_score', 0) or 0)
            if score > existing_score:
                output[matched_idx] = merge_two_leads(lead, existing)
            else:
                output[matched_idx] = merge_two_leads(existing, lead)
            merged_count += 1
        else:
            new_idx = len(output)
            output.append(lead)
            if is_valid_parcel(pid):
                by_parcel[pid] = new_idx
            if addr and len(addr) > 8:
                by_addr[addr] = new_idx

    log.info("Property stacking: %d leads -> %d unique properties (%d merged)",
             len(all_leads), len(output), merged_count)
    return output


def stack_leads(all_leads):
    groups = defaultdict(list)
    ungrouped = []

    for lead in all_leads:
        key = normalize_owner_for_stacking(lead.grantee)
        if key and len(key) >= 6:
            groups[key].append(lead)
        else:
            ungrouped.append(lead)

    stacked_leads = []

    for key, group in groups.items():
        if len(group) == 1:
            stacked_leads.append(group[0])
            continue

        group.sort(key=lambda l: l.seller_score, reverse=True)
        primary = group[0]

        total_score = min(sum(l.seller_score for l in group), 100)

        all_flags = []
        seen_flags = set()
        for lead in group:
            for flag in lead.distress_flags:
                if flag not in seen_flags:
                    all_flags.append(flag)
                    seen_flags.add(flag)

        all_doc_nums  = [l.document_number for l in group]
        all_doc_types = list(dict.fromkeys([l.document_type for l in group]))

        group_by_date = sorted(group, key=lambda l: l.file_date, reverse=True)
        best_legal = next((l.legal_description for l in group_by_date if l.legal_description), primary.legal_description)

        primary.seller_score      = total_score
        primary.distress_flags    = all_flags
        primary.stacked           = True
        primary.stacked_docs      = all_doc_nums
        primary.stacked_types     = all_doc_types
        primary.motivation_count  = len(group)
        primary.legal_description = best_legal
        primary.document_type     = " + ".join(all_doc_types)

        stacked_leads.append(primary)
        log.info("Stacked %d signals for '%s' -> score %d | %s",
                 len(group), key[:40], total_score, ", ".join(all_doc_types))

    stacked_leads.extend(ungrouped)
    stacked_leads.sort(key=lambda l: l.seller_score, reverse=True)

    stacked_count = sum(1 for l in stacked_leads if l.stacked)
    log.info("Stacking: %d total | %d stacked | %d single",
             len(stacked_leads), stacked_count, len(stacked_leads) - stacked_count)
    return stacked_leads


def normalize_legal(raw):
    if not raw:
        return ""
    text = raw.upper().strip()
    text = re.sub(r'\bPB\s+\d+[\s/]\d+\b', '', text)
    text = re.sub(r'\bPG\s+\d+\b', '', text)
    text = re.sub(r'\b(\d{2}\s+){2,}\d+\b', '', text)
    for pattern, replacement in _LEGAL_ABBREV:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r'[^A-Z0-9\s]', ' ', text)
    return ' '.join(text.split())


def classify_legal(norm_legal):
    if not norm_legal:
        return "unknown"
    if _METES_PATTERN.search(norm_legal):
        return "metes_bounds"
    if _RESORT_PATTERN.search(norm_legal):
        return "resort_timeshare"
    if re.search(r'\bCONDOMINIUM\b', norm_legal):
        return "condo"
    if re.search(r'\bLOT\s+\w|\bUNIT\s+\w|\bBLOCK\s+\w', norm_legal):
        return "subdivision"
    return "unknown"


def parse_legal(norm_legal):
    parsed = {"lot": "", "block": "", "unit": "", "section": "", "phase": "", "subdivision": ""}
    if not norm_legal:
        return parsed
    text = norm_legal

    m = re.search(r'\bLOT\s+(\w+)', text)
    if m:
        parsed["lot"] = m.group(1)

    m = re.search(r'\bBLOCK\s+(\w+)', text)
    if m:
        parsed["block"] = m.group(1)

    m = re.search(r'\bUNIT\s+(\w+)', text)
    if m:
        val = m.group(1)
        if val not in _SPELLED_NUMBERS:
            parsed["unit"] = val

    m = re.search(r'\bSECTION\s+(\w+)', text)
    if m:
        parsed["section"] = m.group(1)

    m = re.search(r'\bPHASE\s+(\w+)', text)
    if m:
        parsed["phase"] = m.group(1)

    subdiv = text
    subdiv = re.sub(r'\bLOT\s+\w+\s*', '', subdiv)
    subdiv = re.sub(r'\bBLOCK\s+\w+\s*', '', subdiv)
    subdiv = re.sub(r'^\s*UNIT\s+\w+\s+', '', subdiv)
    subdiv = re.sub(r'\bPARCEL\s+[\w\s]+', '', subdiv)
    subdiv = re.sub(r'\bSECTION\s+\w+\s*', '', subdiv)
    subdiv = re.sub(r'\bPHASE\s+\w+\s*', '', subdiv)
    subdiv = re.sub(r'\bCASE\s*:\s*[\w\s]+', '', subdiv)
    subdiv = ' '.join(subdiv.split()).strip()
    if len(subdiv) >= 3 and not subdiv.isdigit():
        parsed["subdivision"] = subdiv

    return parsed


def legal_tokens(norm_legal):
    if not norm_legal:
        return set()
    tokens = set(norm_legal.split())
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 1}


def clean_owner_field(raw):
    if not raw:
        return []
    text = raw.upper().strip()
    text = _LENDER_NOISE.sub('', text)
    parts = re.split(r',|&|\bAND\b', text)
    owners = []
    for p in parts:
        p = re.sub(r'[^A-Z\s]', ' ', p)
        p = ' '.join(p.split()).strip()
        if len(p) >= 3 and not re.match(r'^(LLC|INC|CORP|TRUST|HOA|ASSOC)', p):
            owners.append(p)
    return owners


def extract_surnames(owners):
    surnames = set()
    for owner in owners:
        parts = owner.split()
        if parts:
            surnames.add(parts[0])
    return surnames


class NALIndex:
    def __init__(self):
        self.records      = {}
        self.lot_index    = defaultdict(list)
        self.unit_index   = defaultdict(list)
        self.block_index  = defaultdict(list)
        self.subdiv_index = defaultdict(list)
        self.surname_index= defaultdict(list)
        self.token_index  = defaultdict(list)


def load_nal_index(path):
    log.info("Building NAL index...")
    idx = NALIndex()
    skipped = 0
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                phy_addr1 = (row.get("PHY_ADDR1") or "").strip()
                phy_city  = (row.get("PHY_CITY")  or "").strip()
                if not phy_addr1 or not phy_city:
                    skipped += 1
                    continue

                phy_addr2 = (row.get("PHY_ADDR2") or "").strip()
                phy_state = (row.get("PHY_STATE")  or "FL").strip()
                phy_zip   = (row.get("PHY_ZIPCD")  or "").strip()[:5]
                own_addr1 = (row.get("OWN_ADDR1") or "").strip()
                own_addr2 = (row.get("OWN_ADDR2") or "").strip()
                own_city  = (row.get("OWN_CITY")  or "").strip()
                own_state = (row.get("OWN_STATE") or "").strip()
                own_zip   = (row.get("OWN_ZIPCD") or "").strip()[:5]
                own_name  = (row.get("OWN_NAME")  or "").strip()
                s_legal   = (row.get("S_LEGAL")   or "").strip()
                av_total  = (row.get("AV_NSD") or row.get("TV_NSD") or "").strip()

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

                norm   = normalize_legal(s_legal)
                ltype  = classify_legal(norm)
                parsed = parse_legal(norm)
                tokens = legal_tokens(norm)
                owners   = clean_owner_field(own_name)
                surnames = extract_surnames(owners)

                nal_id = count
                record = {
                    "nal_id":           nal_id,
                    "parcel_id":        (row.get("PARCEL_ID") or "").strip(),
                    "property_address": property_address,
                    "mailing_address":  mailing_address,
                    "owner_name":       own_name,
                    "owners":           owners,
                    "surnames":         surnames,
                    "assessed_value":   assessed,
                    "s_legal":          s_legal,
                    "norm_legal":       norm,
                    "legal_type":       ltype,
                    "parsed":           parsed,
                    "tokens":           tokens,
                    # Raw split address fields for strict matcher
                    "phy_addr1":        phy_addr1,
                    "phy_city":         phy_city,
                    "phy_state":        phy_state,
                    "phy_zip":          phy_zip,
                    "av_raw":           av_total,
                }
                idx.records[nal_id] = record

                if parsed["lot"]:
                    idx.lot_index[parsed["lot"]].append(nal_id)
                if parsed["unit"]:
                    idx.unit_index[parsed["unit"]].append(nal_id)
                if parsed["block"]:
                    idx.block_index[parsed["block"]].append(nal_id)
                if parsed["subdivision"]:
                    idx.subdiv_index[parsed["subdivision"]].append(nal_id)
                for surname in surnames:
                    if len(surname) >= 3:
                        idx.surname_index[surname].append(nal_id)
                for token in tokens:
                    if len(token) >= 4:
                        idx.token_index[token].append(nal_id)

                count += 1
                if count % 100_000 == 0:
                    log.info("Indexed %d records...", count)

    except Exception as e:
        log.error("NAL index failed: %s", e)
        return idx

    log.info(
        "NAL index: %d records | %d lot keys | %d unit keys | %d subdiv keys | %d surname keys",
        count, len(idx.lot_index), len(idx.unit_index),
        len(idx.subdiv_index), len(idx.surname_index)
    )
    return idx


def generate_candidates(lead_parsed, lead_type, lead_surnames, nal_idx, max_candidates=100):
    candidates = set()

    lot    = lead_parsed.get("lot", "")
    unit   = lead_parsed.get("unit", "")
    block  = lead_parsed.get("block", "")
    subdiv = lead_parsed.get("subdivision", "")

    if lot:
        for nid in nal_idx.lot_index.get(lot, []):
            candidates.add(nid)

    if unit:
        for nid in nal_idx.unit_index.get(unit, []):
            candidates.add(nid)

    if block and lot:
        block_set = set(nal_idx.block_index.get(block, []))
        lot_set   = set(nal_idx.lot_index.get(lot, []))
        candidates.update(block_set & lot_set)

    if subdiv:
        subdiv_tokens = [t for t in subdiv.split()
                         if t not in _STOPWORDS and len(t) >= 4]
        if subdiv_tokens:
            token_sets = []
            for tok in subdiv_tokens:
                s = set(nal_idx.token_index.get(tok, []))
                if s:
                    token_sets.append(s)
            if token_sets:
                intersection = token_sets[0]
                for s in token_sets[1:]:
                    intersection = intersection & s
                    if not intersection:
                        break
                if intersection:
                    candidates.update(intersection)
                elif len(token_sets) >= 2:
                    candidates.update(token_sets[0] | token_sets[1])

    if len(candidates) < 5 and lead_surnames:
        for surname in lead_surnames:
            for nid in nal_idx.surname_index.get(surname, []):
                candidates.add(nid)
            if len(candidates) >= max_candidates:
                break

    return candidates


def score_candidate(lead_parsed, lead_type, lead_norm_legal, lead_surnames, rec):
    score = 0
    notes = []

    r_parsed   = rec["parsed"]
    r_type     = rec["legal_type"]
    r_norm     = rec["norm_legal"]
    r_surnames = rec["surnames"]

    if lead_type == r_type:
        score += 40
        notes.append("type+40")
    elif lead_type == "subdivision" and r_type == "metes_bounds":
        score -= 35
        notes.append("metes_mismatch-35")
    elif lead_type not in ("unknown",) and r_type not in ("unknown",) and lead_type != r_type:
        if not (lead_type in ("condo", "subdivision") and r_type in ("condo", "subdivision")):
            score -= 10
            notes.append("type_mismatch-10")

    if lead_parsed.get("lot") and lead_parsed["lot"] == r_parsed.get("lot"):
        score += 30
        notes.append(f"lot+30({lead_parsed['lot']})")

    if lead_parsed.get("unit") and lead_parsed["unit"] == r_parsed.get("unit"):
        score += 30
        notes.append(f"unit+30({lead_parsed['unit']})")

    if lead_parsed.get("block") and lead_parsed["block"] == r_parsed.get("block"):
        score += 25
        notes.append(f"block+25({lead_parsed['block']})")

    lead_subdiv = lead_parsed.get("subdivision", "")
    r_subdiv    = r_parsed.get("subdivision", "")
    if lead_subdiv and r_subdiv:
        lt = {t for t in lead_subdiv.split() if t not in _STOPWORDS and len(t) >= 3}
        rt = {t for t in r_subdiv.split()    if t not in _STOPWORDS and len(t) >= 3}
        if lt and rt:
            overlap = len(lt & rt) / max(len(lt), 1)
            if overlap >= 0.8:
                score += 35
                notes.append(f"subdiv_tok+35({overlap:.0%})")
            elif overlap >= 0.5:
                score += 20
                notes.append(f"subdiv_tok+20({overlap:.0%})")
            elif overlap >= 0.25:
                score += 10
                notes.append(f"subdiv_tok+10({overlap:.0%})")

        fs = fuzz.token_sort_ratio(lead_subdiv, r_subdiv)
        if fs >= 90:
            score += 20
            notes.append(f"fuzzy+20({fs})")
        elif fs >= 80:
            score += 10
            notes.append(f"fuzzy+10({fs})")

    if lead_norm_legal and r_norm and lead_norm_legal == r_norm:
        score += 10
        notes.append("exact_legal+10")

    if lead_surnames and r_surnames:
        common = lead_surnames & r_surnames
        if common:
            score += 20
            notes.append(f"surname+20({','.join(list(common)[:2])})")
            if len(common) >= 2:
                score += 10
                notes.append("co_owner+10")

    if not lead_parsed.get("lot") and not lead_parsed.get("unit"):
        score -= 15
        notes.append("no_anchor-15")

    return score, " | ".join(notes)


def label_match(score, parsed, notes):
    has_anchor        = "lot+" in notes or "unit+" in notes
    has_strong_subdiv = "subdiv_tok+35" in notes or "exact_legal+10" in notes
    if score >= 85 and has_anchor and has_strong_subdiv:
        return "HIGH"
    if score >= 65:
        return "MEDIUM"
    if score >= 30:
        return "LOW"
    return "NONE"


def match_lead(lead, nal_idx):
    norm_legal = normalize_legal(lead.legal_description or "")
    legal_type = classify_legal(norm_legal)
    parsed     = parse_legal(norm_legal)

    raw_owners = _get_raw_owners(lead)
    owners     = clean_owner_field(raw_owners)
    surnames   = extract_surnames(owners)

    candidates = generate_candidates(parsed, legal_type, surnames, nal_idx)

    if not candidates:
        return _no_match()

    scored = []
    for nid in candidates:
        rec = nal_idx.records.get(nid)
        if not rec:
            continue
        s, notes = score_candidate(parsed, legal_type, norm_legal, surnames, rec)
        scored.append((s, notes, rec))

    if not scored:
        return _no_match()

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_notes, best_rec = scored[0]

    if len(scored) >= 2 and (best_score - scored[1][0]) < 15:
        if "subdiv_tok+35" in best_notes:
            best_score -= 10
            best_notes += " | ambiguous-10"
        else:
            best_score -= 20
            best_notes += " | ambiguous-20"

    label = label_match(best_score, parsed, best_notes)

    return {
        "match_confidence": label,
        "match_reason":     f"score={best_score} | {best_notes}"[:200],
        "match_score":      best_score,
        "property_address": best_rec["property_address"],
        "mailing_address":  best_rec["mailing_address"],
        "owner_name":       best_rec["owner_name"],
        "assessed_value":   best_rec["assessed_value"],
    }


def _no_match():
    return {
        "match_confidence": "NONE",
        "match_reason":     "No candidates generated",
        "match_score":      0,
        "property_address": "",
        "mailing_address":  "",
        "owner_name":       "",
        "assessed_value":   "",
    }


def _get_raw_owners(lead):
    dt = lead.document_type.lower()
    primary = "both"
    for key, val in DOC_TYPE_PRIMARY_NAME.items():
        if key in dt:
            primary = val
            break
    if primary == "grantee":
        return lead.grantee or lead.grantor
    if primary == "grantor":
        return lead.grantor or lead.grantee
    return f"{lead.grantee},{lead.grantor}"


def build_county_search_url(lead):
    raw      = _get_raw_owners(lead)
    owners   = clean_owner_field(raw)
    surnames = extract_surnames(owners)
    if surnames:
        last = sorted(surnames)[0]
        return (
            "https://www.ocpafl.org/searches/ParcelSearch.aspx"
            f"?SearchType=owner&SearchValue={urllib.parse.quote(last)}"
        )
    return OC_APPRAISER_SEARCH


# ── STRICT LEGAL MATCHER ─────────────────────────────────────────────────
# Proven via test_legal_match.py — resolves subdivision lots exactly.
# Condo/unit matching is NOT yet integrated (needs separate approach).

_STOP_WORDS_LEGAL = {'THE','OF','A','AN','AND','OR','IN','AT','TO','FOR','PB','PG','PLAT','BOOK','PAGE'}


_OPTIONAL_LEGAL_WORDS = {
    'REPLAT','PHASE','UNIT','SECTION','PLAT','AMENDED','REVISED',
    'ADDITION','EXTENSION','TRACT','PARCEL','NO'
}


def _parse_legal_strict(raw):
    """
    Parse courthouse legal description into structured fields.
    Separates core subdivision tokens (required for S_LEGAL match)
    from optional words (PHASE, REPLAT, UNIT) that NAL S_LEGAL may truncate.
    """
    if not raw:
        return {}
    text = raw.upper().strip()
    text = re.sub(r'\bLT\b',        'LOT',   text)
    text = re.sub(r'\bBLK\b',       'BLOCK', text)
    text = re.sub(r'\bUNIT\s+NO\b', 'UNIT',  text)
    text = re.sub(r'\bPB\s+\d+[\s/]\d+\b', '', text)
    text = re.sub(r'\bPG\s+\d+\b',           '', text)
    text = re.sub(r'\bCASE\s*:\s*[\w\s]+',   '', text)
    text = re.sub(r'[^A-Z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    result = {
        'lot':'', 'block':'', 'unit':'', 'phase':'',
        'subdivision':'', 'full_phrase':'', 'core_tokens':[], 'raw_norm':text
    }

    for key, pattern in [
        ('lot',   r'\bLOT\s+(\d+)\b'),
        ('block', r'\bBLOCK\s+(\w+)\b'),
        ('unit',  r'\bUNIT\s+(\w+)\b'),
        ('phase', r'\bPHASE\s+(\w+)\b'),
    ]:
        m = re.search(pattern, text)
        if m:
            val = m.group(1)
            result[key] = str(int(val)) if val.isdigit() else val

    subdiv = text
    for pat in [r'\bLOT\s+\d+\b', r'\bBLOCK\s+\w+\b', r'\bUNIT\s+\w+\b',
                r'\bPHASE\s+\w+\b', r'\bSECTION\s+\w+\b']:
        subdiv = re.sub(pat, '', subdiv)
    subdiv = re.sub(r'\s+', ' ', subdiv).strip()
    if len(subdiv) >= 3 and not subdiv.isdigit():
        result['subdivision'] = subdiv

    parts = [result['subdivision']]
    if result['unit']:  parts.append(f"UNIT {result['unit']}")
    if result['phase']: parts.append(f"PHASE {result['phase']}")
    result['full_phrase'] = ' '.join(p for p in parts if p)

    # Core tokens — distinctive words REQUIRED in S_LEGAL
    # Strips optional words (REPLAT, PHASE, etc.) NAL may truncate
    result['core_tokens'] = [
        t for t in result['subdivision'].split()
        if t not in _OPTIONAL_LEGAL_WORDS
        and t not in _STOP_WORDS_LEGAL
        and len(t) >= 3
        and not t.isdigit()
    ]

    return result


def _s_legal_matches(s_legal, full_phrase, core_tokens=None):
    """
    Two-tier S_LEGAL matching:
    - Required: core_tokens (distinctive subdivision name words) ALL must appear
    - Optional: PHASE, UNIT, REPLAT etc. not required (NAL often truncates)

    WATERSIDE ON JOHNS LAKE PHASE 1 REPLAT:
      core_tokens = [WATERSIDE, JOHNS, LAKE]  ← required
      optional    = [PHASE, 1, REPLAT]        ← not required
    Matches: 'WATERSIDE ON JOHNS LAKE - PHAS' ✅
    """
    if not s_legal:
        return False
    tokens = core_tokens if core_tokens else [
        t for t in (full_phrase or '').upper().split()
        if t not in _STOP_WORDS_LEGAL and len(t) >= 3 and not t.isdigit()
    ]
    if not tokens:
        return False
    legal_upper = s_legal.upper()
    return all(re.search(rf'\b{re.escape(t)}\b', legal_upper) for t in tokens)


def _lot_to_suffixes(lot):
    """Lot → candidate parcel ID suffixes. Lot 29 → ['290','029','0029','00029','29']"""
    if not lot:
        return []
    try:
        n = int(lot)
    except ValueError:
        return []
    return [str(n*10).zfill(3), str(n).zfill(3), str(n).zfill(4), str(n).zfill(5), str(n)]


class NALRowIndex:
    """Lightweight index over raw NAL rows for strict matching."""
    def __init__(self, rows):
        self.rows = rows
        # Build S_LEGAL token index for fast subdivision lookup
        self._token_idx = defaultdict(list)
        for i, row in enumerate(rows):
            s = (row.get('S_LEGAL') or '').upper()
            for tok in set(s.split()):
                if len(tok) >= 4:
                    self._token_idx[tok].append(i)

    def find_by_s_legal(self, full_phrase, core_tokens=None):
        """Find all rows where S_LEGAL matches using core tokens."""
        tokens = core_tokens if core_tokens else [
            t for t in (full_phrase or '').upper().split()
            if t not in _STOP_WORDS_LEGAL and len(t) >= 4
        ]
        if not tokens:
            return []
        candidate_sets = [set(self._token_idx.get(t, [])) for t in tokens]
        if not candidate_sets:
            return []
        common = candidate_sets[0]
        for s in candidate_sets[1:]:
            common &= s
            if not common:
                break
        results = []
        for i in common:
            row = self.rows[i]
            if _s_legal_matches(row.get('S_LEGAL',''), full_phrase, core_tokens=tokens):
                results.append(row)
        return results



def strict_legal_match_row(legal_desc, nal_row_idx, filing_party=''):
    """
    PRIMARY matching method — strict structured legal description lookup.
    Three-layer matching proven via test_legal_match.py:
    1. Core subdivision tokens → candidate pool (ignores PHASE/REPLAT/UNIT)
    2. Parcel suffix (lot × 10) → narrows candidates
    3. Owner name confirmation → resolves ambiguous cases
    Returns result dict or None (fall through to fuzzy matcher).
    """
    parsed = _parse_legal_strict(legal_desc)
    lot    = parsed.get('lot','')
    phrase = parsed.get('full_phrase','')
    core   = parsed.get('core_tokens', [])

    if not phrase or not lot:
        return None  # No lot → can't confirm property, fall through

    # Step 1: S_LEGAL filter using core tokens only
    subdiv_candidates = nal_row_idx.find_by_s_legal(phrase, core_tokens=core or None)
    if not subdiv_candidates:
        return None  # No subdivision match — fall through

    # Step 2: Parcel suffix lot filter
    suffixes = _lot_to_suffixes(lot)
    lot_candidates = []
    matched_suffix = ''
    for suffix in suffixes:
        hits = [r for r in subdiv_candidates
                if re.sub(r'[-\s]','',r.get('PARCEL_ID','')).endswith(suffix)]
        if hits:
            lot_candidates = hits
            matched_suffix = suffix
            break

    if not lot_candidates:
        return None  # No lot match — fall through to fuzzy

    # Step 3: Owner confirmation tiebreaker
    if len(lot_candidates) > 1 and filing_party:
        party_tokens = [t for t in filing_party.upper().split() if len(t) >= 4]
        confirmed = [
            r for r in lot_candidates
            if any(tok in (r.get('OWN_NAME') or '').upper() for tok in party_tokens)
        ]
        if len(confirmed) >= 1:
            lot_candidates = confirmed
            log.debug("Owner confirmation: %d → %d candidates", len(lot_candidates), len(confirmed))

    if len(lot_candidates) > 1:
        log.debug("Strict match AMBIGUOUS after owner check: %d for lot %s in %s",
                  len(lot_candidates), lot, phrase)
        return None

    if not lot_candidates:
        return None

    # Exactly one match — HIGH confidence
    row    = lot_candidates[0]
    addr   = (row.get('PHY_ADDR1')  or '').strip()
    city   = (row.get('PHY_CITY')   or '').strip()
    state  = (row.get('PHY_STATE')  or 'FL').strip() or 'FL'
    zipcd  = str(row.get('PHY_ZIPCD') or '').strip()[:5]
    parcel = (row.get('PARCEL_ID')  or '').strip()
    owner  = (row.get('OWN_NAME')   or '').strip()
    s_leg  = (row.get('S_LEGAL')    or '').strip()
    jv     = (row.get('JV') or row.get('AV_NSD') or '').strip()

    av = ''
    if jv:
        try: av = f"${int(jv):,}"
        except ValueError: av = jv

    log.debug("Strict HIGH: parcel=%s addr=%s [lot=%s suffix=%s]",
              parcel, addr, lot, matched_suffix)

    return {
        "match_confidence": "HIGH",
        "match_score":      100,
        "match_reason":     f"strict:S_LEGAL={s_leg[:40]}+suffix={matched_suffix}|lot={lot}",
        "property_address": f"{addr}, {city}, {state} {zipcd}".strip(', '),
        "mailing_address":  "",
        "owner_name":       owner,
        "assessed_value":   av,
        "parcel_id":        parcel,
        "nal_s_legal":      s_leg,
        "prop_street":      addr,
        "prop_city":        city,
        "prop_state":       state,
        "prop_zip":         zipcd,
    }


# ── CONDO / UNIT EXCLUSION ────────────────────────────────────────────────
# Condos and unit-based filings are not wholesaleable single-family targets.
# Excludes from main dashboard/export. HOA records are NOT excluded.

_CONDO_LEGAL_RE = re.compile(
    r'\b(CONDOMINIUM|CONDO|TOWNHOUSE\s+CONDO|VILLA\s+CONDO|'
    r'APARTMENT|TIMESHARE|VACATION\s+CLUB)\b',
    re.IGNORECASE
)

_CONDO_ENTITY_RE = re.compile(
    r'\b(CONDOMINIUM\s+ASSOCIATION|CONDO\s+ASSOCIATION|'
    r'APARTMENT\s+ASSOCIATION|METROWEST\s+CONDOMINIUM|'
    r'CONDOMINIUM\s+ASSOC)\b',
    re.IGNORECASE
)

# Unit indicators in address — only flag when combined with condo legal
_UNIT_ADDR_RE = re.compile(r'\b(UNIT|APT|APARTMENT)\s+\w+', re.IGNORECASE)


def is_condo_lead(lead):
    """
    Returns (True, reason) if lead appears to be a condo/unit property.
    Returns (False, '') if lead appears to be a targetable SFR/land.
    HOA records are NOT excluded — only condo/unit records.
    """
    legal  = (lead.legal_description or '').upper()
    grantee = (lead.grantee or '').upper()
    grantor = (lead.grantor or '').upper()

    # Legal description contains condo/unit keywords
    if _CONDO_LEGAL_RE.search(legal):
        match = _CONDO_LEGAL_RE.search(legal)
        return True, f"Legal description contains condo indicator: {match.group()}"

    # Party/entity name identifies a condo association
    for party in [grantee, grantor]:
        if _CONDO_ENTITY_RE.search(party):
            match = _CONDO_ENTITY_RE.search(party)
            return True, f"Party name identifies condo entity: {match.group()}"

    # Resort/timeshare pattern in legal (already in _RESORT_PATTERN)
    if _RESORT_PATTERN.search(legal):
        return True, "Resort/timeshare property"

    return False, ''


def filter_condo_leads(leads):
    """
    Separates leads into (active_leads, excluded_condos).
    active_leads  → SFR, subdivision, land — goes to dashboard/export
    excluded_condos → condo/unit properties — saved separately for audit
    """
    active   = []
    excluded = []
    for lead in leads:
        condo, reason = is_condo_lead(lead)
        if condo:
            d = asdict(lead) if isinstance(lead, Lead) else dict(lead)
            d["exclude_from_dashboard"] = True
            d["exclude_from_export"]    = True
            d["property_type_detected"] = "condo_or_unit"
            d["exclude_reason"]         = reason
            excluded.append(d)
        else:
            active.append(lead)
    log.info("Condo filter: %d active | %d excluded condos/units", len(active), len(excluded))
    return active, excluded


def save_excluded_condos(excluded, path="data/excluded_condos.json"):
    """Save excluded condo leads for audit purposes."""
    if not excluded:
        return
    os.makedirs("data", exist_ok=True)
    payload = {
        "generated_at":   datetime.utcnow().isoformat() + "Z",
        "total_excluded": len(excluded),
        "note":           "Condo/unit properties excluded from main dashboard and export",
        "leads":          excluded,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    log.info("Saved %d excluded condos -> %s", len(excluded), path)



def _extract_direct_parcel(legal_desc):
    """
    Extract a directly embedded parcel ID from a legal description.
    Some Orange County filings include the parcel inline e.g.:
    'Lot 25 Parcel 25 23 27 1213 00 250 CASA DEL LAGO REPLAT'
    → 252327121300250

    Parcel format: SS TT RR SSSS BB LLL (15 digits when concatenated)
    Also handles: 25-23-27-1213-00-250
    """
    if not legal_desc:
        return []

    text = legal_desc.upper()

    # Pattern 1: hyphenated parcel  e.g. 25-23-27-1213-00-250
    hyphen_matches = re.findall(r'\b\d{2}-\d{2}-\d{2}-\d{4}-\d{2}-\d{3}\b', text)
    results = [re.sub(r'[-\s]', '', m) for m in hyphen_matches]

    # Pattern 2: spaced parcel  e.g. 25 23 27 1213 00 250
    spaced = re.findall(r'\b(\d{2})\s+(\d{2})\s+(\d{2})\s+(\d{4})\s+(\d{2})\s+(\d{3})\b', text)
    results += [''.join(m) for m in spaced]

    # Pattern 3: 15-digit compact  e.g. 252327121300250
    compact = re.findall(r'\b\d{15}\b', text)
    results += compact

    return list(set(results))


def save_no_match_diagnostics(leads, path="data/no_match_diagnostics.csv"):
    """
    Save LOW/NONE confidence leads to a diagnostics CSV for auditing.
    Helps identify why certain leads couldn't be matched.
    """
    import csv as _csv
    diag_leads = []
    for lead in leads:
        d = lead if isinstance(lead, dict) else asdict(lead)
        conf = d.get("match_confidence", "NONE")
        if conf in ("LOW", "NONE"):
            flags = d.get("distress_flags", [])
            if isinstance(flags, list): flags = ", ".join(flags)
            legal = d.get("legal_description", "") or ""
            try:
                parsed = _parse_legal_strict(legal) if legal else {}
            except Exception:
                parsed = {}
            try:
                direct = _extract_direct_parcel(legal) if legal else []
            except Exception:
                direct = []
            diag_leads.append({
                "match_confidence":          conf,
                "match_score":               d.get("match_score", 0),
                "match_reason":              d.get("match_reason", ""),
                "document_type":             d.get("document_type", ""),
                "document_number":           d.get("document_number", ""),
                "grantor":                   d.get("grantor", ""),
                "grantee":                   d.get("grantee", ""),
                "owner_name":                d.get("owner_name", ""),
                "legal_description":         legal,
                "parsed_lot":                parsed.get("lot", ""),
                "parsed_unit":               parsed.get("unit", ""),
                "parsed_subdivision":        parsed.get("subdivision", ""),
                "core_tokens":               "|".join(parsed.get("core_tokens", [])),
                "direct_parcels_in_legal":   "|".join(direct),
                "property_address":          d.get("property_address", ""),
                "parcel_id":                 d.get("parcel_id", ""),
                "distress_flags":            flags,
            })

    if not diag_leads:
        log.info("No LOW/NONE leads to diagnose")
        return

    os.makedirs("data", exist_ok=True)
    fields = list(diag_leads[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(diag_leads)
    log.info("No-match diagnostics saved: %s (%d records)", path, len(diag_leads))


def build_nal_row_index(nal_idx):
    """
    Build a NALRowIndex from the existing NALIndex records.
    Maps stored record fields to the column names NALRowIndex expects.
    """
    rows = []
    for rec in nal_idx.records.values():
        rows.append({
            'PARCEL_ID': rec.get('parcel_id', ''),
            'S_LEGAL':   rec.get('s_legal', ''),   # raw S_LEGAL — not normalized
            'PHY_ADDR1': rec.get('phy_addr1', ''),
            'PHY_CITY':  rec.get('phy_city', ''),
            'PHY_STATE': rec.get('phy_state', 'FL'),
            'PHY_ZIPCD': rec.get('phy_zip', ''),
            'OWN_NAME':  rec.get('owner_name', ''),
            'JV':        rec.get('av_raw', ''),
            'AV_NSD':    '',
        })
    return NALRowIndex(rows)


def enrich_leads(leads, nal_idx):
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NONE": 0}
    strict_hits = 0
    direct_hits = 0
    fuzzy_hits  = 0

    # Build fast row index for strict matching
    nal_row_idx = build_nal_row_index(nal_idx)
    log.info("Built NAL row index for strict matching (%d rows)", len(nal_row_idx.rows))

    for lead in leads:
        strict_result = None
        direct_result = None

        # ── STEP 0: Direct parcel extraction from legal description ──────
        # Some filings embed the parcel ID directly in the legal description.
        # This is the most reliable match — exact parcel lookup.
        if lead.legal_description:
            direct_parcels = _extract_direct_parcel(lead.legal_description)
            for pid in direct_parcels:
                # Look up directly in NAL rows by PARCEL_ID
                matches = [r for r in nal_row_idx.rows
                           if re.sub(r'[-\s]','',r.get('PARCEL_ID','')) == pid]
                if len(matches) == 1:
                    row    = matches[0]
                    addr   = (row.get('PHY_ADDR1')  or '').strip()
                    city   = (row.get('PHY_CITY')   or '').strip()
                    state  = (row.get('PHY_STATE')  or 'FL').strip() or 'FL'
                    zipcd  = str(row.get('PHY_ZIPCD') or '').strip()[:5]
                    owner  = (row.get('OWN_NAME')   or '').strip()
                    jv     = (row.get('JV') or row.get('AV_NSD') or '').strip()
                    av = ''
                    if jv:
                        try: av = f"${int(jv):,}"
                        except ValueError: av = jv
                    direct_result = {
                        "match_confidence": "HIGH",
                        "match_score":      100,
                        "match_reason":     f"direct_parcel:{pid}",
                        "property_address": f"{addr}, {city}, {state} {zipcd}".strip(', '),
                        "assessed_value":   av,
                        "parcel_id":        pid,
                        "nal_s_legal":      (row.get('S_LEGAL') or '').strip(),
                        "prop_street":      addr,
                        "prop_city":        city,
                        "prop_state":       state,
                        "prop_zip":         zipcd,
                        "owner_name":       owner,
                    }
                    break

        # ── STEP 1: Strict structured legal match ────────────────────────
        if not direct_result and lead.legal_description:
            strict_result = strict_legal_match_row(
                lead.legal_description, nal_row_idx,
                filing_party=lead.grantee or lead.grantor or ''
            )

        best_result = direct_result or strict_result

        if best_result:
            lead.match_confidence  = best_result["match_confidence"]
            lead.match_score       = best_result["match_score"]
            lead.match_reason      = best_result["match_reason"]
            lead.property_address  = best_result["property_address"]
            lead.assessed_value    = best_result["assessed_value"]
            lead.needs_enrichment  = False
            lead.parcel_id         = best_result.get("parcel_id", "")
            lead.nal_s_legal       = best_result.get("nal_s_legal", "")
            lead.prop_street       = best_result.get("prop_street", "")
            lead.prop_city         = best_result.get("prop_city", "")
            lead.prop_state        = best_result.get("prop_state", "FL")
            lead.prop_zip          = best_result.get("prop_zip", "")
            lead.county_search_url = build_county_search_url(lead)
            if not lead.owner_name and best_result.get("owner_name"):
                lead.owner_name = best_result["owner_name"]
            if direct_result:
                direct_hits += 1
            else:
                strict_hits += 1

        else:
            # ── FALLBACK: Fuzzy NAL matching ──────────────────────────────
            result = match_lead(lead, nal_idx)
            lead.match_confidence  = result["match_confidence"]
            lead.match_reason      = result["match_reason"]
            lead.match_score       = result["match_score"]
            lead.property_address  = result["property_address"]
            lead.mailing_address   = result["mailing_address"]
            lead.assessed_value    = result["assessed_value"]
            lead.county_search_url = build_county_search_url(lead)
            lead.needs_enrichment  = lead.match_confidence in ("LOW", "NONE")
            # Never overwrite filing party with NAL owner
            if not lead.owner_name and result["owner_name"]:
                lead.owner_name = result["owner_name"]
            if result["match_confidence"] not in ("LOW","NONE"):
                fuzzy_hits += 1

        counts[lead.match_confidence] += 1

    log.info(
        "Match results -> HIGH:%d  MEDIUM:%d  LOW:%d  NONE:%d | direct=%d strict=%d fuzzy=%d",
        counts["HIGH"], counts["MEDIUM"], counts["LOW"], counts["NONE"],
        direct_hits, strict_hits, fuzzy_hits
    )
    return leads


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
        # Determine primary owner name from filing
        # For LP/Tax Deed: grantee is the defendant (property owner being sued) — call them
        # For Lien/Judgment: grantor is the property owner — call them
        dt_lower = dtype.lower()
        if any(x in dt_lower for x in ['lis pendens', 'tax deed', 'death', 'probate']):
            owner = row.get("Grantee", "") or row.get("Grantor", "")
        else:
            owner = row.get("Grantor", "") or row.get("Grantee", "")
        leads.append(Lead(
            document_number=doc_num,
            file_date=row.get("Recording Date", ""),
            grantor=row.get("Grantor", ""),
            grantee=row.get("Grantee", ""),
            legal_description=row.get("Legal", ""),
            document_type=dtype,
            seller_score=score,
            distress_flags=flags,
            owner_name=owner,
        ))
    return leads


def save_csv(leads, path="data/output.csv"):
    os.makedirs("data", exist_ok=True)
    fields = [
        "seller_score", "motivation_count", "document_number", "file_date",
        "document_type", "stacked", "stacked_types",
        "grantor", "grantee", "legal_description",
        "property_address", "mailing_address", "owner_name", "assessed_value",
        "match_confidence", "match_score", "match_reason", "county_search_url",
        "distress_flags", "needs_enrichment", "scraped_at",
        "prop_street", "prop_city", "prop_state", "prop_zip",
        "mail_street", "mail_city", "mail_state", "mail_zip",
        "parcel_id", "tax_years_delinquent", "tax_total_balance",
        "tax_years_list", "tax_cert_status",
        "code_violation_count", "code_violation_types",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            row = asdict(lead) if isinstance(lead, Lead) else dict(lead)
            if isinstance(row.get("distress_flags"), list):
                row["distress_flags"] = ", ".join(row["distress_flags"])
            if isinstance(row.get("stacked_types"), list):
                row["stacked_types"] = " + ".join(row["stacked_types"])
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
        json.dump(payload, f)
    log.info("Saved %d records to %s", len(leads), OUTPUT_PATH)


def main():
    log.info("=== OC Motivated Seller Scraper ===")
    log.info("Date range: %s to %s", DATE_START, DATE_END)

    nal_idx = None
    if download_nal_file():
        nal_idx = load_nal_index(NAL_LOCAL_PATH)
    else:
        log.warning("NAL unavailable - all leads will be NONE confidence")

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
        log.info("Stacking %d raw leads by owner name...", len(new_leads))
        new_leads = stack_leads(new_leads)

    # ── Filter out condo/unit properties before enrichment ────────────────
    if new_leads:
        new_leads, excluded_condos = filter_condo_leads(new_leads)
        save_excluded_condos(excluded_condos)

    if new_leads and nal_idx:
        new_leads = enrich_leads(new_leads, nal_idx)

    existing = load_existing(OUTPUT_PATH)

    # Merge new leads into existing — skip leads whose doc numbers already exist
    existing_nums = set()
    for l in existing:
        doc = l["document_number"] if isinstance(l, dict) else l.document_number
        existing_nums.add(doc)
        stacked = l.get("stacked_docs", []) if isinstance(l, dict) else getattr(l, "stacked_docs", [])
        if isinstance(stacked, str):
            stacked = [stacked] if stacked else []
        for d in stacked:
            existing_nums.add(d)

    merged = list(existing)
    seen   = set(existing_nums)
    added  = 0
    for lead in new_leads:
        doc_num = lead.document_number if isinstance(lead, Lead) else lead.get('document_number', '')
        stacked_docs = lead.stacked_docs if isinstance(lead, Lead) else lead.get('stacked_docs', [])
        if isinstance(stacked_docs, str):
            stacked_docs = [stacked_docs] if stacked_docs else []
        all_nums = [doc_num] + stacked_docs
        if not any(n in seen for n in all_nums):
            merged.append(lead)
            for n in all_nums:
                seen.add(n)
            added += 1

    log.info("Added %d new leads | Total: %d leads", added, len(merged))

    # NOTE: Property-level stacking (dedup by parcel/address across all sources)
    # runs in reenrich.py which executes after all sources are loaded.
    # Do NOT run it here — tax_delinquent.py and code_violations.py haven't run yet.

    merged.sort(
        key=lambda l: l["seller_score"] if isinstance(l, dict) else l.seller_score,
        reverse=True
    )

    log.info("Final total: %d unique properties", len(merged))
    save_json(merged)
    save_csv(merged)
    save_no_match_diagnostics(merged)
    log.info("Done.")


if __name__ == "__main__":
    main()
