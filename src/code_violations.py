"""
code_violations.py — Orange County FL Open Code Violations

Reads data/code_violations.xlsx (committed to repo manually or via
a local script that downloads from netapps.ocfl.net and pushes).

XLSX columns:
  Incident ID | Parcel ID | Incident Address | Incident Type |
  Incident Status | Violation Recorded Date

For each property (grouped by Parcel ID):
  1. Enriches owner name + mailing address from OCPA Azure API
  2. Cross-references against output.json leads by Parcel ID or address
  3. If match found: stacks Code Violation signal, boosts score
  4. If no match: creates new lead
  5. Saves data/code_violations.csv, updates output.json + output.csv
"""
import csv, json, logging, os, re, time, requests, io
from collections import defaultdict
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── PATHS ─────────────────────────────────────────────────────────────────
XLSX_PATH      = "data/code_violations.xlsx"
OUTPUT_PATH    = "data/output.json"
OUTPUT_CSV     = "data/output.csv"
VIOLATIONS_CSV = "data/code_violations.csv"

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
MULTI_VIOLATION_BONUS = 8   # 2+ violations same property

# ── FILTERS ───────────────────────────────────────────────────────────────
SKIP_OWNERS = [
    'DISNEY', 'UNIVERSAL', 'SEAWORLD', 'MARRIOTT', 'HILTON',
    'CITY OF ORLANDO', 'ORANGE COUNTY', 'STATE OF FLORIDA',
    'SCHOOL BOARD', 'BOARD OF COUNTY', 'REEDY CREEK',
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


# ── OCPA ENRICHMENT ───────────────────────────────────────────────────────
def enrich_from_ocpa(parcel_id):
    """
    Uses the same OCPA Azure API that foreclosure.py uses.
    Returns {owner_name, mailing_address, property_type} or {}
    """
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
        owner      = (d.get("ownerName") or "").strip()
        mail_addr  = (d.get("mailAddress") or "").strip()
        mail_city  = (d.get("mailCity") or "").strip()
        mail_state = (d.get("mailState") or "FL").strip()
        mail_zip   = (d.get("mailZip") or "").strip()
        mailing    = ""
        if mail_addr:
            mailing = "{}, {}, {} {}".format(
                mail_addr, mail_city, mail_state, mail_zip).strip()
        return {
            "owner_name":      owner,
            "mailing_address": mailing,
            "property_type":   (d.get("dorDescription") or "").strip(),
        }
    except Exception as e:
        log.debug("OCPA enrich failed %s: %s", parcel_id, e)
        return {}


# ── PARSE XLSX ────────────────────────────────────────────────────────────
def parse_violations_xlsx(path):
    """
    Reads the XLSX and returns list of violation dicts.
    Header row is at index 4 (row 5 in Excel).
    Columns: Incident ID | Parcel ID | Incident Address |
             Incident Type | Incident Status | Violation Recorded Date
    """
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
        log.error("XLSX too short")
        return []

    # Find header row
    headers = None
    data_start = 0
    for i, row in enumerate(rows):
        vals = [str(v).strip() if v else '' for v in row]
        if 'Incident ID' in vals or 'Parcel ID' in vals:
            headers = vals
            data_start = i + 1
            break

    if not headers:
        log.error("Could not find header row in XLSX")
        return []

    log.info("XLSX headers: %s", headers)
    violations = []
    for row in rows[data_start:]:
        if not any(v is not None for v in row):
            continue
        rec = {headers[i]: (str(v).strip() if v is not None else '')
               for i, v in enumerate(row) if i < len(headers)}
        violations.append(rec)

    log.info("Parsed %d violation records", len(violations))
    return violations


# ── GROUP BY PROPERTY ─────────────────────────────────────────────────────
def group_by_property(violations):
    """Group violations by Parcel ID."""
    groups = defaultdict(list)
    for v in violations:
        parcel = clean_parcel(v.get("Parcel ID", ""))
        addr   = clean_address_key(v.get("Incident Address", ""))
        key    = parcel if parcel else addr
        if key:
            groups[key].append(v)
    log.info("Grouped into %d unique properties", len(groups))
    return groups


# ── SAVE RAW CSV ──────────────────────────────────────────────────────────
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
    types  = list(set(v.get("Incident Type", "") for v in viol_list if v.get("Incident Type")))

    owner   = enriched.get("owner_name", "")
    mailing = enriched.get("mailing_address", "")

    score = VIOLATION_BASE_SCORE
    if count >= 3: score += MULTI_VIOLATION_BONUS * 2
    elif count >= 2: score += MULTI_VIOLATION_BONUS

    doc_id = "CV-{}".format(re.sub(r'[^A-Z0-9]', '', group_key.upper())[:15])

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
        "property_address":     addr,
        "mailing_address":      mailing,
        "owner_name":           owner,
        "assessed_value":       "",
        "parcel_id":            parcel,
        "match_confidence":     "HIGH" if addr else "LOW",
        "match_score":          75 if addr else 20,
        "match_reason":         "Open code violation — {} active violation(s): {}".format(
                                    count, "; ".join(types[:3])),
        "county_search_url":    "https://www.ocpafl.org/searches/ParcelSearch.aspx"
                                "?SearchType=parcel&SearchValue={}".format(parcel) if parcel else "",
        "needs_enrichment":     not bool(addr),
        "code_violation_count": count,
        "code_violation_types": "; ".join(types),
        "scraped_at":           datetime.utcnow().isoformat() + "Z",
    }


# ── MAIN ──────────────────────────────────────────────────────────────────
def main():
    log.info("=== Code Violations Cross-Reference ===")

    if not os.path.exists(XLSX_PATH):
        log.warning("No %s found — skipping code violations", XLSX_PATH)
        log.warning("Download from https://netapps.ocfl.net/CETitleViolationSearch/ "
                    "and commit to data/code_violations.xlsx")
        return

    # Parse XLSX
    violations = parse_violations_xlsx(XLSX_PATH)
    if not violations:
        log.warning("No violations parsed — skipping")
        return

    # Group by property
    groups = group_by_property(violations)

    # Enrich with owner/mailing from OCPA API
    log.info("Enriching %d properties from OCPA API...", len(groups))
    enrichment_cache = {}
    enriched_violations = list(violations)  # for CSV save

    for group_key, viol_list in groups.items():
        parcel = clean_parcel(viol_list[0].get("Parcel ID", ""))
        if parcel and parcel not in enrichment_cache:
            enriched = enrich_from_ocpa(parcel)
            enrichment_cache[parcel] = enriched
            if enriched.get("owner_name"):
                log.debug("Enriched %s: %s", parcel, enriched["owner_name"][:30])
            time.sleep(0.3)

    # Add enrichment to raw violations for CSV
    for v in enriched_violations:
        parcel = clean_parcel(v.get("Parcel ID", ""))
        enriched = enrichment_cache.get(parcel, {})
        v["owner_name"]      = enriched.get("owner_name", "")
        v["mailing_address"] = enriched.get("mailing_address", "")

    save_violations_csv(enriched_violations)

    # Load existing leads
    if not os.path.exists(OUTPUT_PATH):
        log.error("No output.json — run scraper.py first")
        return

    with open(OUTPUT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    leads = list(data.get("leads", []))
    log.info("Loaded %d existing leads", len(leads))

    # Build indexes
    parcel_idx, addr_idx = {}, {}
    for i, lead in enumerate(leads):
        pid = clean_parcel(lead.get("parcel_id", ""))
        if is_valid_parcel(pid):
            parcel_idx[pid] = i
        addr = clean_address_key(lead.get("property_address", ""))
        if addr and len(addr) > 5:
            addr_idx[addr] = i

    log.info("Index: %d parcels | %d addresses", len(parcel_idx), len(addr_idx))

    # Cross-reference
    new_leads = []
    stacked = added = skipped = 0

    for group_key, viol_list in groups.items():
        first  = viol_list[0]
        count  = len(viol_list)
        parcel = first.get("Parcel ID", "")
        addr   = first.get("Incident Address", "")
        pid    = clean_parcel(parcel)

        enriched = enrichment_cache.get(pid, {})
        owner    = enriched.get("owner_name", "")

        if should_skip(owner):
            skipped += 1
            continue

        # Find existing lead
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
        if count >= 3: viol_score += MULTI_VIOLATION_BONUS * 2
        elif count >= 2: viol_score += MULTI_VIOLATION_BONUS

        if lead_idx is not None:
            lead = leads[lead_idx]

            # Check already stacked
            flags = lead.get("distress_flags", [])
            if isinstance(flags, str):
                flags = [f.strip() for f in flags.split(",") if f.strip()]
            if any("code_violation" in f.lower() for f in flags):
                lead["code_violation_count"] = count
                leads[lead_idx] = lead
                skipped += 1
                continue

            old_score  = lead.get("seller_score", 0)
            new_score  = min(old_score + viol_score + VIOLATION_STACK_BONUS, 99)
            flags.append("code_violation")

            stacked_types = lead.get("stacked_types", [])
            if isinstance(stacked_types, str):
                stacked_types = [stacked_types] if stacked_types else []
            if "Code Violation" not in stacked_types:
                stacked_types.append("Code Violation")

            stacked_docs = lead.get("stacked_docs", [])
            if isinstance(stacked_docs, str):
                stacked_docs = [stacked_docs] if stacked_docs else []
            doc_id = "CV-{}".format(re.sub(r'[^A-Z0-9]', '', group_key.upper())[:15])
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
            # Fill missing owner/mailing from OCPA
            if not lead.get("owner_name") and owner:
                lead["owner_name"] = owner
            if not lead.get("mailing_address") and enriched.get("mailing_address"):
                lead["mailing_address"] = enriched["mailing_address"]
            if not lead.get("parcel_id") and parcel:
                lead["parcel_id"] = parcel

            leads[lead_idx] = lead
            stacked += 1
            log.info("STACKED: %s | %d violation(s) | score %d->%d | owner=%s",
                     addr[:40], count, old_score, new_score, owner[:20])

        else:
            nl      = group_to_lead(group_key, viol_list, enriched)
            new_idx = len(leads) + len(new_leads)
            new_leads.append(nl)
            if is_valid_parcel(pid):
                parcel_idx[pid] = new_idx
            ak = clean_address_key(addr)
            if ak:
                addr_idx[ak] = new_idx
            added += 1
            log.info("NEW: %s | %d violation(s) | owner=%s",
                     addr[:40], count, owner[:20])

    leads.extend(new_leads)
    leads.sort(
        key=lambda l: l.get("seller_score", 0) if isinstance(l, dict) else 0,
        reverse=True
    )
    log.info("Done: %d stacked | %d new | %d skipped | %d total",
             stacked, added, skipped, len(leads))

    # Save JSON
    data.update({
        "generated_at":  datetime.utcnow().isoformat() + "Z",
        "total_records": len(leads),
        "leads":         leads,
    })
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info("Saved -> %s", OUTPUT_PATH)

    # Save CSV
    csv_fields = [
        "seller_score", "motivation_count", "document_number", "file_date",
        "document_type", "stacked", "stacked_types",
        "grantor", "grantee", "legal_description",
        "property_address", "mailing_address", "owner_name", "assessed_value",
        "parcel_id", "match_confidence", "match_score", "match_reason",
        "county_search_url", "distress_flags", "needs_enrichment",
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
