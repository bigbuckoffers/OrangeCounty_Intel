"""
reenrich.py — Re-enrich existing leads with OCPA data

Reads output.json, finds leads missing owner/mailing/property address details,
calls OCPA Azure API to fill them in.

Stores split address fields for skiptrace-ready CSV export:
  prop_street, prop_city, prop_state, prop_zip
  mail_street, mail_city, mail_state, mail_zip
"""
import json, logging, os, re, time, requests, csv
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

OUTPUT_PATH = "data/output.json"
OUTPUT_CSV  = "data/output.csv"

OCPA_API_URL = "https://ocpa-mainsite-afd-standard.azurefd.net/api/PRC/GetPRCGeneralInfo"
OCPA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":     "application/json",
    "Referer":    "https://ocpaweb.ocpafl.org/",
    "Origin":     "https://ocpaweb.ocpafl.org",
}

STATE_MAP = {
    'ALABAMA': 'AL', 'ALASKA': 'AK', 'ARIZONA': 'AZ', 'ARKANSAS': 'AR',
    'CALIFORNIA': 'CA', 'COLORADO': 'CO', 'CONNECTICUT': 'CT', 'DELAWARE': 'DE',
    'FLORIDA': 'FL', 'GEORGIA': 'GA', 'HAWAII': 'HI', 'IDAHO': 'ID',
    'ILLINOIS': 'IL', 'INDIANA': 'IN', 'IOWA': 'IA', 'KANSAS': 'KS',
    'KENTUCKY': 'KY', 'LOUISIANA': 'LA', 'MAINE': 'ME', 'MARYLAND': 'MD',
    'MASSACHUSETTS': 'MA', 'MICHIGAN': 'MI', 'MINNESOTA': 'MN', 'MISSISSIPPI': 'MS',
    'MISSOURI': 'MO', 'MONTANA': 'MT', 'NEBRASKA': 'NE', 'NEVADA': 'NV',
    'NEW HAMPSHIRE': 'NH', 'NEW JERSEY': 'NJ', 'NEW MEXICO': 'NM', 'NEW YORK': 'NY',
    'NORTH CAROLINA': 'NC', 'NORTH DAKOTA': 'ND', 'OHIO': 'OH', 'OKLAHOMA': 'OK',
    'OREGON': 'OR', 'PENNSYLVANIA': 'PA', 'RHODE ISLAND': 'RI', 'SOUTH CAROLINA': 'SC',
    'SOUTH DAKOTA': 'SD', 'TENNESSEE': 'TN', 'TEXAS': 'TX', 'UTAH': 'UT',
    'VERMONT': 'VT', 'VIRGINIA': 'VA', 'WASHINGTON': 'WA', 'WEST VIRGINIA': 'WV',
    'WISCONSIN': 'WI', 'WYOMING': 'WY', 'DISTRICT OF COLUMBIA': 'DC'
}
SORTED_STATES = sorted(STATE_MAP.items(), key=lambda x: len(x[0]), reverse=True)
VALID_STATES  = set(STATE_MAP.values())

# OC cities for no-comma address parsing
OC_CITIES = [
    'WINTER GARDEN', 'WINTER PARK', 'ALTAMONTE SPRINGS', 'BELLE ISLE',
    'WINDERMERE', 'ORLANDO', 'APOPKA', 'OCOEE', 'MAITLAND', 'EDGEWOOD',
    'UNINCORPORATED', 'ORANGE COUNTY', 'OAKLAND', 'CHRISTMAS', 'GOTHA',
    'EATONVILLE', 'GOLDENROD', 'PINE HILLS', 'CONWAY', 'AZALEA PARK',
    'MOUNT DORA', 'MOUNT PLYMOUTH',
]
OC_CITIES_SORTED = sorted(OC_CITIES, key=len, reverse=True)


# ── ADDRESS NORMALISATION ─────────────────────────────────────────────────

def _replace_state_names(addr):
    """Replace full state names with abbreviations, but only after the last comma
    (to avoid abbreviating street names like 'S PENNSYLVANIA AVE')."""
    last_comma = addr.rfind(',')
    if last_comma == -1:
        # No comma — only replace if state appears immediately before a zip
        for full, abbr in SORTED_STATES:
            m = re.search(r'\b(' + re.escape(full) + r')(?=\s+\d{5}\b)', addr)
            if m:
                addr = addr[:m.start()] + abbr + addr[m.end():]
        return addr
    prefix = addr[:last_comma + 1]
    suffix = addr[last_comma + 1:]
    for full, abbr in SORTED_STATES:
        suffix = re.sub(r'\b' + re.escape(full) + r'\b', abbr, suffix)
    return prefix + suffix


def parse_address(addr, default_state='FL'):
    """
    Parse any address string into (street, city, state, zip).
    Handles:
      - Full state names  → abbreviations  (FLORIDA → FL)
      - zip+4 stripping   (32819-4833 → 32819)
      - No-comma formats  (544 N SEMORAN BLVD Orlando 32807)
      - Road suffixes     (RD, DR, ST …) NOT mistaken for state codes
      - Street numbers    (12628 MAIN ST) NOT mistaken for zip codes
    Returns dict with keys: street, city, state, zip
    """
    if not addr or str(addr).strip() in ('', '—'):
        return {'street': '', 'city': '', 'state': '', 'zip': ''}

    addr = str(addr).strip().upper()

    # Step 1 — replace full state names
    addr = _replace_state_names(addr)

    # Step 2 — remove zip+4
    addr = re.sub(r'(\d{5})-\d{4}', r'\1', addr)

    # Step 3 — extract zip (must be preceded by a non-digit so street numbers are safe)
    zip_match = re.search(r'(?<=\D)\s(\d{5})\b(?!.*\d{5})', addr)
    zip_code  = zip_match.group(1) if zip_match else ''
    if zip_code:
        addr = addr[:zip_match.start()].strip().rstrip(',').strip()

    # Step 4 — extract state (only real 2-letter state codes, not RD/DR/ST/CT/LN)
    state = ''
    state_match = re.search(r'[,\s]+([A-Z]{2})\s*$', addr)
    if state_match and state_match.group(1) in VALID_STATES:
        state = state_match.group(1)
        addr  = addr[:state_match.start()].strip().rstrip(',').strip()

    # Step 5 — split street and city
    parts = [p.strip() for p in addr.split(',')]
    if len(parts) >= 2:
        street = ', '.join(parts[:-1]).strip()
        city   = parts[-1].strip()
    else:
        # No comma — try to find a known OC city at end of string
        city   = ''
        street = addr.strip()
        for known_city in OC_CITIES_SORTED:
            m = re.search(r'\s+' + re.escape(known_city) + r'\s*$', addr)
            if m:
                city   = known_city
                street = addr[:m.start()].strip()
                break

    # Step 6 — default state to FL only when we have other location info
    if not state and (city or zip_code):
        state = default_state

    return {
        'street': street,
        'city':   city,
        'state':  state,
        'zip':    zip_code,
    }


def build_full_address(street, city, state, zip_code):
    """Reconstruct a clean 'street, city, state, zip' string."""
    parts = [p for p in [street, city, state] if p]
    result = ', '.join(parts)
    if zip_code:
        result = f"{result} {zip_code}" if result else zip_code
    return result.strip()


# ── PARCEL HELPERS ────────────────────────────────────────────────────────

def clean_parcel(pid):
    return re.sub(r'[-\s]', '', (pid or '')).strip()


# ── OCPA ENRICHMENT ───────────────────────────────────────────────────────

def enrich_from_ocpa(parcel_id):
    """
    Call OCPA Azure API. Returns a dict with ALL address fields including
    the full property (site) address — not just city/zip.

    FIX: Previous version fetched siteAddress but never stored it.
    Now returns prop_street, prop_city, prop_state, prop_zip AND
    rebuilds property_address from those components.
    """
    pid = clean_parcel(parcel_id)
    if not pid or not pid.isdigit():
        return {}
    try:
        resp = requests.get(
            OCPA_API_URL,
            params={"pid": pid},
            headers=OCPA_HEADERS,
            timeout=15,
        )
        if resp.status_code != 200:
            return {}
        d = resp.json()

        # ── Mailing address ───────────────────────────────────────────────
        mail_street = (d.get("mailAddress") or '').strip()
        mail_city   = (d.get("mailCity")    or '').strip()
        mail_state  = (d.get("mailState")   or 'FL').strip()
        mail_zip    = str(d.get("mailZip")  or '').strip()[:5]

        mailing_full = build_full_address(mail_street, mail_city, mail_state, mail_zip)

        # ── Property (site) address ───────────────────────────────────────
        # FIX: siteAddress was being fetched but never returned — now it is.
        prop_street = (d.get("siteAddress")    or
                       d.get("propertyAddress") or '').strip()
        prop_city   = (d.get("siteCity")       or
                       d.get("propCity")        or '').strip()
        prop_state  = (d.get("siteState")      or
                       d.get("propState")       or 'FL').strip()
        prop_zip    = str(d.get("siteZip")     or
                          d.get("propZip")      or '').strip()[:5]

        property_full = build_full_address(prop_street, prop_city, prop_state, prop_zip)

        # ── Owner ─────────────────────────────────────────────────────────
        owner = (d.get("ownerName") or '').strip()

        # ── Assessed value ────────────────────────────────────────────────
        av = ''
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
            # Full address strings
            "property_address": property_full,
            "mailing_address":  mailing_full,
            # Split property fields
            "prop_street":      prop_street,
            "prop_city":        prop_city,
            "prop_state":       prop_state if prop_state else 'FL',
            "prop_zip":         prop_zip,
            # Split mailing fields
            "mail_street":      mail_street,
            "mail_city":        mail_city,
            "mail_state":       mail_state if mail_state else 'FL',
            "mail_zip":         mail_zip,
            # Extra
            "assessed_value":   av,
            "property_type":    (d.get("dorDescription") or '').strip(),
        }

    except Exception as e:
        log.debug("OCPA enrich failed %s: %s", parcel_id, e)
        return {}


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    log.info("=== Re-enrichment Pass ===")

    if not os.path.exists(OUTPUT_PATH):
        log.error("No %s found", OUTPUT_PATH)
        return

    with open(OUTPUT_PATH, encoding='utf-8') as f:
        data = json.load(f)
    leads = data.get("leads", [])
    log.info("Loaded %d leads", len(leads))

    # Cap OCPA calls per run so the workflow never times out.
    # Each daily run processes the next batch of incomplete leads.
    # After ~7 runs all 14k leads will be fully enriched.
    MAX_OCPA_PER_RUN = 5000
    ocpa_enriched = 0
    addr_parsed   = 0

    for i, lead in enumerate(leads):
        if not isinstance(lead, dict):
            continue

        pid = clean_parcel(lead.get("parcel_id", ""))

        # Determine what's missing
        needs_owner      = not lead.get("owner_name")
        needs_mailing    = not lead.get("mailing_address")
        needs_prop_addr  = not lead.get("property_address") or lead.get("property_address") == "—"
        needs_prop_zip   = not lead.get("prop_zip")
        needs_prop_city  = not lead.get("prop_city")
        needs_any        = needs_owner or needs_mailing or needs_prop_addr or needs_prop_zip or needs_prop_city

        # ── OCPA enrichment ───────────────────────────────────────────────
        # Only call OCPA if something is actually missing AND we haven't
        # hit the per-run cap yet. Leads already enriched are skipped.
        if pid and pid.isdigit() and needs_any and ocpa_enriched < MAX_OCPA_PER_RUN:
            enriched = enrich_from_ocpa(pid)
            if enriched:
                # Owner
                if needs_owner and enriched.get("owner_name"):
                    lead["owner_name"] = enriched["owner_name"]

                # Full address strings — only fill if currently missing/blank
                if needs_prop_addr and enriched.get("property_address"):
                    lead["property_address"] = enriched["property_address"]
                if needs_mailing and enriched.get("mailing_address"):
                    lead["mailing_address"] = enriched["mailing_address"]

                # All split fields — fill any that are missing
                for field in [
                    "prop_street", "prop_city", "prop_state", "prop_zip",
                    "mail_street", "mail_city", "mail_state", "mail_zip",
                    "assessed_value", "property_type",
                ]:
                    if enriched.get(field) and not lead.get(field):
                        lead[field] = enriched[field]

                ocpa_enriched += 1
            time.sleep(0.2)

        # ── Parse mailing address into split fields if not already set ────
        mail_full = lead.get("mailing_address", "")
        if mail_full and not lead.get("mail_street"):
            parsed = parse_address(mail_full, default_state='')
            if parsed["street"]:
                lead["mail_street"] = parsed["street"]
                lead["mail_city"]   = parsed["city"]
                lead["mail_state"]  = parsed["state"]
                lead["mail_zip"]    = parsed["zip"]
                addr_parsed += 1

        # ── Parse property address into split fields if not already set ───
        prop_full = lead.get("property_address", "")
        if prop_full and prop_full != "—":
            # Only parse if we're still missing split fields
            if not lead.get("prop_street") or not lead.get("prop_city") or not lead.get("prop_zip"):
                parsed = parse_address(prop_full, default_state='FL')
                if parsed["street"] and not lead.get("prop_street"):
                    lead["prop_street"] = parsed["street"]
                if parsed["city"] and not lead.get("prop_city"):
                    lead["prop_city"]   = parsed["city"]
                if parsed["state"] and not lead.get("prop_state"):
                    lead["prop_state"]  = parsed["state"]
                if parsed["zip"] and not lead.get("prop_zip"):
                    lead["prop_zip"]    = parsed["zip"]

        # ── Ensure prop_state always defaults to FL for OC properties ─────
        if not lead.get("prop_state") and (lead.get("prop_street") or lead.get("prop_zip")):
            lead["prop_state"] = "FL"

        leads[i] = lead

        if (i + 1) % 1000 == 0:
            log.info("Processed %d / %d leads...", i + 1, len(leads))

    log.info("OCPA enriched: %d | Addresses parsed from string: %d", ocpa_enriched, addr_parsed)

    data["generated_at"]  = datetime.utcnow().isoformat() + "Z"
    data["total_records"] = len(leads)
    data["leads"]         = leads

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info("Saved -> %s", OUTPUT_PATH)

    # ── Write CSV ─────────────────────────────────────────────────────────
    csv_fields = [
        "seller_score", "motivation_count", "document_number", "file_date",
        "document_type", "stacked", "stacked_types",
        "grantor", "grantee", "legal_description",
        "property_address", "mailing_address",
        "owner_name", "assessed_value",
        "parcel_id", "match_confidence", "match_score", "match_reason",
        "county_search_url", "distress_flags", "needs_enrichment",
        "prop_street", "prop_city", "prop_state", "prop_zip",
        "mail_street", "mail_city", "mail_state", "mail_zip",
        "tax_years_delinquent", "tax_total_balance",
        "tax_years_list", "tax_cert_status",
        "code_violation_count", "code_violation_types",
        "scraped_at",
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
    log.info("Done.")


if __name__ == "__main__":
    main()
