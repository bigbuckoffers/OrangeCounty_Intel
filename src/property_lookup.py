"""
property_lookup.py — Find property addresses from legal descriptions + owner names

Strategy (in order):
  1. NAL CSV local match (if NAL file available)
  2. Claude web search (Homes.com, Realtor, Zillow, OCPA, county records, etc.)

Batch-ready: accepts list of records, returns list of results with confidence scores.
"""

import re
import json
import time
import logging
import os
import csv
import requests
from rapidfuzz import fuzz
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OUTPUT_PATH = "data/output.json"
NAL_LOCAL_PATH = "/tmp/NAL_orange.csv"
MAX_LOOKUPS = 200
MIN_CONFIDENCE = 50


# ── LEGAL DESCRIPTION PARSER ──────────────────────────────────────────────────

def parse_legal(legal):
    if not legal:
        return {}
    text = legal.upper().strip()
    result = {}

    pb = re.search(r'\b(\d{1,4})/(\d{1,4})\b', text)
    if pb:
        result["plat_book"] = pb.group(1)
        result["plat_page"] = pb.group(2)

    lot = re.search(r'\bLOT\s+(\w+)\b', text)
    if lot:
        result["lot"] = lot.group(1)

    block = re.search(r'\bBLOCK\s+(\w+)\b', text)
    if block:
        result["block"] = block.group(1)

    unit = re.search(r'\bUNIT\s+(\w+)\b', text)
    if unit:
        result["unit"] = unit.group(1)

    sec = re.search(r'\bSECTION\s+(\w+)\b', text)
    if sec:
        result["section"] = sec.group(1)

    see = re.search(r'\bSEE\s+(\d+)/(\d+)\b', text)
    if see:
        result["ref_book"] = see.group(1)
        result["ref_page"] = see.group(2)

    clean = text
    for pat in [r'\bSEE\s+\d+/\d+\b', r'\b\d+/\d+\b', r'\bLOT\s+\w+\b',
                r'\bBLOCK\s+\w+\b', r'\bUNIT\s+\w+\b', r'\bSECTION\s+\w+\b',
                r'\bPHASE\s+\w+\b', r'\bPB\s+\d+\b', r'\bPG\s+\d+\b']:
        clean = re.sub(pat, '', clean)
    clean = re.sub(r'[^A-Z\s]', ' ', clean)
    clean = ' '.join(clean.split()).strip()
    if len(clean) >= 4:
        result["subdivision"] = clean

    return result


# ── OWNER NAME NORMALIZER ─────────────────────────────────────────────────────

def normalize_owners(raw_owners):
    if not raw_owners:
        return []

    parts = re.split(r'[,&]|\bAND\b', raw_owners.upper())
    names = []
    surnames = set()

    for part in parts:
        part = re.sub(r'[^A-Z\s]', ' ', part).strip()
        tokens = part.split()
        if not tokens:
            continue
        names.append(' '.join(tokens))
        surnames.add(tokens[0])
        if len(tokens) >= 2:
            names.append(' '.join(tokens[1:] + [tokens[0]]))

    for s in surnames:
        if len(s) >= 3:
            names.append(s)

    return list(dict.fromkeys(names))


# ── SEARCH QUERY BUILDER ──────────────────────────────────────────────────────

def build_search_queries(parsed_legal, owner_variations, county="Orange County FL"):
    queries = []
    subdiv = parsed_legal.get("subdivision", "")
    lot = parsed_legal.get("lot", "")
    plat = f"{parsed_legal.get('plat_book','')}/{parsed_legal.get('plat_page','')}" if parsed_legal.get("plat_book") else ""
    surname = owner_variations[-1] if owner_variations else ""

    if subdiv and lot and surname:
        queries.append(f'"{surname}" "{subdiv}" "LOT {lot}"')
    if subdiv and lot:
        queries.append(f'"{subdiv}" "LOT {lot}" "{county}"')
    if plat and lot and subdiv:
        queries.append(f'"{plat}" "LOT {lot}" "{subdiv}"')
    for name in owner_variations[:2]:
        if subdiv:
            queries.append(f'"{name}" "{subdiv}"')
    if subdiv:
        queries.append(f'{subdiv} lot {lot} {county} property address')

    return queries[:6]


# ── NAL LOCAL MATCH ───────────────────────────────────────────────────────────

def build_nal_index(nal_path):
    if not nal_path or not os.path.exists(nal_path):
        return None

    log.info("Building NAL index...")
    index = {"by_owner": {}, "by_subdiv": {}, "records": []}

    try:
        with open(nal_path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                own_name = (row.get("OWN_NAME") or "").upper().strip()
                phy_addr1 = (row.get("PHY_ADDR1") or "").strip()
                phy_city = (row.get("PHY_CITY") or "").strip()
                phy_state = (row.get("PHY_STATE") or "FL").strip()
                phy_zip = (row.get("PHY_ZIPCD") or "").strip()[:5]
                s_legal = (row.get("S_LEGAL") or "").upper().strip()
                parcel = (row.get("PARCEL") or "").strip()

                if not phy_addr1 or not phy_city:
                    continue

                parsed = parse_legal(s_legal)
                rec = {
                    "idx": i,
                    "owner": own_name,
                    "address": phy_addr1,
                    "city": phy_city,
                    "state": phy_state,
                    "zip": phy_zip,
                    "legal": s_legal,
                    "parsed": parsed,
                    "parcel": parcel,
                }
                index["records"].append(rec)

                tokens = own_name.split()
                if tokens:
                    index["by_owner"].setdefault(tokens[0], []).append(i)

                subdiv = parsed.get("subdivision", "")
                if subdiv:
                    first_word = subdiv.split()[0]
                    index["by_subdiv"].setdefault(first_word, []).append(i)

    except Exception as e:
        log.error("NAL index error: %s", e)
        return None

    log.info("NAL index built: %d records", len(index["records"]))
    return index


def nal_lookup(parsed_legal, owner_variations, nal_index):
    if not nal_index:
        return None

    candidates = set()
    surname = owner_variations[-1] if owner_variations else ""

    if surname:
        for rec_idx in nal_index["by_owner"].get(surname, []):
            candidates.add(rec_idx)

    subdiv = parsed_legal.get("subdivision", "")
    if subdiv:
        first_word = subdiv.split()[0]
        for rec_idx in nal_index["by_subdiv"].get(first_word, []):
            candidates.add(rec_idx)

    if not candidates:
        return None

    best_score = 0
    best_rec = None

    for rec_idx in candidates:
        rec = nal_index["records"][rec_idx]
        score = 0
        matched = []

        rec_owner = rec["owner"]
        for name in owner_variations:
            if name in rec_owner or rec_owner in name:
                score += 35
                matched.append(f"owner:{name}")
                break
            elif surname and surname in rec_owner:
                score += 25
                matched.append(f"surname:{surname}")
                break

        rec_subdiv = rec["parsed"].get("subdivision", "")
        if subdiv and rec_subdiv:
            ratio = fuzz.token_sort_ratio(subdiv, rec_subdiv)
            if ratio >= 85:
                score += 30
                matched.append(f"subdiv:{ratio}%")
            elif ratio >= 70:
                score += 15

        lot = parsed_legal.get("lot", "")
        rec_lot = rec["parsed"].get("lot", "")
        if lot and rec_lot and lot == rec_lot:
            score += 30
            matched.append(f"lot:{lot}")

        if parsed_legal.get("plat_book") == rec["parsed"].get("plat_book") and \
           parsed_legal.get("plat_page") == rec["parsed"].get("plat_page") and \
           parsed_legal.get("plat_book"):
            score += 25
            matched.append("plat_match")

        if score > best_score:
            best_score = score
            best_rec = (rec, matched, score)

    if best_rec and best_score >= 60:
        rec, matched, score = best_rec
        return {
            "property_address": f"{rec['address']}, {rec['city']}, {rec['state']} {rec['zip']}",
            "city": rec["city"],
            "state": rec["state"],
            "zip": rec["zip"],
            "parcel_id": rec["parcel"],
            "confidence_score": min(score, 99),
            "matched_fields": matched,
            "source": "NAL_local",
            "notes": f"NAL match score {score}",
        }
    return None


# ── CLAUDE WEB SEARCH ─────────────────────────────────────────────────────────

def claude_web_search(legal, owners, parsed_legal, owner_variations, timeout=30):
    if not ANTHROPIC_API_KEY:
        return None

    subdiv = parsed_legal.get("subdivision", "")
    lot = parsed_legal.get("lot", "")
    plat = f"{parsed_legal.get('plat_book','')}/{parsed_legal.get('plat_page','')}" if parsed_legal.get("plat_book") else ""
    queries = build_search_queries(parsed_legal, owner_variations)
    query_str = "\n".join(f"  {i+1}. {q}" for i, q in enumerate(queries))

    prompt = f"""I need you to find the property address for a property in Orange County, Florida.

DO NOT search for the address directly — the address is unknown.
Instead, search using the owner name and legal description fragments below.

Owner name(s): {owners}
Legal description: {legal}

Parsed details:
- Subdivision: {subdiv}
- Lot number: {lot}
- Plat book/page: {plat}

Use these search queries (strongest first):
{query_str}

Search these sources in order:
1. Orange County Property Appraiser: ocpafl.org or ocpaweb.ocpafl.org
2. Orange County Clerk: myorangeclerk.com
3. Homes.com — search owner name + subdivision
4. Realtor.com — search subdivision + lot
5. Zillow, Redfin, PropertyShark
6. NeighborWho, RealtyHop or similar property index pages
7. General web search with the query strings above

For each result you find, verify:
- Does the owner name match? ({owners})
- Does the subdivision match? ({subdiv})
- Does the lot number match? ({lot})
- Does the plat book/page match? ({plat})

Only return a result if you are confident it matches based on owner + legal description.
The address is the situs/property address, NOT the mailing address.

Reply in this EXACT format only, no other text:
ADDRESS: [full street address only, no city/state/zip]
CITY: [city name]
STATE: FL
ZIP: [5-digit zip]
CONFIDENCE: [HIGH/MEDIUM/LOW]
SOURCE: [URL or site where you found it]

If you cannot find a reliable match, reply:
ADDRESS: NOT FOUND
CONFIDENCE: NONE
SOURCE: none"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )

        if resp.status_code != 200:
            log.warning("Claude API error %d: %s", resp.status_code, resp.text[:100])
            return None

        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

        result = {}
        for line in text.strip().split("\n"):
            line = line.strip()
            if line.startswith("ADDRESS:"):
                result["address"] = line[8:].strip()
            elif line.startswith("CITY:"):
                result["city"] = line[5:].strip()
            elif line.startswith("STATE:"):
                result["state"] = line[6:].strip()
            elif line.startswith("ZIP:"):
                result["zip"] = line[4:].strip()
            elif line.startswith("CONFIDENCE:"):
                result["confidence"] = line[11:].strip().upper()
            elif line.startswith("SOURCE:"):
                result["source"] = line[7:].strip()

        addr = result.get("address", "")
        if not addr or addr.upper() in ("NOT FOUND", "UNKNOWN", ""):
            return None

        conf = result.get("confidence", "LOW")
        score = 85 if conf == "HIGH" else 65 if conf == "MEDIUM" else 45

        return {
            "property_address": f"{addr}, {result.get('city','')}, {result.get('state','FL')} {result.get('zip','')}".strip(", "),
            "city": result.get("city", ""),
            "state": result.get("state", "FL"),
            "zip": result.get("zip", ""),
            "parcel_id": "",
            "confidence_score": score,
            "matched_fields": ["web_search"],
            "source": f"Claude+WebSearch:{result.get('source','')[:60]}",
            "notes": f"Claude web search confidence: {conf}",
        }

    except Exception as e:
        log.debug("Claude search failed: %s", e)
        return None


# ── SINGLE RECORD LOOKUP ──────────────────────────────────────────────────────

def find_property_address(record, nal_index=None, rate_limit=0.5):
    legal = (record.get("legal_description") or "").strip()
    owners = (record.get("owners") or record.get("owner_name") or
              record.get("grantee") or record.get("grantor") or "").strip()

    if not legal and not owners:
        return _no_result("No legal description or owner name")

    parsed = parse_legal(legal)
    owner_vars = normalize_owners(owners)

    # Step 1: NAL local match
    if nal_index:
        result = nal_lookup(parsed, owner_vars, nal_index)
        if result and result["confidence_score"] >= 70:
            return result

    # Step 2: Claude web search
    if ANTHROPIC_API_KEY:
        time.sleep(rate_limit)
        result = claude_web_search(legal, owners, parsed, owner_vars)
        if result and result["confidence_score"] >= MIN_CONFIDENCE:
            return result

    return _no_result("No match found")


def _no_result(reason=""):
    return {
        "property_address": "",
        "city": "",
        "state": "",
        "zip": "",
        "parcel_id": "",
        "confidence_score": 0,
        "matched_fields": [],
        "source": "none",
        "notes": reason,
    }


# ── BATCH PROCESSING ──────────────────────────────────────────────────────────

def batch_lookup(records, nal_path=None, max_records=None, rate_limit=0.5):
    nal_index = build_nal_index(nal_path) if nal_path else None
    to_process = records[:max_records] if max_records else records

    log.info("Batch lookup: %d records", len(to_process))
    results = []
    found = 0

    for i, record in enumerate(to_process):
        result = find_property_address(record, nal_index=nal_index, rate_limit=rate_limit)
        combined = {**record, **result}
        results.append(combined)

        if result["confidence_score"] >= MIN_CONFIDENCE:
            found += 1
            log.info("[%d/%d] FOUND: %s (score:%d source:%s)",
                     i+1, len(to_process), result["property_address"],
                     result["confidence_score"], result["source"])
        else:
            log.info("[%d/%d] NOT FOUND: %s | %s",
                     i+1, len(to_process),
                     record.get("document_number", "?"),
                     record.get("legal_description", "")[:50])

        if (i + 1) % 25 == 0:
            log.info("Progress: %d/%d | Found so far: %d", i+1, len(to_process), found)

    log.info("Done: %d found / %d not found / %d total",
             found, len(to_process)-found, len(to_process))
    return results


# ── MAIN — integrates with output.json ───────────────────────────────────────

def main():
    log.info("=== Property Address Lookup ===")

    if not os.path.exists(OUTPUT_PATH):
        log.error("No output.json found at %s", OUTPUT_PATH)
        return

    with open(OUTPUT_PATH, encoding="utf-8") as f:
        data = json.load(f)

    leads = data.get("leads", [])
    log.info("Loaded %d leads", len(leads))

    # Find leads missing address but having legal description + owner
    candidates = []
    for i, lead in enumerate(leads):
        if not isinstance(lead, dict):
            continue
        has_addr = lead.get("property_address") and lead.get("property_address") not in ("", "—")
        has_prop_city = lead.get("prop_city")
        has_legal = lead.get("legal_description") and len(lead.get("legal_description", "")) > 5
        has_owner = lead.get("owner_name") or lead.get("grantee")
        if not has_addr and not has_prop_city and has_legal and has_owner:
            candidates.append((i, lead))

    log.info("Candidates missing address: %d (capped at %d)", len(candidates), MAX_LOOKUPS)
    to_process = candidates[:MAX_LOOKUPS]

    if not to_process:
        log.info("No candidates to process — all leads already have addresses")
        return

    # Build records for batch lookup
    records = []
    for idx, lead in to_process:
        records.append({
            "_lead_idx": idx,
            "legal_description": lead.get("legal_description", ""),
            "owners": lead.get("owner_name") or lead.get("grantee") or "",
            "document_number": lead.get("document_number", ""),
            "document_type": lead.get("document_type", ""),
        })

    # Run lookup
    nal_path = NAL_LOCAL_PATH if os.path.exists(NAL_LOCAL_PATH) else None
    results = batch_lookup(records, nal_path=nal_path, max_records=MAX_LOOKUPS)

    # Apply results back
    updated = 0
    for result in results:
        if result["confidence_score"] < MIN_CONFIDENCE:
            continue
        lead_idx = result["_lead_idx"]
        lead = leads[lead_idx]
        full_addr = result["property_address"]
        lead["property_address"] = full_addr
        lead["prop_city"] = result["city"]
        lead["prop_state"] = result["state"] or "FL"
        lead["prop_zip"] = result["zip"]
        if result.get("parcel_id"):
            lead["parcel_id"] = result["parcel_id"]
        lead["match_confidence"] = "MEDIUM" if result["confidence_score"] >= 65 else "LOW"
        lead["match_reason"] = f"{result['source']} | score={result['confidence_score']}"
        lead["needs_enrichment"] = False
        leads[lead_idx] = lead
        updated += 1

    log.info("Updated %d leads with found addresses", updated)

    data["leads"] = leads
    data["generated_at"] = datetime.utcnow().isoformat() + "Z"
    data["total_records"] = len(leads)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info("Saved -> %s", OUTPUT_PATH)

    # Also write updated CSV
    import csv as csv_mod
    csv_path = OUTPUT_PATH.replace(".json", ".csv")
    if os.path.exists(csv_path):
        # Re-read and patch the CSV with updated addresses
        rows = []
        with open(csv_path, encoding="utf-8") as f:
            reader = csv_mod.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                rows.append(row)

        # Update rows that were changed
        updated_docs = {leads[r["_lead_idx"]]["document_number"]: leads[r["_lead_idx"]]
                       for r in results if r["confidence_score"] >= MIN_CONFIDENCE}

        for row in rows:
            doc = row.get("document_number", "")
            if doc in updated_docs:
                lead = updated_docs[doc]
                row["property_address"] = lead.get("property_address", row["property_address"])
                row["prop_city"] = lead.get("prop_city", "")
                row["prop_state"] = lead.get("prop_state", "FL")
                row["prop_zip"] = lead.get("prop_zip", "")

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        log.info("CSV updated -> %s", csv_path)

    log.info("Done.")


if __name__ == "__main__":
    main()
