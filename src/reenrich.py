"""
reenrich.py — Re-enrich existing leads with OCPA data

Reads output.json, finds leads missing owner/mailing/property address details,
calls OCPA Azure API to fill them in.

Now also stores:
  - prop_city, prop_state, prop_zip  (from OCPA siteCity/siteZip fields)
  - mail_street, mail_city, mail_state, mail_zip  (parsed from mailing_address)
  - prop_street  (cleaned street-only from property_address)

These split fields feed into the skiptrace-ready CSV export.
"""
import json, logging, os, re, time, requests
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

# ── ADDRESS PARSING ───────────────────────────────────────────────────────

def parse_full_address(addr):
    """
    Parse a full address string into (street, city, state, zip).
    Handles formats like:
      "123 MAIN ST, ORLANDO, FL 32801"
      "123 MAIN ST, ORLANDO FL 32801"
      "123 MAIN ST ORLANDO FL 32801"
    Returns dict with street/city/state/zip — empty string if not found.
    """
    if not addr or addr.strip() in ('—', ''):
        return {'street': '', 'city': '', 'state': '', 'zip': ''}

    addr = addr.strip()

    # Extract zip first (5-digit at end)
    zip_match = re.search(r'\b(\d{5})(-\d{4})?\s*$', addr)
    zipcode = zip_match.group(1) if zip_match else ''
    if zipcode:
        addr = addr[:zip_match.start()].strip().rstrip(',').strip()

    # Extract state (2-letter before zip or at end)
    state_match = re.search(r'\b([A-Z]{2})\s*$', addr.upper())
    state = state_match.group(1) if state_match else ''
    if state:
        addr = addr[:state_match.start()].strip().rstrip(',').strip()

    # Split street and city by last comma
    if ',' in addr:
        parts = [p.strip() for p in addr.rsplit(',', 1)]
        street = parts[0]
        city   = parts[1] if len(parts) > 1 else ''
    else:
        parts = re.split(r'\s{2,}', addr)
        if len(parts) >= 2:
            street = parts[0]
            city   = parts[-1]
        else:
            street = addr
            city   = ''

    return {
        'street': street.strip(),
        'city':   city.strip(),
        'state':  state.strip() or 'FL',
        'zip':    zipcode.strip()
    }


def clean_parcel(pid):
    return re.sub(r'[-\s]', '', (pid or '')).strip()


# ── OCPA ENRICHMENT ───────────────────────────────────────────────────────

def enrich_from_ocpa(parcel_id):
    """
    Call OCPA Azure API. Returns dict with all available fields including
    site address components if present.
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

        # Mailing address
        mail_addr  = (d.get("mailAddress")  or '').strip()
        mail_city  = (d.get("mailCity")     or '').strip()
        mail_state = (d.get("mailState")    or 'FL').strip()
        mail_zip   = (d.get("mailZip")      or '').strip()

        mailing_full = ''
        if mail_addr:
            mailing_full = f"{mail_addr}, {mail_city}, {mail_state} {mail_zip}".strip()

        # Site / property address — OCPA returns these if available
        site_addr  = (d.get("siteAddress")  or d.get("propertyAddress") or '').strip()
        site_city  = (d.get("siteCity")     or d.get("propCity")        or '').strip()
        site_state = (d.get("siteState")    or d.get("propState")       or 'FL').strip()
        site_zip   = (d.get("siteZip")      or d.get("propZip")         or '').strip()

        return {
            "owner_name":      (d.get("ownerName") or '').strip(),
            "mailing_address": mailing_full,
            "mail_street":     mail_addr,
            "mail_city":       mail_city,
            "mail_state":      mail_state,
            "mail_zip":        mail_zip,
            "prop_city":       site_city,
            "prop_state":      site_state if site_state else 'FL',
            "prop_zip":        site_zip,
            "property_type":   (d.get("dorDescription") or '').strip(),
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

    enriched_count = 0
    addr_parsed    = 0

    for i, lead in enumerate(leads):
        if not isinstance(lead, dict):
            continue

        pid = clean_parcel(lead.get("parcel_id", ""))
        needs_owner   = not lead.get("owner_name")
        needs_mailing = not lead.get("mailing_address")
        needs_propzip = not lead.get("prop_zip")

        # ── OCPA enrichment for missing data ──────────────────────────────
        if pid and pid.isdigit() and (needs_owner or needs_mailing or needs_propzip):
            enriched = enrich_from_ocpa(pid)
            if enriched:
                if needs_owner and enriched.get("owner_name"):
                    lead["owner_name"] = enriched["owner_name"]
                if needs_mailing and enriched.get("mailing_address"):
                    lead["mailing_address"] = enriched["mailing_address"]
                for field in ["mail_street","mail_city","mail_state","mail_zip",
                               "prop_city","prop_state","prop_zip"]:
                    if enriched.get(field) and not lead.get(field):
                        lead[field] = enriched[field]
                enriched_count += 1
            time.sleep(0.2)

        # ── Parse mailing address into split fields if not already done ───
        mail_full = lead.get("mailing_address", "")
        if mail_full and not lead.get("mail_street"):
            parsed = parse_full_address(mail_full)
            if parsed["street"]:
                lead["mail_street"] = parsed["street"]
                lead["mail_city"]   = parsed["city"]
                lead["mail_state"]  = parsed["state"] or "FL"
                lead["mail_zip"]    = parsed["zip"]
                addr_parsed += 1

        # ── Parse property address — street only, always FL ───────────────
        prop_full = lead.get("property_address", "")
        if prop_full and prop_full != "—":
            street_only = re.sub(r',.*$', '', prop_full).strip()
            lead["prop_street"] = street_only
            if not lead.get("prop_state"):
                lead["prop_state"] = "FL"

        leads[i] = lead

    log.info("OCPA enriched: %d | Mailing addresses parsed: %d", enriched_count, addr_parsed)

    data["generated_at"]  = datetime.utcnow().isoformat() + "Z"
    data["total_records"] = len(leads)
    data["leads"]         = leads

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info("Saved -> %s", OUTPUT_PATH)

    # Write CSV with all split address fields
    import csv
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


if __name__ == "__main__":
    main()
