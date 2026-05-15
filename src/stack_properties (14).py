"""
stack_properties.py — Final property-level deduplication pass.
"""
import json, logging, os, re, csv, sys, gc
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

print("STACK_PROPERTIES_VERSION = 2026-05-15-v5-pop-safe-types", flush=True)

OUTPUT_PATH = "data/output.json"
OUTPUT_CSV  = "data/output.csv"

# Sources reliable enough to stack by address alone (no parcel ID required)
RELIABLE_SOURCES = {"tax delinquent", "code violation", "foreclosure", "lis pendens"}


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


def normalize_types(value):
    """Split already-joined 'A + B' strings and deduplicate."""
    if not value:
        return []
    raw = value if isinstance(value, list) else [value]
    out = []
    for item in raw:
        if not item:
            continue
        for part in str(item).split(' + '):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def is_address_stackable(lead):
    """Only stack by address for reliable/high-confidence records."""
    confidence = (lead.get('match_confidence') or '').upper()
    if confidence in ('HIGH', 'MEDIUM'):
        return True
    if is_valid_parcel(clean_parcel(lead.get('parcel_id', ''))):
        return True
    doc_type = (lead.get('document_type') or '').lower()
    if any(s in doc_type for s in RELIABLE_SOURCES):
        return True
    return False


def merge_into(primary, secondary):
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

    # Safe type normalization — split any already-joined strings first
    types = []
    for t in (
        normalize_types(primary.get('document_type')) +
        normalize_types(primary.get('stacked_types')) +
        normalize_types(secondary.get('document_type')) +
        normalize_types(secondary.get('stacked_types'))
    ):
        if t not in types:
            types.append(t)
    primary['stacked_types'] = types
    primary['document_type'] = ' + '.join(types)
    primary['stacked']       = True
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

    return primary


def get_rss_mb():
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    except Exception:
        return -1


def stack_by_property(leads):
    """
    Pop from leads as we consume — input shrinks as output grows.
    Net memory stays flat instead of doubling at the midpoint.
    """
    total = len(leads)
    log.info("=== Property stacking pass on %d leads ===", total)
    log.info("MEM before stack: %.1f MB", get_rss_mb())

    # Reverse so pop() gives items in original order
    leads.reverse()

    by_parcel    = {}
    by_addr      = {}
    output       = []
    merged_count = 0
    processed    = 0

    while leads:
        lead = leads.pop()
        processed += 1

        if processed % 5000 == 0:
            log.info("  Stacking progress: %d / %d (%d merged so far) | MEM: %.1f MB",
                     processed, total, merged_count, get_rss_mb())

        if not isinstance(lead, dict):
            output.append(lead)
            continue

        pid   = clean_parcel(lead.get('parcel_id', ''))
        addr  = clean_prop_addr(lead.get('property_address', ''))
        score = float(lead.get('seller_score', 0) or 0)

        matched_idx = None

        # Always match by valid parcel ID
        if is_valid_parcel(pid) and pid in by_parcel:
            matched_idx = by_parcel[pid]

        # Only match by address for reliable records
        if matched_idx is None and addr and len(addr) > 8 and addr in by_addr:
            if is_address_stackable(lead):
                existing = output[by_addr[addr]]
                if is_address_stackable(existing):
                    matched_idx = by_addr[addr]

        if matched_idx is not None:
            existing       = output[matched_idx]
            existing_score = float(existing.get('seller_score', 0) or 0)
            if score > existing_score:
                primary, secondary = lead, existing
            else:
                primary, secondary = existing, lead
            output[matched_idx] = merge_into(primary, secondary)
            merged_count += 1
        else:
            new_idx = len(output)
            output.append(lead)
            if is_valid_parcel(pid):
                by_parcel[pid] = new_idx
            if addr and len(addr) > 8:
                by_addr[addr] = new_idx

    log.info("Property stacking: %d -> %d unique properties (%d merged)",
             total, len(output), merged_count)
    log.info("MEM after stack: %.1f MB", get_rss_mb())
    return output


def main():
    log.info("=== Stack Properties ===")

    if not os.path.exists(OUTPUT_PATH):
        log.error("No output.json found")
        return

    file_size = os.path.getsize(OUTPUT_PATH)
    log.info("output.json size: %.1f MB", file_size / 1024 / 1024)
    log.info("MEM at start: %.1f MB", get_rss_mb())

    log.info("Loading output.json...")
    try:
        with open(OUTPUT_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        log.error("JSON parse error — file likely truncated by previous step: %s", e)
        sys.exit(1)

    generated_at = data.get('generated_at') or (datetime.utcnow().isoformat() + 'Z')
    date_range   = data.get('date_range', '')

    # Steal leads list, free wrapper immediately
    leads = data['leads']
    data.clear()
    del data
    gc.collect()
    log.info("Loaded %d leads (source dict freed) | MEM: %.1f MB", len(leads), get_rss_mb())

    output = stack_by_property(leads)
    del leads  # empty after pop loop, but be explicit
    gc.collect()
    log.info("MEM after del leads + gc: %.1f MB", get_rss_mb())

    output.sort(
        key=lambda l: float(l.get('seller_score', 0) or 0) if isinstance(l, dict) else 0,
        reverse=True
    )

    log.info("Writing output.json (%d leads)...", len(output))
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write('{"generated_at":')
        json.dump(generated_at, f)
        f.write(',"date_range":')
        json.dump(date_range, f)
        f.write(',"total_records":')
        f.write(str(len(output)))
        f.write(',"leads":[')
        for i, lead in enumerate(output):
            if i > 0:
                f.write(',')
            json.dump(lead, f)
        f.write(']}')
    log.info("Saved %d unique properties -> %s", len(output), OUTPUT_PATH)

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
        for lead in output:
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
