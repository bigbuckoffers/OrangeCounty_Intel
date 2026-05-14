"""
stack_properties.py — Final property-level deduplication pass.

Runs AFTER reenrich.py, foreclosure.py, merger.py, tax_delinquent.py,
and code_violations.py have all completed.

At this point every signal for every property is in output.json.
This script merges duplicate records for the same property into one,
combining all signals, doc numbers, and flags.

One record per property. Always.
"""
import json, logging, os, re, csv
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

OUTPUT_PATH = "data/output.json"
OUTPUT_CSV  = "data/output.csv"


def clean_parcel(pid):
    return re.sub(r'[-\s]', '', (pid or '')).strip()


def is_valid_parcel(pid):
    c = clean_parcel(pid)
    return bool(c and c.isdigit() and len(c) >= 10)


def clean_prop_addr(addr):
    if not addr:
        return ''
    addr = str(addr).upper().strip().split(',')[0].strip()
    return re.sub(r'\s+', ' ', addr).strip()


def stack_by_property(leads):
    """
    Single O(n) pass — groups leads by parcel ID (primary) then address (fallback).
    Uses dict lookups — O(1) per lead — so 16k leads takes under 1 second.
    Merges signals, flags, doc numbers from duplicates into one record.
    """
    total = len(leads)
    log.info("=== Property stacking pass on %d leads ===", total)
    by_parcel = {}  # clean_parcel → index in output
    by_addr   = {}  # clean_addr   → index in output
    output    = []
    merged_count = 0

    for i, lead in enumerate(leads):
        if i > 0 and i % 5000 == 0:
            log.info("  Stacking progress: %d / %d (%d merged so far)", i, total, merged_count)
        if not isinstance(lead, dict):
            output.append(lead)
            continue

        pid   = clean_parcel(lead.get('parcel_id', ''))
        addr  = clean_prop_addr(lead.get('property_address', ''))
        score = float(lead.get('seller_score', 0) or 0)

        matched_idx = None
        if is_valid_parcel(pid) and pid in by_parcel:
            matched_idx = by_parcel[pid]
        elif addr and len(addr) > 8 and addr in by_addr:
            matched_idx = by_addr[addr]

        if matched_idx is not None:
            existing       = output[matched_idx]
            existing_score = float(existing.get('seller_score', 0) or 0)
            primary, secondary = (lead, existing) if score > existing_score else (existing, lead)

            # Merge distress flags
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
                [primary.get('document_number', ''), secondary.get('document_number', '')] +
                p_docs + s_docs
            ))
            primary['stacked_docs'] = [d for d in all_docs if d]

            # Merge doc types
            p_types = primary.get('stacked_types', [])
            s_types = secondary.get('stacked_types', [])
            if isinstance(p_types, str): p_types = [p_types] if p_types else []
            if isinstance(s_types, str): s_types = [s_types] if s_types else []
            all_types = list(dict.fromkeys(
                [primary.get('document_type', '')] + p_types +
                [secondary.get('document_type', '')] + s_types
            ))
            all_types = [t for t in all_types if t]
            primary['stacked_types']    = all_types
            primary['document_type']    = ' + '.join(all_types)
            primary['stacked']          = True
            primary['motivation_count'] = (
                primary.get('motivation_count', 1) +
                secondary.get('motivation_count', 1)
            )

            # Fill missing fields from secondary
            for field in [
                'prop_street', 'prop_city', 'prop_state', 'prop_zip',
                'mail_street', 'mail_city', 'mail_state', 'mail_zip',
                'owner_name', 'mailing_address', 'assessed_value',
                'parcel_id', 'county_search_url',
                'code_violation_count', 'code_violation_types',
                'tax_years_delinquent', 'tax_total_balance',
                'tax_years_list', 'tax_cert_status',
            ]:
                if not primary.get(field) and secondary.get(field):
                    primary[field] = secondary[field]

            output[matched_idx] = primary
            merged_count += 1
        else:
            new_idx = len(output)
            output.append(lead)
            if is_valid_parcel(pid):
                by_parcel[pid] = new_idx
            if addr and len(addr) > 8:
                by_addr[addr] = new_idx

    log.info("Property stacking: %d -> %d unique properties (%d merged)",
             len(leads), len(output), merged_count)
    return output


def main():
    log.info("=== Stack Properties ===")

    if not os.path.exists(OUTPUT_PATH):
        log.error("No output.json found")
        return

    with open(OUTPUT_PATH, encoding='utf-8') as f:
        data = json.load(f)

    leads = data.get('leads', [])
    log.info("Loaded %d leads", len(leads))

    leads = stack_by_property(leads)

    leads.sort(
        key=lambda l: float(l.get('seller_score', 0) or 0) if isinstance(l, dict) else 0,
        reverse=True
    )

    data['generated_at']  = datetime.utcnow().isoformat() + 'Z'
    data['total_records'] = len(leads)
    data['leads']         = leads

    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    log.info("Saved %d unique properties -> %s", len(leads), OUTPUT_PATH)

    # Rewrite CSV
    csv_fields = [
        "seller_score", "motivation_count", "document_number", "file_date",
        "document_type", "stacked", "stacked_types",
        "grantor", "grantee", "legal_description",
        "property_address", "mailing_address", "owner_name", "assessed_value",
        "parcel_id", "match_confidence", "match_score", "match_reason",
        "county_search_url", "distress_flags", "needs_enrichment",
        "prop_street", "prop_city", "prop_state", "prop_zip",
        "mail_street", "mail_city", "mail_state", "mail_zip",
        "tax_years_delinquent", "tax_total_balance",
        "tax_years_list", "tax_cert_status",
        "code_violation_count", "code_violation_types",
        "scraped_at",
    ]
    with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction='ignore')
        writer.writeheader()
        for lead in leads:
            row = dict(lead) if isinstance(lead, dict) else {}
            if isinstance(row.get('distress_flags'), list):
                row['distress_flags'] = ', '.join(row['distress_flags'])
            if isinstance(row.get('stacked_types'), list):
                row['stacked_types'] = ' + '.join(row['stacked_types'])
            writer.writerow(row)
    log.info("CSV saved -> %s", OUTPUT_CSV)
    log.info("Done.")


if __name__ == "__main__":
    main()
