"""
scraper.py — Orange County FL Motivated Seller Lead Scraper
============================================================
Scrapes public deed/document records from the Orange County Comptroller's
self-service portal, scores each lead based on distress signals, and
outputs structured JSON + an HTML dashboard.

Distress Scoring:
  Tax delinquency  → +30 pts
  Code violation   → +25 pts
  Probate filing   → +20 pts
  Multiple liens   → +15 pts
  Divorce/Bankrupt → +10 pts
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional
import unicodedata

import requests
from bs4 import BeautifulSoup

# ── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL = "https://selfservice.or.occompt.com/ssweb/search/DOCSEARCH2950S1"
OUTPUT_PATH = "/data/output.json"
DASHBOARD_PATH = "/dashboard/index.html"
REQUEST_DELAY = 1.5          # seconds between requests (polite crawling)
MAX_PAGES = 50               # safety ceiling to prevent runaway loops
SESSION_TIMEOUT = 30         # seconds

# Keywords that lift the seller-distress score
DISTRESS_KEYWORDS = {
    "tax_delinquency": [
        "tax deed", "tax certificate", "delinquent tax", "tax lien",
        "tax sale", "unpaid tax",
    ],
    "code_violation": [
        "code violation", "code enforcement", "nuisance", "unsafe structure",
        "building violation",
    ],
    "probate": [
        "probate", "estate of", "personal representative", "administrator",
        "executor", "decedent",
    ],
    "multiple_liens": [
        "lis pendens", "lien", "judgment lien", "mechanics lien",
        "materialman", "claim of lien",
    ],
    "divorce_bankruptcy": [
        "divorce", "dissolution of marriage", "bankruptcy", "chapter 7",
        "chapter 11", "chapter 13", "trustee in bankruptcy",
    ],
}

SCORE_MAP = {
    "tax_delinquency": 30,
    "code_violation": 25,
    "probate": 20,
    "multiple_liens": 15,
    "divorce_bankruptcy": 10,
}


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Lead:
    document_number: str = ""
    file_date: str = ""
    grantor: str = ""           # seller
    grantee: str = ""           # buyer
    legal_description: str = ""
    property_address: str = ""
    document_type: str = ""
    # Scoring
    seller_score: int = 0
    distress_flags: list = field(default_factory=list)
    raw_text: str = ""          # full record text used for keyword scan
    scraped_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat() + "Z"
    )


# ── Helpers ───────────────────────────────────────────────────────────────────
def clean_text(text: Optional[str]) -> str:
    """Normalise whitespace and strip invisible Unicode from scraped text."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    return re.sub(r"\s+", " ", text).strip()


def score_lead(lead: Lead) -> Lead:
    """
    Scan the lead's concatenated text for distress keywords and
    accumulate a seller score capped at 100.
    """
    haystack = " ".join([
        lead.grantor, lead.grantee, lead.legal_description,
        lead.property_address, lead.document_type, lead.raw_text,
    ]).lower()

    flags = []
    total = 0
    for category, keywords in DISTRESS_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            flags.append(category)
            total += SCORE_MAP[category]

    lead.distress_flags = flags
    lead.seller_score = min(total, 100)
    return lead


# ── Scraper ───────────────────────────────────────────────────────────────────
class OrangeCountyScraper:
    """
    Scrapes deed/document records from the Orange County Comptroller
    self-service search portal.

    The portal is a classic ASP.NET WebForms app that uses __VIEWSTATE /
    __EVENTVALIDATION hidden fields and standard POST-based pagination.
    We mimic a browser session by carrying cookies and form state across
    requests.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (compatible; OrangeCountyLeadScraper/1.0; "
                "+https://github.com/your-org/lead-scraper)"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })

    def _get_hidden_fields(self, soup: BeautifulSoup) -> dict:
        """Extract ASP.NET ViewState and other hidden form inputs."""
        hidden = {}
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "")
            value = inp.get("value", "")
            if name:
                hidden[name] = value
        return hidden

    def _initial_search(self) -> tuple[BeautifulSoup, dict]:
        """
        Load the search page and submit a broad query to retrieve all
        recent deed records for Orange County, FL.

        Returns the result-page soup and the hidden-field state dict.
        """
        log.info("Loading search page: %s", BASE_URL)
        try:
            resp = self.session.get(BASE_URL, timeout=SESSION_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Failed to load search page: %s", exc)
            raise

        soup = BeautifulSoup(resp.text, "html.parser")
        hidden = self._get_hidden_fields(soup)

        # Build the search form payload.
        # We leave the name fields blank to get ALL documents (broad sweep).
        # Adjust DocType / date range as needed for production use.
        payload = {
            **hidden,
            # Common field names found on the OCCOMPT self-service portal:
            "ctl00$cphMain$txtDocType": "",          # all doc types
            "ctl00$cphMain$txtGrantorLastName": "",
            "ctl00$cphMain$txtGrantorFirstName": "",
            "ctl00$cphMain$txtGranteeName": "",
            "ctl00$cphMain$txtStartDate": "01/01/2020",
            "ctl00$cphMain$txtEndDate": datetime.today().strftime("%m/%d/%Y"),
            "ctl00$cphMain$btnSearch": "Search",
        }

        log.info("Submitting initial search …")
        try:
            resp = self.session.post(
                BASE_URL, data=payload, timeout=SESSION_TIMEOUT
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Search submission failed: %s", exc)
            raise

        soup = BeautifulSoup(resp.text, "html.parser")
        return soup, self._get_hidden_fields(soup)

    def _parse_results_table(self, soup: BeautifulSoup) -> list[Lead]:
        """
        Parse the HTML results table on a single page and return a list
        of partially-populated Lead objects.

        The table layout on the OCCOMPT portal is a standard HTML table;
        column positions may need adjustment if the site changes.
        """
        leads: list[Lead] = []

        # The main results table is usually the largest table on the page
        table = soup.find("table", id=re.compile(r"grd|grid|result", re.I))
        if table is None:
            # Fallback: grab the largest table
            tables = soup.find_all("table")
            if not tables:
                log.warning("No results table found on page.")
                return leads
            table = max(tables, key=lambda t: len(t.find_all("tr")))

        rows = table.find_all("tr")
        if not rows:
            return leads

        # Detect header row to map column indices dynamically
        header_row = rows[0]
        headers = [clean_text(th.get_text()).lower()
                   for th in header_row.find_all(["th", "td"])]

        col = {
            "doc_num":    self._find_col(headers, ["doc", "document", "instrument"]),
            "file_date":  self._find_col(headers, ["date", "filed", "recorded"]),
            "doc_type":   self._find_col(headers, ["type"]),
            "grantor":    self._find_col(headers, ["grantor", "seller", "from"]),
            "grantee":    self._find_col(headers, ["grantee", "buyer", "to"]),
            "legal":      self._find_col(headers, ["legal", "description", "parcel"]),
            "address":    self._find_col(headers, ["address", "property", "location"]),
        }

        for row in rows[1:]:  # skip header
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            def cell(key: str) -> str:
                idx = col.get(key)
                if idx is not None and idx < len(cells):
                    return clean_text(cells[idx].get_text())
                return ""

            # Also capture the full row text for keyword scanning
            raw = clean_text(row.get_text(" "))

            lead = Lead(
                document_number=cell("doc_num"),
                file_date=cell("file_date"),
                grantor=cell("grantor"),
                grantee=cell("grantee"),
                legal_description=cell("legal"),
                property_address=cell("address"),
                document_type=cell("doc_type"),
                raw_text=raw,
            )

            # Only include rows that have at least a document number
            if lead.document_number:
                leads.append(score_lead(lead))

        log.info("  Parsed %d leads from page.", len(leads))
        return leads

    @staticmethod
    def _find_col(headers: list[str], keywords: list[str]) -> Optional[int]:
        """Return the first column index whose header contains a keyword."""
        for kw in keywords:
            for i, h in enumerate(headers):
                if kw in h:
                    return i
        return None

    def _next_page_payload(
        self, soup: BeautifulSoup, hidden: dict, page: int
    ) -> Optional[dict]:
        """
        Build the POST payload required to navigate to the next page.
        Returns None if no next-page control is found.
        """
        # Look for a pager link/button labelled with the next page number
        # or a generic "Next" label
        next_label = str(page + 1)
        for link in soup.find_all(["a", "input", "button"]):
            text = clean_text(link.get_text())
            if text in (next_label, "Next", ">", "»"):
                event_target = link.get("href", "")
                # WebForms __doPostBack pattern
                match = re.search(
                    r"__doPostBack\('([^']+)','([^']*)'\)", event_target
                )
                if match:
                    target, arg = match.group(1), match.group(2)
                    return {
                        **hidden,
                        "__EVENTTARGET": target,
                        "__EVENTARGUMENT": arg,
                    }
        return None

    def scrape(self) -> list[Lead]:
        """
        Main entry point. Iterates through all result pages and returns
        a deduplicated, scored list of Lead objects.
        """
        all_leads: list[Lead] = []
        seen_doc_nums: set[str] = set()

        try:
            soup, hidden = self._initial_search()
        except Exception:
            log.error("Aborting scrape due to initial-search failure.")
            return all_leads

        for page in range(1, MAX_PAGES + 1):
            log.info("Scraping page %d …", page)
            page_leads = self._parse_results_table(soup)

            for lead in page_leads:
                if lead.document_number not in seen_doc_nums:
                    seen_doc_nums.add(lead.document_number)
                    all_leads.append(lead)

            # Check for next page
            next_payload = self._next_page_payload(soup, hidden, page)
            if next_payload is None:
                log.info("No further pages found. Stopping at page %d.", page)
                break

            time.sleep(REQUEST_DELAY)
            try:
                resp = self.session.post(
                    BASE_URL, data=next_payload, timeout=SESSION_TIMEOUT
                )
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")
                hidden = self._get_hidden_fields(soup)
            except requests.RequestException as exc:
                log.error("Pagination request failed on page %d: %s", page, exc)
                break

        log.info(
            "Scrape complete. Total unique leads: %d", len(all_leads)
        )
        return sorted(all_leads, key=lambda l: l.seller_score, reverse=True)


# ── Output helpers ────────────────────────────────────────────────────────────
def save_json(leads: list[Lead]) -> None:
    """Serialise leads to /data/output.json."""
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total_records": len(leads),
        "leads": [asdict(l) for l in leads],
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    log.info("JSON saved → %s (%d records)", OUTPUT_PATH, len(leads))


def save_dashboard(leads: list[Lead]) -> None:
    """Generate a self-contained HTML dashboard at /dashboard/index.html."""
    os.makedirs(os.path.dirname(DASHBOARD_PATH), exist_ok=True)

    def flag_badges(flags: list[str]) -> str:
        label_map = {
            "tax_delinquency":   ("TAX LIEN",    "#e53e3e"),
            "code_violation":    ("CODE VIO.",   "#d69e2e"),
            "probate":           ("PROBATE",     "#805ad5"),
            "multiple_liens":    ("MULTI-LIEN",  "#3182ce"),
            "divorce_bankruptcy":("DIVORCE/BK",  "#319795"),
        }
        html = ""
        for f in flags:
            label, color = label_map.get(f, (f.upper(), "#718096"))
            html += (
                f'<span style="background:{color};color:#fff;'
                f'padding:2px 7px;border-radius:3px;font-size:11px;'
                f'font-weight:700;margin-right:4px;letter-spacing:.5px">'
                f'{label}</span>'
            )
        return html

    def score_color(score: int) -> str:
        if score >= 70:
            return "#e53e3e"
        if score >= 40:
            return "#d69e2e"
        return "#38a169"

    rows_html = ""
    for i, lead in enumerate(leads, 1):
        sc = lead.seller_score
        color = score_color(sc)
        badges = flag_badges(lead.distress_flags)
        rows_html += f"""
        <tr class="lead-row" style="--row-i:{i}">
          <td class="score-cell">
            <div class="score-ring" style="--c:{color}">
              <span>{sc}</span>
            </div>
          </td>
          <td><strong>{lead.document_number or '—'}</strong><br>
              <small class="muted">{lead.document_type or ''}</small></td>
          <td>{lead.file_date or '—'}</td>
          <td>{lead.grantor or '—'}</td>
          <td>{lead.grantee or '—'}</td>
          <td class="address-col">{lead.property_address or '—'}<br>
              <small class="muted">{lead.legal_description[:80] + '…' if len(lead.legal_description) > 80 else lead.legal_description}</small>
          </td>
          <td>{badges if badges else '<span class="muted">None</span>'}</td>
        </tr>"""

    generated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    total = len(leads)
    hot = sum(1 for l in leads if l.seller_score >= 50)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OC Motivated Seller Leads</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0d0f14;
    --surface: #151820;
    --border: #1e2330;
    --accent: #e84040;
    --accent2: #f5a623;
    --text: #e8eaf0;
    --muted: #5a6070;
    --card: #181c26;
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: 'IBM Plex Mono', monospace;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding-bottom: 60px;
  }}

  /* ── Header ── */
  header {{
    background: linear-gradient(135deg, #0d0f14 0%, #131928 100%);
    border-bottom: 1px solid var(--border);
    padding: 32px 48px 28px;
    display: flex;
    align-items: flex-end;
    gap: 32px;
    flex-wrap: wrap;
  }}
  header h1 {{
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 2rem;
    line-height: 1;
    color: #fff;
    letter-spacing: -1px;
  }}
  header h1 span {{ color: var(--accent); }}
  header .meta {{
    font-size: 11px;
    color: var(--muted);
    margin-left: auto;
    text-align: right;
    line-height: 1.8;
  }}

  /* ── Stat cards ── */
  .stats {{
    display: flex;
    gap: 16px;
    padding: 28px 48px 0;
    flex-wrap: wrap;
  }}
  .stat-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px 28px;
    min-width: 160px;
    position: relative;
    overflow: hidden;
  }}
  .stat-card::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
  }}
  .stat-card.red::before  {{ background: var(--accent); }}
  .stat-card.gold::before {{ background: var(--accent2); }}
  .stat-card.green::before {{ background: #38a169; }}
  .stat-card .num {{
    font-family: 'Syne', sans-serif;
    font-size: 2.4rem;
    font-weight: 800;
    line-height: 1;
    color: #fff;
  }}
  .stat-card .label {{
    font-size: 10px;
    color: var(--muted);
    margin-top: 6px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }}

  /* ── Search / filter bar ── */
  .toolbar {{
    padding: 24px 48px 0;
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    align-items: center;
  }}
  .toolbar input {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font-family: inherit;
    font-size: 13px;
    padding: 10px 16px;
    width: 280px;
    outline: none;
    transition: border-color .2s;
  }}
  .toolbar input:focus {{ border-color: var(--accent); }}
  .toolbar select {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font-family: inherit;
    font-size: 13px;
    padding: 10px 14px;
    outline: none;
    cursor: pointer;
  }}
  .toolbar label {{ font-size: 12px; color: var(--muted); }}

  /* ── Table ── */
  .table-wrap {{
    margin: 24px 48px 0;
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: auto;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12.5px;
  }}
  thead th {{
    background: var(--surface);
    padding: 14px 16px;
    text-align: left;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
  }}
  thead th:hover {{ color: var(--text); }}
  thead th.sorted {{ color: var(--accent); }}
  .lead-row {{
    border-bottom: 1px solid var(--border);
    animation: fadeUp .4s ease both;
    animation-delay: calc(var(--row-i) * 30ms);
  }}
  @keyframes fadeUp {{
    from {{ opacity: 0; transform: translateY(8px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}
  .lead-row:last-child {{ border-bottom: none; }}
  .lead-row:hover td {{ background: #1a1f2e; }}
  td {{
    padding: 14px 16px;
    vertical-align: top;
    transition: background .15s;
  }}
  .address-col {{ max-width: 220px; }}
  .muted {{ color: var(--muted); font-size: 11px; }}
  small.muted {{ display: block; margin-top: 3px; }}

  /* ── Score ring ── */
  .score-cell {{ text-align: center; }}
  .score-ring {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 44px;
    height: 44px;
    border-radius: 50%;
    border: 2px solid var(--c, #38a169);
    color: var(--c, #38a169);
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 15px;
  }}

  /* ── Empty state ── */
  .empty {{
    text-align: center;
    padding: 80px 0;
    color: var(--muted);
    font-size: 14px;
  }}
  .empty h2 {{ font-family: 'Syne',sans-serif; font-size: 1.5rem; color: var(--text); margin-bottom: 8px; }}

  /* ── Footer ── */
  footer {{
    margin-top: 40px;
    text-align: center;
    font-size: 11px;
    color: var(--muted);
  }}
</style>
</head>
<body>

<header>
  <div>
    <h1>ORANGE COUNTY <span>FL</span><br>MOTIVATED SELLERS</h1>
  </div>
  <div class="meta">
    Generated: {generated}<br>
    Source: Orange County Comptroller Public Records<br>
    Scored by distress signal analysis
  </div>
</header>

<div class="stats">
  <div class="stat-card red">
    <div class="num">{total}</div>
    <div class="label">Total Leads</div>
  </div>
  <div class="stat-card gold">
    <div class="num">{hot}</div>
    <div class="label">Hot Leads ≥50</div>
  </div>
  <div class="stat-card green">
    <div class="num">{total - hot}</div>
    <div class="label">Warm Leads &lt;50</div>
  </div>
</div>

<div class="toolbar">
  <input type="text" id="searchBox" placeholder="Search grantor, grantee, address …" oninput="filterTable()">
  <label>Min score:</label>
  <select id="minScore" onchange="filterTable()">
    <option value="0">All</option>
    <option value="10">10+</option>
    <option value="25">25+</option>
    <option value="50">50+</option>
    <option value="70">70+</option>
  </select>
  <label>Flag:</label>
  <select id="flagFilter" onchange="filterTable()">
    <option value="">All flags</option>
    <option value="tax_delinquency">Tax Lien</option>
    <option value="code_violation">Code Violation</option>
    <option value="probate">Probate</option>
    <option value="multiple_liens">Multiple Liens</option>
    <option value="divorce_bankruptcy">Divorce / Bankruptcy</option>
  </select>
</div>

<div class="table-wrap">
  <table id="leadsTable">
    <thead>
      <tr>
        <th onclick="sortTable(0)">Score</th>
        <th onclick="sortTable(1)">Doc #</th>
        <th onclick="sortTable(2)">File Date</th>
        <th onclick="sortTable(3)">Grantor (Seller)</th>
        <th onclick="sortTable(4)">Grantee (Buyer)</th>
        <th onclick="sortTable(5)">Property Address</th>
        <th>Distress Flags</th>
      </tr>
    </thead>
    <tbody id="tableBody">
{rows_html if rows_html else '<tr><td colspan="7" class="empty"><h2>No records found</h2>Run scraper.py to populate data.</td></tr>'}
    </tbody>
  </table>
</div>

<footer>
  Data sourced from Orange County Comptroller public records portal &nbsp;|&nbsp; For investment research only
</footer>

<script>
  // ── Search / filter ──────────────────────────────────────────────────────
  function filterTable() {{
    const q = document.getElementById('searchBox').value.toLowerCase();
    const minScore = parseInt(document.getElementById('minScore').value) || 0;
    const flagFilter = document.getElementById('flagFilter').value;
    const rows = document.querySelectorAll('.lead-row');
    rows.forEach(row => {{
      const text = row.innerText.toLowerCase();
      const score = parseInt(row.querySelector('.score-ring span').textContent) || 0;
      const badges = row.querySelector('td:last-child').innerText.toLowerCase();
      const matchText = !q || text.includes(q);
      const matchScore = score >= minScore;
      const matchFlag = !flagFilter || badges.includes(flagFilter.replace('_',' '));
      row.style.display = (matchText && matchScore && matchFlag) ? '' : 'none';
    }});
  }}

  // ── Sort ─────────────────────────────────────────────────────────────────
  let sortDir = {{}};
  function sortTable(col) {{
    const tbody = document.getElementById('tableBody');
    const rows = Array.from(tbody.querySelectorAll('.lead-row'));
    const asc = !sortDir[col];
    sortDir = {{[col]: asc}};
    rows.sort((a, b) => {{
      const av = a.cells[col]?.innerText.trim() || '';
      const bv = b.cells[col]?.innerText.trim() || '';
      const an = parseFloat(av), bn = parseFloat(bv);
      if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
      return asc ? av.localeCompare(bv) : bv.localeCompare(av);
    }});
    rows.forEach(r => tbody.appendChild(r));
    document.querySelectorAll('thead th').forEach((th, i) => {{
      th.classList.toggle('sorted', i === col);
    }});
  }}
</script>
</body>
</html>"""

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("Dashboard saved → %s", DASHBOARD_PATH)


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    log.info("=== Orange County FL Motivated Seller Scraper ===")
    scraper = OrangeCountyScraper()
    leads = scraper.scrape()

    if not leads:
        log.warning(
            "No leads returned. The county portal may be unavailable "
            "or require CAPTCHA bypass. Generating empty outputs."
        )

    save_json(leads)
    save_dashboard(leads)
    log.info("Done. Open /dashboard/index.html to review leads.")


if __name__ == "__main__":
    main()
