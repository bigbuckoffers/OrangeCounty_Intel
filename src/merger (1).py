"""
merger.py — Merge foreclosure auctions into the unified leads output.

Runs after reenrich.py and foreclosure.py.

For each foreclosure:
  1. If parcel ID matches an existing lead — stack signals, boost score
  2. If no match — create a new lead entry from the foreclosure data

All leads (comptroller + foreclosure) end up in one output.json sorted by score.
Foreclosure-only leads get document_type = "Foreclosure Auction"
Stacked leads show all motivation types combined.
"""
import json, logging, os, re, urllib.parse
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

OUTPUT_PATH       = "data/output.json"
FORECLOSURES_PATH = "data/foreclosures.json"
OCPA_WEB_URL      = "https://ocpaweb.ocpafl.org/parcelsearch/Parcel%20ID/{}"

# Scoring
AUCTION_BASE_SCORE  = 35
URGENCY_7_DAYS      = 15
URGENCY_14_DAYS     = 8
ABSENTEE_BONUS      = 20
HOMESTEAD_BONUS     = 5
STACK_2_BONUS       = 10
STACK_3_BONUS       = 20


def clean_parcel(pid):
    return re.sub(r'[-\s]', '', (pid or "")).strip()


def is_valid_parcel(pid):
    cleaned = clean_parcel(pid)
    return bool(cleaned and cleaned.isdigit() and len(cleaned) >= 10)


def calc_auction_score(fc):
    score = AUCTION_BASE_SCORE
    days  = fc.get("days_until_auction", 99)
    if days <= 7:
        score += URGENCY_7_DAYS
    elif days <= 14:
        score += URGENCY_14_DAYS
    if fc.get("absentee_owner"):
        score += ABSENTEE_BONUS
    if fc.get("homestead"):
        score += HOMESTEAD_BONUS
    return score


def foreclosure_to_lead(fc):
    """Convert a foreclosure record into a lead dict."""
    auction_score = calc_auction_score(fc)
    parcel_id     = fc.get("parcel_id", "")
    # Clean invalid parcel placeholders from realforeclose.com
    _INVALID_PARCELS = {"MULTIPLE PARCELS", "PROPERTY APPRAISER", "LIQUOR LICENSE",
                        "N/A", "NA", "NONE", ""}
    if parcel_id.upper().strip() in _INVALID_PARCELS or not parcel_id.replace("-","").replace(" ","").isdigit():
        parcel_id = ""
    address       = fc.get("address", "")

    county_url = (OCPA_WEB_URL.format(urllib.parse.quote(parcel_id))
                  if is_valid_parcel(parcel_id) else fc.get("ocpa_url", ""))

    return {
        "document_number":   fc.get("case_number", ""),
        "file_date":         fc.get("auction_date", ""),
        "grantor":           "",
        "grantee":           fc.get("owner_name", ""),
        "legal_description": "",
        "document_type":     "Foreclosure Auction",
        "seller_score":      min(auction_score, 100),
        "distress_flags":    ["foreclosure_auction"],
        "stacked":           False,
        "stacked_docs":      [fc.get("case_number", "")],
        "stacked_types":     ["Foreclosure Auction"],
        "motivation_count":  1,
        "motivation_signals": [
            {
                "source":  "RealForeclose",
                "type":    "Foreclosure Auction",
                "date":    fc.get("auction_date", ""),
                "score":   auction_score,
                "doc_number": fc.get("case_number", ""),
            }
        ],
        "property_address":  address,
        "prop_street":       fc.get("prop_street", "") or
                             (address.split(",")[0].strip() if address else ""),
        "prop_city":         fc.get("prop_city", "") or
                             (address.split(",")[1].strip() if address and "," in address else ""),
        "prop_state":        "FL",
        "prop_zip":          fc.get("prop_zip", "") or
                             (address.split(",")[-1].strip().split()[-1]
                              if address and "," in address else ""),
        "mailing_address":   fc.get("mailing_address", ""),
        "mail_street":       fc.get("mail_street", ""),
        "mail_city":         fc.get("mail_city", ""),
        "mail_state":        fc.get("mail_state", "FL"),
        "mail_zip":          fc.get("mail_zip", ""),
        "owner_name":        fc.get("owner_name", ""),
        "assessed_value":    fc.get("assessed_value", ""),
        "parcel_id":         parcel_id,
        "match_confidence":  "HIGH" if address else "NONE",
        "match_score":       90 if address else 0,
        "match_reason":      "Foreclosure auction — direct from realforeclose.com",
        "county_search_url": county_url,
        "needs_enrichment":  not bool(address),
        # Auction-specific fields
        "auction_date":           fc.get("auction_date", ""),
        "auction_time":           fc.get("auction_time", ""),
        "auction_status":         fc.get("status", "ACTIVE"),
        "auction_days_left":      fc.get("days_until_auction", 0),
        "auction_final_judgment": fc.get("final_judgment", ""),
        "auction_case_number":    fc.get("case_number", ""),
        "auction_url":            fc.get("auction_url", ""),
        "comptroller_url":        fc.get("comptroller_url", ""),
        "homestead":              fc.get("homestead", False),
        "absentee_owner":         fc.get("absentee_owner", False),
        "scraped_at":             fc.get("scraped_at", datetime.utcnow().isoformat() + "Z"),
    }


def main():
    log.info("=== Merger: Foreclosures → Leads ===")

    # Load existing leads
    if not os.path.exists(OUTPUT_PATH):
        log.error("No output.json found")
        return
    with open(OUTPUT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    leads = data.get("leads", [])
    log.info("Loaded %d existing leads", len(leads))

    # Load foreclosures
    if not os.path.exists(FORECLOSURES_PATH):
        log.warning("No foreclosures.json found — skipping merge")
        return
    with open(FORECLOSURES_PATH, encoding="utf-8") as f:
        fc_data = json.load(f)
    foreclosures = fc_data.get("foreclosures", [])
    active_fc = [f for f in foreclosures if f.get("status") == "ACTIVE"]
    log.info("Loaded %d active foreclosures", len(active_fc))

    # ── BUILD LOOKUP INDEXES ───────────────────────────────────────────────
    # Track by parcel ID, address, case number, AND document number
    # so we never add the same property twice regardless of run frequency
    parcel_idx   = {}
    addr_idx     = {}
    case_num_idx = {}

    for i, lead in enumerate(leads):
        # Parcel ID index
        pid = clean_parcel(lead.get("parcel_id", ""))
        if is_valid_parcel(pid):
            parcel_idx[pid] = i

        # Address index
        addr = (lead.get("property_address") or "").upper().strip()
        key  = addr.split(",")[0].strip()
        if key:
            addr_idx[key] = i

        # Case/document number index — catches foreclosure-only leads
        # that were added in previous runs
        doc = (lead.get("document_number") or "").strip()
        if doc:
            case_num_idx[doc] = i

        # Also index all stacked doc numbers
        stacked_docs = lead.get("stacked_docs") or []
        if isinstance(stacked_docs, str):
            stacked_docs = [stacked_docs] if stacked_docs else []
        for d in stacked_docs:
            if d:
                case_num_idx[d] = i

        # Index auction case number field too
        auction_case = (lead.get("auction_case_number") or "").strip()
        if auction_case:
            case_num_idx[auction_case] = i

    log.info("Indexes built: %d parcels | %d addresses | %d case numbers",
             len(parcel_idx), len(addr_idx), len(case_num_idx))
    # ──────────────────────────────────────────────────────────────────────

    new_leads = []
    stacked   = 0
    added     = 0
    skipped   = 0

    for fc in active_fc:
        pid      = clean_parcel(fc.get("parcel_id", ""))
        fc_addr  = (fc.get("address") or "").upper().strip()
        fc_key   = fc_addr.split(",")[0].strip()
        case_num = (fc.get("case_number") or "").strip()

        # ── FIND EXISTING LEAD ─────────────────────────────────────────────
        # Check in priority order:
        # 1. Case number (most reliable — catches re-runs on same data)
        # 2. Parcel ID (catches cross-source matches)
        # 3. Address (fallback)
        lead_idx = None

        if case_num and case_num in case_num_idx:
            lead_idx = case_num_idx[case_num]
            log.debug("Matched by case number: %s", case_num)
        elif is_valid_parcel(pid) and pid in parcel_idx:
            lead_idx = parcel_idx[pid]
            log.debug("Matched by parcel: %s", pid)
        elif fc_key and fc_key in addr_idx:
            lead_idx = addr_idx[fc_key]
            log.debug("Matched by address: %s", fc_key)
        # ──────────────────────────────────────────────────────────────────

        auction_score = calc_auction_score(fc)

        if lead_idx is not None:
            lead = leads[lead_idx]

            # Check if this exact foreclosure case is already stacked on this lead
            existing_stacked = lead.get("stacked_docs") or []
            if isinstance(existing_stacked, str):
                existing_stacked = [existing_stacked] if existing_stacked else []

            existing_auction_case = (lead.get("auction_case_number") or "").strip()

            if case_num and (case_num in existing_stacked or case_num == existing_auction_case):
                # Already stacked — just update the auction date/status in case it changed
                lead["auction_date"]      = fc.get("auction_date", "")
                lead["auction_days_left"] = fc.get("days_until_auction", 0)
                lead["auction_status"]    = fc.get("status", "ACTIVE")
                leads[lead_idx] = lead
                skipped += 1
                log.debug("Already stacked, updated date only: %s", case_num)
                continue

            # Stack onto existing lead — new foreclosure signal
            old_score = lead.get("seller_score", 0)
            new_score = min(old_score + auction_score, 100)

            # Combine distress flags
            flags = lead.get("distress_flags", [])
            if isinstance(flags, str):
                flags = [f.strip() for f in flags.split(",") if f.strip()]
            if "foreclosure_auction" not in flags:
                flags.append("foreclosure_auction")

            # Combine stacked types
            stacked_types = lead.get("stacked_types", [])
            if isinstance(stacked_types, str):
                stacked_types = [stacked_types] if stacked_types else []
            if "Foreclosure Auction" not in stacked_types:
                stacked_types.append("Foreclosure Auction")

            # Combine stacked docs
            stacked_docs = lead.get("stacked_docs", [])
            if isinstance(stacked_docs, str):
                stacked_docs = [stacked_docs] if stacked_docs else []
            if case_num and case_num not in stacked_docs:
                stacked_docs.append(case_num)

            # Motivation signals
            signals = lead.get("motivation_signals", [])
            signals.append({
                "source":     "RealForeclose",
                "type":       "Foreclosure Auction",
                "date":       fc.get("auction_date", ""),
                "score":      auction_score,
                "doc_number": case_num,
            })

            mot_count = lead.get("motivation_count", 1) + 1

            # Stack bonus
            if mot_count >= 3:
                new_score = min(new_score + STACK_3_BONUS, 100)
            elif mot_count >= 2:
                new_score = min(new_score + STACK_2_BONUS, 100)

            lead["seller_score"]       = new_score
            lead["distress_flags"]     = flags
            lead["stacked"]            = True
            lead["stacked_types"]      = stacked_types
            lead["stacked_docs"]       = stacked_docs
            lead["motivation_count"]   = mot_count
            lead["motivation_signals"] = signals
            lead["document_type"]      = " + ".join(stacked_types)

            # Fill in missing data from foreclosure
            if not lead.get("property_address") and fc.get("address"):
                lead["property_address"] = fc["address"]
            if not lead.get("owner_name") and fc.get("owner_name"):
                lead["owner_name"] = fc["owner_name"]
            if not lead.get("mailing_address") and fc.get("mailing_address"):
                lead["mailing_address"] = fc["mailing_address"]
            if not lead.get("parcel_id") and is_valid_parcel(pid):
                lead["parcel_id"] = fc["parcel_id"]
                lead["county_search_url"] = OCPA_WEB_URL.format(
                    urllib.parse.quote(fc["parcel_id"]))
            if not lead.get("assessed_value") and fc.get("assessed_value"):
                lead["assessed_value"] = fc["assessed_value"]

            # Add/update auction fields
            lead["auction_date"]           = fc.get("auction_date", "")
            lead["auction_time"]           = fc.get("auction_time", "")
            lead["auction_status"]         = fc.get("status", "ACTIVE")
            lead["auction_days_left"]      = fc.get("days_until_auction", 0)
            lead["auction_final_judgment"] = fc.get("final_judgment", "")
            lead["auction_case_number"]    = case_num
            lead["auction_url"]            = fc.get("auction_url", "")
            lead["comptroller_url"]        = fc.get("comptroller_url", "")
            lead["homestead"]              = fc.get("homestead", False)
            lead["absentee_owner"]         = fc.get("absentee_owner", False)

            leads[lead_idx] = lead

            # Update indexes so later iterations don't double-add
            if case_num:
                case_num_idx[case_num] = lead_idx
            if is_valid_parcel(pid):
                parcel_idx[pid] = lead_idx
            if fc_key:
                addr_idx[fc_key] = lead_idx

            stacked += 1
            log.info("Stacked FC onto lead: %s | score %d→%d",
                     fc.get("address","")[:50], old_score, new_score)

        else:
            # New lead from foreclosure — not in any existing data
            new_lead = foreclosure_to_lead(fc)
            new_leads.append(new_lead)

            # Immediately add to indexes so subsequent iterations
            # in this same run don't add the same property again
            new_idx = len(leads) + len(new_leads) - 1
            if case_num:
                case_num_idx[case_num] = new_idx
            if is_valid_parcel(pid):
                parcel_idx[pid] = new_idx
            if fc_key:
                addr_idx[fc_key] = new_idx

            added += 1
            log.info("New FC lead: %s | score %d",
                     fc.get("address","")[:50], new_lead["seller_score"])

    # Add new foreclosure-only leads
    leads.extend(new_leads)

    # ── FINAL DEDUP PASS ───────────────────────────────────────────────────
    # Safety net — remove any remaining duplicates by parcel ID or case number
    # This catches anything that slipped through from previous runs before this fix
    seen_parcels = set()
    seen_cases   = set()
    deduped_leads = []

    for lead in leads:
        pid  = clean_parcel(lead.get("parcel_id", ""))
        doc  = (lead.get("document_number") or "").strip()
        case = (lead.get("auction_case_number") or "").strip()

        parcel_key = pid  if is_valid_parcel(pid) else None
        case_key   = doc  or case or None

        is_dup = False
        if parcel_key and parcel_key in seen_parcels:
            is_dup = True
        elif case_key and case_key in seen_cases and not parcel_key:
            is_dup = True

        if is_dup:
            log.debug("Final dedup removed duplicate: parcel=%s case=%s", pid, case_key)
            continue

        if parcel_key:
            seen_parcels.add(parcel_key)
        if case_key:
            seen_cases.add(case_key)
        deduped_leads.append(lead)

    removed = len(leads) - len(deduped_leads)
    if removed > 0:
        log.info("Final dedup removed %d duplicate leads", removed)
    leads = deduped_leads
    # ──────────────────────────────────────────────────────────────────────

    # Sort by score descending
    leads.sort(key=lambda l: l.get("seller_score", 0) if isinstance(l, dict)
               else l.seller_score, reverse=True)

    log.info("Merge complete: %d stacked | %d new | %d skipped (already exists) | %d dupes removed | %d total",
             stacked, added, skipped, removed, len(leads))

    # Save
    data["generated_at"]  = datetime.utcnow().isoformat() + "Z"
    data["total_records"] = len(leads)
    data["leads"]         = leads

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info("Saved %d total leads to %s", len(leads), OUTPUT_PATH)


if __name__ == "__main__":
    main()
