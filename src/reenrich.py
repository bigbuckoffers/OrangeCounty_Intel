"""
reenrich.py — Match leads against OCPA ArcGIS live parcel API
Replaces NAL file matching with direct OCPA queries by owner name.
Gives clean property addresses, assessed values, exemption codes,
and clickable public records URLs for every matched lead.
"""
import json, logging, os, csv, re, time, urllib.parse
from datetime import datetime
from rapidfuzz import fuzz

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

OUTPUT_PATH = "data/output.json"

# OCPA ArcGIS parcel layer — queryable by owner name
OCPA_QUERY_URL = (
    "https://vgispublic.ocpafl.org/server/rest/services"
    "/DYNAMIC/Dynamic_Parcels/MapServer/0/query"
)

# Public records URL template — clickable link to OCPA parcel page
OCPA_PARCEL_URL = "https://ocpaweb.ocpafl.org/parcelsearch/Parcel%20ID/{parcel_id}"

# Rate limit — be polite to the server
RATE_LIMIT = 0.5

DOC_TYPE_PRIMARY_NAME = {
    "lis pendens": "grantee", "lp": "grantee",
    "lien": "grantor",        "ln": "grantor",
    "judgment": "grantor",    "j":  "grantor",
    "probate": "both",        "prcp": "both",
    "domestic": "both",       "drd": "both",
    "tax deed": "grantee",    "td":  "grantee",
    "death":    "grantee",    "dc":  "grantee",
    "notice":   "grantor",    "noc": "grantor",
}

_LENDER_NOISE = re.compile(
    r'\b(JPMORGAN|JMORGAN|PMORGAN|CHASE|BANK\s+OF|WELLS\s+FARGO|'
    r'CITIBANK|COUNTRYWIDE|NATIONSTAR|OCWEN|SETERUS|PHH|'
    r'QUICKEN|ROCKET|PENNYMAC|FREEDOM|SERVICING|SERVICER|'
    r'FEDERAL\s+NATIONAL|FANNIE|FREDDIE|SECRETARY|HUD\b|'
    r'HOMEOWNERS\s+ASSOCIATION|HOA\b|COMMUNITY\s+ASSOCIATION)\b',
    re.IGNORECASE
)

# OCPA exemption codes -> human readable
EXEMPT_MAP = {
    "0":   "",
    "01":  "Homestead",
    "02":  "Homestead",
    "03":  "Homestead + Senior",
    "04":  "Homestead + Veteran",
    "05":  "Homestead + Disability",
    "06":  "Homestead + Widow",
    "07":  "Veteran",
    "08":  "Disability",
    "09":  "Senior",
    "10":  "Institutional",
    "11":  "Government",
    "12":  "Religious",
    "13":  "Charitable",
}

# DOR property use codes that indicate residential
RESIDENTIAL_DOR = {
    "0100", "0101", "0102", "0103", "0104", "0105", "0106", "0107", "0108",
    "0110", "0111", "0115", "0120", "0121",
}


def clean_name(raw):
    if not raw:
        return ""
    text = _LENDER_NOISE.sub('', raw.upper().strip())
    text = re.sub(r'[^A-Z\s,]', ' ', text)
    return ' '.join(text.split()).strip()


def extract_surname(name):
    """Extract first token (surname) from cleaned owner name."""
    name = clean_name(name)
    if not name:
        return ""
    # Try comma-separated: SMITH, JOHN -> SMITH
    if ',' in name:
        return name.split(',')[0].strip().split()[0]
    return name.split()[0]


def get_raw_owner(lead):
    dt = (lead.get("document_type") or "").lower()
    primary = "both"
    for key, val in DOC_TYPE_PRIMARY_NAME.items():
        if key in dt:
            primary = val
            break
    grantee = lead.get("grantee", "") or ""
    grantor = lead.get("grantor", "") or ""
    if primary == "grantee": return grantee or grantor
    if primary == "grantor": return grantor or grantee
    return grantee or grantor


def query_ocpa(surname, max_results=10):
    """Query OCPA ArcGIS by owner surname. Returns list of parcel records."""
    if not surname or len(surname) < 3:
        return []
    try:
        import requests
        where = f"NAME1 LIKE '{surname.upper()}%'"
        params = {
            "where": where,
            "outFields": "PARCEL,NAME1,NAME2,PROP_NAME,CITY_CODE,DOR_CODE,"
                         "EXEMPT_CODE,TOTAL_MKT,TOTAL_ASSD,MAIL_ADDR1,"
                         "MAIL_ADDR2,MAIL_CITY,MAIL_STATE,MAIL_ZIPCD,"
                         "SITE_ADDR,SITE_CITY,SITE_ZIP",
            "returnGeometry": "false",
            "resultRecordCount": max_results,
            "f": "json",
        }
        resp = requests.get(OCPA_QUERY_URL, params=params, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        features = data.get("features", [])
        return [f.get("attributes", {}) for f in features]
    except Exception as e:
        log.error("OCPA query failed for '%s': %s", surname, e)
        return []


def score_match(lead_name, parcel):
    """Score how well a parcel record matches the lead owner name."""
    parcel_name = (parcel.get("NAME1") or "") + " " + (parcel.get("NAME2") or "")
    parcel_name = parcel_name.strip()
    if not parcel_name:
        return 0
    score = fuzz.token_sort_ratio(clean_name(lead_name), clean_name(parcel_name))
    return score


def parcel_to_result(parcel, match_score, lead_name):
    """Convert OCPA parcel record to a result dict."""
    parcel_id = (parcel.get("PARCEL") or "").strip()

    # Property address
    site_addr = (parcel.get("SITE_ADDR") or "").strip()
    site_city = (parcel.get("SITE_CITY") or "").strip()
    site_zip  = str(parcel.get("SITE_ZIP") or "").strip()[:5]
    if site_addr and site_city:
        prop_addr = f"{site_addr}, {site_city}, FL {site_zip}".strip()
    else:
        prop_addr = ""

    # Mailing address
    mail1 = (parcel.get("MAIL_ADDR1") or "").strip()
    mail2 = (parcel.get("MAIL_ADDR2") or "").strip()
    mail_city  = (parcel.get("MAIL_CITY") or "").strip()
    mail_state = (parcel.get("MAIL_STATE") or "FL").strip()
    mail_zip   = str(parcel.get("MAIL_ZIPCD") or "").strip()[:5]
    mail_addr  = mail1
    if mail2: mail_addr += f" {mail2}"
    if mail_city: mail_addr += f", {mail_city}"
    if mail_state: mail_addr += f", {mail_state}"
    if mail_zip: mail_addr += f" {mail_zip}"
    mail_addr = mail_addr.strip() or prop_addr

    # Assessed value
    assessed = ""
    try:
        av = int(float(parcel.get("TOTAL_ASSD") or 0))
        if av > 0:
            assessed = f"${av:,}"
    except:
        pass

    # Exemption type
    exempt_code = str(parcel.get("EXEMPT_CODE") or "").strip().lstrip("0") or "0"
    exemption = EXEMPT_MAP.get(exempt_code, "")

    # Absentee owner flag
    absentee = ""
    if mail_addr and prop_addr and mail_city and site_city:
        if mail_city.upper() != site_city.upper():
            absentee = "Absentee owner"

    # Build motivation flags from exemptions + absentee
    flags = []
    if exemption:
        flags.append(exemption)
    if absentee:
        flags.append(absentee)

    # Clickable OCPA parcel URL
    parcel_url = OCPA_PARCEL_URL.format(parcel_id=urllib.parse.quote(parcel_id)) if parcel_id else ""

    # Confidence
    if match_score >= 90:
        confidence = "HIGH"
        mscore = 90
    elif match_score >= 75:
        confidence = "MEDIUM"
        mscore = 70
    else:
        confidence = "LOW"
        mscore = 40

    return {
        "property_address":  prop_addr,
        "mailing_address":   mail_addr,
        "owner_name":        (parcel.get("NAME1") or "").strip(),
        "assessed_value":    assessed,
        "match_confidence":  confidence,
        "match_score":       mscore,
        "match_reason":      f"OCPA ArcGIS name match score={match_score} | {clean_name(lead_name)[:40]}",
        "county_search_url": parcel_url,
        "exemption_type":    exemption,
        "absentee_owner":    bool(absentee),
        "parcel_id":         parcel_id,
        "motivation_flags":  flags,
        "needs_enrichment":  confidence in ("LOW",),
    }


def match_lead(lead):
    raw_owner = get_raw_owner(lead)
    surname = extract_surname(raw_owner)
    if not surname or len(surname) < 3:
        return None

    parcels = query_ocpa(surname)
    if not parcels:
        return None

    # Score each candidate
    scored = []
    for parcel in parcels:
        s = score_match(raw_owner, parcel)
        scored.append((s, parcel))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_parcel = scored[0]

    # Only accept if name match is good enough
    if best_score < 60:
        return None

    return parcel_to_result(best_parcel, best_score, raw_owner)


def build_county_search_url(lead):
    raw = get_raw_owner(lead)
    surname = extract_surname(raw)
    if surname:
        return (
            "https://www.ocpafl.org/searches/ParcelSearch.aspx"
            f"?SearchType=owner&SearchValue={urllib.parse.quote(surname)}"
        )
    return "https://www.ocpafl.org/searches/ParcelSearch.aspx"


def main():
    log.info("=== Re-enrichment run (OCPA ArcGIS) ===")

    if not os.path.exists(OUTPUT_PATH):
        log.error("No output.json found at %s", OUTPUT_PATH)
        return

    with open(OUTPUT_PATH, encoding="utf-8") as f:
        data = json.load(f)

    leads = data.get("leads", [])
    log.info("Loaded %d leads from output.json", len(leads))

    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NONE": 0}
    updated = 0

    for i, lead in enumerate(leads):
        result = match_lead(lead)
        if result:
            lead["property_address"]  = result["property_address"]
            lead["mailing_address"]   = result["mailing_address"]
            lead["owner_name"]        = result["owner_name"]
            lead["assessed_value"]    = result["assessed_value"]
            lead["match_confidence"]  = result["match_confidence"]
            lead["match_score"]       = result["match_score"]
            lead["match_reason"]      = result["match_reason"]
            lead["county_search_url"] = result["county_search_url"]
            lead["needs_enrichment"]  = result["needs_enrichment"]
            # Add new fields
            lead["exemption_type"]    = result.get("exemption_type", "")
            lead["absentee_owner"]    = result.get("absentee_owner", False)
            lead["parcel_id"]         = result.get("parcel_id", "")
            lead["motivation_flags"]  = result.get("motivation_flags", [])
            updated += 1
        else:
            lead["match_confidence"] = lead.get("match_confidence", "NONE")
            lead["match_score"]      = lead.get("match_score", 0)
            lead["match_reason"]     = lead.get("match_reason", "No OCPA match")
            lead["needs_enrichment"] = True
            if "county_search_url" not in lead or not lead["county_search_url"]:
                lead["county_search_url"] = build_county_search_url(lead)

        counts[lead.get("match_confidence", "NONE")] += 1
        time.sleep(RATE_LIMIT)

        if (i + 1) % 100 == 0:
            log.info("Progress: %d/%d | HIGH:%d MEDIUM:%d LOW:%d NONE:%d",
                     i+1, len(leads),
                     counts["HIGH"], counts["MEDIUM"], counts["LOW"], counts["NONE"])

    log.info(
        "Results -> HIGH:%d  MEDIUM:%d  LOW:%d  NONE:%d | Updated: %d",
        counts["HIGH"], counts["MEDIUM"], counts["LOW"], counts["NONE"], updated
    )

    data["generated_at"]  = datetime.utcnow().isoformat() + "Z"
    data["total_records"] = len(leads)
    data["leads"]         = leads

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info("Saved %d re-enriched leads to %s", len(leads), OUTPUT_PATH)

    # Save CSV
    os.makedirs("data", exist_ok=True)
    fields = [
        "seller_score", "motivation_count", "document_number", "file_date",
        "document_type", "stacked", "stacked_types",
        "grantor", "grantee", "legal_description",
        "property_address", "mailing_address", "owner_name", "assessed_value",
        "exemption_type", "absentee_owner", "parcel_id",
        "match_confidence", "match_score", "match_reason", "county_search_url",
        "distress_flags", "motivation_flags", "needs_enrichment", "scraped_at"
    ]
    with open("data/output.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            row = dict(lead)
            for fld in ("distress_flags", "stacked_types", "motivation_flags"):
                if isinstance(row.get(fld), list):
                    row[fld] = ", ".join(str(x) for x in row[fld])
            writer.writerow({k: row.get(k, "") for k in fields})
    log.info("CSV saved.")


if __name__ == "__main__":
    main()
