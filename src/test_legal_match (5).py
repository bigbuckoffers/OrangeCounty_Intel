"""
test_legal_match.py — Diagnostic: Can we resolve legal description to address via NAL parcel ID?

Test case:
  Legal:    Lot 29 Rose Hill Groves Unit No 1
  Expected: 8507 White Rose Dr, Orlando, FL 32818
  Parcel:   22-22-28-7670-00-290
  Clean:    222228767000290

NAL columns: CO_NO, PARCEL_ID, FILE_T, ASMNT_YR, BAS_STRT, ATV_STRT, GRP_NO, DOR_UC, PA_UC, SPASS_CD
"""
import csv, re, os, sys

NAL_PATH = "/tmp/NAL_orange.csv"
TARGET_PARCEL = "222228767000290"
SUBDIV_TOKENS = ["ROSE", "HILL", "GROVES"]


def clean_parcel(pid):
    return re.sub(r'[-\s]', '', (pid or '')).strip()


def row_contains_tokens(row, tokens):
    haystack = ' '.join(str(v) for v in row.values()).upper()
    return all(t in haystack for t in tokens)


def main():
    if not os.path.exists(NAL_PATH):
        print(f"ERROR: NAL file not found at {NAL_PATH}")
        sys.exit(1)

    print("Loading NAL...")
    nal_rows = []
    with open(NAL_PATH, encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        print(f"All NAL columns ({len(headers)}): {headers}")
        for row in reader:
            nal_rows.append(row)
    print(f"Loaded {len(nal_rows):,} rows\n")

    # Step 1: Direct parcel lookup
    print("=" * 60)
    print(f"STEP 1: Direct parcel lookup for {TARGET_PARCEL}")
    print("=" * 60)
    direct = [r for r in nal_rows if clean_parcel(r.get('PARCEL_ID', '')) == TARGET_PARCEL]
    print(f"Exact match count: {len(direct)}")
    for r in direct:
        print(f"FULL ROW: {dict(r)}")

    # Step 2: Subdivision candidates
    print(f"\n{'=' * 60}")
    print(f"STEP 2: Find Rose Hill Groves candidates")
    print("=" * 60)
    candidates = [r for r in nal_rows if row_contains_tokens(r, SUBDIV_TOKENS)]
    print(f"Candidates with ROSE + HILL + GROVES: {len(candidates)}")

    # Step 3: Print all candidates with parcel breakdown
    print(f"\n{'=' * 60}")
    print(f"STEP 3: Parcel ID structure for all {len(candidates)} candidates")
    print("=" * 60)
    print(f"{'RAW PARCEL':<25} {'CLEAN':<18} {'LAST4':<6} {'LAST3':<5} {'BAS_STRT':<35} {'ATV_STRT':<20} {'GRP_NO'}")
    print("-" * 120)
    for r in candidates:
        raw   = r.get('PARCEL_ID', '')
        clean = clean_parcel(raw)
        last3 = clean[-3:] if len(clean) >= 3 else '?'
        last4 = clean[-4:] if len(clean) >= 4 else '?'
        bas   = r.get('BAS_STRT', '')
        atv   = r.get('ATV_STRT', '')
        grp   = r.get('GRP_NO', '')
        print(f"{raw:<25} {clean:<18} {last4:<6} {last3:<5} {bas:<35} {atv:<20} {grp}")

    # Step 4: Targeted suffix searches
    print(f"\n{'=' * 60}")
    print(f"STEP 4: Suffix searches within candidates")
    print("=" * 60)
    for suffix in ["767000290", "00290", "290", "0290"]:
        matches = [r for r in candidates if clean_parcel(r.get('PARCEL_ID', '')).endswith(suffix)]
        print(f"Ends with '{suffix}': {len(matches)}")
        for r in matches:
            print(f"  PARCEL: {r.get('PARCEL_ID','')} | BAS_STRT: {r.get('BAS_STRT','')} | ATV_STRT: {r.get('ATV_STRT','')}")

    # Step 5: Nearest lot neighbors
    print(f"\n{'=' * 60}")
    print(f"STEP 5: Nearest neighbors ending in 280/290/300/028/029/030")
    print("=" * 60)
    for suffix in ["280", "290", "300", "029", "030", "028"]:
        matches = [r for r in candidates if clean_parcel(r.get('PARCEL_ID', '')).endswith(suffix)]
        for r in matches:
            print(f"  ends '{suffix}' → PARCEL: {r.get('PARCEL_ID','')} | BAS_STRT: {r.get('BAS_STRT','')} | ATV_STRT: {r.get('ATV_STRT','')}")

    print("\nDone.")
    sys.exit(0)


if __name__ == "__main__":
    main()
