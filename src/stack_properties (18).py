"""
stack_properties.py — Final property-level deduplication pass.
"""
import json, logging, os, re, csv, sys, gc
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

print("STACK_PROPERTIES_VERSION = 2026-05-15-v6-confidence-first", flush=True)

OUTPUT_PATH = "data/output.json"
OUTPUT_CSV  = "data/output.csv"

# ── CONFIDENCE-FIRST PRIMARY SELECTION ────────────────────────────────────

_CONFIDENCE_RANK = {
    "HIGH":         5,
    "MEDIUM":       4,
    "LOW":          2,
    "NEEDS_REVIEW": 1,
    "NONE":         0,
    "SKIPPED_CONDO":0,
    "":             0,
}

def _match_quality(lead):
    """
    Returns a tuple for primary selection.
    Priority: direct_parcel > strict_legal > confidence rank > seller_score
    Higher tuple = better primary candidate.
    """
    reason     = (lead.get("match_reason") or "").lower()
    confidence = (lead.get("match_confidence") or "").upper()
    score      = float(lead.get("seller_score", 0) or 0)
    rank       = _CONFIDENCE_RANK.get(confidence, 0)
    is_direct  = 1 if "direct_parcel" in reason or "direct=" in reason else 0
    is_strict  = 1 if "strict_legal" in reason or "strict=" in reason else 0
    return (is_direct, is_strict, rank, score)


def clean_parcel(pid):
    return re.sub(r"[-\s]", "", (pid or "")).strip()

def is_valid_parcel(pid):
    c = clean_parcel(pid)
    return bool(c and c.isdigit() and len(c) >= 10)

def clean_prop_addr(addr):
    if not addr:
        return ""
    addr = str(addr).upper().strip().split(",")[0].strip()
    return re.sub(r"\s+", " ", addr).strip()

def normalize_types(value):
    if not value:
        return []
    raw = value if isinstance(value, list) else [value]
    out = []
    for item in raw:
        for part in str(item).split(" + "):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out

def is_address_stackable(lead):
    confidence = (lead.get("match_confidence") or "").upper()
    if confidence in ("HIGH", "MEDIUM"):
        return True
    if is_valid_parcel(clean_parcel(lead.get("parcel_id", ""))):
        return True
    doc_type = (lead.get("document_type") or "").lower()
    if any(s in doc_type for s in ("tax delinquent", "code violation", "foreclosure", "lis pendens")):
        return True
    return False

def merge_into(primary, secondary):
    # Distress flags
    p_flags = primary.get("distress_flags", [])
    s_flags = secondary.get("distress_flags", [])
    if isinstance(p_flags, str): p_flags = [f.strip() for f in p_flags.split(",") if f.strip()]
    if isinstance(s_flags, str): s_flags = [f.strip() for f in s_flags.split(",") if f.strip()]
    seen = set(p_flags)
    for flag in s_flags:
        if flag not in seen:
            p_flags.append(flag)
            seen.add(flag)
    primary["distress_flags"] = p_flags

    # Doc numbers
    p_docs = primary.get("stacked_docs", [])
    s_docs = secondary.get("stacked_docs", [])
    if isinstance(p_docs, str): p_docs = [p_docs] if p_docs else []
    if isinstance(s_docs, str): s_docs = [s_docs] if s_docs else []
    all_docs = list(dict.fromkeys(
        [primary.get("document_number", ""), secondary.get("document_number", "")] +
        p_docs + s_docs
    ))
    primary["stacked_docs"] = [d for d in all_docs if d]

    # Doc types — safe normalization
    types = []
    for t in (
        normalize_types(primary.get("document_type")) +
        normalize_types(primary.get("stacked_types")) +
        normalize_types(secondary.get("document_type")) +
        normalize_types(secondary.get("stacked_types"))
    ):
        if t not in types:
            types.append(t)
    primary["stacked_types"]    = types
    primary["document_type"]    = " + ".join(types)
    primary["stacked"]          = True
    primary["motivation_count"] = (
        primary.get("motivation_count", 1) +
        secondary.get("motivation_count", 1)
    )

    # Fill missing fields from secondary — never overwrite primary's good data
    for field in [
        "prop_street", "prop_city", "prop_state", "prop_zip",
        "mail_street", "mail_city", "mail_state", "mail_zip",
        "owner_name", "mailing_address", "assessed_value",
        "parcel_id", "county_search_url",
        "code_violation_count", "code_violation_types",
        "tax_years_delinquent", "tax_total_balance",
        "tax_years_list", "tax_cert_status",
    ]:
        if not primary.get(field) and secondary.get(field):
            primary[field] = secondary[field]

    return primary


def stack_by_property(leads):
    """
    Pop from leads as we consume — input shrinks as output grows.
    Primary selection: confidence-first, then seller_score as tiebreaker.
    Safety rule: never merge two records with different valid parcel IDs.
    """
    total = len(leads)
    log.info("=== Property stacking pass on %d leads ===", total)

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
            log.info("  Stacking progress: %d / %d (%d merged so far)",
                     processed, total, merged_count)

        if not isinstance(lead, dict):
            output.append(lead)
            continue

        pid  = clean_parcel(lead.get("parcel_id", ""))
        addr = clean_prop_addr(lead.get("property_address", ""))

        matched_idx = None

        # Match by valid parcel ID first
        if is_valid_parcel(pid) and pid in by_parcel:
            matched_idx = by_parcel[pid]

        # Match by address only if:
        # - no parcel match found
        # - both records are address-stackable
        # - neither has a conflicting valid parcel ID
        if matched_idx is None and addr and len(addr) > 8 and addr in by_addr:
            if is_address_stackable(lead):
                candidate_idx = by_addr[addr]
                existing = output[candidate_idx]
                existing_pid = clean_parcel(existing.get("parcel_id", ""))
                # SAFETY: never merge if both have different valid parcel IDs
                if is_valid_parcel(pid) and is_valid_parcel(existing_pid) and pid != existing_pid:
                    pass  # Different properties — do not merge
                elif is_address_stackable(existing):
                    matched_idx = candidate_idx

        if matched_idx is not None:
            existing = output[matched_idx]
            # CONFIDENCE-FIRST primary selection
            if _match_quality(lead) > _match_quality(existing):
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
    return output


def main():
    log.info("=== Stack Properties ===")

    if not os.path.exists(OUTPUT_PATH):
        log.error("No output.json found")
        return

    file_size = os.path.getsize(OUTPUT_PATH)
    log.info("output.json size: %.1f MB", file_size / 1024 / 1024)

    log.info("Loading output.json...")
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        log.error("JSON parse error — file likely truncated: %s", e)
        sys.exit(1)

    generated_at = data.get("generated_at") or (datetime.utcnow().isoformat() + "Z")
    date_range   = data.get("date_range", "")

    leads = data["leads"]
    data.clear()
    del data
    gc.collect()
    log.info("Loaded %d leads (source dict freed)", len(leads))

    output = stack_by_property(leads)
    del leads
    gc.collect()

    output.sort(
        key=lambda l: float(l.get("seller_score", 0) or 0) if isinstance(l, dict) else 0,
        reverse=True
    )

    log.info("Writing output.json (%d leads)...", len(output))
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write('{"generated_at":')
        json.dump(generated_at, f)
        f.write(',"date_range":')
        json.dump(date_range, f)
        f.write(',"total_records":')
        f.write(str(len(output)))
        f.write(',"leads":[')
        for i, lead in enumerate(output):
            if i > 0:
                f.write(",")
            json.dump(lead, f)
        f.write("]}") 
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
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for lead in output:
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
