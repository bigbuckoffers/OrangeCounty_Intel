"""
code_violations.py — Orange County FL Open Code Violations

Reads data/code_violations.xlsx (downloaded weekly from
https://netapps.ocfl.net/CETitleViolationSearch/ and committed to repo).

XLSX columns (header at row 5):
  Incident ID | Parcel ID | Incident Address | Incident Type |
  Incident Status | Violation Recorded Date

For each property (grouped by Parcel ID):
  1. Enriches owner name + mailing address from OCPA Azure API
     (same API used by foreclosure.py — confirmed working)
  2. Cross-references against output.json leads by Parcel ID or address
  3. If match found: stacks Code Violation signal, boosts score
  4. If no match: creates new lead
  5. Saves data/code_violations.csv, updates output.json + output.csv

Run after tax_delinquent.py in GitHub Actions.
"""
import csv, json, logging, os, re, time, requests
from collections import defaultdict
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── PATHS ─────────────────────────────────────────────────────────────────
XLSX_PATH      = "data/code_violations.xlsx"
OUTPUT_PATH    = "data/output.json"
OUTPUT_CSV     = "data/output.csv"
VIOLATIONS_CSV = "data/code_violations.csv"

# OCPA Azure API
OCPA_API_URL = "https://ocpa-mainsite-afd-standard.azurefd.net/api/PRC/GetPRCGeneralInfo"
OCPA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":     "application/json",
    "Referer":    "https://ocpaweb.ocpafl.org/",
    "Origin":     "https://ocpaweb.ocpafl.org",
}

# ── SCORING ───────────────────────────────────────────────────────────────
VIOLATION_BASE_SCORE  = 18
VIOLATION_STACK_BONUS = 12
MULTI_VIOLATION_BONUS = 8

# ── FILTERS ───────────────────────────────────────────────────────────────
SKIP_OWNERS = [
    'DISNEY', 'UNIVERSAL', 'SEAWORLD', 'MARRIOTT', 'HILTON',
    'CITY OF ORLANDO', 'ORANGE COUNTY', 'STATE OF FLORIDA',
    'SCHOOL BOARD', 'BOARD OF COUNTY', 'REEDY CREEK',
    'WYNDHAM', 'HYATT', 'SHERATON', 'RITZ', 'WESTIN',
    'SIMON PROPERTY', 'MALL AT MILLENNIA', 'PREMIUM OUTLETS',
]


def clean_parcel(pid):
    return re.sub(r'[-\s]', '', (pid or "")).strip()

def is_valid_parcel(pid):
    c = clean_parcel(pid)
    return bool(c and len(c) >= 10)

def clean_address_key(addr):
    if not addr: return ""
    addr = addr.upper().strip()
    addr = re.sub(r',.*', '', addr)
    return re.sub(r'\s+', ' ', addr).strip()

def should_skip(owner):
    up = (owner or "").upper()
    return any(s in up for s in SKIP_OWNERS)

def build_full_address(street, city, state, zip_code):
    parts = [p for p in [street, city, state] if p]
    result = ', '.join(parts)
    if zip_code:
        result = f"{result} {zip_code}" if result else zip_code
    return result.strip()


# ── OCPA ENRICHMENT ───────────────────────────────────────────────────────
# FIX: Now returns full property address fields (street, city, state, zip)
# not just owner and mailing. This is the main fix for missing city/zip on CV leads.
def enrich_from_ocpa(parcel_id):
    pid = clean_parcel(parcel_id)
    if not pid or not pid.isdigit():
        return {}
    try:
        resp = requests.get(
            OCPA_API_URL,
            params={"pid": pid},
            headers=OCPA_HEADERS,
            timeout=15
        )
        if resp.status_code != 200:
            return {}
        d = resp.json()

        # ── Owner ────────────────────────────────────────────────────────
        owner = (d.get("ownerName") or "").strip()

        # ── Mailing address ───────────────────────────────────────────────
        mail_street = (d.get("mailAddress") or "").strip()
        mail_city   = (d.get("mailCity")    or "").strip()
        mail_state  = (d.get("mailState")   or "FL").strip()
        mail_zip    = str(d.get("mailZip")  or "").strip()[:5]
        mailing_full = build_full_address(mail_street, mail_city, mail_state, mail_zip)

        # ── Property (site) address ───────────────────────────────────────
        # FIX: fetch the full property address from OCPA so CV leads
        # get city and zip, not just the raw incident address from the XLSX
        prop_street = (d.get("siteAddress")    or
                       d.get("propertyAddress") or "").strip()
        prop_city   = (d.get("siteCity")       or
                       d.get("propCity")        or "").strip()
        prop_state  = (d.get("siteState")      or
                       d.get("propState")       or "FL").strip()
        prop_zip    = str(d.get("siteZip")     or
                          d.get("propZip")      or "").strip()[:5]
        property_full = build_full_address(prop_street, prop_city, prop_state, prop_zip)

        # ── Assessed value ────────────────────────────────────────────────
        av = ""
        for key in ("justValue", "assessedValue", "totalValue", "av_nsd"):
            val = d.get(key)
            if val:
                try:
                    av = f"${int(float(str(val).replace(',', ''))):,}"
                    break
                except (ValueError, TypeError):
                    pass

        return {
            "owner_name":       owner,
            "mailing_address":  mailing_full,
            "property_type":    (d.get("dorDescription") or "").strip(),
            # Full property address string
            "property_address": property_full,
            # Split property fields
            "prop_street":      prop_street,
            "prop_city":        prop_city,
            "prop_state":       prop_state if prop_state else "FL",
            "prop_zip":         prop_zip,
            # Split mailing fields
            "mail_street":      mail_street,
            "mail_city":        mail_city,
            "mail_state":       mail_state if mail_state else "FL",
            "mail_zip":         mail_zip,
            # Assessed value
            "assessed_value":   av,
        }
    except Exception as e:
        log.debug("OCPA enrich failed %s: %s", parcel_id, e)
        return {}


# ── PARSE XLSX ────────────────────────────────────────────────────────────
def parse_violations_xlsx(path):
    try:
        import openpyxl
    except ImportError:
        log.error("openpyxl not installed — run: pip install openpyxl")
        return []
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as e:
        log.error("Failed to open XLSX: %s", e)
        return []

    if len(rows) < 5:
        log.error("XLSX too short — expected header at row 5")
        return []

    headers = None
    data_start = 0
    for i, row in enumerate(rows):
        vals = [str(v).strip() if v else '' for v in row]
        if 'Incident ID' in vals or 'Parcel ID' in vals:
            headers = vals
            data_start = i + 1
            log.info("Found header at row %d: %s", i + 1, headers)
            break

    if not headers:
        log.error("Could not find header row in XLSX")
        return []

    violations = []
    for row in rows[data_start:]:
        if not any(v is not None for v in row):
            continue
        rec = {headers[i]: (str(v).strip() if v is not None else '')
               for i, v in enumerate(row) if i < len(headers)}
        violations.append(rec)

    log.info("Parsed %d violation records from %s", len(violations), path)
    return violations


# ── GROUP BY PROPERTY ─────────────────────────────────────────────────────
def group_by_property(violations):
    groups = defaultdict(list)
    for v in violations:
        parcel = clean_parcel(v.get("Parcel ID", ""))
        addr   = clean_address_key(v.get("Incident Address", ""))
        key    = parcel if is_valid_parcel(parcel) else addr
        if key:
            groups[key].append(v)
    log.info("Grouped %d violations into %d unique properties",
             len(violations), len(groups))
    return groups


# ── SAVE RAW VIOLATIONS CSV ───────────────────────────────────────────────
def save_violations_csv(violations):
    if not violations:
        return
    os.makedirs("data", exist_ok=True)
    fields = ["Incident ID", "Parcel ID", "Incident Address",
              "Incident Type", "Incident Status", "Violation Recorded Date",
              "owner_name", "mailing_address"]
    with open(VIOLATIONS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(violations)
    log.info("Saved %d raw violations -> %s", len(violations), VIOLATIONS_CSV)


# ── BUILD NEW LEAD ────────────────────────────────────────────────────────
def group_to_lead(group_key, viol_list, enriched):
    first  = viol_list[0]
    count  = len(viol_list)
    parcel = first.get("Parcel ID", "")
    addr   = first.get("Incident Address", "")
    types  = list(set(
        v.get("Incident Type", "") for v in viol_list
        if v.get("Incident Type")
    ))
    owner   = enriched.get("owner_name", "")
    mailing = enriched.get("mailing_address", "")

    # FIX: Use OCPA property address if available, fall back to incident address
    prop_address = enriched.get("property_address", "") or addr
    prop_street  = enriched.get("prop_street", "")
    prop_city    = enriched.get("prop_city", "")
    prop_state   = enriched.get("prop_state", "FL")
    prop_zip     = enriched.get("prop_zip", "")
    mail_street  = enriched.get("mail_street", "")
    mail_city    = enriched.get("mail_city", "")
    mail_state   = enriched.get("mail_state", "FL")
    mail_zip     = enriched.get("mail_zip", "")
    assessed     = enriched.get("assessed_value", "")

    score = VIOLATION_BASE_SCORE
    if count >= 3:
        score += MULTI_VIOLATION_BONUS * 2
    elif count >= 2:
        score += MULTI_VIOLATION_BONUS

    safe_key  = re.sub(r'[^A-Z0-9]', '', group_key.upper())[:15]
    doc_id    = "CV-{}".format(safe_key)
    types_str = "; ".join(types[:5])

    return {
        "document_number":      doc_id,
        "file_date":            datetime.utcnow().strftime("%Y-%m-%d"),
        "grantor":              "",
        "grantee":              owner,
        "legal_description":    "",
        "document_type":        "Code Violation",
        "seller_score":         min(score, 99),
        "distress_flags":       ["code_violation"],
        "stacked":              False,
        "stacked_docs":         [doc_id],
        "stacked_types":        ["Code Violation"],
        "motivation_count":     1,
        "property_address":     prop_address,
        "mailing_address":      mailing,
        "owner_name":           owner,
        "assessed_value":       assessed,
        "parcel_id":            parcel,
        # Split property address fields — populated from OCPA
        "prop_street":          prop_street,
        "prop_city":            prop_city,
        "prop_state":           prop_state,
        "prop_zip":             prop_zip,
        # Split mailing fields
        "mail_street":          mail_street,
        "mail_city":            mail_city,
        "mail_state":           mail_state,
        "mail_zip":             mail_zip,
        "match_confidence":     "HIGH" if prop_city else ("MEDIUM" if addr else "LOW"),
        "match_score":          75 if prop_city else (50 if addr else 20),
        "match_reason":         "Open code violation — {} active violation(s): {}".format(
                                    count, types_str),
        "county_search_url":    (
            "https://www.ocpafl.org/searches/ParcelSearch.aspx"
            "?SearchType=parcel&SearchValue={}".format(parcel)
        ) if parcel else "",
        "needs_enrichment":     not bool(prop_city),
        "code_violation_count": count,
        "code_violation_types": types_str,
        "scraped_at":           datetime.utcnow().isoformat() + "Z",
    }


# ── MAIN ──────────────────────────────────────────────────────────────────
def main():
    log.info("=== Code Violations Cross-Reference ===")

    if not os.path.exists(XLSX_PATH):
        log.warning("No %s found — skipping code violations step", XLSX_PATH)
        log.warning("To add violations data:")
        log.warning("  1. Go to https://netapps.ocfl.net/CETitleViolationSearch/")
        log.warning("  2. Click the 'here' link to download the XLSX")
        log.warning("  3. Rename it code_violations.xlsx")
        log.warning("  4. Upload to data/ folder in your GitHub repo and commit")
        return

    violations = parse_violations_xlsx(XLSX_PATH)
    if not violations:
        log.warning("No violations parsed — skipping")
        return

    groups = group_by_property(violations)

    enrichment_cache = {}
    save_violations_csv(list(violations))

    if not os.path.exists(OUTPUT_PATH):
        log.error("No output.json found — run scraper.py first")
        return

    with open(OUTPUT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    leads = list(data.get("leads", []))
    log.info("Loaded %d existing leads", len(leads))

    parcel_idx = {}
    addr_idx   = {}
    for i, lead in enumerate(leads):
        pid = clean_parcel(lead.get("parcel_id", ""))
        if is_valid_parcel(pid):
            parcel_idx[pid] = i
        addr = clean_address_key(lead.get("property_address", ""))
        if addr and len(addr) > 5:
            addr_idx[addr] = i

    log.info("Index: %d parcels | %d addresses", len(parcel_idx), len(addr_idx))

    new_leads = []
    stacked = added = skipped = 0

    for group_key, viol_list in groups.items():
        first  = viol_list[0]
        count  = len(viol_list)
        parcel = first.get("Parcel ID", "")
        addr   = first.get("Incident Address", "")
        pid    = clean_parcel(parcel)

        # Always enrich from OCPA for every property — this is the key fix
        # Previously only enriched for new leads, causing stacked leads to miss city/zip
        if pid and is_valid_parcel(pid) and pid not in enrichment_cache:
            enrichment_cache[pid] = enrich_from_ocpa(pid)
            time.sleep(0.25)
        enriched = enrichment_cache.get(pid, {})
        owner    = enriched.get("owner_name", "")

        lead_idx = None
        if is_valid_parcel(pid) and pid in parcel_idx:
            lead_idx = parcel_idx[pid]
        if lead_idx is None:
            ak = clean_address_key(addr)
            if ak and ak in addr_idx:
                lead_idx = addr_idx[ak]
        if lead_idx is not None and lead_idx >= len(leads):
            lead_idx = None

        viol_score = VIOLATION_BASE_SCORE
        if count >= 3:
            viol_score += MULTI_VIOLATION_BONUS * 2
        elif count >= 2:
            viol_score += MULTI_VIOLATION_BONUS

        if lead_idx is not None:
            lead = leads[lead_idx]

            flags = lead.get("distress_flags", [])
            if isinstance(flags, str):
                flags = [f.strip() for f in flags.split(",") if f.strip()]
            if any("code_violation" in f.lower() for f in flags):
                lead["code_violation_count"] = count
                leads[lead_idx] = lead
                skipped += 1
                continue

            old_score = lead.get("seller_score", 0)
            new_score = min(old_score + viol_score + VIOLATION_STACK_BONUS, 99)
            flags.append("code_violation")

            stacked_types = lead.get("stacked_types", [])
            if isinstance(stacked_types, str):
                stacked_types = [stacked_types] if stacked_types else []
            if "Code Violation" not in stacked_types:
                stacked_types.append("Code Violation")

            stacked_docs = lead.get("stacked_docs", [])
            if isinstance(stacked_docs, str):
                stacked_docs = [stacked_docs] if stacked_docs else []
            safe_key = re.sub(r'[^A-Z0-9]', '', group_key.upper())[:15]
            doc_id   = "CV-{}".format(safe_key)
            if doc_id not in stacked_docs:
                stacked_docs.append(doc_id)

            lead.update({
                "seller_score":         new_score,
                "stacked":              True,
                "distress_flags":       flags,
                "stacked_types":        stacked_types,
                "stacked_docs":         stacked_docs,
                "motivation_count":     lead.get("motivation_count", 1) + 1,
                "document_type":        " + ".join(stacked_types),
                "code_violation_count": count,
            })
            # FIX: Fill in missing address fields from OCPA on stacked leads too
            if not lead.get("owner_name") and owner:
                lead["owner_name"] = owner
            if not lead.get("mailing_address") and enriched.get("mailing_address"):
                lead["mailing_address"] = enriched["mailing_address"]
            if not lead.get("parcel_id") and parcel:
                lead["parcel_id"] = parcel
            # Fill missing property address fields from OCPA
            for field in ["prop_street", "prop_city", "prop_state", "prop_zip",
                          "mail_street", "mail_city", "mail_state", "mail_zip",
                          "assessed_value"]:
                if enriched.get(field) and not lead.get(field):
                    lead[field] = enriched[field]
            # If property address is just a raw incident address (no city), upgrade it
            if enriched.get("property_address") and not lead.get("prop_city"):
                lead["property_address"] = enriched["property_address"]
                lead["prop_street"]      = enriched.get("prop_street", "")
                lead["prop_city"]        = enriched.get("prop_city", "")
                lead["prop_state"]       = enriched.get("prop_state", "FL")
                lead["prop_zip"]         = enriched.get("prop_zip", "")

            leads[lead_idx] = lead
            stacked += 1
            log.info("STACKED: %s | %d violation(s) | score %d->%d | owner=%s",
                     addr[:40], count, old_score, new_score, owner[:20])

        else:
            enriched = enrichment_cache.get(pid, {})
            if should_skip(enriched.get("owner_name", "")):
                skipped += 1
                continue
            nl      = group_to_lead(group_key, viol_list, enriched)
            new_idx = len(leads) + len(new_leads)
            new_leads.append(nl)
            if is_valid_parcel(pid):
                parcel_idx[pid] = new_idx
            ak = clean_address_key(addr)
            if ak:
                addr_idx[ak] = new_idx
            added += 1
            log.info("NEW: %s | city=%s | zip=%s | owner=%s",
                     addr[:40], nl.get("prop_city",""), nl.get("prop_zip",""), owner[:20])

    leads.extend(new_leads)
    leads.sort(
        key=lambda l: l.get("seller_score", 0) if isinstance(l, dict) else 0,
        reverse=True
    )
    log.info("Done: %d stacked | %d new | %d skipped | %d total",
             stacked, added, skipped, len(leads))

    data.update({
        "generated_at":  datetime.utcnow().isoformat() + "Z",
        "total_records": len(leads),
        "leads":         leads,
    })
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)
    log.info("Saved -> %s", OUTPUT_PATH)

    csv_fields = [
        "seller_score", "motivation_count", "document_number", "file_date",
        "document_type", "stacked", "stacked_types",
        "grantor", "grantee", "legal_description",
        "property_address", "mailing_address", "owner_name", "assessed_value",
        "parcel_id", "match_confidence", "match_score", "match_reason",
        "county_search_url", "distress_flags", "needs_enrichment",
        "prop_street", "prop_city", "prop_state", "prop_zip",
        "mail_street", "mail_city", "mail_state", "mail_zip",
        "code_violation_count", "code_violation_types", "scraped_at"
    ]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            row = dict(lead) if isinstance(lead, dict) else {}
            if isinstance(row.get("distress_flags"), list):
                row["distress_flags"] = ", ".join(row["distress_flags"])
            if isinstance(row.get("stacked_types"), list):
                row["stacked_types"] = " + ".join(row["stacked_types"])
            writer.writerow(row)
    log.info("CSV saved -> %s", OUTPUT_CSV)


if __name__ == "__main__":
    main()
