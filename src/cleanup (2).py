"""
cleanup.py — Full dataset cleanup pass for PropSignal output.json.

Runs on the FULL existing output.json, not just new leads.
Applies:
  1. Condo/unit exclusion (multi-signal: legal, parties, address, owner)
  2. Unsafe LOW/NONE no-anchor address clearing
  3. Property stacking/dedup on cleaned leads
  4. Saves output.json, output.csv, excluded_condos.json, no_match_diagnostics.csv

Run manually or add as a pipeline step after stack_properties.py.
"""
import json, logging, os, re, csv, gc
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

print("CLEANUP_VERSION = 2026-05-15-v1", flush=True)

OUTPUT_PATH      = "data/output.json"
OUTPUT_CSV       = "data/output.csv"
EXCLUDED_PATH    = "data/excluded_condos.json"
DIAGNOSTICS_PATH = "data/no_match_diagnostics.csv"

# ── CONDO DETECTION PATTERNS ──────────────────────────────────────────────

# Legal description keywords that mean condo/unit (not wholesaleable)
_CONDO_LEGAL_RE = re.compile(
    r'\b(CONDOMINIUM|CONDO|TOWNHOUSE\s+CONDO|VILLA\s+CONDO|'
    r'APARTMENT|TIMESHARE|VACATION\s+CLUB)\b',
    re.IGNORECASE
)

# Party/entity names that identify condo associations
_CONDO_ENTITY_RE = re.compile(
    r'\b(CONDOMINIUM\s+ASSOCIATION|CONDO\s+ASSOCIATION|'
    r'APARTMENT\s+ASSOCIATION|METROWEST\s+CONDOMINIUM|'
    r'CONDOMINIUM\s+ASSOC)\b',
    re.IGNORECASE
)

# Resort/timeshare brands in legal or party names
_RESORT_PATTERN = re.compile(
    r'\b(DISNEY|MARRIOTT|HILTON|SHERATON|WYNDHAM|WESTGATE|BLUEGREEN|'
    r'TIMESHARE|VISTANA|VACATION\s+CLUB|RESORT\s+CLUB|'
    r'GRAND\s+FLORIDIAN|ANIMAL\s+KINGDOM|WILDERNESS\s+LODGE|'
    r'BOARDWALK|SARATOGA|OLD\s+KEY\s+WEST)\b',
    re.IGNORECASE
)

# Address contains UNIT/APT number = condo (e.g. "8815 WORLDQUEST BLVD UNIT 2106")
_UNIT_ADDR_RE = re.compile(r'\b(UNIT|APT|APARTMENT)\s+\w+', re.IGNORECASE)

# Subdivision lot — NOT a condo even if name contains "UNIT"
# e.g. "Lot 39 Villas at Signal Hill Unit One" — has LOT, is fine
_HAS_LOT_RE = re.compile(r'\bLOT\s+\w+', re.IGNORECASE)

# Owner/party keywords that mean condo/resort entity
_CONDO_OWNER_RE = re.compile(
    r'\b(CONDOMINIUM|TIMESHARE|VACATION\s+CLUB|'
    r'RESORT|SUITES\s+ORLANDO)\b',
    re.IGNORECASE
)


def is_condo_lead(lead):
    """
    Returns (True, reason) if lead is a condo/unit property.
    Returns (False, '') if lead is a targetable SFR/land.

    Multi-signal: checks legal, parties, owner, address.
    Safe for subdivision lots containing 'UNIT' in the name.
    """
    legal   = (lead.get('legal_description') or '').upper()
    grantor = (lead.get('grantor') or '').upper()
    grantee = (lead.get('grantee') or '').upper()
    owner   = (lead.get('owner_name') or '').upper()
    addr    = (lead.get('property_address') or
               lead.get('prop_street') or '').upper()
    doc_type = (lead.get('document_type') or '').upper()

    # 1. Legal description contains condo keyword
    if legal and _CONDO_LEGAL_RE.search(legal):
        m = _CONDO_LEGAL_RE.search(legal)
        return True, f"Legal contains condo indicator: {m.group()}"

    # 2. Party/entity name is a condo association
    for label, party in [('grantor', grantor), ('grantee', grantee), ('owner', owner)]:
        if _CONDO_ENTITY_RE.search(party):
            m = _CONDO_ENTITY_RE.search(party)
            return True, f"{label} identifies condo entity: {m.group()}"

    # 3. Resort/timeshare in legal
    if legal and _RESORT_PATTERN.search(legal):
        m = _RESORT_PATTERN.search(legal)
        return True, f"Resort/timeshare in legal: {m.group()}"

    # 4. Property address has UNIT/APT number
    #    BUT only if legal does NOT show this is a subdivision lot
    #    (e.g. "Lot 39 Villas at Signal Hill Unit One" is fine)
    if addr and _UNIT_ADDR_RE.search(addr):
        if not legal or not _HAS_LOT_RE.search(legal):
            m = _UNIT_ADDR_RE.search(addr)
            return True, f"Address contains unit indicator: {m.group()}"

    # 5. Resort/timeshare brand in party names
    for label, party in [('grantor', grantor), ('grantee', grantee), ('owner', owner)]:
        if _RESORT_PATTERN.search(party):
            m = _RESORT_PATTERN.search(party)
            return True, f"{label} contains resort keyword: {m.group()}"

    # 6. Owner contains condo/resort keywords
    if _CONDO_OWNER_RE.search(owner):
        m = _CONDO_OWNER_RE.search(owner)
        return True, f"Owner contains condo/resort keyword: {m.group()}"

    return False, ''


def clear_unsafe_address(lead):
    """
    Step 2: If a lead has LOW/NONE confidence + no parcel ID + no_anchor in match_reason,
    the property address was guessed from a fuzzy match and can't be trusted.
    Clear it and flag for re-enrichment.
    """
    confidence = (lead.get('match_confidence') or '').upper()
    parcel     = re.sub(r'[-\s]', '', lead.get('parcel_id') or '').strip()
    reason     = (lead.get('match_reason') or '').lower()

    if confidence in ('LOW', 'NONE', '') and not parcel and 'no_anchor' in reason:
        lead['property_address'] = ''
        lead['prop_street']      = ''
        lead['prop_city']        = ''
        lead['prop_state']       = ''
        lead['prop_zip']         = ''
        lead['needs_enrichment'] = True
        lead['match_confidence'] = 'NEEDS_REVIEW'
        lead['match_reason']     = (lead.get('match_reason') or '') + \
                                   ' | cleared unsafe low-confidence no-anchor address'
        return True
    return False


# ── STACKING ──────────────────────────────────────────────────────────────

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
    if not value:
        return []
    raw = value if isinstance(value, list) else [value]
    out = []
    for item in raw:
        for part in str(item).split(' + '):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out

def is_address_stackable(lead):
    confidence = (lead.get('match_confidence') or '').upper()
    if confidence in ('HIGH', 'MEDIUM'):
        return True
    if is_valid_parcel(clean_parcel(lead.get('parcel_id', ''))):
        return True
    doc_type = (lead.get('document_type') or '').lower()
    if any(s in doc_type for s in ('tax delinquent', 'code violation', 'foreclosure', 'lis pendens')):
        return True
    return False

def merge_into(primary, secondary):
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

    p_docs = primary.get('stacked_docs', [])
    s_docs = secondary.get('stacked_docs', [])
    if isinstance(p_docs, str): p_docs = [p_docs] if p_docs else []
    if isinstance(s_docs, str): s_docs = [s_docs] if s_docs else []
    all_docs = list(dict.fromkeys(
        [primary.get('document_number', ''), secondary.get('document_number', '')] +
        p_docs + s_docs
    ))
    primary['stacked_docs'] = [d for d in all_docs if d]

    types = []
    for t in (
        normalize_types(primary.get('document_type')) +
        normalize_types(primary.get('stacked_types')) +
        normalize_types(secondary.get('document_type')) +
        normalize_types(secondary.get('stacked_types'))
    ):
        if t not in types:
            types.append(t)
    primary['stacked_types']    = types
    primary['document_type']    = ' + '.join(types)
    primary['stacked']          = True
    primary['motivation_count'] = (
        primary.get('motivation_count', 1) +
        secondary.get('motivation_count', 1)
    )

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

def stack_by_property(leads):
    total = len(leads)
    log.info("=== Stacking %d leads ===", total)
    leads.reverse()
    by_parcel = {}
    by_addr   = {}
    output    = []
    merged    = 0
    processed = 0

    while leads:
        lead = leads.pop()
        processed += 1
        if processed % 5000 == 0:
            log.info("  Stack progress: %d / %d (%d merged)", processed, total, merged)

        if not isinstance(lead, dict):
            output.append(lead)
            continue

        pid   = clean_parcel(lead.get('parcel_id', ''))
        addr  = clean_prop_addr(lead.get('property_address', ''))
        score = float(lead.get('seller_score', 0) or 0)

        matched_idx = None
        if is_valid_parcel(pid) and pid in by_parcel:
            matched_idx = by_parcel[pid]
        if matched_idx is None and addr and len(addr) > 8 and addr in by_addr:
            if is_address_stackable(lead):
                existing = output[by_addr[addr]]
                if is_address_stackable(existing):
                    matched_idx = by_addr[addr]

        if matched_idx is not None:
            existing = output[matched_idx]
            if score > float(existing.get('seller_score', 0) or 0):
                primary, secondary = lead, existing
            else:
                primary, secondary = existing, lead
            output[matched_idx] = merge_into(primary, secondary)
            merged += 1
        else:
            new_idx = len(output)
            output.append(lead)
            if is_valid_parcel(pid):
                by_parcel[pid] = new_idx
            if addr and len(addr) > 8:
                by_addr[addr] = new_idx

    log.info("Stack: %d -> %d unique (%d merged)", total, len(output), merged)
    return output


# ── SAVE HELPERS ──────────────────────────────────────────────────────────

def save_excluded_condos(excluded, existing_path=EXCLUDED_PATH):
    """Merge new exclusions with any previously excluded condos."""
    existing = []
    if os.path.exists(existing_path):
        try:
            with open(existing_path, encoding='utf-8') as f:
                d = json.load(f)
                existing = d.get('excluded_condos', [])
        except Exception:
            pass

    # Deduplicate by document_number
    seen_docs = {r.get('document_number') for r in existing if r.get('document_number')}
    for r in excluded:
        if r.get('document_number') not in seen_docs:
            existing.append(r)
            seen_docs.add(r.get('document_number'))

    with open(existing_path, 'w', encoding='utf-8') as f:
        json.dump({
            "generated_at":   datetime.utcnow().isoformat() + 'Z',
            "total_excluded": len(existing),
            "excluded_condos": existing,
        }, f)
    log.info("Saved %d total excluded condos -> %s", len(existing), existing_path)


def save_diagnostics(leads, path=DIAGNOSTICS_PATH):
    """Save LOW/NONE/NEEDS_REVIEW leads to CSV for audit."""
    flagged = [l for l in leads if isinstance(l, dict) and
               (l.get('match_confidence') or '').upper() in ('LOW', 'NONE', 'NEEDS_REVIEW')]
    if not flagged:
        log.info("No LOW/NONE/NEEDS_REVIEW leads for diagnostics")
        return
    fields = ['document_number', 'file_date', 'document_type', 'grantor', 'grantee',
              'legal_description', 'property_address', 'owner_name', 'parcel_id',
              'match_confidence', 'match_score', 'match_reason', 'needs_enrichment', 'scraped_at']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(flagged)
    log.info("Saved %d diagnostics -> %s", len(flagged), path)


def save_output_json(leads, generated_at, date_range, path=OUTPUT_PATH):
    log.info("Writing %s (%d leads)...", path, len(leads))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('{"generated_at":')
        json.dump(generated_at, f)
        f.write(',"date_range":')
        json.dump(date_range, f)
        f.write(',"total_records":')
        f.write(str(len(leads)))
        f.write(',"leads":[')
        for i, lead in enumerate(leads):
            if i > 0:
                f.write(',')
            json.dump(lead, f)
        f.write(']}')
    log.info("Saved -> %s", path)


def save_output_csv(leads, path=OUTPUT_CSV):
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
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction='ignore')
        writer.writeheader()
        for lead in leads:
            row = dict(lead) if isinstance(lead, dict) else {}
            if isinstance(row.get('distress_flags'), list):
                row['distress_flags'] = ', '.join(row['distress_flags'])
            if isinstance(row.get('stacked_types'), list):
                row['stacked_types'] = ' + '.join(row['stacked_types'])
            writer.writerow(row)
    log.info("CSV saved -> %s", path)


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    log.info("=== PropSignal Cleanup Pass ===")

    if not os.path.exists(OUTPUT_PATH):
        log.error("No output.json found at %s", OUTPUT_PATH)
        return

    file_size = os.path.getsize(OUTPUT_PATH)
    log.info("output.json size: %.1f MB", file_size / 1024 / 1024)

    log.info("Loading output.json...")
    try:
        with open(OUTPUT_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        log.error("JSON parse error: %s", e)
        return

    generated_at = data.get('generated_at') or (datetime.utcnow().isoformat() + 'Z')
    date_range   = data.get('date_range', '')
    leads        = data['leads']
    data.clear()
    del data
    gc.collect()

    log.info("Loaded %d leads", len(leads))

    # ── STEP 1: Condo exclusion ───────────────────────────────────────────
    log.info("Step 1: Condo/unit exclusion...")
    active   = []
    excluded = []
    for lead in leads:
        if not isinstance(lead, dict):
            active.append(lead)
            continue
        is_condo, reason = is_condo_lead(lead)
        if is_condo:
            lead['exclude_from_dashboard']  = True
            lead['exclude_from_export']     = True
            lead['property_type_detected']  = 'condo_or_unit'
            lead['exclude_reason']          = reason
            lead['excluded_reason']         = reason
            lead['match_confidence']        = 'SKIPPED_CONDO'
            excluded.append(lead)
        else:
            active.append(lead)

    log.info("Condo exclusion: %d active | %d excluded", len(active), len(excluded))

    # ── STEP 2: Clear unsafe LOW/NONE no-anchor addresses ────────────────
    log.info("Step 2: Clearing unsafe low-confidence no-anchor addresses...")
    cleared = 0
    for lead in active:
        if isinstance(lead, dict) and clear_unsafe_address(lead):
            cleared += 1
    log.info("Cleared %d unsafe addresses", cleared)

    # ── STEP 3: Stack/dedup ───────────────────────────────────────────────
    log.info("Step 3: Stacking/deduplicating %d active leads...", len(active))
    active = stack_by_property(active)
    gc.collect()

    # ── STEP 4: Sort by score ─────────────────────────────────────────────
    active.sort(
        key=lambda l: float(l.get('seller_score', 0) or 0) if isinstance(l, dict) else 0,
        reverse=True
    )

    # ── STEP 5: Save everything ───────────────────────────────────────────
    log.info("Step 5: Saving outputs...")
    save_output_json(active, generated_at, date_range)
    save_output_csv(active)
    save_excluded_condos(excluded)
    save_diagnostics(active)

    log.info("=== Cleanup complete ===")
    log.info("  Active leads:   %d", len(active))
    log.info("  Excluded condos: %d", len(excluded))
    log.info("  Cleared addresses: %d", cleared)


if __name__ == "__main__":
    main()
