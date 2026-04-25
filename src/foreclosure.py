"""
foreclosure.py — Orange County FL Foreclosure Auction Scraper
Source: myorangeclerk.realforeclose.com + ocpaweb.ocpafl.org

Step 1: Scrape realforeclose.com for auction listings
  - Gets: case #, parcel ID, address, assessed value, final judgment
Step 2: For each parcel ID, fetch ocpaweb.ocpafl.org to get:
  - Owner name, mailing address, homestead status
  URL pattern: https://ocpaweb.ocpafl.org/parcelsearch/Parcel%20ID/{parcel_id}
"""
import json, logging, os, csv, re, time, requests, urllib.parse
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_URL     = "https://myorangeclerk.realforeclose.com"
CALENDAR_URL = f"{BASE_URL}/index.cfm"
OCPA_WEB_URL = "https://ocpaweb.ocpafl.org/parcelsearch/Parcel%20ID/{}"

OUTPUT_PATH = "data/foreclosures.json"
CSV_PATH    = "data/foreclosures.csv"
LEADS_PATH  = "data/output.json"

MIN_DAYS_AHEAD = 3
DAYS_AHEAD     = 90

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Referer":         BASE_URL,
}

TOKEN_MAP = [
    ('@A', '<div class="'), ('@B', '</div>'), ('@C', 'class="'),
    ('@D', '<div>'), ('@E', 'AUCTION'), ('@F', '</td><td'),
    ('@G', '</td></tr>'), ('@H', '<tr><td '), ('@I', 'table'),
    ('@J', 'p_back="NextCheck='), ('@K', 'style="Display:none"'),
    ('@L', '/index.cfm?zaction=auction&zmethod=details&AID='),
]


def decompress_html(compressed):
    html = compressed
    for token, replacement in TOKEN_MAP:
        html = html.replace(token, replacement)
    return html


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        r = session.get(BASE_URL + "/index.cfm", timeout=30)
        log.info("Session init: HTTP %d | cookies: %s",
                 r.status_code, list(session.cookies.keys()))
        time.sleep(2)
    except Exception as e:
        log.error("Session init failed: %s", e)
    return session


# ---------------------------------------------------------------------------
# OCPA enrichment — scrape owner name + mailing address from property card
# ---------------------------------------------------------------------------

def enrich_from_ocpa_web(session, auction):
    """
    Fetch ocpaweb.ocpafl.org/parcelsearch/Parcel%20ID/{parcel_id}
    and scrape owner name, mailing address, homestead status.
    Uses plain text parsing — same data visible on screen.
    """
    parcel_id = auction.get("parcel_id", "")
    if not parcel_id or not parcel_id.strip().isdigit():
        return auction

    url = OCPA_WEB_URL.format(urllib.parse.quote(parcel_id))
    try:
        resp = requests.get(url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept":     "text/html,application/xhtml+xml,*/*;q=0.9",
        }, timeout=20)
        if resp.status_code != 200:
            log.debug("OCPA web HTTP %d for %s", resp.status_code, parcel_id)
            return auction

        # Parse the plain text — same structure we confirmed in browser
        lines = [l.strip() for l in resp.text.replace('\r','').split('\n')
                 if l.strip()]

        owner_name   = ""
        mail_addr    = ""
        prop_addr    = ""
        city_zip     = ""
        homestead    = False
        absentee     = False

        for i, line in enumerate(lines):
            if line == "Name(s):" and i+1 < len(lines):
                owner_name = lines[i+1]

            elif line == "Physical Street Address:" and i+1 < len(lines):
                if not prop_addr:
                    prop_addr = lines[i+1]

            elif line == "Mailing Address On File:" and i+1 < len(lines):
                # Mailing address is next 1-2 lines until "Incorrect Mailing"
                mail_lines = []
                j = i + 1
                while j < len(lines) and "Incorrect Mailing" not in lines[j] and j < i+4:
                    mail_lines.append(lines[j])
                    j += 1
                mail_addr = " ".join(mail_lines).strip()

            elif line == "Postal City and Zip:" and i+1 < len(lines):
                city_zip = lines[i+1]

            elif "Has Homestead" in line:
                homestead = True

        # Build full property address with city/zip
        if prop_addr and city_zip and not auction.get("address"):
            auction["address"] = f"{prop_addr}, {city_zip}"

        # Absentee = mailing city differs from property city
        if mail_addr and city_zip:
            city_part = city_zip.split(",")[0].strip().upper()
            if city_part and city_part not in mail_addr.upper():
                absentee = True

        if owner_name:
            auction["owner_name"] = owner_name
        if mail_addr:
            auction["mailing_address"] = mail_addr
        auction["homestead"]     = homestead
        auction["absentee_owner"] = absentee
        auction["ocpa_url"]      = url

        log.debug("OCPA: %s | owner=%s | mail=%s | homestead=%s",
                  parcel_id, owner_name, mail_addr[:30], homestead)

    except Exception as e:
        log.debug("OCPA web failed %s: %s", parcel_id, e)

    return auction


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def fetch_auction_dates(session, days_ahead=90):
    today = datetime.today().date()
    auction_dates = set()
    for i in range(14):
        auction_dates.add(today + timedelta(days=i))

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
                log.info("Calendar %d-%02d: %d dates", year, month, len(valid))
        except Exception as e:
            log.error("Calendar %d-%02d: %s", year, month, e)
        time.sleep(1)

    return auction_dates


def parse_calendar_html(html):
    soup = BeautifulSoup(html, "html.parser")
    dates = set()
    for a in soup.find_all("a", title=True):
        title = a.get("title", "")
        cell_text = a.get_text() + (a.parent.get_text() if a.parent else "")
        if "Foreclosure" not in cell_text and "FC" not in cell_text:
            continue
        for fmt in ("%B-%d-%Y", "%b-%d-%Y", "%m-%d-%Y"):
            try:
                dates.add(datetime.strptime(title, fmt).date())
                break
            except:
                pass
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
    return dates


# ---------------------------------------------------------------------------
# Preview page + AJAX
# ---------------------------------------------------------------------------

def fetch_preview_page(session, date):
    date_str = date.strftime("%m/%d/%Y")
    params = {"zaction": "AUCTION", "Zmethod": "PREVIEW", "AUCTIONDATE": date_str}
    try:
        page_resp = session.get(CALENDAR_URL, params=params, timeout=30)
        if page_resp.status_code != 200:
            return []
    except Exception as e:
        log.error("Page load failed %s: %s", date_str, e)
        return []

    time.sleep(0.5)
    listings = []
    ts = int(datetime.now().timestamp() * 1000)

    for area in ["W", "R"]:
        ajax_params = {
            "zaction": "AUCTION", "Zmethod": "UPDATE", "FNC": "LOAD",
            "AREA": area, "PageDir": "0", "doR": "1",
            "tx": str(ts), "bypassPage": "0",
        }
        ajax_headers = {
            **HEADERS,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{CALENDAR_URL}?zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE={date_str}",
        }
        try:
            ajax_resp = session.get(CALENDAR_URL, params=ajax_params,
                                    headers=ajax_headers, timeout=30)
            if ajax_resp.status_code != 200:
                continue
            data = ajax_resp.json()
            compressed_html = data.get("retHTML", "")
            if not compressed_html:
                continue
            html = decompress_html(compressed_html)
            area_listings = parse_auction_html(html, date)
            listings.extend(area_listings)
        except Exception as e:
            log.debug("AJAX %s area=%s: %s", date_str, area, e)
        time.sleep(0.3)

    if listings:
        log.info("  %s: %d listings", date_str, len(listings))
    return listings


def parse_auction_html(html, date):
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    for item in soup.find_all("div", class_="AUCTION_ITEM"):
        listing = parse_auction_item(item, date)
        if listing:
            listings.append(listing)
    return listings


def parse_auction_item(item, date):
    labels = item.find_all("div", class_="AD_LBL")
    values = item.find_all("div", class_="AD_DTA")
    fields = {}
    addr_lines = []
    collecting_addr = False

    for i, lbl in enumerate(labels):
        label  = lbl.get_text(strip=True).rstrip(":").strip().upper()
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
            else:
                fields["ocpa_url"] = OCPA_WEB_URL.format(
                    urllib.parse.quote(val)) if val and val.isdigit() else ""
            collecting_addr = False
        elif label == "PROPERTY ADDRESS":
            addr_lines = [val]
            collecting_addr = True
        elif label == "" and collecting_addr:
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

    address   = ", ".join(line for line in addr_lines if line)
    parcel_id = fields.get("parcel_id", "")
    case_num  = fields.get("case_number", "")

    if not parcel_id and not case_num and not address:
        return None

    today     = datetime.today().date()
    days_left = (date - today).days

    return {
        "auction_date":       date.strftime("%Y-%m-%d"),
        "auction_time":       "11:00 AM ET",
        "case_number":        case_num,
        "parcel_id":          parcel_id,
        "address":            address,
        "final_judgment":     fields.get("final_judgment", ""),
        "assessed_value":     fields.get("assessed_value", ""),
        "opening_bid":        fields.get("opening_bid", ""),
        "owner_name":         "",
        "mailing_address":    "",
        "homestead":          False,
        "absentee_owner":     False,
        "ocpa_url":           fields.get("ocpa_url", ""),
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
# Classify
# ---------------------------------------------------------------------------

def classify_auction(auction, today):
    try:
        d    = datetime.strptime(auction["auction_date"], "%Y-%m-%d").date()
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
# Cross-reference with leads
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
        fc_pid = re.sub(r'[-\s]', '', fc.get("parcel_id", ""))
        if fc_pid and fc_pid in parcel_idx:
            lead_idx = parcel_idx[fc_pid]
        if lead_idx is None:
            fc_key = (fc.get("address","") or "").upper().strip().split(",")[0].strip()
            if fc_key and fc_key in addr_idx:
                lead_idx = addr_idx[fc_key]

        if lead_idx is not None:
            leads[lead_idx]["auction_date"]           = fc["auction_date"]
            leads[lead_idx]["auction_time"]           = fc.get("auction_time","")
            leads[lead_idx]["auction_status"]         = fc["status"]
            leads[lead_idx]["auction_url"]            = fc["auction_url"]
            leads[lead_idx]["auction_final_judgment"] = fc.get("final_judgment","")
            leads[lead_idx]["auction_case_number"]    = fc.get("case_number","")
            if not leads[lead_idx].get("parcel_id") and fc.get("parcel_id"):
                leads[lead_idx]["parcel_id"]          = fc["parcel_id"]
                leads[lead_idx]["county_search_url"]  = fc.get("ocpa_url","")
            leads[lead_idx]["seller_score"] = min(
                leads[lead_idx].get("seller_score", 0) + 35, 100)
            fc["matched_lead"] = True
            matched += 1
            log.info("Matched: %s", fc.get("address","")[:60])

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
    log.info("Saved %d | ACTIVE:%d EXPIRED:%d TOO_SOON:%d",
             len(foreclosures), active, expired, soon)

    fields = [
        "status","days_until_auction","auction_date","auction_time",
        "address","owner_name","mailing_address","homestead","absentee_owner",
        "final_judgment","assessed_value","opening_bid",
        "case_number","parcel_id","ocpa_url","comptroller_url",
        "auction_url","matched_lead","scraped_at"
    ]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for fc in sorted(foreclosures,
                         key=lambda x: x.get("days_until_auction", 999)):
            writer.writerow({k: fc.get(k,"") for k in fields})
    log.info("CSV saved.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== Foreclosure Auction Scraper (90 days) ===")
    today   = datetime.today().date()
    session = make_session()

    log.info("Fetching auction calendar...")
    auction_dates = fetch_auction_dates(session, DAYS_AHEAD)
    log.info("Checking %d dates", len(auction_dates))

    all_listings = []
    for date in sorted(auction_dates):
        listings = fetch_preview_page(session, date)
        for listing in listings:
            # Enrich from OCPA web page using parcel ID
            if listing.get("parcel_id") and listing["parcel_id"].isdigit():
                listing = enrich_from_ocpa_web(session, listing)
                time.sleep(0.5)
            all_listings.append(classify_auction(listing, today))
        if listings:
            time.sleep(0.5)

    # Deduplicate
    seen    = set()
    deduped = []
    for fc in all_listings:
        pid = fc.get("parcel_id","")
        key = pid if pid else f"{fc.get('address','')}-{fc.get('auction_date','')}"
        if key and key not in seen:
            seen.add(key)
            deduped.append(fc)

    log.info("Total:%d ACTIVE:%d TOO_SOON:%d EXPIRED:%d",
             len(deduped),
             sum(1 for f in deduped if f["status"]=="ACTIVE"),
             sum(1 for f in deduped if f["status"]=="TOO_SOON"),
             sum(1 for f in deduped if f["status"]=="EXPIRED"))

    deduped = cross_reference_leads(deduped, LEADS_PATH)
    save(deduped)
    log.info("Done.")


if __name__ == "__main__":
    main()
