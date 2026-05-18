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



NAL_LOCAL_PATH = "/tmp/NAL_orange.csv"

# Fields that come from courthouse filing — never overwrite from NAL
COURTHOUSE_FIELDS = {
    "document_number", "file_date", "document_type",
    "grantor", "grantee", "legal_description",
    "distress_flags", "seller_score", "scraped_at",
}

# Confidence ranks — used to decide whether to trust old match
_REVALIDATE_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "NONE": 0,
                    "NEEDS_REVIEW": 0, "SKIPPED_CONDO": 0, "": 0}


def _load_nal_row_index():
    """Load NAL file and build NALRowIndex for strict matching."""
    if not os.path.exists(NAL_LOCAL_PATH):
        log.warning("NAL file not found at %s — skipping revalidation", NAL_LOCAL_PATH)
        return None

    import csv as _csv
    import re as _re

    log.info("Loading NAL file for revalidation: %s", NAL_LOCAL_PATH)
    rows = []
    try:
        with open(NAL_LOCAL_PATH, encoding="utf-8", errors="replace") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                pid      = (row.get("PARCEL_ID") or "").strip()
                s_legal  = (row.get("S_LEGAL")   or "").strip()
                addr1    = (row.get("PHY_ADDR1")  or "").strip()
                city     = (row.get("PHY_CITY")   or "").strip()
                state    = (row.get("PHY_STATE")  or "FL").strip() or "FL"
                zipcd    = str(row.get("PHY_ZIPCD") or "").strip()[:5]
                own_name = (row.get("OWN_NAME")   or "").strip()
                av       = (row.get("AV_NSD") or row.get("TV_NSD") or "").strip()
                if not addr1 or not city:
                    continue
                rows.append({
                    "PARCEL_ID": pid,
                    "S_LEGAL":   s_legal,
                    "PHY_ADDR1": addr1,
                    "PHY_CITY":  city,
                    "PHY_STATE": state,
                    "PHY_ZIPCD": zipcd,
                    "OWN_NAME":  own_name,
                    "AV_NSD":    av,
                })
    except Exception as e:
        log.error("Failed to load NAL: %s", e)
        return None

    log.info("NAL loaded: %d records", len(rows))
    return rows


def _build_row_index(rows):
    """Build token index over S_LEGAL for fast lookup."""
    from collections import defaultdict
    token_idx = defaultdict(list)
    for i, row in enumerate(rows):
        s = (row.get("S_LEGAL") or "").upper()
        for tok in set(s.split()):
            if len(tok) >= 4:
                token_idx[tok].append(i)
    return token_idx


def _s_legal_matches(s_legal, core_tokens):
    """Check if all core tokens appear in s_legal."""
    s = s_legal.upper()
    return all(tok in s for tok in core_tokens)


def _parse_legal_for_revalidate(raw):
    """Parse legal description into lot + core subdivision tokens."""
    if not raw:
        return None, []
    text = raw.upper().strip()
    text = re.sub(r"[^A-Z0-9\s]", " ", text)
    text = re.sub(r"\bUNIT\s+NO\b", "UNIT", text)
    text = re.sub(r"\s+", " ", text).strip()

    lot_match = re.search(r"\bLOT\s+(\d+)\b", text)
    if not lot_match:
        return None, []
    lot = int(lot_match.group(1))

    # Strip structural tokens to get subdivision name
    subdiv = text
    for pat in [r"\bLOT\s+\d+\b", r"\bBLOCK\s+\w+\b",
                r"\bUNIT\s+\w+\b", r"\bPHASE\s+\w+\b",
                r"\bSECTION\s+\w+\b"]:
        subdiv = re.sub(pat, "", subdiv)
    subdiv = re.sub(r"\s+", " ", subdiv).strip()

    _STRIP = {"REPLAT","PHASE","UNIT","SECTION","PLAT","AMENDED","REVISED",
              "ADDITION","EXTENSION","TRACT","PARCEL","NO","THE","OF","A",
              "AN","AND","OR","IN","AT","TO","FOR","LOT","LOTS","BLOCK"}
    core = [t for t in subdiv.split()
            if t not in _STRIP and len(t) >= 3 and not t.isdigit()]
    return lot, core


def _lot_to_suffixes(lot):
    base = lot * 10
    return {str(base).zfill(4), str(base).zfill(5),
            str(base).zfill(6), str(lot).zfill(4)}


def _extract_direct_parcel(legal):
    """
    Try to extract a parcel ID directly from the legal description.
    Handles three formats:
      1. 15-digit compact:       222228767000290
      2. Hyphenated:             22-22-28-7670-00-290
      3. Spaced (after PARCEL):  25 23 27 1213 00 250  -> 252327121300250
    """
    if not legal:
        return None

    upper = legal.upper()

    # 1. 15-digit compact
    m = re.search(r"\b(\d{15})\b", legal)
    if m:
        return m.group(1)

    # 2. Hyphenated xx-xx-xx-xxxx-xx-xxx(x)
    m = re.search(r"\b(\d{2}-\d{2}-\d{2}-\d{4}-\d{2}-\d{3,4})\b", legal)
    if m:
        return re.sub(r"[-\s]", "", m.group(1))

    # 3. Spaced parcel after "PARCEL" keyword: "Parcel 25 23 27 1213 00 250"
    #    Matches 2+2+2+4+2+3 or 2+2+2+4+2+4 digit groups separated by spaces
    m = re.search(
        r"\bPARCEL\s+(\d{2})\s+(\d{2})\s+(\d{2})\s+(\d{4})\s+(\d{2})\s+(\d{3,4})\b",
        upper
    )
    if m:
        return "".join(m.groups())

    return None


def _strict_match(legal, filing_party, rows, token_idx, doc_number="",
                  existing_address="", existing_parcel="", existing_reason=""):
    """
    Run direct + strict matching on a legal description.
    Returns (parcel, addr1, city, state, zipcd, own_name, av, source) or None.
    """
    debug = doc_number == "20260267650"

    if debug:
        log.info("[DEBUG %s] ========================================", doc_number)
        log.info("[DEBUG %s] legal_description  = %r", doc_number, legal)
        log.info("[DEBUG %s] filing_party       = %r", doc_number, filing_party)
        log.info("[DEBUG %s] existing_address   = %r", doc_number, existing_address)
        log.info("[DEBUG %s] existing_parcel    = %r", doc_number, existing_parcel)
        log.info("[DEBUG %s] existing_reason    = %r", doc_number, existing_reason)

    # Direct parcel extraction
    direct_pid = _extract_direct_parcel(legal)
    if debug:
        log.info("[DEBUG %s] direct parcel extraction result = %r", doc_number, direct_pid)

    if direct_pid:
        clean = re.sub(r"[-\s]", "", direct_pid)
        for row in rows:
            rp = re.sub(r"[-\s]", "", row.get("PARCEL_ID", ""))
            if rp == clean:
                if debug:
                    log.info("[DEBUG %s] DIRECT PARCEL match: %s -> addr=%s owner=%s",
                             doc_number, clean, row.get("PHY_ADDR1"), row.get("OWN_NAME"))
                return (row["PARCEL_ID"], row["PHY_ADDR1"], row["PHY_CITY"],
                        row.get("PHY_STATE","FL"), row["PHY_ZIPCD"],
                        row["OWN_NAME"], row.get("AV_NSD",""), "direct_parcel")
        if debug:
            log.info("[DEBUG %s] direct parcel %s not found in NAL rows", doc_number, clean)

    # Strict lot/subdivision match
    lot, core = _parse_legal_for_revalidate(legal)
    if debug:
        log.info("[DEBUG %s] parsed lot = %s | core tokens = %s", doc_number, lot, core)

    if not lot:
        if debug:
            log.info("[DEBUG %s] FALLTHROUGH: no lot number parsed from legal", doc_number)
        return None
    if not core:
        if debug:
            log.info("[DEBUG %s] FALLTHROUGH: no core subdivision tokens parsed", doc_number)
        return None

    # Find subdivision candidates via token index
    candidate_sets = [set(token_idx.get(t, [])) for t in core]
    if not any(candidate_sets):
        if debug:
            log.info("[DEBUG %s] FALLTHROUGH: tokens %s not in NAL index at all", doc_number, core)
        return None

    common = candidate_sets[0]
    for s in candidate_sets[1:]:
        common &= s

    if debug:
        log.info("[DEBUG %s] token intersection: %d rows share all tokens %s",
                 doc_number, len(common), core)

    subdiv_candidates = [rows[i] for i in common
                         if _s_legal_matches(rows[i].get("S_LEGAL",""), core)]

    if debug:
        log.info("[DEBUG %s] S_LEGAL candidates after token filter: %d", doc_number, len(subdiv_candidates))
        for r in subdiv_candidates[:8]:
            log.info("[DEBUG %s]   s_legal_cand: parcel=%-20s s_legal=%r addr=%s",
                     doc_number, r.get("PARCEL_ID",""), r.get("S_LEGAL",""), r.get("PHY_ADDR1",""))

    if not subdiv_candidates:
        if debug:
            log.info("[DEBUG %s] FALLTHROUGH: no S_LEGAL rows contain all tokens %s", doc_number, core)
        return None

    # Filter by parcel suffix
    suffixes = _lot_to_suffixes(lot)
    if debug:
        log.info("[DEBUG %s] lot=%s -> suffixes tried: %s", doc_number, lot, sorted(suffixes))

    lot_candidates = []
    matched_suffix = ""
    for suffix in suffixes:
        hits = [r for r in subdiv_candidates
                if re.sub(r"[-\s]", "", r.get("PARCEL_ID","")).endswith(suffix)]
        if hits:
            lot_candidates = hits
            matched_suffix = suffix
            break

    if debug:
        log.info("[DEBUG %s] lot candidates after suffix filter: %d (matched suffix=%r)",
                 doc_number, len(lot_candidates), matched_suffix)
        for r in lot_candidates[:5]:
            log.info("[DEBUG %s]   lot_cand: parcel=%-20s addr=%s owner=%s",
                     doc_number, r.get("PARCEL_ID",""), r.get("PHY_ADDR1",""), r.get("OWN_NAME",""))

    if not lot_candidates:
        if debug:
            log.info("[DEBUG %s] FALLTHROUGH: none of %d s_legal candidates matched suffixes %s",
                     doc_number, len(subdiv_candidates), sorted(suffixes))
            # Show what suffixes the candidates actually have
            for r in subdiv_candidates[:5]:
                rp = re.sub(r"[-\s]", "", r.get("PARCEL_ID",""))
                log.info("[DEBUG %s]   candidate parcel=%s ends_in=%s",
                         doc_number, rp, rp[-6:] if len(rp) >= 6 else rp)
        return None

    # Owner confirmation tiebreaker
    chosen = lot_candidates[0]
    if len(lot_candidates) > 1 and filing_party:
        party_tokens = [t for t in filing_party.upper().split() if len(t) >= 4]
        confirmed = [r for r in lot_candidates
                     if any(tok in (r.get("OWN_NAME") or "").upper()
                            for tok in party_tokens)]
        if debug:
            log.info("[DEBUG %s] owner confirmation: party_tokens=%s confirmed=%d",
                     doc_number, party_tokens, len(confirmed))
        if confirmed:
            chosen = confirmed[0]
        else:
            if debug:
                log.info("[DEBUG %s] owner confirmation failed — using first lot candidate",
                         doc_number)
    elif debug and len(lot_candidates) == 1:
        log.info("[DEBUG %s] single lot candidate — no owner confirmation needed", doc_number)

    if debug:
        log.info("[DEBUG %s] STRICT MATCH SUCCESS:", doc_number)
        log.info("[DEBUG %s]   final parcel              = %s", doc_number, chosen.get("PARCEL_ID"))
        log.info("[DEBUG %s]   final address             = %s", doc_number, chosen.get("PHY_ADDR1"))
        log.info("[DEBUG %s]   final city/state/zip      = %s %s %s",
                 doc_number, chosen.get("PHY_CITY"), chosen.get("PHY_STATE"), chosen.get("PHY_ZIPCD"))
        log.info("[DEBUG %s]   final current_ocpa_owner  = %s", doc_number, chosen.get("OWN_NAME"))

    return (chosen["PARCEL_ID"], chosen["PHY_ADDR1"], chosen["PHY_CITY"],
            chosen.get("PHY_STATE","FL"), chosen["PHY_ZIPCD"],
            chosen["OWN_NAME"], chosen.get("AV_NSD",""), "strict_legal")



def _primary_party(party_str):
    """
    Extract the primary property owner from a compound party string.
    County filings list multiple parties in one field separated by commas:
      "MAYSONET BETTY, ROSE HILL GROVES HOMEOWNERS ASSOCIATION INC, SUNNOVA TE MANAGEMENT III LLC"
    The actual property owner is always the FIRST non-HOA/association name.
    """
    if not party_str:
        return ""
    _SKIP = {"ASSOCIATION", "HOMEOWNERS", "HOA", "MANAGEMENT",
             "UNKNOWN SPOUSE", "UNKNOWN TENANT", "TENANT IN POSSESSION",
             "TRUSTEE", "IN POSSESSION"}
    parts = [p.strip() for p in party_str.split(",") if p.strip()]
    for part in parts:
        if not any(kw in part.upper() for kw in _SKIP):
            return part
    return parts[0] if parts else ""

def revalidate_with_nal(leads):
    """
    Re-run direct/strict matching on every lead using courthouse legal_description.
    If strict/direct returns a better match than what's stored, overwrite property fields.
    Never overwrites courthouse fields (grantor, grantee, legal_description, etc).
    """
    rows = _load_nal_row_index()
    if rows is None:
        log.warning("NAL not available — skipping revalidation pass")
        return leads

    token_idx = _build_row_index(rows)
    log.info("Revalidating %d leads against NAL (%d records)...", len(leads), len(rows))

    updated  = 0
    skipped  = 0
    no_legal = 0

    for lead in leads:
        if not isinstance(lead, dict):
            continue

        legal    = (lead.get("legal_description") or "").strip()
        doc_num  = (lead.get("document_number") or "")

        if not legal:
            no_legal += 1
            continue

        # Filing party for owner confirmation
        filing_party = (lead.get("grantee") or lead.get("grantor") or "").strip()

        result = _strict_match(
            legal, filing_party, rows, token_idx,
            doc_number=doc_num,
            existing_address=(lead.get("property_address") or ""),
            existing_parcel=(lead.get("parcel_id") or ""),
            existing_reason=(lead.get("match_reason") or ""),
        )

        if result is None:
            skipped += 1
            continue

        parcel, addr1, city, state, zipcd, own_name, av, source = result

        # Only update if strict/direct found something
        # Always trust strict/direct over old fuzzy HIGH
        old_confidence = (lead.get("match_confidence") or "").upper()
        old_reason     = (lead.get("match_reason") or "").lower()
        old_is_fuzzy   = ("surname" in old_reason or
                          ("high" == old_confidence and
                           "strict" not in old_reason and
                           "direct" not in old_reason))

        old_parcel = re.sub(r"[-\s]", "", lead.get("parcel_id") or "")
        new_parcel = re.sub(r"[-\s]", "", parcel)

        # Always overwrite with strict/direct result — old HIGH may be from fuzzy
        # The only exception: if old was already strict/direct AND parcels match
        old_is_strict = ("strict_legal" in old_reason or "direct_parcel" in old_reason
                         or "revalidated" in old_reason)
        should_update = (
            not old_parcel          # no existing parcel
            or old_is_fuzzy         # old match was fuzzy
            or not old_is_strict    # old was not strict/direct
            or new_parcel != old_parcel  # strict found different parcel
        )

        # Determine correct courthouse owner from filing party
        # For Lis Pendens/Probate/Death: grantee = defendant = motivated seller
        # For Liens/Judgments: grantor = property owner
        grantee      = (lead.get("grantee") or "").strip()
        grantor      = (lead.get("grantor") or "").strip()
        # Grantee is always the property owner — grantor is always the filer
        courthouse_owner = _primary_party(grantee) or _primary_party(grantor)

        debug = doc_num == "20260267650"
        if debug:
            log.info("[DEBUG %s] overwrite decision: should_update=%s old_is_fuzzy=%s old_is_strict=%s old_parcel=%s new_parcel=%s",
                     doc_num, should_update, old_is_fuzzy,
                     ("strict_legal" in old_reason or "direct_parcel" in old_reason),
                     old_parcel, new_parcel)

        # ALWAYS reset owner_name to courthouse filing party when strict/direct
        # match succeeds — even if address/parcel didn't change.
        # This fixes cases where a bad stacking merge left the wrong owner name
        # on an already-corrected address record.
        if courthouse_owner:
            old_owner = lead.get("owner_name", "")
            if old_owner != courthouse_owner:
                if debug:
                    log.info("[DEBUG %s] owner_name reset (always): old=%r -> new=%r",
                             doc_num, old_owner, courthouse_owner)
                lead["owner_name"] = courthouse_owner

        if should_update:
            prop_addr = f"{addr1}, {city}, {state} {zipcd}".strip()

            if debug:
                log.info("[DEBUG %s] OVERWRITING: old_addr=%r -> new_addr=%r | old_parcel=%s -> new_parcel=%s",
                         doc_num,
                         lead.get("property_address"), prop_addr,
                         lead.get("parcel_id"), parcel)

            # Validate city — NAL occasionally has wrong city (e.g. street name as city)
            # If city isn't a known OC/FL city AND looks like a street name word, default Orlando
            _STREET_TYPES = {'ST','AVE','BLVD','DR','RD','LN','CT','WAY','PL','CIR',
                              'TER','TRL','PKWY','HWY','STREET','AVENUE','BOULEVARD',
                              'DRIVE','ROAD','LANE','COURT','CIRCLE','PLACE','TRAIL'}
            _VALID_OC_CITIES = {
                'ORLANDO','APOPKA','OCOEE','WINTER GARDEN','WINTER PARK','MAITLAND',
                'ALTAMONTE SPRINGS','CASSELBERRY','LONGWOOD','SANFORD','KISSIMMEE',
                'CELEBRATION','WINDERMERE','BELLE ISLE','PINE HILLS','HUNTERS CREEK',
                'DOCTOR PHILLIPS','BUENAVENTURA LAKES','EDGEWOOD','PINE CASTLE',
                'OAK RIDGE','GOLDENROD','EATONVILLE','AZALEA PARK','CONWAY',
                'CLERMONT','MINNEOLA','GROVELAND','DAVENPORT','REUNION','LEESBURG',
                'TAVARES','MOUNT DORA','OAKLAND','GOTHA','CHRISTMAS','ST CLOUD',
                'DELTONA','DELAND','DEBARY','LAKE MARY',
            }
            city_upper = city.upper().strip()
            if city_upper and city_upper not in _VALID_OC_CITIES:
                # Could be a street name word used as city — default to Orlando
                log.warning("Suspicious NAL city %r for parcel %s — defaulting to ORLANDO",
                            city, parcel)
                city = 'Orlando'
                prop_addr = f"{addr1}, {city}, {state} {zipcd}".strip()

            lead["property_address"]        = prop_addr
            lead["prop_street"]             = addr1
            lead["prop_city"]               = city
            lead["prop_state"]              = state
            lead["prop_zip"]                = zipcd
            lead["parcel_id"]               = parcel
            lead["current_ocpa_owner_name"] = own_name
            lead["match_confidence"]        = "HIGH"
            lead["match_reason"]            = f"{source} | revalidated by cleanup"
            lead["needs_enrichment"]        = False
            if av:
                try:
                    av_int = int(av)
                    if av_int > 0:
                        lead["assessed_value"] = f"${av_int:,}"
                except (ValueError, TypeError):
                    pass
            updated += 1

    log.info("Revalidation: %d updated | %d no strict match | %d no legal",
             updated, skipped, no_legal)
    return leads


# ── STACKING ──────────────────────────────────────────────────────────────

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
    """
    reason     = (lead.get("match_reason") or "").lower()
    confidence = (lead.get("match_confidence") or "").upper()
    score      = float(lead.get("seller_score", 0) or 0)
    rank       = _CONFIDENCE_RANK.get(confidence, 0)
    is_direct  = 1 if "direct_parcel" in reason or "direct=" in reason else 0
    is_strict  = 1 if "strict_legal" in reason or "strict=" in reason else 0
    return (is_direct, is_strict, rank, score)


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
                candidate_idx = by_addr[addr]
                existing = output[candidate_idx]
                existing_pid = clean_parcel(existing.get('parcel_id', ''))
                # SAFETY: never merge if both have different valid parcel IDs
                if is_valid_parcel(pid) and is_valid_parcel(existing_pid) and pid != existing_pid:
                    pass  # Different properties — do not merge
                elif is_address_stackable(existing):
                    matched_idx = candidate_idx

        if matched_idx is not None:
            existing = output[matched_idx]
            # CONFIDENCE-FIRST: best match quality wins, score only as tiebreaker
            if _match_quality(lead) > _match_quality(existing):
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

    # ── STEP 3: NAL revalidation — re-run strict/direct matcher ─────────
    log.info("Step 3: NAL revalidation pass...")
    active = revalidate_with_nal(active)
    gc.collect()

    # ── STEP 4: Stack/dedup ───────────────────────────────────────────────
    log.info("Step 4: Stacking/deduplicating %d active leads...", len(active))
    active = stack_by_property(active)
    gc.collect()

    # ── STEP 5: Sort by score ─────────────────────────────────────────────
    active.sort(
        key=lambda l: float(l.get('seller_score', 0) or 0) if isinstance(l, dict) else 0,
        reverse=True
    )

    # ── STEP 6: Save everything ───────────────────────────────────────────
    log.info("Step 6: Saving outputs...")
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
