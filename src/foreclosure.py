"""
foreclosure.py — Orange County FL Foreclosure Auction Scraper
Source: myorangeclerk.realforeclose.com

Page structure (confirmed from live HTML):
  generic "Auction Details"
    generic "Auction Starts" / generic "05/01/2026 11:00 AM ET"
    generic "Case #:" / link "2024-CA-006165-O"
    generic "Final Judgment Amount:" / generic "$184,647.91"
    generic "Parcel ID:" / link "312218022401890" href="https://ocpaweb.ocpafl.org/..."
    generic "Property Address:" / generic "2533 BRAMPTON CT" / generic "ORLANDO, 32817"
    generic "Assessed Value:" / generic "$222,099.00"

Workflow:
  1. Check calendar for dates with auctions (next 90 days)
  2. For each date fetch PREVIEW page and parse all "Auction Details" blocks
  3. Enrich each listing from OCPA ArcGIS using parcel ID
  4. Classify: ACTIVE (3+ days), TOO_SOON (<3 days), EXPIRED (past)
  5. Cross-reference with existing leads by parcel ID or address
  6. Save to data/foreclosures.json and data/foreclosures.csv
"""
import json, logging, os, csv, re, time, requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_URL     = "https://myorangeclerk.realforeclose.com"
CALENDAR_URL = f"{BASE_URL}/index.cfm"
OCPA_API_URL = (
    "https://vgispublic.ocpafl.org/server/rest/services"
    "/DYNAMIC/Dynamic_Parcels/MapServer/0/query"
)
OCPA_WEB_URL = "https://ocpaweb.ocpafl.org/parcelsearch/Parcel%20ID/{}"

OUTPUT_PATH = "data/foreclosures.json"
CSV_PATH    = "data/foreclosures.csv"
LEADS_PATH  = "data/output.json"

MIN_DAYS_AHEAD = 3
DAYS_AHEAD     = 90

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection":      "keep-alive",
    "Referer":         BASE_URL,
}


# ---------------------------------------------------------------------------
# Step 1 — Calendar: find dates that have auctions
# ---------------------------------------------------------------------------

def fetch_auction_dates(session, days_ahead=90):
    """
    Returns a set of dates that have foreclosure auctions.
    Checks the calendar month by month.
    Also always checks next 14 days directly as safety net.
    """
    today = datetime.today().date()
    auction_dates = set()

    # Always check next 14 days directly
    for i in range(14):
        auction_dates.add(today + timedelta(days=i))

    # Check calendar month by month
    months = set()
    for i in range(days_ahead):
        d = today + timedelta(days=i)
        months.add((d.year, d.month))

    for year, month in sorted(months):
        params = {
            "zaction":    "user",
            "zmethod":    "calendar",
            "selCalDate": f"{{ts '{year}-{month:02d}-01 00:00:00'}}",
        }
        try:
            resp = session.get(CALENDAR_URL, params=params, timeout=30)
            if resp.status_code == 200:
                dates = parse_calendar_html(resp.text, year, month, today, days_ahead)
                auction_dates.update(dates)
                log.info("Calendar %d-%02d: found %d auction dates", year, month, len(dates))
        except Exception as e:
            log.error("Calendar fetch failed for %d-%02d: %s", year, month, e)
        time.sleep(1)

    # Filter to only dates within our window
    return {d for d in auction_dates if (d - today).days <= days_ahead}


def parse_calendar_html(html, year, month, today, days_ahead):
    """Parse calendar HTML to find days with FC (foreclosure) auctions."""
    soup = BeautifulSoup(html, "html.parser")
    dates = set()

    # Look for links that contain AUCTIONDATE parameter
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r'AUCTIONDATE=(\d{2}/\d{2}/\d{4})', href, re.IGNORECASE)
        if m:
            try:
                d = datetime.strptime(m.group(1), "%m/%d/%Y").date()
                # Only add if the cell also contains "FC"
                cell_text = a.get_text() + (a.parent.get_text() if a.parent else "")
                if "FC" in cell_text or "Foreclosure" in cell_text:
                    dates.add(d)
            except:
                pass

    # Also scan all table cells for FC mentions with day numbers
    for td in soup.find_all("td"):
        text = td.get_text()
        if "FC" not in text:
            continue
        # Find any AUCTIONDATE links within this cell
        for a in td.find_all("a", href=True):
            m = re.search(r'AUCTIONDATE=(\d{2}/\d{2}/\d{4})', a["href"], re.IGNORECASE)
            if m:
                try:
                    dates.add(datetime.strptime(m.group(1), "%m/%d/%Y").date())
                except:
                    pass

    return dates


# ---------------------------------------------------------------------------
# Step 2 — Preview page: parse all "Auction Details" blocks
# ---------------------------------------------------------------------------

def fetch_preview_page(session, date):
    """Fetch and parse the auction preview page for a given date."""
    date_str = date.strftime("%m/%d/%Y")
    params = {
        "zaction":     "AUCTION",
        "Zmethod":     "PREVIEW",
        "AUCTIONDATE": date_str,
    }
    try:
        resp = session.get(CALENDAR_URL, params=params, timeout=30)
        if resp.status_code != 200:
            log.warning("Preview HTTP %d for %s", resp.status_code, date_str)
            return []
        listings = parse_preview_html(resp.text, date)
        log.info("  %s: found %d listings", date_str, len(listings))
        return listings
    except Exception as e:
        log.error("Preview fetch failed %s: %s", date_str, e)
        return []


def parse_preview_html(html, date):
    """
    Parse the preview page HTML.
    Each listing is inside a div/section with class containing 'AUCTION_DETAILS'
    or identified by the label pattern we confirmed from the live page.

    Confirmed structure from live page read:
      generic "Auction Details"
        generic "Auction Starts" + generic "05/01/2026 11:00 AM ET"
        generic "Case #:" + link "2024-CA-006165-O"
        generic "Final Judgment Amount:" + generic "$184,647.91"
        generic "Parcel ID:" + link "312218022401890" href="ocpaweb.ocpafl.org/..."
        generic "Property Address:" + generic "2533 BRAMPTON CT" + generic "ORLANDO, 32817"
        generic "Assessed Value:" + generic "$222,099.00"
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    # Find all elements that contain "Auction Details" header text
    # These are the container divs for each listing
    auction_detail_containers = []

    # Method 1: find by class (realforeclose uses AUCTION_DETAILS class)
    for el in soup.find_all(class_=re.compile(r'AUCTION_DETAILS|AUCTION_ITEM', re.I)):
        auction_detail_containers.append(el)

    # Method 2: find divs/tds that contain the "Proof of Publication" label
    # which uniquely identifies each auction block
    if not auction_detail_containers:
        for el in soup.find_all(string=re.compile(r'Proof of Publication', re.I)):
            parent = el.parent
            # Walk up to find the containing block
            for _ in range(5):
                if parent and parent.name in ('div', 'td', 'tr', 'section', 'table'):
                    # Check if this container has a Parcel ID link
                    if parent.find('a', href=re.compile(r'parcelsearch|parcel', re.I)):
                        auction_detail_containers.append(parent)
                        break
                if parent:
                    parent = parent.parent

    # Method 3: find all parcel ID links and work backwards to get the container
    if not auction_detail_containers:
        for a in soup.find_all('a', href=re.compile(r'parcelsearch.*Parcel.*ID', re.I)):
            parent = a.parent
            for _ in range(8):
                if parent and parent.name in ('div', 'td', 'section'):
                    text = parent.get_text()
                    if 'Case' in text and 'Address' in text:
                        auction_detail_containers.append(parent)
                        break
                if parent:
                    parent = parent.parent

    # Parse each container
    seen_parcels = set()
    for container in auction_detail_containers:
        listing = extract_listing_from_block(container, date)
        if listing:
            pid = listing.get("parcel_id", "")
            if pid and pid in seen_parcels:
                continue
            if pid:
                seen_parcels.add(pid)
            listings.append(listing)

    return listings


def extract_listing_from_block(block, date):
    """Extract all fields from a single auction listing block."""
    text = block.get_text("\n", strip=True)

    # Auction time
    time_m = re.search(r'(\d{2}/\d{2}/\d{4}\s+\d+:\d+\s+[AP]M\s+ET)', text)
    auction_time = time_m.group(1) if time_m else f"{date.strftime('%m/%d/%Y')} 11:00 AM ET"

    # Case number — from link or text
    case_num = ""
    case_link = block.find('a', href=re.compile(r'occompt|ssweb', re.I))
    if case_link:
        case_num = case_link.get_text(strip=True)
    if not case_num:
        m = re.search(r'(\d{4}-CA-[\d\w-]+)', text)
        case_num = m.group(1) if m else ""

    # Parcel ID — from link to ocpaweb
    parcel_id = ""
    parcel_url = ""
    parcel_link = block.find('a', href=re.compile(r'parcelsearch|ocpaweb', re.I))
    if parcel_link:
        parcel_id  = parcel_link.get_text(strip=True)
        parcel_url = parcel_link.get('href', '')
    if not parcel_id:
        m = re.search(r'\b(\d{12,15})\b', text)
        parcel_id = m.group(1) if m else ""

    # Property address — two lines after "Property Address:"
    address = ""
    addr_section = re.search(
        r'Property Address:\s*\n?\s*(.+?)\n\s*(.+?\d{5})',
        text, re.IGNORECASE | re.DOTALL
    )
    if addr_section:
        line1 = addr_section.group(1).strip()
        line2 = addr_section.group(2).strip()
        address = f"{line1}, {line2}"
    else:
        # Try finding address pattern directly
        m = re.search(
            r'(\d+\s+[\w\s]+(?:CT|DR|ST|AVE|BLVD|LN|WAY|CIR|RD|PL|TER|LOOP|PKWY)\w*'
            r'[\s,]+(?:ORLANDO|KISSIMMEE|WINTER\s+GARDEN|OCOEE|APOPKA|MAITLAND|'
            r'WINDERMERE|BELLE\s+ISLE|EDGEWOOD|EATONVILLE|BAY\s+LAKE|LAKE\s+BUENA\s+VISTA)'
            r'[\s,]+\d{5})',
            text, re.IGNORECASE
        )
        address = m.group(0).strip() if m else ""

    # Final judgment amount
    judgment = ""
    m = re.search(r'Final Judgment Amount:\s*\$?([\d,]+\.?\d*)', text, re.IGNORECASE)
    judgment = f"${m.group(1)}" if m else ""

    # Assessed value
    assessed = ""
    m = re.search(r'Assessed Value:\s*\$?([\d,]+\.?\d*)', text, re.IGNORECASE)
    assessed = f"${m.group(1)}" if m else ""

    # Skip if we have nothing useful
    if not parcel_id and not case_num and not address:
        return None

    today = datetime.today().date()
    days_left = (date - today).days

    return {
        "auction_date":       date.strftime("%Y-%m-%d"),
        "auction_time":       auction_time,
        "case_number":        case_num,
        "parcel_id":          parcel_id,
        "address":            re.sub(r'\s+', ' ', address).strip(),
        "final_judgment":     judgment,
        "assessed_value":     assessed,
        "owner_name":         "",
        "mailing_address":    "",
        "legal_description":  "",
        "homestead":          False,
        "absentee_owner":     False,
        "ocpa_url":           parcel_url or (OCPA_WEB_URL.format(parcel_id) if parcel_id else ""),
        "auction_url":        f"{CALENDAR_URL}?zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE={date.strftime('%m/%d/%Y')}",
        "comptroller_url":    "",
        "source":             "myorangeclerk.realforeclose.com",
        "days_until_auction": days_left,
        "status":             "PENDING",
        "matched_lead":       False,
        "scraped_at":         datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# Step 3 — OCPA enrichment by parcel ID
# ---------------------------------------------------------------------------

def enrich_from_ocpa(session, auction):
    """Query OCPA ArcGIS by parcel ID to get owner, addresses, exemption."""
    parcel_id = re.sub(r'[-\s]', '', auction.get("parcel_id", ""))
    if not parcel_id:
        return auction

    try:
        params = {
            "where":          f"PARCEL='{parcel_id}'",
            "outFields":      ("PARCEL,NAME1,NAME2,SITE_ADDR,SITE_CITY,SITE_ZIP,"
                               "MAIL_ADDR1,MAIL_ADDR2,MAIL_CITY,MAIL_STATE,MAIL_ZIPCD,"
                               "TOTAL_ASSD,EXEMPT_CODE"),
            "returnGeometry": "false",
            "f":              "json",
        }
        resp = session.get(OCPA_API_URL, params=params, timeout=15)
        if resp.status_code != 200:
            return auction

        features = resp.json().get("features", [])
        if not features:
            return auction

        a = features[0].get("attributes", {})

        # Owner name
        name = f"{(a.get('NAME1') or '').strip()} {(a.get('NAME2') or '').strip()}".strip()
        auction["owner_name"] = name

        # Property address from OCPA (fill in if blank)
        site_addr = (a.get("SITE_ADDR") or "").strip()
        site_city = (a.get("SITE_CITY") or "").strip()
        site_zip  = str(a.get("SITE_ZIP") or "").strip()[:5]
        if site_addr and site_city and not auction["address"]:
            auction["address"] = f"{site_addr}, {site_city}, FL {site_zip}"

        # Mailing address
        parts = [
            (a.get("MAIL_ADDR1") or "").strip(),
            (a.get("MAIL_ADDR2") or "").strip(),
            (a.get("MAIL_CITY")  or "").strip(),
            (a.get("MAIL_STATE") or "FL").strip(),
            str(a.get("MAIL_ZIPCD") or "").strip()[:5],
        ]
        auction["mailing_address"] = " ".join(p for p in parts if p).strip()

        # Assessed value (fill if blank)
        if not auction["assessed_value"]:
            try:
                av = int(float(a.get("TOTAL_ASSD") or 0))
                if av > 0:
                    auction["assessed_value"] = f"${av:,}"
            except:
                pass

        # Homestead & absentee
        exempt = str(a.get("EXEMPT_CODE") or "").strip().lstrip("0") or "0"
        auction["homestead"] = exempt in ("1","2","3","4","5","6")

        mail_city = (a.get("MAIL_CITY") or "").strip().upper()
        if mail_city and site_city:
            auction["absentee_owner"] = mail_city != site_city.upper()

    except Exception as e:
        log.debug("OCPA enrich failed for %s: %s", parcel_id, e)

    return auction


# ---------------------------------------------------------------------------
# Step 4 — Classify by days until auction
# ---------------------------------------------------------------------------

def classify_auction(auction, today):
    try:
        d = datetime.strptime(auction["auction_date"], "%Y-%m-%d").date()
        days = (d - today).days
        auction["days_until_auction"] = days
        if days < 0:
            auction["status"] = "EXPIRED"
        elif days < MIN_DAYS_AHEAD:
            auction["status"] = "TOO_SOON"
        else:
            auction["status"] = "ACTIVE"
    except:
        auction["status"] = "UNKNOWN"
    return auction


# ---------------------------------------------------------------------------
# Step 5 — Cross-reference with existing leads
# ---------------------------------------------------------------------------

def cross_reference_leads(foreclosures, leads_path):
    if not os.path.exists(leads_path):
        return foreclosures
    try:
        with open(leads_path, encoding="utf-8") as f:
            data = json.load(f)
        leads = data.get("leads", [])
    except:
        return foreclosures

    # Index leads by parcel ID and street address
    parcel_idx = {}
    addr_idx   = {}
    for i, lead in enumerate(leads):
        pid = re.sub(r'[-\s]', '', (lead.get("parcel_id") or ""))
        if len(pid) >= 10:
            parcel_idx[pid] = i
        addr = (lead.get("property_address") or "").upper().strip()
        key  = addr.split(",")[0].strip()
        if key:
            addr_idx[key] = i

    matched = 0
    for fc in foreclosures:
        lead_idx = None

        # Parcel ID match first
        fc_pid = re.sub(r'[-\s]', '', fc.get("parcel_id", ""))
        if fc_pid and fc_pid in parcel_idx:
            lead_idx = parcel_idx[fc_pid]

        # Address match fallback
        if lead_idx is None:
            fc_key = (fc.get("address","") or "").upper().strip().split(",")[0].strip()
            if fc_key and fc_key in addr_idx:
                lead_idx = addr_idx[fc_key]

        if lead_idx is not None:
            leads[lead_idx]["auction_date"]          = fc["auction_date"]
            leads[lead_idx]["auction_time"]          = fc.get("auction_time","")
            leads[lead_idx]["auction_status"]        = fc["status"]
            leads[lead_idx]["auction_url"]           = fc["auction_url"]
            leads[lead_idx]["auction_final_judgment"]= fc.get("final_judgment","")
            leads[lead_idx]["auction_case_number"]   = fc.get("case_number","")
            # Fill in parcel ID if lead didn't have one
            if not leads[lead_idx].get("parcel_id") and fc.get("parcel_id"):
                leads[lead_idx]["parcel_id"]         = fc["parcel_id"]
                leads[lead_idx]["county_search_url"] = fc.get("ocpa_url","")
            # Boost score
            leads[lead_idx]["seller_score"] = min(
                leads[lead_idx].get("seller_score", 0) + 35, 100
            )
            fc["matched_lead"] = True
            matched += 1
            log.info("Matched: %s -> lead updated", fc.get("address","")[:60])

    if matched > 0:
        data["leads"] = leads
        with open(leads_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info("Updated %d leads with auction data", matched)

    return foreclosures


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save(foreclosures):
    os.makedirs("data", exist_ok=True)
    active  = sum(1 for f in foreclosures if f["status"] == "ACTIVE")
    expired = sum(1 for f in foreclosures if f["status"] == "EXPIRED")
    soon    = sum(1 for f in foreclosures if f["status"] == "TOO_SOON")

    payload = {
        "generated_at":  datetime.utcnow().isoformat() + "Z",
        "total_records": len(foreclosures),
        "active":  active,
        "expired": expired,
        "too_soon": soon,
        "foreclosures": sorted(foreclosures, key=lambda x: x.get("days_until_auction", 999)),
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    log.info("Saved %d foreclosures (%d active, %d expired)", len(foreclosures), active, expired)

    fields = [
        "status","days_until_auction","auction_date","auction_time",
        "address","owner_name","mailing_address","homestead","absentee_owner",
        "final_judgment","assessed_value","case_number","parcel_id",
        "legal_description","ocpa_url","auction_url","matched_lead","scraped_at"
    ]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for fc in sorted(foreclosures, key=lambda x: x.get("days_until_auction", 999)):
            writer.writerow({k: fc.get(k,"") for k in fields})
    log.info("CSV saved.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== Foreclosure Auction Scraper (90 days) ===")
    today = datetime.today().date()
    session = requests.Session()
    session.headers.update(HEADERS)

    # Step 1 — get auction dates from calendar
    log.info("Fetching auction calendar...")
    auction_dates = fetch_auction_dates(session, DAYS_AHEAD)
    log.info("Found %d dates to check", len(auction_dates))

    # Step 2 — fetch listings for each date
    all_listings = []
    for date in sorted(auction_dates):
        listings = fetch_preview_page(session, date)
        if listings:
            # Step 3 — enrich from OCPA
            for i, listing in enumerate(listings):
                if listing.get("parcel_id"):
                    listings[i] = enrich_from_ocpa(session, listing)
                    time.sleep(0.3)
            # Step 4 — classify
            for listing in listings:
                all_listings.append(classify_auction(listing, today))
        time.sleep(0.5)

    # Deduplicate by parcel ID
    seen = set()
    deduped = []
    for fc in all_listings:
        pid = fc.get("parcel_id","")
        key = pid if pid else f"{fc.get('address','')}-{fc.get('auction_date','')}"
        if key and key not in seen:
            seen.add(key)
            deduped.append(fc)

    log.info(
        "Total: %d | ACTIVE: %d | TOO_SOON: %d | EXPIRED: %d",
        len(deduped),
        sum(1 for f in deduped if f["status"] == "ACTIVE"),
        sum(1 for f in deduped if f["status"] == "TOO_SOON"),
        sum(1 for f in deduped if f["status"] == "EXPIRED"),
    )

    # Step 5 — cross-reference
    deduped = cross_reference_leads(deduped, LEADS_PATH)

    # Step 6 — save
    save(deduped)
    log.info("Done.")


if __name__ == "__main__":
    main()
