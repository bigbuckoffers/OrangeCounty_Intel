"""
foreclosure.py — Orange County FL Foreclosure Auction Scraper
Source: myorangeclerk.realforeclose.com

Scrapes upcoming foreclosure auction listings for the next 30 days.
Logic:
  - Only pulls auctions with auction_date >= TODAY + 3 days (rolling gap)
  - Flags auctions where auction_date < TODAY as EXPIRED (for CRM alerts)
  - Saves to data/foreclosures.json and data/foreclosures.csv
  - Cross-references with existing output.json leads by address/owner

Runs daily after scraper.py and reenrich.py.
"""
import json, logging, os, csv, re, time, requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_URL       = "https://myorangeclerk.realforeclose.com"
CALENDAR_URL   = f"{BASE_URL}/index.cfm"
OUTPUT_PATH    = "data/foreclosures.json"
CSV_PATH       = "data/foreclosures.csv"
LEADS_PATH     = "data/output.json"

# Only show auctions at least this many days in the future
MIN_DAYS_AHEAD = 3
# Scrape this many days into the future
DAYS_AHEAD     = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL,
}


# ---------------------------------------------------------------------------
# Fetch auction list for a single date
# ---------------------------------------------------------------------------

def fetch_auction_date(session, date):
    """Fetch all auctions for a specific date. Returns list of auction dicts."""
    date_str = date.strftime("%m/%d/%Y")
    params = {
        "zaction":    "AUCTION",
        "Zmethod":    "PREVIEW",
        "AUCTIONDATE": date_str,
    }
    try:
        resp = session.get(CALENDAR_URL, params=params, timeout=30)
        if resp.status_code != 200:
            log.warning("HTTP %d for date %s", resp.status_code, date_str)
            return []
        return parse_auction_list(resp.text, date)
    except Exception as e:
        log.error("Fetch failed for %s: %s", date_str, e)
        return []


def parse_auction_list(html, date):
    """Parse the auction preview page for a given date."""
    soup = BeautifulSoup(html, "html.parser")
    auctions = []

    # RealForeclose uses table rows with class "AUCTION_ITEM"
    items = soup.find_all("div", class_=re.compile(r"AUCTION_ITEM|listingDetails", re.I))

    if not items:
        # Try table-based layout
        rows = soup.find_all("tr", class_=re.compile(r"odd|even", re.I))
        items = rows

    for item in items:
        auction = parse_auction_item(item, date)
        if auction:
            auctions.append(auction)

    return auctions


def parse_auction_item(item, date):
    """Extract fields from a single auction listing element."""
    text = item.get_text(" ", strip=True)
    if not text or len(text) < 20:
        return None

    # Try to extract case number
    case_match = re.search(r'(\d{4}\s*CA\s*\d+|\d{4}-CA-\d+|Case[:\s]+[\w-]+)', text, re.I)
    case_num = case_match.group(0).strip() if case_match else ""

    # Try to extract address — look for pattern like "123 Main St, Orlando FL"
    addr_match = re.search(
        r'(\d+\s+[\w\s]+(?:ST|AVE|DR|LN|BLVD|WAY|CT|CIR|RD|PL|TER|TERR)\w*'
        r'[,\s]+(?:ORLANDO|KISSIMMEE|OCOEE|APOPKA|WINTER GARDEN|MAITLAND|BELLE ISLE|WINDERMERE)'
        r'[\w\s,]*(?:FL|FLORIDA)[\s,]*\d{5})',
        text, re.I
    )
    address = addr_match.group(0).strip() if addr_match else ""

    # Opening bid
    bid_match = re.search(r'\$[\d,]+\.?\d*', text)
    opening_bid = bid_match.group(0).strip() if bid_match else ""

    # Parcel ID
    parcel_match = re.search(r'\b(\d{2}-\d{2}-\d{2}-\d{4}-\d{2}-\d{3,4}|\d{2}\s\d{2}\s\d{2}\s\d{4}\s\d{2}\s\d{3,4})\b', text)
    parcel_id = parcel_match.group(0).strip() if parcel_match else ""

    # Get any links for the detail URL
    link = item.find("a", href=True)
    detail_url = ""
    if link:
        href = link["href"]
        if href.startswith("http"):
            detail_url = href
        elif href.startswith("/"):
            detail_url = BASE_URL + href
        else:
            detail_url = f"{BASE_URL}/{href}"

    auction_date_str = date.strftime("%Y-%m-%d")

    return {
        "auction_date":    auction_date_str,
        "case_number":     case_num,
        "address":         address,
        "opening_bid":     opening_bid,
        "parcel_id":       parcel_id,
        "detail_url":      detail_url,
        "source":          "myorangeclerk.realforeclose.com",
        "scraped_at":      datetime.utcnow().isoformat() + "Z",
        "days_until_auction": (date - datetime.today().date()).days,
        "status":          "UPCOMING",
    }


# ---------------------------------------------------------------------------
# Fetch detail page for richer data
# ---------------------------------------------------------------------------

def fetch_auction_detail(session, auction):
    """Hit the detail page to get full address and parcel info."""
    if not auction.get("detail_url"):
        return auction
    try:
        resp = session.get(auction["detail_url"], timeout=20)
        if resp.status_code != 200:
            return auction
        soup = BeautifulSoup(resp.text, "html.parser")

        # RealForeclose detail pages have labeled fields
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True).upper()
                value = cells[1].get_text(strip=True)
                if "ADDRESS" in label and value and not auction["address"]:
                    auction["address"] = value
                elif "PARCEL" in label and value and not auction["parcel_id"]:
                    auction["parcel_id"] = value
                elif "OPENING BID" in label or "MINIMUM BID" in label:
                    if value:
                        auction["opening_bid"] = value
                elif "CASE" in label and value and not auction["case_number"]:
                    auction["case_number"] = value

    except Exception as e:
        log.debug("Detail fetch failed: %s", e)
    return auction


# ---------------------------------------------------------------------------
# Date filtering logic
# ---------------------------------------------------------------------------

def classify_auction(auction, today):
    """
    Classify auction status:
      EXPIRED  — auction date has passed (alert for CRM removal)
      TOO_SOON — auction is within MIN_DAYS_AHEAD (< 3 days away, skip)
      ACTIVE   — auction is 3+ days away (target these)
    """
    try:
        auction_dt = datetime.strptime(auction["auction_date"], "%Y-%m-%d").date()
        days_left = (auction_dt - today).days
        auction["days_until_auction"] = days_left

        if days_left < 0:
            auction["status"] = "EXPIRED"
        elif days_left < MIN_DAYS_AHEAD:
            auction["status"] = "TOO_SOON"
        else:
            auction["status"] = "ACTIVE"
    except Exception:
        auction["status"] = "UNKNOWN"

    return auction


# ---------------------------------------------------------------------------
# Cross-reference with existing leads
# ---------------------------------------------------------------------------

def cross_reference_leads(foreclosures, leads_path):
    """
    For each foreclosure with an address, check if it matches
    any existing lead in output.json. If so, flag the lead as
    also having an upcoming auction and add the auction date.
    """
    if not os.path.exists(leads_path):
        return foreclosures

    try:
        with open(leads_path, encoding="utf-8") as f:
            data = json.load(f)
        leads = data.get("leads", [])
    except Exception:
        return foreclosures

    # Build address index from leads
    lead_addr_index = {}
    for i, lead in enumerate(leads):
        addr = (lead.get("property_address") or "").upper().strip()
        if addr:
            # Use first part of address (street number + name) as key
            parts = addr.split(",")
            if parts:
                lead_addr_index[parts[0].strip()] = i

    matched = 0
    for fc in foreclosures:
        fc_addr = (fc.get("address") or "").upper().strip()
        if not fc_addr:
            continue
        fc_parts = fc_addr.split(",")
        fc_key = fc_parts[0].strip() if fc_parts else ""

        if fc_key in lead_addr_index:
            lead_idx = lead_addr_index[fc_key]
            leads[lead_idx]["auction_date"]        = fc["auction_date"]
            leads[lead_idx]["auction_opening_bid"] = fc["opening_bid"]
            leads[lead_idx]["auction_status"]      = fc["status"]
            leads[lead_idx]["auction_url"]         = fc["detail_url"]
            # Stack the distress score
            existing_score = leads[lead_idx].get("seller_score", 0)
            leads[lead_idx]["seller_score"] = min(existing_score + 35, 100)
            matched += 1
            fc["matched_lead"] = True
            log.info("Cross-referenced: %s -> lead matched", fc_addr[:60])

    if matched > 0:
        data["leads"] = leads
        with open(leads_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info("Updated %d leads with auction data", matched)

    return foreclosures


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_foreclosures(foreclosures):
    os.makedirs("data", exist_ok=True)

    payload = {
        "generated_at":   datetime.utcnow().isoformat() + "Z",
        "total_records":  len(foreclosures),
        "active":         sum(1 for f in foreclosures if f["status"] == "ACTIVE"),
        "expired":        sum(1 for f in foreclosures if f["status"] == "EXPIRED"),
        "too_soon":       sum(1 for f in foreclosures if f["status"] == "TOO_SOON"),
        "foreclosures":   foreclosures,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    log.info("Saved %d foreclosures to %s", len(foreclosures), OUTPUT_PATH)

    fields = [
        "status", "days_until_auction", "auction_date",
        "address", "opening_bid", "case_number", "parcel_id",
        "detail_url", "source", "scraped_at"
    ]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for fc in foreclosures:
            writer.writerow({k: fc.get(k, "") for k in fields})
    log.info("CSV saved: %s", CSV_PATH)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== Foreclosure Auction Scraper ===")
    today = datetime.today().date()

    session = requests.Session()
    session.headers.update(HEADERS)

    all_auctions = []

    # Scrape the next DAYS_AHEAD days
    for i in range(DAYS_AHEAD):
        target_date = today + timedelta(days=i)
        log.info("Fetching auctions for %s", target_date.strftime("%m/%d/%Y"))
        auctions = fetch_auction_date(session, target_date)

        if auctions:
            # Fetch detail pages for better data
            for j, auction in enumerate(auctions):
                if auction.get("detail_url"):
                    auction = fetch_auction_detail(session, auction)
                    auctions[j] = auction
                    time.sleep(0.5)

        # Classify each auction
        classified = [classify_auction(a, today) for a in auctions if a]
        all_auctions.extend(classified)

        if classified:
            log.info("  Found %d auctions for %s", len(classified), target_date)
        time.sleep(1)

    log.info(
        "Total: %d | ACTIVE: %d | EXPIRED: %d | TOO_SOON: %d",
        len(all_auctions),
        sum(1 for f in all_auctions if f["status"] == "ACTIVE"),
        sum(1 for f in all_auctions if f["status"] == "EXPIRED"),
        sum(1 for f in all_auctions if f["status"] == "TOO_SOON"),
    )

    # Cross-reference with existing leads
    all_auctions = cross_reference_leads(all_auctions, LEADS_PATH)

    # Save
    save_foreclosures(all_auctions)
    log.info("Done.")


if __name__ == "__main__":
    main()
