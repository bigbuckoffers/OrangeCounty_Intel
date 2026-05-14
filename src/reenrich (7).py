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

# Expanded OC + surrounding cities list — sorted longest first to avoid partial matches
OC_CITIES = [
    # Multi-word first (longest first to avoid partial matches)
    'ALTAMONTE SPRINGS', 'BUENAVENTURA LAKES', 'DOCTOR PHILLIPS', 'HUNTERS CREEK',
    'WINTER GARDEN', 'WINTER PARK', 'MOUNT DORA', 'MOUNT PLYMOUTH',
    'BELLE ISLE', 'PINE HILLS', 'PINE CASTLE', 'OAK RIDGE', 'ST CLOUD',
    'LAKE MARY', 'AZALEA PARK', 'CONWAY', 'GOLDENROD', 'EATONVILLE',
    'ORANGE COUNTY', 'UNINCORPORATED',
    # Single word
    'WINDERMERE', 'CASSELBERRY', 'CELEBRATION', 'KISSIMMEE', 'LONGWOOD',
    'MAITLAND', 'MINNEOLA', 'GROVELAND', 'CLERMONT', 'REUNION',
    'TAVARES', 'LEESBURG', 'APOPKA', 'ORLANDO', 'SANFORD', 'OCOEE',
    'DAVENPORT', 'EDGEWOOD', 'GOLDENROD', 'CHRISTMAS', 'GOTHA', 'OAKLAND',
    # Nearby counties that show up in OC violations
    'DELTONA', 'DELAND', 'DEBARY', 'OSTEEN', 'ENTERPRISE',
]
OC_CITIES_SORTED = sorted(OC_CITIES, key=len, reverse=True)

# Unit/suite designators — strip these from the end of street when parsing
UNIT_PATTERN = re.compile(
    r'\s+(?:APT|APARTMENT|UNIT|STE|SUITE|#|BLDG|BUILDING|LOT|SPACE|SP|FL|FLOOR)'
    r'[\s#]*[\w\-]+\s*$',
    re.IGNORECASE
)


# ── ADDRESS NORMALISATION ─────────────────────────────────────────────────

def _replace_state_names(addr):
    """Replace full state names with abbreviations, but only after the last comma
    (to avoid abbreviating street names like 'S PENNSYLVANIA AVE')."""
    last_comma = addr.rfind(',')
    if last_comma == -1:
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


def _strip_unit(street):
    """Remove apartment/unit/suite suffixes from a street string."""
    return UNIT_PATTERN.sub('', street).strip().rstrip(',').strip()


def parse_address(addr, default_state='FL'):
    """
    Parse any address string into (street, city, state, zip).
    Handles:
      - Full state names  → abbreviations  (FLORIDA → FL)
      - zip+4 stripping   (32819-4833 → 32819)
      - No-comma formats  (544 N SEMORAN BLVD Orlando 32807)
      - Apartment/suite   (6755 TANGLEWOOD BAY DR APT 2415, ORLANDO FL 32821)
      - Truncated strings with ellipsis (...)
      - Road suffixes NOT mistaken for state codes
    Returns dict with keys: street, city, state, zip
    """
    if not addr or str(addr).strip() in ('', '—', 'nan'):
        return {'street': '', 'city': '', 'state': '', 'zip': ''}

    addr = str(addr).strip().upper()

    # Step 0 — clean up truncation artifacts (...) from code violation XLSX
    addr = re.sub(r'\s*\.\.\.\s*', ' ', addr).strip()

    # Step 1 — replace full state names
    addr = _replace_state_names(addr)

    # Step 2 — remove zip+4
    addr = re.sub(r'(\d{5})-\d{4}', r'\1', addr)

    # Step 3 — extract zip (must be preceded by a non-digit so street numbers are safe)
    zip_code = ''
    zip_match = re.search(r'(?<!\d)\b(\d{5})\b(?!.*\d{5})', addr)
    if zip_match:
        # Make sure it's not a street number (i.e. not at the very start)
        if zip_match.start() > 3:
            zip_code = zip_match.group(1)
            addr = addr[:zip_match.start()].strip().rstrip(',').strip()

    # Step 4 — extract state (only real 2-letter state codes)
    state = ''
    state_match = re.search(r'[,\s]+([A-Z]{2})\s*$', addr)
    if state_match and state_match.group(1) in VALID_STATES:
        state = state_match.group(1)
        addr  = addr[:state_match.start()].strip().rstrip(',').strip()

    # Step 5 — split street and city
    parts = [p.strip() for p in addr.split(',')]

    if len(parts) >= 2:
        # Has comma(s) — last part is city, everything before is street
        city   = parts[-1].strip()
        street = ', '.join(parts[:-1]).strip()

        # Edge case: if "city" looks like a unit number, merge back and try city detection
        if re.match(r'^(APT|UNIT|STE|SUITE|#|BLDG|LOT)\s*[\w\-]+$', city, re.IGNORECASE):
            street = addr.strip()
            city   = ''
            for known_city in OC_CITIES_SORTED:
                m = re.search(r'\b' + re.escape(known_city) + r'\b', street)
                if m:
                    city   = known_city
                    street = (street[:m.start()] + street[m.end():]).strip().rstrip(',').strip()
                    break
    else:
        # No comma — try known city detection at end of string
        city   = ''
        street = addr.strip()
        for known_city in OC_CITIES_SORTED:
            m = re.search(r'\s+' + re.escape(known_city) + r'\s*$', addr)
            if m:
                city   = known_city
                street = addr[:m.start()].strip()
                break

    # Step 6 — strip unit/apt suffix from street
    street = _strip_unit(street)

    # Reject numeric-only streets — these are unit numbers not street addresses
    if street and re.match(r'^\d+$', street.strip()):
        street = ''

    # Step 7 — default state to FL only when we have other location info
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
    the full property (site) address.
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
            "property_address": property_full,
            "mailing_address":  mailing_full,
            "prop_street":      prop_street,
            "prop_city":        prop_city,
            "prop_state":       prop_state if prop_state else 'FL',
            "prop_zip":         prop_zip,
            "mail_street":      mail_street,
            "mail_city":        mail_city,
            "mail_state":       mail_state if mail_state else 'FL',
            "mail_zip":         mail_zip,
            "assessed_value":   av,
            "property_type":    (d.get("dorDescription") or '').strip(),
        }

    except Exception as e:
        log.debug("OCPA enrich failed %s: %s", parcel_id, e)
        return {}


# ── MAIN ──────────────────────────────────────────────────────────────────

def clean_parcel_r(pid):
    return re.sub(r'[-\s]', '', (pid or '')).strip()

def is_valid_parcel_r(pid):
    c = clean_parcel_r(pid)
    return bool(c and c.isdigit() and len(c) >= 10)

def clean_prop_addr_r(addr):
    if not addr: return ''
    addr = str(addr).upper().strip().split(',')[0].strip()
    return re.sub(r'\s+', ' ', addr).strip()

def stack_by_property(leads):
    """
    Final dedup pass — one record per property.
    Groups by parcel ID first, property address second.
    Merges signals, flags, and doc numbers from duplicates into one record.
    """
    log.info("=== Property stacking pass on %d leads ===", len(leads))
    by_parcel = {}
    by_addr   = {}
    output    = []
    merged_count = 0

    for lead in leads:
        if not isinstance(lead, dict):
            output.append(lead)
            continue

        pid   = clean_parcel_r(lead.get('parcel_id', ''))
        addr  = clean_prop_addr_r(lead.get('property_address', ''))
        score = float(lead.get('seller_score', 0) or 0)

        matched_idx = None
        if is_valid_parcel_r(pid) and pid in by_parcel:
            matched_idx = by_parcel[pid]
        elif addr and len(addr) > 8 and addr in by_addr:
            matched_idx = by_addr[addr]

        if matched_idx is not None:
            existing       = output[matched_idx]
            existing_score = float(existing.get('seller_score', 0) or 0)

            # Primary = higher scored, secondary = lower scored
            if score > existing_score:
                primary, secondary = lead, existing
            else:
                primary, secondary = existing, lead

            # Merge flags
            p_flags = primary.get('distress_flags', [])
            s_flags = secondary.get('distress_flags', [])
            if isinstance(p_flags, str): p_flags = [f.strip() for f in p_flags.split(',') if f.strip()]
            if isinstance(s_flags, str): s_flags = [f.strip() for f in s_flags.split(',') if f.strip()]
            seen = set(p_flags)
            for flag in s_flags:
                if flag not in seen:
                    p_flags.append(flag)
                    seen.add(flag)
            primary['distress_flags'] = p_flags

            # Merge doc numbers
            p_docs = primary.get('stacked_docs', [])
            s_docs = secondary.get('stacked_docs', [])
            if isinstance(p_docs, str): p_docs = [p_docs] if p_docs else []
            if isinstance(s_docs, str): s_docs = [s_docs] if s_docs else []
            all_docs = list(dict.fromkeys(
                [primary.get('document_number',''), secondary.get('document_number','')] +
                p_docs + s_docs
            ))
            primary['stacked_docs'] = [d for d in all_docs if d]

            # Merge doc types
            p_types = primary.get('stacked_types', [])
            s_types = secondary.get('stacked_types', [])
            if isinstance(p_types, str): p_types = [p_types] if p_types else []
            if isinstance(s_types, str): s_types = [s_types] if s_types else []
            all_types = list(dict.fromkeys(
                [primary.get('document_type','')] + p_types +
                [secondary.get('document_type','')] + s_types
            ))
            all_types = [t for t in all_types if t]
            primary['stacked_types']  = all_types
            primary['document_type']  = ' + '.join(all_types)
            primary['stacked']        = True
            primary['motivation_count'] = primary.get('motivation_count',1) + secondary.get('motivation_count',1)

            # Fill missing fields from secondary
            for field in ['prop_street','prop_city','prop_state','prop_zip',
                          'mail_street','mail_city','mail_state','mail_zip',
                          'owner_name','mailing_address','assessed_value',
                          'parcel_id','county_search_url',
                          'code_violation_count','code_violation_types',
                          'tax_years_delinquent','tax_total_balance']:
                if not primary.get(field) and secondary.get(field):
                    primary[field] = secondary[field]

            output[matched_idx] = primary
            merged_count += 1
        else:
            new_idx = len(output)
            output.append(lead)
            if is_valid_parcel_r(pid):
                by_parcel[pid] = new_idx
            if addr and len(addr) > 8:
                by_addr[addr] = new_idx

    log.info("Property stacking: %d -> %d unique properties (%d merged)",
             len(leads), len(output), merged_count)
    return output


def main():
    log.info("=== Re-enrichment Pass ===")

    if not os.path.exists(OUTPUT_PATH):
        log.error("No %s found", OUTPUT_PATH)
        return

    with open(OUTPUT_PATH, encoding='utf-8') as f:
        data = json.load(f)
    leads = data.get("leads", [])
    log.info("Loaded %d leads", len(leads))

    MAX_OCPA_PER_RUN = 5000
    OCPA_TIME_BUDGET = 15 * 60  # stop OCPA calls after 15 minutes
    ocpa_start_time  = time.time()
    ocpa_enriched = 0
    addr_parsed   = 0

    for i, lead in enumerate(leads):
        if not isinstance(lead, dict):
            continue

        pid = clean_parcel(lead.get("parcel_id", ""))

        needs_owner   = not lead.get("owner_name")
        needs_mailing = not lead.get("mailing_address")
        # Only skip OCPA if the lead has a COMPLETE property address
        # All four fields must be present and non-empty
        # If ANY field is missing, call OCPA to get the full address
        has_complete_address = (
            bool(lead.get("prop_street", "").strip()) and
            bool(lead.get("prop_city", "").strip()) and
            bool(lead.get("prop_state", "").strip()) and
            bool(lead.get("prop_zip", "").strip()) and
            not re.match(r'^\d+$', (lead.get("prop_street","") or "").strip())
        )
        needs_ocpa = not has_complete_address

        # ── OCPA enrichment ───────────────────────────────────────────────
        ocpa_time_ok = (time.time() - ocpa_start_time) < OCPA_TIME_BUDGET
        if pid and pid.isdigit() and needs_ocpa and ocpa_enriched < MAX_OCPA_PER_RUN and ocpa_time_ok:
            enriched = enrich_from_ocpa(pid)
            if enriched:
                if needs_owner and enriched.get("owner_name"):
                    lead["owner_name"] = enriched["owner_name"]
                if not lead.get("property_address") and enriched.get("property_address"):
                    lead["property_address"] = enriched["property_address"]
                if needs_mailing and enriched.get("mailing_address"):
                    lead["mailing_address"] = enriched["mailing_address"]
                for field in [
                    "prop_street", "prop_city", "prop_state", "prop_zip",
                    "mail_street", "mail_city", "mail_state", "mail_zip",
                    "assessed_value", "property_type",
                ]:
                    if enriched.get(field) and not lead.get(field):
                        lead[field] = enriched[field]
                ocpa_enriched += 1
                time.sleep(0.1)  # only rate-limit on success

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
                addr_parsed += 1

        # ── Fix numeric-only prop_street (e.g. "6205" from CV unit number) ──
        # If prop_street is just a number, the address is bad — clear it and
        # let OCPA fill it in on next run using the parcel ID
        prop_street = lead.get("prop_street", "")
        if prop_street and re.match(r'^\d+$', prop_street.strip()):
            log.debug("Clearing numeric-only prop_street '%s' for %s", prop_street, lead.get("parcel_id",""))
            lead["prop_street"] = ""
            lead["prop_city"]   = ""
            lead["prop_zip"]    = ""
            # Also clear the full property_address if it's just the number
            if re.match(r'^\d+$', (lead.get("property_address","") or "").strip()):
                lead["property_address"] = ""

        # ── Ensure prop_state always defaults to FL for OC properties ─────
        if not lead.get("prop_state") and (lead.get("prop_street") or lead.get("prop_zip")):
            lead["prop_state"] = "FL"

        # ── Force re-parse if city or zip still missing ───────────────────
        # This catches code violation leads where the incident address
        # had truncation (...) or apartment suffixes that broke parsing
        if lead.get("prop_street") and (not lead.get("prop_city") or not lead.get("prop_zip")):
            # Try parsing the full property address again with the fixed parser
            prop_full = lead.get("property_address", "")
            if prop_full and prop_full != "—":
                parsed = parse_address(prop_full, default_state='FL')
                if parsed["city"] and not lead.get("prop_city"):
                    lead["prop_city"] = parsed["city"]
                if parsed["zip"] and not lead.get("prop_zip"):
                    lead["prop_zip"]  = parsed["zip"]
                if parsed["state"] and not lead.get("prop_state"):
                    lead["prop_state"] = parsed["state"]

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
