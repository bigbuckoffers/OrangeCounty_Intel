"""
reenrich.py — Re-run matching engine against all existing leads in output.json
Run once to backfill property addresses for leads already collected.
"""
import json, logging, os, csv, re, urllib.parse
from collections import defaultdict
from datetime import datetime
from rapidfuzz import fuzz

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

OUTPUT_PATH    = "data/output.json"
NAL_LOCAL_PATH = "/tmp/NAL_orange.csv"
NAL_GDRIVE_ID  = "1X1nZkK07FJV3BmUFHUFvpZA1hLEl4UP9"

OC_APPRAISER_SEARCH = "https://www.ocpafl.org/searches/ParcelSearch.aspx"

DOC_TYPE_PRIMARY_NAME = {
    "lis pendens": "grantee", "lp": "grantee",
    "lien": "grantor",        "ln": "grantor",
    "judgment": "grantor",    "j":  "grantor",
    "probate": "both",        "prcp": "both",
    "domestic": "both",       "drd": "both",
}

_LEGAL_ABBREV = [
    (r'\bBLK\b',    'BLOCK'),  (r'\bSEC\b',    'SECTION'),
    (r'\bSUBD\b',   'SUBDIVISION'), (r'\bSUB\b', 'SUBDIVISION'),
    (r'\bADD\b',    'ADDITION'), (r'\bESTS\b',  'ESTATES'),
    (r'\bEST\b',    'ESTATES'), (r'\bHTS\b',   'HEIGHTS'),
    (r'\bHGTS\b',   'HEIGHTS'), (r'\bCONDM\b', 'CONDOMINIUM'),
    (r'\bCONDO\b',  'CONDOMINIUM'), (r'\bCOND\b', 'CONDOMINIUM'),
    (r'\bVIL\b',    'VILLAS'), (r'\bVLS\b',    'VILLAS'),
    (r'\b1ST\b',    'FIRST'),  (r'\b2ND\b',    'SECOND'),
    (r'\b3RD\b',    'THIRD'),  (r'\bPK\b',     'PARK'),
    (r'\bGDNS\b',   'GARDENS'), (r'\bGARD\b',  'GARDENS'),
    (r'\bLOT:\s*',  'LOT '),   (r'\bUNIT:\s*', 'UNIT '),
    (r'\bBLOCK:\s*','BLOCK '),
]

_STOPWORDS = {
    'THE','OF','A','AN','AND','OR','IN','AT','TO','FOR',
    'PT','PB','PG','PLAT','BOOK','PAGE','THEREOF','THENCE',
    'BEARING','DEGREES','FEET','NORTH','SOUTH','EAST','WEST',
}

_METES_PATTERN = re.compile(
    r'\b(THE\s+[NSEW]\b|N\s*1/2|S\s*1/2|E\s*1/2|W\s*1/2|'
    r'NALF|NELF|SWLY|NWLY|SELY|NELY|HALF|SALF|EALF|WALF|'
    r'THEREOF|THENCE|BEARING|DEGREES|FEET\s+OF|'
    r'NE\s*1/4|NW\s*1/4|SE\s*1/4|SW\s*1/4|'
    r'LESS\s+AND\s+EXCEPT|COMMENC)\b', re.IGNORECASE
)

_RESORT_PATTERN = re.compile(
    r'\b(DISNEY|MARRIOTT|HILTON|SHERATON|WYNDHAM|WESTGATE|BLUEGREEN|'
    r'TIMESHARE|VISTANA|VACATION\s+CLUB|RESORT\s+CLUB|'
    r'GRAND\s+FLORIDIAN|ANIMAL\s+KINGDOM|WILDERNESS\s+LODGE|'
    r'BOARDWALK|SARATOGA|OLD\s+KEY\s+WEST)\b', re.IGNORECASE
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
# Preprocessing
# ---------------------------------------------------------------------------

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

def classify_legal(norm):
    if not norm: return "unknown"
    if _METES_PATTERN.search(norm): return "metes_bounds"
    if _RESORT_PATTERN.search(norm): return "resort_timeshare"
    if re.search(r'\bCONDOMINIUM\b', norm): return "condo"
    if re.search(r'\bLOT\s+\w|\bUNIT\s+\w|\bBLOCK\s+\w', norm): return "subdivision"
    return "unknown"

def parse_legal(norm):
    p = {"lot":"","block":"","unit":"","section":"","phase":"","subdivision":""}
    if not norm: return p
    m = re.search(r'\bLOT\s+(\w+)', norm);   p["lot"]     = m.group(1) if m else ""
    m = re.search(r'\bBLOCK\s+(\w+)', norm); p["block"]   = m.group(1) if m else ""
    m = re.search(r'\bUNIT\s+(\w+)', norm);  p["unit"]    = m.group(1) if m else ""
    m = re.search(r'\bSECTION\s+(\w+)', norm); p["section"] = m.group(1) if m else ""
    m = re.search(r'\bPHASE\s+(\w+)', norm); p["phase"]   = m.group(1) if m else ""
    subdiv = norm
    subdiv = re.sub(r'\bLOT\s+\w+\s*', '', subdiv)
    subdiv = re.sub(r'\bBLOCK\s+\w+\s*', '', subdiv)
    subdiv = re.sub(r'^\s*UNIT\s+\w+\s+', '', subdiv)
    subdiv = re.sub(r'\bPARCEL\s+[\w\s]+', '', subdiv)
    subdiv = re.sub(r'\bSECTION\s+\w+\s*', '', subdiv)
    subdiv = re.sub(r'\bPHASE\s+\w+\s*', '', subdiv)
    subdiv = ' '.join(subdiv.split()).strip()
    if len(subdiv) >= 3 and not subdiv.isdigit():
        p["subdivision"] = subdiv
    return p

def legal_tokens(norm):
    if not norm: return set()
    return {t for t in norm.split() if t not in _STOPWORDS and len(t) > 1}

def clean_owner_field(raw):
    if not raw: return []
    text = _LENDER_NOISE.sub('', raw.upper().strip())
    parts = re.split(r',|&|\bAND\b', text)
    owners = []
    for p in parts:
        p = re.sub(r'[^A-Z\s]', ' ', p)
        p = ' '.join(p.split()).strip()
        if len(p) >= 3 and not re.match(r'^(LLC|INC|CORP|TRUST|HOA|ASSOC)', p):
            owners.append(p)
    return owners

def extract_surnames(owners):
    return {o.split()[0] for o in owners if o.split()}


# ---------------------------------------------------------------------------
# NAL Index
# ---------------------------------------------------------------------------

class NALIndex:
    def __init__(self):
        self.records       = {}
        self.lot_index     = defaultdict(list)
        self.unit_index    = defaultdict(list)
        self.block_index   = defaultdict(list)
        self.subdiv_index  = defaultdict(list)
        self.surname_index = defaultdict(list)
        self.token_index   = defaultdict(list)

def load_nal_index(path):
    log.info("Building NAL index...")
    idx = NALIndex()
    count = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            phy_addr1 = (row.get("PHY_ADDR1") or "").strip()
            phy_city  = (row.get("PHY_CITY")  or "").strip()
            if not phy_addr1 or not phy_city:
                continue
            phy_addr2 = (row.get("PHY_ADDR2") or "").strip()
            phy_state = (row.get("PHY_STATE") or "FL").strip()
            phy_zip   = (row.get("PHY_ZIPCD") or "").strip()[:5]
            own_addr1 = (row.get("OWN_ADDR1") or "").strip()
            own_addr2 = (row.get("OWN_ADDR2") or "").strip()
            own_city  = (row.get("OWN_CITY")  or "").strip()
            own_state = (row.get("OWN_STATE") or "").strip()
            own_zip   = (row.get("OWN_ZIPCD") or "").strip()[:5]
            own_name  = (row.get("OWN_NAME")  or "").strip()
            s_legal   = (row.get("S_LEGAL")   or "").strip()
            av_total  = (row.get("AV_NSD") or row.get("TV_NSD") or "").strip()

            prop_addr = phy_addr1
            if phy_addr2: prop_addr += f" {phy_addr2}"
            prop_addr += f", {phy_city}, {phy_state} {phy_zip}".strip()

            mail_addr = own_addr1
            if own_addr2: mail_addr += f" {own_addr2}"
            if own_city:  mail_addr += f", {own_city}"
            if own_state: mail_addr += f", {own_state}"
            if own_zip:   mail_addr += f" {own_zip}"
            mail_addr = mail_addr.strip() or prop_addr

            assessed = ""
            try:
                av = int(av_total)
                if av > 0: assessed = f"${av:,}"
            except: pass

            norm     = normalize_legal(s_legal)
            ltype    = classify_legal(norm)
            parsed   = parse_legal(norm)
            tokens   = legal_tokens(norm)
            owners   = clean_owner_field(own_name)
            surnames = extract_surnames(owners)

            nid = count
            idx.records[nid] = {
                "property_address": prop_addr,
                "mailing_address":  mail_addr,
                "owner_name":       own_name,
                "owners":           owners,
                "surnames":         surnames,
                "assessed_value":   assessed,
                "norm_legal":       norm,
                "legal_type":       ltype,
                "parsed":           parsed,
                "tokens":           tokens,
            }

            if parsed["lot"]:   idx.lot_index[parsed["lot"]].append(nid)
            if parsed["unit"]:  idx.unit_index[parsed["unit"]].append(nid)
            if parsed["block"]: idx.block_index[parsed["block"]].append(nid)
            if parsed["subdivision"]: idx.subdiv_index[parsed["subdivision"]].append(nid)
            for s in surnames:
                if len(s) >= 3: idx.surname_index[s].append(nid)
            for t in tokens:
                if len(t) >= 4: idx.token_index[t].append(nid)

            count += 1
            if count % 100_000 == 0:
                log.info("Indexed %d...", count)

    log.info("NAL index: %d records | %d lot | %d unit | %d subdiv | %d surname",
             count, len(idx.lot_index), len(idx.unit_index),
             len(idx.subdiv_index), len(idx.surname_index))
    return idx


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def generate_candidates(parsed, legal_type, surnames, nal_idx, max_cands=100):
    candidates = set()
    lot    = parsed.get("lot", "")
    unit   = parsed.get("unit", "")
    block  = parsed.get("block", "")
    subdiv = parsed.get("subdivision", "")

    if lot:
        candidates.update(nal_idx.lot_index.get(lot, []))
    if unit:
        candidates.update(nal_idx.unit_index.get(unit, []))
    if block and lot:
        candidates.update(
            set(nal_idx.block_index.get(block, [])) &
            set(nal_idx.lot_index.get(lot, []))
        )
    if subdiv:
        toks = [t for t in subdiv.split() if t not in _STOPWORDS and len(t) >= 4]
        if toks:
            sets = [set(nal_idx.token_index.get(t, [])) for t in toks if nal_idx.token_index.get(t)]
            if sets:
                inter = sets[0]
                for s in sets[1:]:
                    inter = inter & s
                    if not inter: break
                if inter:
                    candidates.update(inter)
                elif len(sets) >= 2:
                    candidates.update(sets[0] | sets[1])

    if len(candidates) < 5 and surnames:
        for s in surnames:
            candidates.update(nal_idx.surname_index.get(s, []))
            if len(candidates) >= max_cands: break

    return candidates

def score_candidate(parsed, legal_type, norm_legal, surnames, rec):
    score = 0
    notes = []
    rp = rec["parsed"]
    rt = rec["legal_type"]

    if legal_type == rt:
        score += 40; notes.append("type+40")
    elif legal_type == "subdivision" and rt == "metes_bounds":
        score -= 35; notes.append("metes-35")
    elif legal_type not in ("unknown",) and rt not in ("unknown",) and legal_type != rt:
        score -= 10; notes.append("type_mismatch-10")

    if parsed.get("lot") and parsed["lot"] == rp.get("lot"):
        score += 30; notes.append(f"lot+30({parsed['lot']})")
    if parsed.get("unit") and parsed["unit"] == rp.get("unit"):
        score += 30; notes.append(f"unit+30({parsed['unit']})")
    if parsed.get("block") and parsed["block"] == rp.get("block"):
        score += 25; notes.append(f"block+25({parsed['block']})")

    ls = parsed.get("subdivision","")
    rs = rp.get("subdivision","")
    if ls and rs:
        lt2 = {t for t in ls.split() if t not in _STOPWORDS and len(t) >= 3}
        rt2 = {t for t in rs.split() if t not in _STOPWORDS and len(t) >= 3}
        if lt2 and rt2:
            ov = len(lt2 & rt2) / max(len(lt2), 1)
            if ov >= 0.8:   score += 35; notes.append(f"subdiv+35({ov:.0%})")
            elif ov >= 0.5: score += 20; notes.append(f"subdiv+20({ov:.0%})")
            elif ov >= 0.25:score += 10; notes.append(f"subdiv+10({ov:.0%})")
        fs = fuzz.token_sort_ratio(ls, rs)
        if fs >= 90:   score += 20; notes.append(f"fuzzy+20({fs})")
        elif fs >= 80: score += 10; notes.append(f"fuzzy+10({fs})")

    if norm_legal and rec["norm_legal"] and norm_legal == rec["norm_legal"]:
        score += 10; notes.append("exact+10")

    if surnames and rec["surnames"]:
        common = surnames & rec["surnames"]
        if common:
            score += 20; notes.append(f"surname+20({','.join(list(common)[:2])})")
            if len(common) >= 2: score += 10; notes.append("co_owner+10")

    if not parsed.get("lot") and not parsed.get("unit"):
        score -= 15; notes.append("no_anchor-15")

    return score, " | ".join(notes)

def label_match(score, parsed):
    has_anchor = bool(parsed.get("lot") or parsed.get("unit"))
    if score >= 85 and has_anchor: return "HIGH"
    if score >= 65: return "MEDIUM"
    if score >= 40: return "LOW"
    return "NONE"

def get_raw_owners(lead):
    dt = (lead.get("document_type") or "").lower()
    primary = "both"
    for key, val in DOC_TYPE_PRIMARY_NAME.items():
        if key in dt:
            primary = val
            break
    grantee = lead.get("grantee","") or ""
    grantor = lead.get("grantor","") or ""
    if primary == "grantee": return grantee or grantor
    if primary == "grantor": return grantor or grantee
    return f"{grantee},{grantor}"

def build_search_url(lead):
    raw      = get_raw_owners(lead)
    owners   = clean_owner_field(raw)
    surnames = extract_surnames(owners)
    if surnames:
        last = sorted(surnames)[0]
        return (
            "https://www.ocpafl.org/searches/ParcelSearch.aspx"
            f"?SearchType=owner&SearchValue={urllib.parse.quote(last)}"
        )
    return OC_APPRAISER_SEARCH

def match_lead(lead, nal_idx):
    norm_legal = normalize_legal(lead.get("legal_description","") or "")
    legal_type = classify_legal(norm_legal)
    parsed     = parse_legal(norm_legal)
    raw_owners = get_raw_owners(lead)
    owners     = clean_owner_field(raw_owners)
    surnames   = extract_surnames(owners)

    candidates = generate_candidates(parsed, legal_type, surnames, nal_idx)
    if not candidates:
        return None

    scored = []
    for nid in candidates:
        rec = nal_idx.records.get(nid)
        if not rec: continue
        s, notes = score_candidate(parsed, legal_type, norm_legal, surnames, rec)
        scored.append((s, notes, rec))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_notes, best_rec = scored[0]

    if len(scored) >= 2 and (best_score - scored[1][0]) < 15:
        best_score -= 20
        best_notes += " | ambiguous-20"

    label = label_match(best_score, parsed)
    return {
        "match_confidence": label,
        "match_score":      best_score,
        "match_reason":     f"score={best_score} | {best_notes}"[:200],
        "property_address": best_rec["property_address"],
        "mailing_address":  best_rec["mailing_address"],
        "owner_name":       best_rec["owner_name"],
        "assessed_value":   best_rec["assessed_value"],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def download_nal():
    if os.path.exists(NAL_LOCAL_PATH):
        log.info("NAL file already present")
        return True
    log.info("Downloading NAL file...")
    try:
        import gdown
        gdown.download(
            f"https://drive.google.com/uc?id={NAL_GDRIVE_ID}",
            NAL_LOCAL_PATH, quiet=False
        )
        return os.path.getsize(NAL_LOCAL_PATH) > 1_000_000
    except Exception as e:
        log.error("Download failed: %s", e)
        return False

def main():
    log.info("=== Re-enrichment run ===")

    if not download_nal():
        log.error("NAL file unavailable — aborting")
        return

    nal_idx = load_nal_index(NAL_LOCAL_PATH)

    if not os.path.exists(OUTPUT_PATH):
        log.error("No output.json found at %s", OUTPUT_PATH)
        return

    with open(OUTPUT_PATH, encoding="utf-8") as f:
        data = json.load(f)

    leads = data.get("leads", [])
    log.info("Loaded %d leads from output.json", len(leads))

    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NONE": 0}
    for lead in leads:
        result = match_lead(lead, nal_idx)
        if result:
            lead["match_confidence"]  = result["match_confidence"]
            lead["match_score"]       = result["match_score"]
            lead["match_reason"]      = result["match_reason"]
            lead["property_address"]  = result["property_address"]
            lead["mailing_address"]   = result["mailing_address"]
            lead["owner_name"]        = result["owner_name"]
            lead["assessed_value"]    = result["assessed_value"]
            lead["needs_enrichment"]  = result["match_confidence"] in ("LOW","NONE")
        else:
            lead["match_confidence"] = "NONE"
            lead["match_score"]      = 0
            lead["match_reason"]     = "No candidates"
            lead["needs_enrichment"] = True
        lead["county_search_url"] = build_search_url(lead)
        counts[lead.get("match_confidence","NONE")] += 1

    log.info(
        "Results -> HIGH:%d  MEDIUM:%d  LOW:%d  NONE:%d",
        counts["HIGH"], counts["MEDIUM"], counts["LOW"], counts["NONE"]
    )

    data["generated_at"]  = datetime.utcnow().isoformat() + "Z"
    data["total_records"] = len(leads)
    data["leads"]         = leads

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info("Saved %d re-enriched leads to %s", len(leads), OUTPUT_PATH)

    # Also save CSV
    os.makedirs("data", exist_ok=True)
    fields = [
        "seller_score","document_number","file_date","document_type",
        "grantor","grantee","legal_description",
        "property_address","mailing_address","owner_name","assessed_value",
        "match_confidence","match_score","match_reason","county_search_url",
        "distress_flags","needs_enrichment","scraped_at"
    ]
    with open("data/output.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            if isinstance(lead.get("distress_flags"), list):
                lead["distress_flags"] = ", ".join(lead["distress_flags"])
            writer.writerow({k: lead.get(k,"") for k in fields})
    log.info("CSV saved.")

if __name__ == "__main__":
    main()
