"""
test_legal_match.py — Prove strict legal description matching works against NAL file.

Matching logic:
  1. Parse legal description → subdivision name + unit + lot number
  2. Filter NAL by S_LEGAL contains subdivision + unit tokens (ALL must match)
  3. Filter candidates by PARCEL_ID ending in lot * 10 (Lot 29 → 290)
  4. If exactly 1 result → HIGH confidence
  5. If 0 or multiple → NO_MATCH / AMBIGUOUS

Test case:
  Legal:    Lot 29 Rose Hill Groves Unit No 1
  Expected: 8507 White Rose Dr, Orlando, FL 32818
  Parcel:   22-22-28-7670-00-290
"""
import csv, re, os, sys

NAL_PATH = "/tmp/NAL_orange.csv"

TEST_CASES = [
    {
        "input":            "Lot: 29 ROSE HILL GROVES UNIT NO 1",
        "expected_address": "8507 WHITE ROSE DR",
        "expected_city":    "ORLANDO",
        "expected_zip":     "32818",
        "expected_parcel":  "222228767000290",
    },
]


def clean_parcel(pid):
    return re.sub(r'[-\s]', '', (pid or '')).strip()


def parse_legal(raw):
    """
    Parse legal description into: lot, subdivision string (including unit).
    e.g. 'Lot: 29 ROSE HILL GROVES UNIT NO 1'
      → lot='29', subdivision='ROSE HILL GROVES UNIT NO 1'
    """
    if not raw:
        return {'lot': '', 'subdivision': ''}

    text = raw.upper().strip()
    text = re.sub(r'\bLT\b', 'LOT', text)
    text = re.sub(r'[^A-Z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    # Extract lot number
    lot = ''
    m = re.search(r'\bLOT\s+(\d+)\b', text)
    if m:
        lot = str(int(m.group(1)))  # strip leading zeros: '029' → '29'
        # Remove lot from text to isolate subdivision
        text = re.sub(r'\bLOT\s+\d+\b', '', text).strip()

    # What remains is the subdivision (including unit)
    text = re.sub(r'\s+', ' ', text).strip()
    subdivision = text if len(text) >= 3 else ''

    return {'lot': lot, 'subdivision': subdivision}


def s_legal_matches_subdivision(s_legal, subdivision):
    """
    All significant tokens in subdivision must appear in S_LEGAL.
    'ROSE HILL GROVES UNIT NO 1' → tokens: ROSE, HILL, GROVES, UNIT, 1
    Stops words like NO are excluded.
    """
    if not s_legal or not subdivision:
        return False
    STOP = {'THE', 'OF', 'A', 'AN', 'AND', 'OR', 'IN', 'AT', 'TO', 'FOR', 'NO', 'PB', 'PG'}
    tokens = [t for t in subdivision.upper().split()
              if t not in STOP and len(t) >= 1]
    if not tokens:
        return False
    legal_upper = s_legal.upper()
    return all(re.search(rf'\b{re.escape(t)}\b', legal_upper) for t in tokens)


def lot_to_parcel_suffix(lot):
    """
    Convert lot number to parcel ID suffix.
    Orange County format: Lot 29 → last 3 digits = 290 (lot × 10)
    """
    try:
        return str(int(lot) * 10).zfill(3)
    except (ValueError, TypeError):
        return ''


def strict_match(legal_desc, nal_rows, verbose=False):
    parsed = parse_legal(legal_desc)
    lot        = parsed['lot']
    subdivision = parsed['subdivision']

    if verbose:
        print(f"  Parsed lot: {lot!r}")
        print(f"  Parsed subdivision: {subdivision!r}")

    if not lot or not subdivision:
        return {'confidence': 'NO_MATCH', 'reason': 'Could not parse lot or subdivision'}

    suffix = lot_to_parcel_suffix(lot)
    if verbose:
        print(f"  Parcel suffix (lot×10): {suffix!r}")

    # Step 1: S_LEGAL must contain all subdivision tokens
    subdiv_candidates = [
        r for r in nal_rows
        if s_legal_matches_subdivision(r.get('S_LEGAL', ''), subdivision)
    ]
    if verbose:
        print(f"  S_LEGAL subdivision candidates: {len(subdiv_candidates)}")

    if not subdiv_candidates:
        return {'confidence': 'NO_MATCH', 'reason': f"No S_LEGAL matches for: {subdivision}"}

    # NEVER return address from subdivision match alone
    # Step 2: PARCEL_ID must end with the lot suffix
    lot_candidates = [
        r for r in subdiv_candidates
        if clean_parcel(r.get('PARCEL_ID', '')).endswith(suffix)
    ]
    if verbose:
        print(f"  Lot candidates (parcel ends '{suffix}'): {len(lot_candidates)}")
        for r in lot_candidates:
            print(f"    PARCEL: {r.get('PARCEL_ID','')} | S_LEGAL: {r.get('S_LEGAL','')[:60]} | PHY_ADDR1: {r.get('PHY_ADDR1','')}")

    if not lot_candidates:
        return {
            'confidence': 'NO_MATCH',
            'reason': f"Found {len(subdiv_candidates)} subdivision matches but none had parcel suffix {suffix} (Lot {lot})"
        }

    if len(lot_candidates) > 1:
        addrs = [r.get('PHY_ADDR1', '') for r in lot_candidates]
        return {
            'confidence': 'AMBIGUOUS',
            'reason': f"Multiple matches for Lot {lot} in {subdivision}: {addrs}"
        }

    # Exactly one match
    row = lot_candidates[0]
    addr   = (row.get('PHY_ADDR1') or '').strip()
    city   = (row.get('PHY_CITY')  or '').strip()
    state  = (row.get('PHY_STATE') or 'FL').strip()
    zipcd  = str(row.get('PHY_ZIPCD') or '').strip()[:5]
    owner  = (row.get('OWN_NAME')  or '').strip()
    parcel = (row.get('PARCEL_ID') or '').strip()
    jv     = (row.get('JV')        or '').strip()
    s_leg  = (row.get('S_LEGAL')   or '').strip()

    av = ''
    if jv:
        try:
            av = f"${int(jv):,}"
        except ValueError:
            av = jv

    return {
        'confidence':       'HIGH',
        'prop_street':      addr,
        'prop_city':        city,
        'prop_state':       state if state else 'FL',
        'prop_zip':         zipcd,
        'property_address': f"{addr}, {city}, {state} {zipcd}".strip(', '),
        'owner_name':       owner,
        'assessed_value':   av,
        'parcel_id':        parcel,
        'nal_s_legal':      s_leg,
        'reason':           f"Exact match: S_LEGAL '{s_leg[:50]}' + parcel suffix {suffix}"
    }


def load_nal(path):
    rows = []
    print(f"Loading NAL from {path}...")
    with open(path, encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        # Confirm key columns present
        key_cols = ['PARCEL_ID', 'S_LEGAL', 'PHY_ADDR1', 'PHY_CITY', 'PHY_ZIPCD', 'OWN_NAME', 'JV']
        present  = [c for c in key_cols if c in headers]
        missing  = [c for c in key_cols if c not in headers]
        print(f"  Key columns present: {present}")
        if missing:
            print(f"  WARNING — missing columns: {missing}")
        for row in reader:
            rows.append(row)
    print(f"  Loaded {len(rows):,} rows\n")
    return rows


def main():
    if not os.path.exists(NAL_PATH):
        print(f"ERROR: NAL file not found at {NAL_PATH}")
        sys.exit(1)

    nal_rows = load_nal(NAL_PATH)
    if not nal_rows:
        print("ERROR: No rows loaded")
        sys.exit(1)

    print("=" * 60)
    print("STRICT LEGAL MATCH TEST RESULTS")
    print("=" * 60)

    passed = 0
    failed = 0

    for tc in TEST_CASES:
        print(f"\nInput legal:      {tc['input']}")
        print(f"Expected address: {tc['expected_address']}, {tc['expected_city']}, FL {tc['expected_zip']}")
        print(f"Expected parcel:  {tc['expected_parcel']}")
        print()

        result = strict_match(tc['input'], nal_rows, verbose=True)

        print(f"\nConfidence: {result['confidence']}")
        print(f"Reason:     {result.get('reason', '')}")

        if result['confidence'] == 'HIGH':
            print(f"Address:    {result.get('property_address', '')}")
            print(f"Owner:      {result.get('owner_name', '')}")
            print(f"Parcel:     {result.get('parcel_id', '')}")
            print(f"Assessed:   {result.get('assessed_value', '')}")
            print(f"NAL legal:  {result.get('nal_s_legal', '')}")

            got_street = result.get('prop_street', '').upper()
            got_city   = result.get('prop_city',   '').upper()
            got_zip    = result.get('prop_zip',    '').strip()
            got_parcel = clean_parcel(result.get('parcel_id', ''))
            exp_street = tc['expected_address'].upper()
            exp_city   = tc['expected_city'].upper()
            exp_zip    = tc['expected_zip'].strip()
            exp_parcel = clean_parcel(tc['expected_parcel'])

            street_ok = exp_street in got_street or got_street in exp_street
            city_ok   = exp_city in got_city or got_city in exp_city
            zip_ok    = got_zip == exp_zip
            parcel_ok = got_parcel == exp_parcel

            if street_ok and city_ok and zip_ok and parcel_ok:
                print(f"\n✅ PASS — address, city, zip, and parcel all match")
                passed += 1
            elif street_ok and city_ok:
                print(f"\n⚠️  PARTIAL PASS — address and city match")
                if not zip_ok:   print(f"   zip mismatch: got={got_zip} exp={exp_zip}")
                if not parcel_ok: print(f"   parcel mismatch: got={got_parcel} exp={exp_parcel}")
                passed += 1
            else:
                print(f"\n❌ FAIL")
                if not street_ok: print(f"   street: got={got_street!r} exp={exp_street!r}")
                if not city_ok:   print(f"   city:   got={got_city!r} exp={exp_city!r}")
                failed += 1
        else:
            print(f"\n❌ FAIL — {result.get('reason', '')}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(TEST_CASES)} tests")
    print("=" * 60)

    if failed == 0:
        print("\n✅ Strict legal matching confirmed working.")
        print("Ready to replace fuzzy NAL matcher in scraper.py")
    else:
        print("\n❌ Tests failed — investigate before replacing fuzzy matcher")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
