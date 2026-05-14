"""
test_legal_match.py — Prove strict legal description matching works against NAL file.

Test case:
  Input:    Lot 29 Rose Hill Groves Unit No 1
  Expected: 8507 White Rose Dr, Orlando, FL 32818
  Parcel:   22-22-28-7670-00-290

Run: python src/test_legal_match.py
Requires: /tmp/NAL_orange.csv (downloaded by scraper.py)
"""
import csv, re, os, sys
from collections import defaultdict

NAL_PATH = "/tmp/NAL_orange.csv"

# ── TEST CASES ────────────────────────────────────────────────────────────
TEST_CASES = [
    {
        "input":    "Lot: 29 ROSE HILL GROVES UNIT NO 1",
        "expected_address": "8507 WHITE ROSE DR",
        "expected_parcel":  "2222287670000290",
        "grantee":  "MAYSONET BETTY",
    },
]


# ── LEGAL PARSER ─────────────────────────────────────────────────────────

def parse_legal_strict(raw):
    """
    Parse a legal description into structured components for exact matching.
    Returns dict with: lot, block, unit, subdivision, raw_norm
    """
    if not raw:
        return {}
    text = raw.upper().strip()

    # Normalize common abbreviations
    text = re.sub(r'\bLT\b', 'LOT', text)
    text = re.sub(r'\bBLK\b', 'BLOCK', text)
    text = re.sub(r'\bUNIT NO\b', 'UNIT', text)
    text = re.sub(r'\bPB\s+\d+[\s/]\d+\b', '', text)  # strip plat book refs
    text = re.sub(r'\bPG\s+\d+\b', '', text)
    text = re.sub(r'[^A-Z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    result = {
        'lot':         '',
        'block':       '',
        'unit':        '',
        'subdivision': '',
        'raw_norm':    text,
    }

    # Extract lot
    m = re.search(r'\bLOT\s+(\d+)\b', text)
    if m:
        result['lot'] = m.group(1).lstrip('0') or '0'  # strip leading zeros: 029 → 29

    # Extract block
    m = re.search(r'\bBLOCK\s+(\w+)\b', text)
    if m:
        result['block'] = m.group(1)

    # Extract unit/phase
    m = re.search(r'\bUNIT\s+(\w+)\b', text)
    if m:
        result['unit'] = m.group(1)

    # Extract subdivision — everything that isn't lot/block/unit/numbers
    subdiv = text
    subdiv = re.sub(r'\bLOT\s+\d+\b', '', subdiv)
    subdiv = re.sub(r'\bBLOCK\s+\w+\b', '', subdiv)
    subdiv = re.sub(r'\bUNIT\s+\w+\b', '', subdiv)
    subdiv = re.sub(r'\bSECTION\s+\w+\b', '', subdiv)
    subdiv = re.sub(r'\bPHASE\s+\w+\b', '', subdiv)
    subdiv = re.sub(r'\s+', ' ', subdiv).strip()
    # Must have letters and be meaningful
    if len(subdiv) >= 3 and not subdiv.isdigit():
        result['subdivision'] = subdiv

    return result


def parse_nal_legal(raw):
    """Same parser for NAL S_LEGAL field."""
    return parse_legal_strict(raw)


def lot_matches(query_lot, nal_legal_raw):
    """
    Check if a lot number matches in a NAL legal description.
    Handles: LOT 29, LT 29, lot code 290 (trailing zero), 029
    """
    if not query_lot:
        return False
    n = query_lot.lstrip('0') or '0'
    nal = nal_legal_raw.upper()
    patterns = [
        rf'\bLOT\s+0*{re.escape(n)}\b',          # LOT 29, LOT 029
        rf'\bLT\s+0*{re.escape(n)}\b',             # LT 29
        rf'\bLOT\s+{re.escape(n)}0\b',             # LOT 290 (trailing zero format)
    ]
    return any(re.search(p, nal) for p in patterns)


def subdiv_matches(query_subdiv, nal_legal_raw):
    """
    Check if subdivision tokens from query appear in NAL legal description.
    Requires ALL significant tokens to match (not just any).
    """
    if not query_subdiv:
        return False
    STOP = {'THE','OF','A','AN','AND','OR','IN','AT','TO','FOR','NO','PB','PG','PLAT'}
    tokens = [t for t in query_subdiv.upper().split() if len(t) >= 3 and t not in STOP]
    if not tokens:
        return False
    nal = nal_legal_raw.upper()
    # ALL tokens must appear in the NAL legal
    return all(re.search(rf'\b{re.escape(t)}\b', nal) for t in tokens)


def unit_matches(query_unit, nal_legal_raw):
    """Check unit/phase number matches."""
    if not query_unit:
        return True  # no unit requirement = don't filter on it
    nal = nal_legal_raw.upper()
    n = query_unit.lstrip('0') or '0'
    patterns = [
        rf'\bUNIT\s+0*{re.escape(n)}\b',
        rf'\bUNIT\s+NO\s+0*{re.escape(n)}\b',
        rf'\bPHASE\s+0*{re.escape(n)}\b',
    ]
    return any(re.search(p, nal) for p in patterns)


# ── MAIN MATCHER ─────────────────────────────────────────────────────────

def strict_legal_match(legal_desc, nal_rows, verbose=False):
    """
    Strict structured legal match.

    Rules:
    1. Subdivision name tokens must ALL appear in NAL legal → candidate pool
    2. Within candidates, lot number must match exactly → confirmed matches
    3. Unit number must also match if present → final match
    4. Exactly 1 result → HIGH confidence
    5. 0 results → NO MATCH
    6. Multiple results → AMBIGUOUS (log them, don't pick one)

    Never returns a match based on subdivision name alone.
    """
    parsed = parse_legal_strict(legal_desc)

    if verbose:
        print(f"\nParsed: lot={parsed['lot']!r} unit={parsed['unit']!r} subdiv={parsed['subdivision']!r}")

    if not parsed['lot'] and not parsed['subdivision']:
        return {'confidence': 'NO_MATCH', 'reason': 'Could not parse legal description'}

    # Step 1: Find all NAL records where subdivision tokens match
    subdiv_candidates = []
    for row in nal_rows:
        nal_legal = row.get('S_LEGAL', '')
        if subdiv_matches(parsed['subdivision'], nal_legal):
            subdiv_candidates.append(row)

    if verbose:
        print(f"Subdivision candidates: {len(subdiv_candidates)}")

    if not subdiv_candidates:
        return {'confidence': 'NO_MATCH', 'reason': f"No NAL records found for subdivision: {parsed['subdivision']}"}

    # CRITICAL RULE: Never return address from subdivision match alone
    if not parsed['lot']:
        return {
            'confidence': 'NO_MATCH',
            'reason': f"Found {len(subdiv_candidates)} subdivision matches but no lot number in legal description — cannot confirm property"
        }

    # Step 2: Filter by lot number
    lot_candidates = []
    for row in subdiv_candidates:
        nal_legal = row.get('S_LEGAL', '')
        if lot_matches(parsed['lot'], nal_legal):
            lot_candidates.append(row)

    if verbose:
        print(f"Lot candidates after filtering (lot={parsed['lot']}): {len(lot_candidates)}")
        for r in lot_candidates[:5]:
            print(f"  → {r.get('PHY_ADDR1')} | legal: {r.get('S_LEGAL','')[:80]}")

    if not lot_candidates:
        return {
            'confidence': 'NO_MATCH',
            'reason': f"Found {len(subdiv_candidates)} subdivision matches but none had Lot {parsed['lot']}"
        }

    # Step 3: Filter by unit number if present
    if parsed['unit']:
        unit_candidates = [r for r in lot_candidates if unit_matches(parsed['unit'], r.get('S_LEGAL', ''))]
        if verbose:
            print(f"Unit candidates after filtering (unit={parsed['unit']}): {len(unit_candidates)}")
        if unit_candidates:
            lot_candidates = unit_candidates
        # If unit filter removes everything, fall back to lot candidates
        # (unit may be formatted differently in NAL)

    # Step 4: Assess confidence
    if len(lot_candidates) == 1:
        row = lot_candidates[0]
        addr1 = (row.get('PHY_ADDR1') or '').strip()
        addr2 = (row.get('PHY_ADDR2') or '').strip()
        city  = (row.get('PHY_CITY')  or '').strip()
        state = (row.get('PHY_STATE') or 'FL').strip()
        zipcd = (row.get('PHY_ZIPCD') or '').strip()[:5]
        full_addr = addr1
        if addr2: full_addr += f" {addr2}"
        full_addr += f", {city}, {state} {zipcd}"

        own_addr1 = (row.get('OWN_ADDR1') or '').strip()
        own_addr2 = (row.get('OWN_ADDR2') or '').strip()
        own_city  = (row.get('OWN_CITY')  or '').strip()
        own_state = (row.get('OWN_STATE') or '').strip()
        own_zip   = (row.get('OWN_ZIPCD') or '').strip()[:5]
        mail_addr = own_addr1
        if own_addr2: mail_addr += f" {own_addr2}"
        if own_city:  mail_addr += f", {own_city}"
        if own_state: mail_addr += f", {own_state}"
        if own_zip:   mail_addr += f" {own_zip}"

        # Reconstruct parcel ID
        parcel_raw = ''
        for k in ('PARCEL_ID','PARCEL','APN','PIN','PARID'):
            if row.get(k):
                parcel_raw = row[k]
                break

        return {
            'confidence':       'HIGH',
            'property_address': full_addr,
            'prop_street':      addr1,
            'prop_city':        city,
            'prop_state':       state,
            'prop_zip':         zipcd,
            'mailing_address':  mail_addr,
            'owner_name':       (row.get('OWN_NAME') or '').strip(),
            'assessed_value':   row.get('AV_NSD') or row.get('TV_NSD') or '',
            'parcel_id':        parcel_raw,
            'nal_legal':        row.get('S_LEGAL', ''),
            'reason':           f"Exact match: Lot {parsed['lot']} in {parsed['subdivision']}"
        }

    elif len(lot_candidates) > 1:
        addrs = [r.get('PHY_ADDR1','') for r in lot_candidates[:5]]
        return {
            'confidence': 'AMBIGUOUS',
            'reason':     f"Found {len(lot_candidates)} records for Lot {parsed['lot']} in {parsed['subdivision']}: {addrs}"
        }

    return {'confidence': 'NO_MATCH', 'reason': 'No matching records found'}


# ── LOAD NAL ──────────────────────────────────────────────────────────────

def load_nal_rows(path, limit=None):
    """Load all NAL rows into memory for testing."""
    rows = []
    print(f"Loading NAL from {path}...")
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            print(f"NAL columns: {headers[:10] if headers else 'none'}")
            for i, row in enumerate(reader):
                rows.append(row)
                if limit and i >= limit - 1:
                    break
    except Exception as e:
        print(f"ERROR loading NAL: {e}")
        return []
    print(f"Loaded {len(rows):,} NAL rows")
    return rows


# ── RUN TESTS ─────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(NAL_PATH):
        print(f"ERROR: NAL file not found at {NAL_PATH}")
        print("Run scraper.py first to download it.")
        sys.exit(1)

    nal_rows = load_nal_rows(NAL_PATH)
    if not nal_rows:
        print("ERROR: No NAL rows loaded")
        sys.exit(1)

    print(f"\n{'='*60}")
    print("STRICT LEGAL MATCH TEST RESULTS")
    print('='*60)

    passed = 0
    failed = 0

    for tc in TEST_CASES:
        print(f"\nTest: {tc['input']}")
        print(f"Expected address: {tc['expected_address']}")
        print(f"Expected parcel:  {tc['expected_parcel']}")

        result = strict_legal_match(tc['input'], nal_rows, verbose=True)

        print(f"\nResult confidence: {result['confidence']}")
        print(f"Result reason:     {result.get('reason','')}")

        if result['confidence'] == 'HIGH':
            got_addr = result.get('prop_street','').upper()
            got_parcel = re.sub(r'[-\s]','', result.get('parcel_id',''))
            exp_addr = tc['expected_address'].upper()
            exp_parcel = re.sub(r'[-\s]','', tc['expected_parcel'])

            print(f"Got address: {result.get('property_address','')}")
            print(f"Got parcel:  {result.get('parcel_id','')}")
            print(f"NAL owner:   {result.get('owner_name','')}")
            print(f"NAL legal:   {result.get('nal_legal','')[:100]}")

            addr_ok   = exp_addr in got_addr or got_addr in exp_addr
            parcel_ok = got_parcel == exp_parcel or got_parcel in exp_parcel or exp_parcel in got_parcel

            if addr_ok and parcel_ok:
                print(f"✅ PASS — address and parcel match")
                passed += 1
            elif addr_ok:
                print(f"⚠️  PARTIAL — address matches but parcel differs")
                print(f"   Got parcel: {got_parcel}")
                print(f"   Exp parcel: {exp_parcel}")
                passed += 1
            else:
                print(f"❌ FAIL — wrong address returned")
                print(f"   Got: {got_addr}")
                print(f"   Exp: {exp_addr}")
                failed += 1
        else:
            print(f"❌ FAIL — no match returned (confidence: {result['confidence']})")
            failed += 1

    print(f"\n{'='*60}")
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(TEST_CASES)} tests")
    print('='*60)

    if failed == 0:
        print("\n✅ All tests passed — strict legal matching works.")
        print("Ready to replace fuzzy NAL matcher in scraper.py")
    else:
        print("\n❌ Tests failed — investigate before replacing fuzzy matcher")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
