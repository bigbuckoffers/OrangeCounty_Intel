"""
tax_delinquent.py — Orange County FL Delinquent Tax Cross-Reference

Loads the Orange County delinquent tax roll CSV and:
  1. Groups records by parcel ID (Account Number) to count years delinquent
  2. Cross-references against ALL existing leads in output.json by:
       - Parcel ID (most reliable)
       - Property address (fallback)
       - Owner name fuzzy match (last resort)
  3. If match found: stacks Delinquent Taxes signal, boosts score
  4. If no match: creates new lead from tax record (early-stage motivated seller)
  5. Filters out commercial/government/resort properties
  6. Saves updated output.json and output.csv

Run this after scraper.py and merger.py.
"""
import csv, json, logging, os, re
from collections import defaultdict
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── PATHS ──────────────────────────────────────────────────────────────────
TAX_CSV_PATH = "data/delinquent_taxes.csv"
OUTPUT_PATH  = "data/output.json"
OUTPUT_CSV   = "data/output.csv"

# ── SCORING ────────────────────────────────────────────────────────────────
TAX_BASE_SCORE     = 20
TAX_YEARS_2_BONUS  = 8
TAX_YEARS_3_BONUS  = 15
TAX_YEARS_5_BONUS  = 22
TAX_YEARS_10_BONUS = 30
TAX_STACK_BONUS    = 15

# ── FILTERS ────────────────────────────────────────────────────────────────
SKIP_OWNERS = [
    'DISNEY', 'WALT DISNEY', 'UNIVERSAL', 'SEAWORLD', 'MARRIOTT', 'HILTON',
    'SHERATON', 'WYNDHAM', 'HYATT', 'STARWOOD', 'RITZ', 'WESTIN',
    'CITY OF ORLANDO', 'ORANGE COUNTY', 'STATE OF FLORIDA', 'FLORIDA DOT',
    'GOAA', 'ORLANDO AVIATION', 'JETBLUE', 'SOUTHWEST AIRLINES',
    'SCHOOL BOARD', 'BOARD OF COUNTY', 'REEDY CREEK',
    'TISHMAN', 'EPCOT', 'BAY LAKE', 'MAGIC KINGDOM',
    'SIMON PROPERTY', 'MALL AT MILLENNIA', 'PREMIUM OUTLETS',
]

SKIP_ADDRESSES = [
    'BAY LAKE', 'LAKE BUENA VISTA', 'EPCOT', 'DREAM TREE',
    'MAGIC KINGDOM', 'ANIMAL KINGDOM',
]

MIN_BALANCE  = 500.0
MAX_ASSESSED = 5_000_000


def clean_parcel(pid):
    return re.sub(r'[-\s]', '', (pid or "")).strip()


def is_valid_parcel(pid):
    cleaned = clean_parcel(pid)
    return bool(cleaned and len(cleaned) >= 10)


def clean_address_key(addr):
    if not addr:
        return ""
    addr = addr.upper().strip()
    addr = re.sub(r',.*', '', addr)
    addr = re.sub(r'\s+', ' ', addr).strip()
    return addr


def clean_owner_key(name):
    if not name:
        return ""
    name = name.upper().strip()
    name = re.sub(r'[^A-Z\s]', ' ', name)
    name = ' '.join(name.split())
    return name


def should_skip(owner_name, property_address, assessed_val):
    owner_up = (owner_name or "").upper()
    addr_up  = (property_address or "").upper()

    for skip in SKIP_OWNERS:
        if skip in owner_up:
            return True, "Skip owner: {}".format(skip)

    for skip in SKIP_ADDRESSES:
        if skip in addr_up:
            return True, "Skip address: {}".format(skip)

    try:
        val = float(str(assessed_val).replace('$', '').replace(',', '').strip())
        if val > MAX_ASSESSED:
            return True, "Skip high assessed: {}".format(val)
    except:
        pass

    return False, ""


def parse_tax_csv(path):
    log.info("Loading delinquent tax CSV: %s", path)
    groups = defaultdict(lambda: {
        "years": [],
        "years_count": 0,
        "total_balance": 0.0,
        "owner_name": "",
        "property_address": "",
        "billing_address": "",
        "assessed_value": 0.0,
        "account_number": "",
        "cert_status": "",
    })

    skipped = 0
    loaded  = 0

    with open(path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            acct      = (row.get("Account Number") or "").strip()
            owner     = (row.get("Owner Name") or "").strip()
            prop_addr = (row.get("Property Address") or "").strip()
            bill_addr = (row.get("Billing Address") or "").strip()
            tax_yr    = (row.get("Tax Yr") or row.get("Roll Yr") or "").strip()
            assessed  = (row.get("Assessed Value") or "0").strip()
            balance   = (row.get("Balance Amount") or "0").strip()
            cert_st   = (row.get("Cert Status") or "").strip()

            if not acct or acct == "Grand Total":
                continue

            try:
                bal = float(balance.replace('$', '').replace(',', ''))
            except:
                bal = 0.0

            if bal < MIN_BALANCE:
                continue

            try:
                av = float(assessed.replace('$', '').replace(',', ''))
            except:
                av = 0.0

            skip, reason = should_skip(owner, prop_addr, av)
            if skip:
                skipped += 1
                continue

            try:
                yr = int(tax_yr)
            except:
                yr = 0

            g = groups[acct]
            g["account_number"] = acct
            if yr and yr not in g["years"]:
                g["years"].append(yr)
            g["total_balance"] += bal
            if owner:
                g["owner_name"] = owner
            if prop_addr:
                g["property_address"] = prop_addr
            if bill_addr:
                g["billing_address"] = bill_addr
            if av > g["assessed_value"]:
                g["assessed_value"] = av
            if cert_st and cert_st not in ("-- None --", ""):
                g["cert_status"] = cert_st

            loaded += 1

    # Finalize
    result = {}
    for acct, g in groups.items():
        g["years"].sort()
        g["years_count"] = len(g["years"])
        result[acct] = g

    log.info("Tax CSV: %d rows loaded | %d parcels grouped | %d skipped",
             loaded, len(result), skipped)
    return result


def calc_tax_score(years_count, total_balance):
    score = TAX_BASE_SCORE
    if years_count >= 10:
        score += TAX_YEARS_10_BONUS
    elif years_count >= 5:
        score += TAX_YEARS_5_BONUS
    elif years_count >= 3:
        score += TAX_YEARS_3_BONUS
    elif years_count >= 2:
        score += TAX_YEARS_2_BONUS
    if total_balance >= 50000:
        score += 10
    elif total_balance >= 20000:
        score += 6
    elif total_balance >= 5000:
        score += 3
    return score


def tax_record_to_lead(acct, tax):
    years_count   = tax["years_count"]
    total_balance = tax["total_balance"]
    score         = calc_tax_score(years_count, total_balance)
    prop_addr     = tax["property_address"]
    bill_addr     = tax["billing_address"]
    owner         = tax["owner_name"]

    absentee = False
    prop_key = clean_address_key(prop_addr)
    bill_key = clean_address_key(bill_addr)
    if prop_key and bill_key and prop_key != bill_key:
        absentee = True
        score += 8

    years_str = ", ".join(str(y) for y in sorted(tax["years"]))
    flag_note = "delinquent_taxes_{}yr".format(years_count)

    return {
        "document_number":      "TAX-{}".format(acct),
        "file_date":            "{}-01-01".format(max(tax["years"])) if tax["years"] else "",
        "grantor":              "",
        "grantee":              owner,
        "legal_description":    "",
        "document_type":        "Delinquent Taxes",
        "seller_score":         min(score, 99),
        "distress_flags":       [flag_note],
        "stacked":              False,
        "stacked_docs":         ["TAX-{}".format(acct)],
        "stacked_types":        ["Delinquent Taxes"],
        "motivation_count":     1,
        "property_address":     prop_addr,
        "mailing_address":      bill_addr,
        "owner_name":           owner,
        "assessed_value":       "${:,.2f}".format(tax["assessed_value"]) if tax["assessed_value"] else "",
        "parcel_id":            acct,
        "match_confidence":     "HIGH" if prop_addr else "LOW",
        "match_score":          80 if prop_addr else 20,
        "match_reason":         "Delinquent tax roll — {} year(s) unpaid | balance ${:,.2f}".format(years_count, total_balance),
        "county_search_url":    "https://www.ocpafl.org/searches/ParcelSearch.aspx?SearchType=parcel&SearchValue={}".format(acct),
        "needs_enrichment":     not bool(prop_addr),
        "absentee_owner":       absentee,
        "tax_years_delinquent": years_count,
        "tax_years_list":       years_str,
        "tax_total_balance":    round(total_balance, 2),
        "tax_cert_status":      tax["cert_status"],
        "scraped_at":           datetime.utcnow().isoformat() + "Z",
    }


def main():
    log.info("=== Delinquent Tax Cross-Reference ===")

    if not os.path.exists(TAX_CSV_PATH):
        log.error("Tax CSV not found: %s — upload to data/delinquent_taxes.csv", TAX_CSV_PATH)
        return

    tax_records = parse_tax_csv(TAX_CSV_PATH)
    log.info("Loaded %d unique delinquent parcels", len(tax_records))

    if not os.path.exists(OUTPUT_PATH):
        log.error("No output.json found — run scraper.py and merger.py first")
        return

    with open(OUTPUT_PATH, encoding="utf-8") as f:
        data = json.load(f)

    # Work on a plain list copy — never modify while indexing
    leads = list(data.get("leads", []))
    log.info("Loaded %d existing leads", len(leads))

    # ── BUILD INDEXES ──────────────────────────────────────────────────────
    # Indexes map lookup key -> position in `leads` list
    # New leads go into a separate list and are appended at the end
    # so indexes into `leads` stay valid throughout the loop
    parcel_idx = {}
    addr_idx   = {}
    owner_idx  = {}

    for i, lead in enumerate(leads):
        pid = clean_parcel(lead.get("parcel_id", ""))
        if is_valid_parcel(pid):
            parcel_idx[pid] = i
        raw_pid = (lead.get("parcel_id") or "").strip()
        if raw_pid:
            parcel_idx[raw_pid] = i

        addr = clean_address_key(lead.get("property_address", ""))
        if addr and len(addr) > 5:
            addr_idx[addr] = i

        owner = clean_owner_key(
            lead.get("owner_name", "") or lead.get("grantee", "")
        )
        if owner and len(owner) >= 4:
            owner_idx[owner] = i

    log.info("Indexes: %d parcels | %d addresses | %d owners",
             len(parcel_idx), len(addr_idx), len(owner_idx))

    # ── CROSS-REFERENCE ────────────────────────────────────────────────────
    new_leads = []   # collected separately — appended after loop
    stacked   = 0
    new_added = 0
    skipped   = 0

    for acct, tax in tax_records.items():
        years_count   = tax["years_count"]
        total_balance = tax["total_balance"]
        prop_addr     = tax["property_address"]
        owner_name    = tax["owner_name"]

        if years_count == 0:
            continue

        tax_score = calc_tax_score(years_count, total_balance)

        # ── FIND EXISTING LEAD ─────────────────────────────────────────────
        lead_idx = None

        cleaned = clean_parcel(acct)
        if cleaned in parcel_idx:
            lead_idx = parcel_idx[cleaned]
        elif acct in parcel_idx:
            lead_idx = parcel_idx[acct]

        if lead_idx is None:
            addr_key = clean_address_key(prop_addr)
            if addr_key and addr_key in addr_idx:
                lead_idx = addr_idx[addr_key]

        if lead_idx is None:
            owner_key = clean_owner_key(owner_name)
            if owner_key and owner_key in owner_idx:
                lead_idx = owner_idx[owner_key]

        # Guard — index must be inside the original leads list
        if lead_idx is not None and lead_idx >= len(leads):
            lead_idx = None

        # ── STACK OR ADD ───────────────────────────────────────────────────
        if lead_idx is not None:
            lead = leads[lead_idx]

            existing_flags = lead.get("distress_flags", [])
            if isinstance(existing_flags, str):
                existing_flags = [f.strip() for f in existing_flags.split(",") if f.strip()]

            already_has_tax = any(
                "delinquent" in f.lower() or "tax" in f.lower()
                for f in existing_flags
            )
            if already_has_tax:
                lead["tax_years_delinquent"] = years_count
                lead["tax_total_balance"]    = round(total_balance, 2)
                lead["tax_cert_status"]      = tax["cert_status"]
                leads[lead_idx] = lead
                skipped += 1
                continue

            old_score = lead.get("seller_score", 0)
            new_score = min(old_score + tax_score + TAX_STACK_BONUS, 99)

            existing_flags.append("delinquent_taxes_{}yr".format(years_count))
            lead["distress_flags"] = existing_flags

            stacked_types = lead.get("stacked_types", [])
            if isinstance(stacked_types, str):
                stacked_types = [stacked_types] if stacked_types else []
            if "Delinquent Taxes" not in stacked_types:
                stacked_types.append("Delinquent Taxes")

            stacked_docs = lead.get("stacked_docs", [])
            if isinstance(stacked_docs, str):
                stacked_docs = [stacked_docs] if stacked_docs else []
            tax_doc = "TAX-{}".format(acct)
            if tax_doc not in stacked_docs:
                stacked_docs.append(tax_doc)

            mot_count = lead.get("motivation_count", 1) + 1

            lead["seller_score"]         = new_score
            lead["stacked"]              = True
            lead["stacked_types"]        = stacked_types
            lead["stacked_docs"]         = stacked_docs
            lead["motivation_count"]     = mot_count
            lead["document_type"]        = " + ".join(stacked_types)
            lead["tax_years_delinquent"] = years_count
            lead["tax_years_list"]       = ", ".join(str(y) for y in sorted(tax["years"]))
            lead["tax_total_balance"]    = round(total_balance, 2)
            lead["tax_cert_status"]      = tax["cert_status"]

            if not lead.get("property_address") and prop_addr:
                lead["property_address"] = prop_addr
            if not lead.get("owner_name") and owner_name:
                lead["owner_name"] = owner_name
            if not lead.get("mailing_address") and tax["billing_address"]:
                lead["mailing_address"] = tax["billing_address"]
            if not lead.get("assessed_value") and tax["assessed_value"]:
                lead["assessed_value"] = "${:,.2f}".format(tax["assessed_value"])
            if not lead.get("parcel_id"):
                lead["parcel_id"] = acct

            leads[lead_idx] = lead
            stacked += 1
            log.info("Stacked tax: %s | %d yr | score %d->%d",
                     prop_addr[:45], years_count, old_score, new_score)

        else:
            # New lead — goes into separate list, NOT into leads yet
            # so existing lead_idx values stay valid
            new_lead = tax_record_to_lead(acct, tax)
            new_leads.append(new_lead)
            new_added += 1
            bal_str = "${:,.0f}".format(total_balance)
            log.info("New tax lead: %s | owner=%s | %d yr | bal=%s | score=%d",
                     prop_addr[:40], owner_name[:25],
                     years_count, bal_str, new_lead["seller_score"])

    # Append all new leads at once after loop completes
    leads.extend(new_leads)

    # ── SORT ───────────────────────────────────────────────────────────────
    leads.sort(
        key=lambda l: l.get("seller_score", 0) if isinstance(l, dict) else 0,
        reverse=True
    )

    log.info("Done: %d stacked | %d new tax leads | %d already had tax | %d total",
             stacked, new_added, skipped, len(leads))

    # ── SAVE JSON ──────────────────────────────────────────────────────────
    data["generated_at"]  = datetime.utcnow().isoformat() + "Z"
    data["total_records"] = len(leads)
    data["leads"]         = leads

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info("Saved %d total leads to %s", len(leads), OUTPUT_PATH)

    # ── SAVE CSV ───────────────────────────────────────────────────────────
    fields = [
        "seller_score", "motivation_count", "document_number", "file_date",
        "document_type", "stacked", "stacked_types",
        "grantor", "grantee", "legal_description",
        "property_address", "mailing_address", "owner_name", "assessed_value",
        "parcel_id", "match_confidence", "match_score", "match_reason",
        "county_search_url", "distress_flags", "needs_enrichment",
        "tax_years_delinquent", "tax_total_balance", "tax_years_list",
        "tax_cert_status", "absentee_owner", "scraped_at"
    ]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            row = dict(lead) if isinstance(lead, dict) else {}
            if isinstance(row.get("distress_flags"), list):
                row["distress_flags"] = ", ".join(row["distress_flags"])
            if isinstance(row.get("stacked_types"), list):
                row["stacked_types"] = " + ".join(row["stacked_types"])
            writer.writerow(row)
    log.info("CSV saved: %s", OUTPUT_CSV)


if __name__ == "__main__":
    main()
