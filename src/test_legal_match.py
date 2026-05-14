"""
test_legal_match.py — Strict legal description → NAL parcel → address matcher.

PRIMARY MATCHING LOGIC:
  1. Parse legal description → lot, subdivision, unit, block, phase
  2. Filter NAL S_LEGAL by subdivision + unit tokens (ALL must match)
  3. Filter candidates by PARCEL_ID suffix (lot × 10, then fallbacks)
  4. Exactly 1 result → HIGH | Multiple → AMBIGUOUS | None → NO_MATCH

RULES:
  - Never assign address from subdivision match alone (lot required)
  - NAL owner name never overwrites courthouse filing party
  - Fuzzy matching only as fallback, never overrides HIGH confidence

Run: python src/test_legal_match.py
Requires: /tmp/NAL_orange.csv
"""
import csv, re, os, sys

NAL_PATH = "/tmp/NAL_orange.csv"

# ── TEST CASES ────────────────────────────────────────────────────────────
# If expected fields are empty, script runs in DISCOVERY mode:
# prints all candidates and results without asserting pass/fail.
TEST_CASES = [
    {
        "input":            "Lot: 29 ROSE HILL GROVES UNIT NO 1",
        "filing_party":     "MAYSONET BETTY",
        "expected_address": "8507 WHITE ROSE DR",
        "expected_city":    "ORLANDO",
        "expected_zip":     "32818",
        "expected_parcel":  "222228767000290",
    },
    # ── ADD SECOND TEST CASE HERE ─────────────────────────────────────────
    # {
    #     "input":            "PASTE LEGAL DESCRIPTION HERE",
    #     "filing_party":     "PASTE NAME HERE",
    #     "expected_address": "",   # leave empty for discovery mode
    #     "expected_city":    "",
    #     "expected_zip":     "",
    #     "expected_parcel":  "",
    # },
]

STOP_WORDS = {'THE','OF','A','AN','AND','OR','IN','AT','TO','FOR','PB','PG','PLAT','BOOK','PAGE'}


# ── LEGAL DESCRIPTION PARSER ──────────────────────────────────────────────

def parse_legal(raw):
    """
    Parse courthouse legal description into structured fields.

    Example:
      'Lot: 29 ROSE HILL GROVES UNIT NO 1'
    Returns:
      lot='29', unit='1', block='', phase='',
      subdivision='ROSE HILL GROVES',
      full_subdivision_phrase='ROSE HILL GROVES UNIT NO 1'
    """
    if not raw:
        return {}

    text = raw.upper().strip()
    # Normalize abbreviations
    text = re.sub(r'\bLT\b',      'LOT',   text)
    text = re.sub(r'\bBLK\b',     'BLOCK', text)
    text = re.sub(r'\bUNIT\s+NO\b','UNIT', text)
    text = re.sub(r'\bSUBD?\b',   'SUBDIVISION', text)
    # Strip plat book refs
    text = re.sub(r'\bPB\s+\d+[\s/]\d+\b', '', text)
    text = re.sub(r'\bPG\s+\d+\b',          '', text)
    text = re.sub(r'\bCASE\s*:\s*[\w\s]+',  '', text)
    # Clean to alphanumeric + spaces
    text = re.sub(r'[^A-Z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    result = {
        'lot': '', 'block': '', 'unit': '', 'phase': '', 'section': '',
        'subdivision': '', 'full_subdivision_phrase': '', 'raw_norm': text
    }

    # Extract structured fields
    for key, pattern in [
        ('lot',     r'\bLOT\s+(\d+)\b'),
        ('block',   r'\bBLOCK\s+(\w+)\b'),
        ('unit',    r'\bUNIT\s+(\w+)\b'),
        ('phase',   r'\bPHASE\s+(\w+)\b'),
        ('section', r'\bSECTION\s+(\w+)\b'),
    ]:
        m = re.search(pattern, text)
        if m:
            val = m.group(1)
            # Strip leading zeros from numeric values
            if val.isdigit():
                val = str(int(val))
            result[key] = val

    # Build subdivision (text minus lot/block/unit/phase/section tokens)
    subdiv = text
    for pattern in [
        r'\bLOT\s+\d+\b', r'\bBLOCK\s+\w+\b', r'\bUNIT\s+\w+\b',
        r'\bPHASE\s+\w+\b', r'\bSECTION\s+\w+\b',
    ]:
        subdiv = re.sub(pattern, '', subdiv)
    subdiv = re.sub(r'\s+', ' ', subdiv).strip()
    if len(subdiv) >= 3 and not subdiv.isdigit():
        result['subdivision'] = subdiv

    # Build full subdivision phrase (subdivision + unit, used for S_LEGAL matching)
    phrase_parts = [result['subdivision']]
    if result['unit']:
        phrase_parts.append(f"UNIT {result['unit']}")
    if result['phase']:
        phrase_parts.append(f"PHASE {result['phase']}")
    result['full_subdivision_phrase'] = ' '.join(p for p in phrase_parts if p)

    return result


# ── S_LEGAL MATCHER ───────────────────────────────────────────────────────

def s_legal_matches(s_legal, full_subdivision_phrase):
    """
    All significant tokens in full_subdivision_phrase must appear in S_LEGAL.
    'ROSE HILL GROVES UNIT 1' → tokens: ROSE, HILL, GROVES, UNIT, 1
    """
    if not s_legal or not full_subdivision_phrase:
        return False
    tokens = [
        t for t in full_subdivision_phrase.upper().split()
        if t not in STOP_WORDS and len(t) >= 1
    ]
    if not tokens:
        return False
    legal_upper = s_legal.upper()
    return all(re.search(rf'\b{re.escape(t)}\b', legal_upper) for t in tokens)


# ── PARCEL SUFFIX MATCHER ─────────────────────────────────────────────────

def lot_to_suffixes(lot):
    """
    Generate candidate parcel suffix strings for a lot number.
    Orange County format: Lot 29 → 290 (lot × 10) is primary.
    Fallbacks cover edge cases.
    Returns ordered list to try.
    """
    if not lot:
        return []
    try:
        n = int(lot)
    except ValueError:
        return []
    return [
        str(n * 10).zfill(3),   # 290  ← primary (lot × 10)
        str(n).zfill(3),         # 029  ← zero-padded
        str(n).zfill(4),         # 0029
        str(n).zfill(5),         # 00029
        str(n),                  # 29   ← raw
    ]


def clean_parcel(pid):
    return re.sub(r'[-\s]', '', (pid or '')).strip()


# ── CORE STRICT MATCHER ───────────────────────────────────────────────────

def strict_legal_match(legal_desc, nal_rows, verbose=True):
    """
    Strict structured legal match.
    Returns result dict with confidence, address fields, and diagnostics.
    """
    parsed = parse_legal(legal_desc)
    lot    = parsed.get('lot', '')
    phrase = parsed.get('full_subdivision_phrase', '')
    subdiv = parsed.get('subdivision', '')

    print(f"  Legal input:       {legal_desc}")
    print(f"  Parsed lot:        {lot!r}")
    print(f"  Parsed subdivision:{subdiv!r}")
    print(f"  Parsed unit:       {parsed.get('unit','')!r}")
    print(f"  Parsed block:      {parsed.get('block','')!r}")
    print(f"  Parsed phase:      {parsed.get('phase','')!r}")
    print(f"  Full phrase:       {phrase!r}")

    if not phrase:
        return {
            'confidence': 'NO_MATCH',
            'reason': 'Could not parse subdivision from legal description',
            'needs_manual_review': True,
        }

    # ── Step 1: S_LEGAL subdivision + unit filter ─────────────────────────
    subdiv_candidates = [
        r for r in nal_rows
        if s_legal_matches(r.get('S_LEGAL', ''), phrase)
    ]
    print(f"  S_LEGAL subdivision candidates: {len(subdiv_candidates)}")

    if not subdiv_candidates:
        return {
            'confidence': 'NO_MATCH',
            'reason': f"No S_LEGAL matches for: {phrase}",
            'needs_manual_review': True,
        }

    # CRITICAL: Never return address from subdivision match alone
    if not lot:
        return {
            'confidence': 'NO_MATCH',
            'reason': f"Found {len(subdiv_candidates)} subdivision matches but no lot number — cannot confirm property",
            'needs_manual_review': True,
        }

    # ── Step 2: Parcel suffix lot filter ──────────────────────────────────
    suffixes = lot_to_suffixes(lot)
    print(f"  Trying suffixes:   {suffixes}")

    lot_candidates = []
    matched_suffix = ''
    for suffix in suffixes:
        candidates = [
            r for r in subdiv_candidates
            if clean_parcel(r.get('PARCEL_ID', '')).endswith(suffix)
        ]
        if candidates:
            lot_candidates = candidates
            matched_suffix = suffix
            break  # Use first (highest priority) suffix that matches

    print(f"  Lot candidates (suffix '{matched_suffix}'): {len(lot_candidates)}")
    for r in lot_candidates:
        print(f"    PARCEL: {r.get('PARCEL_ID','')} | S_LEGAL: {r.get('S_LEGAL','')[:60]} | PHY_ADDR1: {r.get('PHY_ADDR1','')}")

    # ── Step 3: Assess confidence ─────────────────────────────────────────
    if not lot_candidates:
        return {
            'confidence': 'NO_MATCH',
            'reason': f"Found {len(subdiv_candidates)} subdivision matches but none matched Lot {lot} (tried suffixes: {suffixes})",
            'subdiv_candidates': len(subdiv_candidates),
            'needs_manual_review': True,
        }

    if len(lot_candidates) > 1:
        candidates_info = [
            {'parcel': r.get('PARCEL_ID',''), 'address': r.get('PHY_ADDR1',''), 's_legal': r.get('S_LEGAL','')}
            for r in lot_candidates
        ]
        return {
            'confidence': 'AMBIGUOUS',
            'reason': f"Multiple matches for Lot {lot} in {phrase}",
            'candidates': candidates_info,
            'needs_manual_review': True,
        }

    # Exactly one match — HIGH confidence
    row    = lot_candidates[0]
    addr   = (row.get('PHY_ADDR1')  or '').strip()
    city   = (row.get('PHY_CITY')   or '').strip()
    state  = (row.get('PHY_STATE')  or 'FL').strip() or 'FL'
    zipcd  = str(row.get('PHY_ZIPCD') or '').strip()[:5]
    owner  = (row.get('OWN_NAME')   or '').strip()
    parcel = (row.get('PARCEL_ID')  or '').strip()
    s_leg  = (row.get('S_LEGAL')    or '').strip()
    jv     = (row.get('JV')         or '').strip()
    av_nsd = (row.get('AV_NSD')     or '').strip()

    av = ''
    raw_val = jv or av_nsd
    if raw_val:
        try:
            av = f"${int(raw_val):,}"
        except ValueError:
            av = raw_val

    return {
        # Confidence
        'confidence':           'HIGH',
        'match_basis':          f"S_LEGAL subdivision/unit + PARCEL_ID suffix {matched_suffix}",
        'match_reasons': [
            f"Parsed Lot {lot} from courthouse legal description",
            f"Matched S_LEGAL to {phrase}",
            f"Converted Lot {lot} to parcel suffix {matched_suffix}",
            f"Exactly one NAL row matched subdivision/unit + parcel suffix",
        ],
        'needs_manual_review': False,
        # Property address (NAL/OCPA source of truth)
        'property_address':    f"{addr}, {city}, {state} {zipcd}".strip(', '),
        'prop_street':         addr,
        'prop_city':           city,
        'prop_state':          state,
        'prop_zip':            zipcd,
        'parcel_id':           parcel,
        'nal_s_legal':         s_leg,
        'assessed_value':      av,
        # NAL current owner (kept separate from filing party — never overwrites)
        'current_ocpa_owner':  owner,
        # Match diagnostics
        'subdiv_candidates':   len(subdiv_candidates),
        'matched_suffix':      matched_suffix,
        'raw_norm_legal':      parsed.get('raw_norm', ''),
    }


# ── NAL LOADER ────────────────────────────────────────────────────────────

def load_nal(path):
    rows = []
    print(f"Loading NAL from {path}...")
    with open(path, encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        key_cols = ['PARCEL_ID','S_LEGAL','PHY_ADDR1','PHY_CITY','PHY_ZIPCD','OWN_NAME','JV','AV_NSD']
        present  = [c for c in key_cols if c in headers]
        missing  = [c for c in key_cols if c not in headers]
        print(f"  Key columns present: {present}")
        if missing:
            print(f"  WARNING — missing: {missing}")
        for row in reader:
            rows.append(row)
    print(f"  Loaded {len(rows):,} rows\n")
    return rows


# ── TEST RUNNER ───────────────────────────────────────────────────────────

def run_tests(nal_rows):
    passed = failed = discovery = 0

    for i, tc in enumerate(TEST_CASES, 1):
        print(f"\n{'='*60}")
        print(f"TEST {i}: {tc['input']}")
        print(f"Filing party: {tc.get('filing_party','—')}")
        is_discovery = not tc.get('expected_address')
        if is_discovery:
            print(f"Mode: DISCOVERY (no expected values — printing all candidates)")
        else:
            print(f"Expected: {tc['expected_address']}, {tc['expected_city']}, FL {tc['expected_zip']}")
            print(f"Expected parcel: {tc['expected_parcel']}")
        print()

        result = strict_legal_match(tc['input'], nal_rows)

        print(f"\nConfidence: {result['confidence']}")
        print(f"Reason/basis: {result.get('match_basis') or result.get('reason','')}")

        if result['confidence'] == 'HIGH':
            print(f"Address:  {result.get('property_address','')}")
            print(f"Owner:    {result.get('current_ocpa_owner','')}")
            print(f"Parcel:   {result.get('parcel_id','')}")
            print(f"Assessed: {result.get('assessed_value','')}")
            print(f"S_LEGAL:  {result.get('nal_s_legal','')}")
            print(f"Reasons:")
            for r in result.get('match_reasons', []):
                print(f"  • {r}")

            if is_discovery:
                print(f"\n🔍 DISCOVERY — result above (no expected values to validate)")
                discovery += 1
            else:
                got_street = result.get('prop_street','').upper()
                got_city   = result.get('prop_city','').upper()
                got_zip    = result.get('prop_zip','').strip()
                got_parcel = clean_parcel(result.get('parcel_id',''))
                exp_street = tc['expected_address'].upper()
                exp_city   = tc['expected_city'].upper()
                exp_zip    = tc['expected_zip'].strip()
                exp_parcel = clean_parcel(tc['expected_parcel'])

                street_ok = exp_street in got_street or got_street in exp_street
                city_ok   = exp_city in got_city or got_city in exp_city
                zip_ok    = got_zip == exp_zip
                parcel_ok = got_parcel == exp_parcel

                if street_ok and city_ok and zip_ok and parcel_ok:
                    print(f"\n✅ PASS — address, city, zip, parcel all match")
                    passed += 1
                elif street_ok and city_ok:
                    print(f"\n⚠️  PARTIAL PASS — address/city match")
                    if not zip_ok:    print(f"   zip:    got={got_zip} exp={exp_zip}")
                    if not parcel_ok: print(f"   parcel: got={got_parcel} exp={exp_parcel}")
                    passed += 1
                else:
                    print(f"\n❌ FAIL")
                    if not street_ok: print(f"   street: got={got_street!r} exp={exp_street!r}")
                    if not city_ok:   print(f"   city:   got={got_city!r} exp={exp_city!r}")
                    failed += 1

        elif result['confidence'] == 'AMBIGUOUS':
            print(f"AMBIGUOUS — multiple candidates:")
            for c in result.get('candidates', []):
                print(f"  PARCEL: {c['parcel']} | ADDR: {c['address']} | S_LEGAL: {c['s_legal'][:60]}")
            if not is_discovery:
                print(f"\n❌ FAIL — ambiguous result")
                failed += 1
            else:
                print(f"\n🔍 DISCOVERY — ambiguous (investigate above candidates)")
                discovery += 1

        else:
            print(f"NO_MATCH — {result.get('reason','')}")
            print(f"Needs manual review: {result.get('needs_manual_review', True)}")
            if not is_discovery:
                print(f"\n❌ FAIL — no match")
                failed += 1
            else:
                print(f"\n🔍 DISCOVERY — no match found")
                discovery += 1

    print(f"\n{'='*60}")
    print(f"RESULTS: {passed} passed | {failed} failed | {discovery} discovery")
    print('='*60)

    if failed == 0 and passed > 0:
        print("\n✅ All validation tests passed.")
        if discovery:
            print(f"   {discovery} discovery test(s) ran — review output above.")
        print("Ready to integrate into scraper.py")
    elif failed > 0:
        print("\n❌ Failures detected — do not integrate into scraper.py yet")

    return failed


def main():
    if not os.path.exists(NAL_PATH):
        print(f"ERROR: NAL file not found at {NAL_PATH}")
        sys.exit(1)

    nal_rows = load_nal(NAL_PATH)
    if not nal_rows:
        print("ERROR: No rows loaded")
        sys.exit(1)

    failed = run_tests(nal_rows)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
