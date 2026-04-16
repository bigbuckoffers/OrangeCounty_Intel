"""
scraper.py — Orange County FL Automated Motivated Seller Scraper

Matching architecture (2-stage retrieval + weighted scoring):

Stage 1 — Candidate generation using inverted indexes (no O(n^2)):
  lot_index / unit_index / block_index / subdiv token index / surname index

Stage 2 — Weighted scoring per candidate:
  +40  legal type match
  +30  exact lot match
  +30  exact unit match
  +25  exact block match
  +35  strong subdivision token containment (>=80%)
  +20  moderate subdivision token containment (>=50%)
  +10  weak subdivision token containment (>=25%)
  +20  fuzzy subdivision similarity >= 90
  +10  fuzzy subdivision similarity 80-89
  +20  owner surname overlap
  +10  co-owner overlap (2+ surnames match)
  +10  exact normalized legal match
  -35  lead=subdivision but NAL=metes_bounds
  -20  multiple competing candidates with close scores (ambiguous)
  -15  no parcel anchor (no lot, no unit)

Labels:
  HIGH   = score >= 85 AND has parcel anchor (lot or unit match)
  MEDIUM = score 65-84
  LOW    = score 40-64
  NONE   = score < 40
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

# ---------------------------------------------------------------------------
# Normalization constants
# ---------------------------------------------------------------------------

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
    r'SECRETARY\s+OF\s+HOUSING|HOUSING\s+AND\s+UR|HUD\b|'
    r'HOMEOWNERS\s+ASSOCIATION|HOA\b|COMMUNITY\s+ASSOCIATION)\b',
    re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Lead dataclass
# ---------------------------------------------------------------------------

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
    match_confidence:  str  = "NONE"
    match_score:       int  = 0
    match_reason:      str  = ""
    county_search_url: str  = ""
    needs_enrichment:  bool = False
    scraped_at:        str  = field(default_factory=lambda: datetime.utcnow().isoformat()+"Z")


# ---------------------------------------------------------------------------
# Distress scoring
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


# ===========================================================================
# PREPROCESSING
# ===========================================================================

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
        parsed["unit"] = m.group(1)

    m = re.search(r'\bSECTION\s+(\w+)', text)
    if m:
        parsed["section"] = m.group(1)

    m = re.search(r'\bPHASE\s+(\w+)', text)
    if m:
        parsed["phase"] = m.group(1)

    # Subdivision: strip structural tokens, keep name
    subdiv = text
    subdiv = re.sub(r'\bLOT\s+\w+\s*', '', subdiv)
    subdiv = re.sub(r'\bBLOCK\s+\w+\s*', '', subdiv)
    subdiv = re.sub(r'^\s*UNIT\s+\w+\s+', '', subdiv)  # leading unit ref only
    subdiv = re.sub(r'\bPARCEL\s+[\w\s]+', '', subdiv)
    subdiv = re.sub(r'\bSECTION\s+\w+\s*', '', subdiv)
    subdiv = re.sub(r'\bPHASE\s+\w+\s*', '', subdiv)
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


# ===========================================================================
# NAL INDEXING
# ===========================================================================

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


# ===========================================================================
# CANDIDATE GENERATION
# ===========================================================================

def generate_candidates(lead_parsed, lead_type, lead_surnames, nal_idx, max_candidates=100):
    candidates = set()

    lot    = lead_parsed.get("lot", "")
    unit   = lead_parsed.get("unit", "")
    block  = lead_parsed.get("block", "")
    subdiv = lead_parsed.get("subdivision", "")

    # Lot candidates
    if lot:
        for nid in nal_idx.lot_index.get(lot, []):
            candidates.add(nid)

    # Unit candidates
    if unit:
        for nid in nal_idx.unit_index.get(unit, []):
            candidates.add(nid)

    # Block + lot intersection
    if block and lot:
        block_set = set(nal_idx.block_index.get(block, []))
        lot_set   = set(nal_idx.lot_index.get(lot, []))
        candidates.update(block_set & lot_set)

    # Subdivision token intersection
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
                    # Fall back to union of first 2 tokens if intersection empty
                    candidates.update(token_sets[0] | token_sets[1])

    # Surname fallback
    if len(candidates) < 5 and lead_surnames:
        for surname in lead_surnames:
            for nid in nal_idx.surname_index.get(surname, []):
                candidates.add(nid)
            if len(candidates) >= max_candidates:
                break

    return candidates


# ===========================================================================
# WEIGHTED SCORING
# ===========================================================================

def score_candidate(lead_parsed, lead_type, lead_norm_legal,
                    lead_surnames, rec):
    score = 0
    notes = []

    r_parsed   = rec["parsed"]
    r_type     = rec["legal_type"]
    r_norm     = rec["norm_legal"]
    r_surnames = rec["surnames"]

    # Legal type
    if lead_type == r_type:
        score += 40
        notes.append("type+40")
    elif lead_type == "subdivision" and r_type == "metes_bounds":
        score -= 35
        notes.append("metes_mismatch-35")
    elif lead_type not in ("unknown",) and r_type not in ("unknown",) and lead_type != r_type:
        score -= 10
        notes.append("type_mismatch-10")

    # Exact lot
    if lead_parsed.get("lot") and lead_parsed["lot"] == r_parsed.get("lot"):
        score += 30
        notes.append(f"lot+30({lead_parsed['lot']})")

    # Exact unit
    if lead_parsed.get("unit") and lead_parsed["unit"] == r_parsed.get("unit"):
        score += 30
        notes.append(f"unit+30({lead_parsed['unit']})")

    # Exact block
    if lead_parsed.get("block") and lead_parsed["block"] == r_parsed.get("block"):
        score += 25
        notes.append(f"block+25({lead_parsed['block']})")

    # Subdivision token overlap
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

        # Fuzzy as supporting feature only
        fs = fuzz.token_sort_ratio(lead_subdiv, r_subdiv)
        if fs >= 90:
            score += 20
            notes.append(f"fuzzy+20({fs})")
        elif fs >= 80:
            score += 10
            notes.append(f"fuzzy+10({fs})")

    # Exact normalized legal
    if lead_norm_legal and r_norm and lead_norm_legal == r_norm:
        score += 10
        notes.append("exact_legal+10")

    # Owner surname
    if lead_surnames and r_surnames:
        common = lead_surnames & r_surnames
        if common:
            score += 20
            notes.append(f"surname+20({','.join(list(common)[:2])})")
            if len(common) >= 2:
                score += 10
                notes.append("co_owner+10")

    # No anchor penalty
    if not lead_parsed.get("lot") and not lead_parsed.get("unit"):
        score -= 15
        notes.append("no_anchor-15")

    return score, " | ".join(notes)


def label_match(score, lead_parsed):
    has_anchor = bool(lead_parsed.get("lot") or lead_parsed.get("unit"))
    if score >= 85 and has_anchor:
        return "HIGH"
    if score >= 65:
        return "MEDIUM"
    if score >= 40:
        return "LOW"
    return "NONE"


# ===========================================================================
# MAIN MATCH FUNCTION
# ===========================================================================

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

    # Ambiguity penalty
    if len(scored) >= 2 and (best_score - scored[1][0]) < 15:
        best_score -= 20
        best_notes += " | ambiguous-20"

    label = label_match(best_score, parsed)

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


# ===========================================================================
# ENRICHMENT
# ===========================================================================

def enrich_leads(leads, nal_idx):
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NONE": 0}
    for lead in leads:
        result = match_lead(lead, nal_idx)
        lead.match_confidence  = result["match_confidence"]
        lead.match_reason      = result["match_reason"]
        lead.match_score       = result["match_score"]
        lead.property_address  = result["property_address"]
        lead.mailing_address   = result["mailing_address"]
        lead.owner_name        = result["owner_name"]
        lead.assessed_value    = result["assessed_value"]
        lead.county_search_url = build_county_search_url(lead)
        lead.needs_enrichment  = lead.match_confidence in ("LOW", "NONE")
        counts[lead.match_confidence] += 1
    log.info(
        "Match results -> HIGH:%d  MEDIUM:%d  LOW:%d  NONE:%d",
        counts["HIGH"], counts["MEDIUM"], counts["LOW"], counts["NONE"]
    )
    return leads


# ===========================================================================
# COMPTROLLER FETCH
# ===========================================================================

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


# ===========================================================================
# PERSISTENCE
# ===========================================================================

def save_csv(leads, path="data/output.csv"):
    os.makedirs("data", exist_ok=True)
    fields = [
        "seller_score", "document_number", "file_date", "document_type",
        "grantor", "grantee", "legal_description",
        "property_address", "mailing_address", "owner_name", "assessed_value",
        "match_confidence", "match_score", "match_reason", "county_search_url",
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


# ===========================================================================
# MAIN
# ===========================================================================

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

    if new_leads and nal_idx:
        new_leads = enrich_leads(new_leads, nal_idx)

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
