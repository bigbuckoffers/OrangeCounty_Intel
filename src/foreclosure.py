"""
foreclosure.py — Orange County FL Foreclosure Auction Scraper
Source: myorangeclerk.realforeclose.com

Confirmed HTML structure (live page inspection):
  Calendar links use title="May-01-2026" format with "Foreclosure" text
  Preview page structure:
    div.AUCTION_ITEM — one per listing
      div.AUCTION_STATS > div.Astat_DATA — auction time
      div.AUCTION_DETAILS
        div.AD_LBL / div.AD_DTA pairs:
          "Case #:" / <a>2024-CA-006165-O</a>
          "Final Judgment Amount:" / "$184,647.91"
          "Parcel ID:" / <a href="ocpaweb...">312218022401890</a>
          "Property Address:" / "2533 BRAMPTON CT"
          "" / "ORLANDO, 32817"
          "Assessed Value:" / "$222,099.00"

Requires session cookie (CFID/CFTOKEN) — must hit homepage first.
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
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Referer":         BASE_URL,
}


# ---------------------------------------------------------------------------
# Session — must hit homepage first to get CFID/CFTOKEN cookies
# ---------------------------------------------------------------------------

def make_session():
    """Initialize session with cookies by hitting homepage first."""
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        r = session.get(BASE_URL + "/index.cfm", timeout=30)
        log.info("Session init: HTTP %d | cookies: %s",
                 r.status_code, list(session.cookies.keys()))
        time.sleep(2)
        # Also hit the calendar page to fully establish context
        session.get(
            CALENDAR_URL,
            params={
                "zaction":    "user",
                "zmethod":    "calendar",
                "selCalDate": "{ts '2026-05-01 00:00:00'}",
            },
            timeout=30
        )
        time.sleep(1)
    except Exception as e:
        log.error("Session init failed: %s", e)
    return session


# ---------------------------------------------------------------------------
# Step 1 — Calendar: find dates with auctions
# ---------------------------------------------------------------------------

def fetch_auction_dates(session, days_ahead=90):
    """
    Returns set of dates that have foreclosure auctions.
    Checks calendar month by month + always checks next 14 days directly.
    """
    today = datetime.today().date()
    auction_dates = set()

    # Always check next 14 days directly as safety net
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
                dates = parse_calendar_html(resp.text)
                valid = {d for d in dates if 0 <= (d - today).days <= days_ahead}
                auction_dates.update(valid)
                log.info("Calendar %d-%02d: %d auction dates found", year, month, len(valid))
            else:
                log.warning("Calendar HTTP %d for %d-%02d", resp.status_code, year, month)
        except Exception as e:
            log.error("Calendar %d-%02d failed: %s", year, month, e)
        time.sleep(1)

    return auction_dates


def parse_calendar_html(html):
    """
    Parse calendar HTML to find days with foreclosure auctions.
    Calendar links use title="May-01-2026" format.
    """
    soup = BeautifulSoup(html, "html.parser")
    dates = set()

    # Method 1: links with title="May-01-2026" format that contain Foreclosure
    for a in soup.find_all("a", title=True):
        title = a.get("title", "")
        # Check surrounding context for "Foreclosure" or "FC"
        cell_text = a.get_text() + (a.parent.get_text() if a.parent else "")
        if "Foreclosure" not in cell_text and "FC" not in cell_text:
            continue
        # Parse "May-01-2026" format
        for fmt in ("%B-%d-%Y", "%b-%d-%Y", "%m-%d-%Y"):
            try:
                dates.add(datetime.strptime(title, fmt).date())
                break
            except:
                pass

    # Method 2: any link with AUCTIONDATE in href near FC text
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = re.search(r'AUCTIONDATE=(\d{2}/\d{2}/\d{4})', href, re.IGNORECASE)
        if not m:
            continue
        cell_text = a.get_text() + (a.parent.get_text() if a.parent else "")
        if "Foreclosure" in cell_text or "FC" in cell_text:
            try:
                dates.add(datetime.strptime(m.group(1), "%m/%d/%Y").date())
            except:
                pass

    # Method 3: scan all table cells for FC + day numbers
    for td in soup.find_all("td"):
        text = td.get_text()
        if "FC" not in text and "Foreclosure" not in text:
            continue
        for a in td.find_all("a", href=True):
            m = re.search(r'AUCTIONDATE=(\d{2}/\d{2}/\d{4})', a["href"], re.IGNORECASE)
            if m:
                try:
                    dates.add(datetime.strptime(m.group(1), "%m/%d/%Y").date())
                except:
                    pass
            t = a.get("title","")
            for fmt in ("%B-%d-%Y", "%b-%d-%Y"):
                try:
                    dates.add(datetime.strptime(t, fmt).date())
                    break
                except:
                    pass

    return dates


# ---------------------------------------------------------------------------
# Step 2 — Preview page: parse all .AUCTION_ITEM blocks
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
        if listings:
            log.info("  %s: %d listings found", date_str, len(listings))
        return listings
    except Exception as e:
        log.error("Preview failed %s: %s", date_str, e)
        return []


def parse_preview_html(html, date):
    """
    Parse preview page using confirmed CSS classes:
      .AUCTION_ITEM  — one per listing
      .Astat_DATA    — auction time
      .AD_LBL        — field labels
      .AD_DTA        — field values
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    auction_items = soup.find_all("div", class_="AUCTION_ITEM")
    log.debug("  Found %d .AUCTION_ITEM divs", len(auction_items))

    for item in auction_items:
        listing = parse_auction_item(item, date)
        if listing:
            listings.append(listing)

    return listings


def parse_auction_item(item, date):
    """Extract all fields from one .AUCTION_ITEM div using .AD_LBL/.AD_DTA pairs."""

    # Auction time
    time_el = item.find(class_=re.compile(r'Astat_DATA', re.I))
    auction_time = time_el.get_text(strip=True) if time_el else \
                   f"{date.strftime('%m/%d/%Y')} 11:00 AM ET"

    # Get all label/value pairs
    labels = item.find_all("div", class_="AD_LBL")
    values = item.find_all("div", class_="AD_DTA")

    fields = {}
    addr_lines = []
    collecting_addr = False

    for i, lbl in enumerate(labels):
        label = lbl.get_text(strip=True).rstrip(":").strip().upper()
        val_el = values[i] if i < len(values) else None
        if not val_el:
            continue

        val      = val_el.get_text(strip=True)
        val_link = val_el.find("a")

        if label == "CASE #":
            fields["case_number"] = val
            if val_link:
                fields["comptroller_url"] = val_link.get("href", "")
            collecting_addr = False

        elif label == "FINAL JUDGMENT AMOUNT":
            fields["final_judgment"] = val
            collecting_addr = False

        elif label == "PARCEL ID":
            fields["parcel_id"] = val
            if val_link:
                fields["ocpa_url"] = val_link.get("href", "")
            collecting_addr = False

        elif label == "PROPERTY ADDRESS":
            addr_lines = [val]
            collecting_addr = True

        elif label == "" and collecting_addr:
            # Second line of address (city, zip)
            addr_lines.append(val)
            collecting_addr = False

        elif label == "ASSESSED VALUE":
            fields["assessed_value"] = val
            collecting_addr = False

        elif label == "PLAINTIFF MAX BID":
            fields["opening_bid"] = "" if val == "Hidden" else val
            collecting_addr = False

        else:
            collecting_addr = False

    # Build address
    address = ", ".join(line for line in addr_lines if line)

    parcel_id = fields.get("parcel_id", "")
    case_num  = fields.get("case_number", "")

    if not parcel_id and not case_num and not address:
        return None

    today     = datetime.today().date()
    days_left = (date - today).days

    return {
        "auction_date":       date.strftime("%Y-%m-%d"),
        "auction_time":       auction_time,
        "case_number":        case_num,
        "parcel_id":          parcel_id,
        "address":            address,
        "final_judgment":     fields.get("final_judgment", ""),
        "assessed_value":     fields.get("assessed_value", ""),
        "opening_bid":        fields.get("opening_bid", ""),
        "owner_name":         "",
        "mailing_address":    "",
        "legal_description":  "",
        "homestead":          False,
        "absentee_owner":     False,
        "ocpa_url":           fields.get("ocpa_url",
                                OCPA_WEB_URL.format(parcel_id) if parcel_id else ""),
        "comptroller_url":    fields.get("comptroller_url", ""),
        "auction_url":        (f"{CALENDAR_URL}?zaction=AUCTION&Zmethod=PREVIEW"
                               f"&AUCTIONDATE={date.strftime('%m/%d/%Y')}"),
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
        n1 = (a.get("NAME1") or "").strip()
        n2 = (a.get("NAME2") or "").strip()
        auction["owner_name"] = f"{n1} {n2}".strip()

        # Property address — fill if blank
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

        # Assessed value — fill if blank
        if not auction["assessed_value"]:
            try:
                av = int(float(a.get("TOTAL_ASSD") or 0))
                if av > 0:
                    auction["assessed_value"] = f"${av:,}"
            except:
                pass

        # Homestead
        exempt = str(a.get("EXEMPT_CODE") or "").strip().lstrip("0") or "0"
        auction["homestead"] = exempt in ("1", "2", "3", "4", "5", "6")

        # Absentee owner
        mail_city = (a.get("MAIL_CITY") or "").strip().upper()
        if mail_city and site_city:
            auction["absentee_owner"] = mail_city != site_city.upper()

    except Exception as e:
        log.debug("OCPA enrich failed %s: %s", parcel_id, e)

    return auction


# ---------------------------------------------------------------------------
# Step 4 — Classify by days until auction
# ---------------------------------------------------------------------------

def classify_auction(auction, today):
    try:
        d     = datetime.strptime(auction["auction_date"], "%Y-%m-%d").date()
        days  = (d - today).days
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

    # Index by parcel ID and street address
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

        # Parcel ID match first (most reliable)
        fc_pid = re.sub(r'[-\s]', '', fc.get("parcel_id", ""))
        if fc_pid and fc_pid in parcel_idx:
            lead_idx = parcel_idx[fc_pid]

        # Address match fallback
        if lead_idx is None:
            fc_key = (fc.get("address", "") or "").upper().strip().split(",")[0].strip()
            if fc_key and fc_key in addr_idx:
                lead_idx = addr_idx[fc_key]

        if lead_idx is not None:
            leads[lead_idx]["auction_date"]           = fc["auction_date"]
            leads[lead_idx]["auction_time"]           = fc.get("auction_time", "")
            leads[lead_idx]["auction_status"]         = fc["status"]
            leads[lead_idx]["auction_url"]            = fc["auction_url"]
            leads[lead_idx]["auction_final_judgment"] = fc.get("final_judgment", "")
            leads[lead_idx]["auction_case_number"]    = fc.get("case_number", "")
            if not leads[lead_idx].get("parcel_id") and fc.get("parcel_id"):
                leads[lead_idx]["parcel_id"]          = fc["parcel_id"]
                leads[lead_idx]["county_search_url"]  = fc.get("ocpa_url", "")
            leads[lead_idx]["seller_score"] = min(
                leads[lead_idx].get("seller_score", 0) + 35, 100
            )
            fc["matched_lead"] = True
            matched += 1
            log.info("Matched: %s", fc.get("address", "")[:60])

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
        "active":        active,
        "expired":       expired,
        "too_soon":      soon,
        "foreclosures":  sorted(foreclosures,
                                key=lambda x: x.get("days_until_auction", 999)),
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    log.info("Saved %d foreclosures | ACTIVE:%d EXPIRED:%d TOO_SOON:%d",
             len(foreclosures), active, expired, soon)

    fields = [
        "status", "days_until_auction", "auction_date", "auction_time",
        "address", "owner_name", "mailing_address", "homestead", "absentee_owner",
        "final_judgment", "assessed_value", "opening_bid",
        "case_number", "parcel_id", "legal_description",
        "ocpa_url", "comptroller_url", "auction_url",
        "matched_lead", "scraped_at"
    ]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for fc in sorted(foreclosures,
                         key=lambda x: x.get("days_until_auction", 999)):
            writer.writerow({k: fc.get(k, "") for k in fields})
    log.info("CSV saved.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== Foreclosure Auction Scraper (90 days) ===")
    today = datetime.today().date()

    # Must init session with homepage cookies first
    session = make_session()

    # Step 1 — calendar
    log.info("Fetching auction calendar...")
    auction_dates = fetch_auction_dates(session, DAYS_AHEAD)
    log.info("Checking %d dates total", len(auction_dates))

    # Steps 2+3 — preview pages + OCPA enrichment
    all_listings = []
    for date in sorted(auction_dates):
        listings = fetch_preview_page(session, date)
        for listing in listings:
            if listing.get("parcel_id"):
                listing = enrich_from_ocpa(session, listing)
                time.sleep(0.3)
            all_listings.append(classify_auction(listing, today))
        if listings:
            time.sleep(0.5)

    # Deduplicate by parcel ID
    seen   = set()
    deduped = []
    for fc in all_listings:
        pid = fc.get("parcel_id", "")
        key = pid if pid else f"{fc.get('address','')}-{fc.get('auction_date','')}"
        if key and key not in seen:
            seen.add(key)
            deduped.append(fc)

    log.info(
        "Total: %d | ACTIVE:%d TOO_SOON:%d EXPIRED:%d",
        len(deduped),
        sum(1 for f in deduped if f["status"] == "ACTIVE"),
        sum(1 for f in deduped if f["status"] == "TOO_SOON"),
        sum(1 for f in deduped if f["status"] == "EXPIRED"),
    )

    # Step 5 — cross-reference with leads
    deduped = cross_reference_leads(deduped, LEADS_PATH)

    # Step 6 — save
    save(deduped)
    log.info("Done.")


if __name__ == "__main__":
    main()
