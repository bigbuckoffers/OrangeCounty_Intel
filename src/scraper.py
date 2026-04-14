"""
scraper.py — Orange County FL Motivated Seller Lead Scraper
============================================================
Targets the Tyler Technologies / Orange County Comptroller public records portal.
Searches for high-value distress document types:
  - Lis Pendens      (pre-foreclosure)       → +30 pts
  - Deed (Tax Deed)  (tax foreclosure)        → +30 pts
  - Lien             (unpaid debt)            → +15 pts
  - Judgment         (court ordered debt)     → +15 pts
  - Probate          (owner deceased)         → +20 pts
  - Domestic Relations (divorce)              → +10 pts
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL    = "https://selfservice.or.occompt.com/ssweb/search/DOCSEARCH2950S1"
OUTPUT_PATH    = "data/output.json"
DASHBOARD_PATH = "dashboard/index.html"
REQUEST_DELAY  = 2.0   # seconds between page requests
MAX_PAGES      = 10    # max pages per doc type
TIMEOUT        = 30    # request timeout seconds

# Date range — last 90 days
END_DATE   = datetime.today()
START_DATE = END_DATE - timedelta(days=90)
DATE_START = START_DATE.strftime("%m/%d/%Y")
DATE_END   = END_DATE.strftime("%m/%d/%Y")

# Document types to search + their distress score
TARGET_DOC_TYPES = [
    ("Lis Pendens",              30),
    ("Deed",                     30),
    ("Lien",                     15),
    ("Judgment",                 15),
    ("Probate Court Paper",      20),
    ("Domestic Relations Deed",  10),
    ("Domestic Relations Court Pape", 10),
]

# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Lead:
    document_number:   str = ""
    file_date:         str = ""
    grantor:           str = ""
    grantee:           str = ""
    legal_description: str = ""
    property_address:  str = ""
    document_type:     str = ""
    seller_score:      int = 0
    distress_flags:    list = field(default_factory=list)
    scraped_at:        str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


# ── Helpers ───────────────────────────────────────────────────────────────────
def clean(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def score_lead(doc_type: str, base_score: int) -> tuple[int, list]:
    """Return (score, flags) based on document type."""
    dt = doc_type.lower()
    flags = []
    score = base_score

    if "lis pendens" in dt:
        flags.append("lis_pendens")
    if "tax deed" in dt or "tax cert" in dt:
        flags.append("tax_delinquency")
        score = max(score, 30)
    if "lien" in dt:
        flags.append("multiple_liens")
    if "judgment" in dt:
        flags.append("judgment")
    if "probate" in dt:
        flags.append("probate")
    if "domestic" in dt or "divorce" in dt:
        flags.append("divorce_bankruptcy")

    return min(score, 100), flags


# ── Scraper ───────────────────────────────────────────────────────────────────
class OrangeCountyScraper:
    """
    Scrapes the Orange County FL Comptroller public records portal.
    Uses date-range + document-type filtering to get focused results.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE_URL,
        })

    def _load_page(self, url: str, params: dict = None, data: dict = None) -> Optional[BeautifulSoup]:
        """GET or POST a page and return BeautifulSoup, or None on failure."""
        try:
            if data:
                resp = self.session.post(url, data=data, params=params, timeout=TIMEOUT)
            else:
                resp = self.session.get(url, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as exc:
            log.error("Request failed: %s", exc)
            return None

    def _parse_results(self, soup: BeautifulSoup, doc_type: str, base_score: int) -> list[Lead]:
        """Parse one results page and return Lead objects."""
        leads = []

        # Each record is a card/row — Tyler Tech uses div-based layout
        # Records are identified by document number pattern
        records = soup.find_all("div", class_=re.compile(r"result|record|row|item", re.I))

        # Fallback: parse table rows if divs not found
        if not records:
            records = soup.find_all("tr")

        for rec in records:
            text = clean(rec.get_text(" "))
            if not text or len(text) < 10:
                continue

            # Look for document number pattern (8+ digits)
            doc_match = re.search(r'\b(2024\d{6}|2025\d{6}|2026\d{6})\b', text)
            if not doc_match:
                continue

            doc_num = doc_match.group(1)

            # Extract date
            date_match = re.search(r'\b(\d{1,2}/\d{1,2}/\d{4})\b', text)
            file_date = date_match.group(1) if date_match else ""

            # Extract grantor/grantee from labeled sections
            grantor = ""
            grantee = ""

            grantor_tag = rec.find(string=re.compile(r"Grantor", re.I))
            if grantor_tag and grantor_tag.parent:
                sib = grantor_tag.parent.find_next_sibling()
                if sib:
                    grantor = clean(sib.get_text())

            grantee_tag = rec.find(string=re.compile(r"Grantee", re.I))
            if grantee_tag and grantee_tag.parent:
                sib = grantee_tag.parent.find_next_sibling()
                if sib:
                    grantee = clean(sib.get_text())

            # Extract legal description
            legal = ""
            legal_tag = rec.find(string=re.compile(r"^Legal$", re.I))
            if legal_tag and legal_tag.parent:
                sib = legal_tag.parent.find_next_sibling()
                if sib:
                    legal = clean(sib.get_text())

            score, flags = score_lead(doc_type, base_score)

            lead = Lead(
                document_number=doc_num,
                file_date=file_date,
                grantor=grantor,
                grantee=grantee,
                legal_description=legal,
                property_address=legal,  # legal desc is the address proxy
                document_type=doc_type,
                seller_score=score,
                distress_flags=flags,
            )
            leads.append(lead)

        return leads

    def _scrape_doc_type(self, doc_type: str, base_score: int) -> list[Lead]:
        """Scrape all pages for a single document type."""
        leads = []
        log.info("Scraping doc type: %s", doc_type)

        # Build search params for Tyler Technologies portal
        params = {
            "RecordingDateStart": DATE_START,
            "RecordingDateEnd":   DATE_END,
            "DocTypeID":          doc_type,
        }

        for page in range(1, MAX_PAGES + 1):
            params["Page"] = page
            soup = self._load_page(BASE_URL, params=params)
            if not soup:
                log.warning("No response for %s page %d", doc_type, page)
                break

            page_leads = self._parse_results(soup, doc_type, base_score)
            log.info("  Page %d: %d leads", page, len(page_leads))

            if not page_leads:
                # Also try alternate parsing — look for the raw text blocks
                alt_leads = self._parse_text_blocks(soup, doc_type, base_score)
                if alt_leads:
                    leads.extend(alt_leads)
                break

            leads.extend(page_leads)

            # Check if there's a next page
            next_btn = soup.find("a", string=re.compile(r"next|>|»", re.I))
            if not next_btn:
                break

            time.sleep(REQUEST_DELAY)

        return leads

    def _parse_text_blocks(self, soup: BeautifulSoup, doc_type: str, base_score: int) -> list[Lead]:
        """
        Alternative parser that reads the Tyler Tech card layout directly.
        Looks for the document number + Grantor/Grantee/Legal pattern.
        """
        leads = []
        full_text = soup.get_text("\n")
        
        # Find all document blocks by splitting on document number pattern
        blocks = re.split(r'\n(?=\d{11}\s*[·•]\s)', full_text)
        
        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # Document number
            doc_match = re.match(r'(\d{11})', block)
            if not doc_match:
                continue
            doc_num = doc_match.group(1)

            # Date
            date_match = re.search(r'(\d{2}/\d{2}/\d{4})', block)
            file_date = date_match.group(1) if date_match else ""

            # Grantor — text after "Grantor" label
            grantor = ""
            g_match = re.search(r'Grantor[^\n]*\n([^\n]+)', block, re.I)
            if g_match:
                grantor = clean(g_match.group(1))

            # Grantee — text after "Grantee" label  
            grantee = ""
            ge_match = re.search(r'Grantee[^\n]*\n([^\n]+)', block, re.I)
            if ge_match:
                grantee = clean(ge_match.group(1))

            # Legal description
            legal = ""
            l_match = re.search(r'Legal\n([^\n]+)', block, re.I)
            if l_match:
                legal = clean(l_match.group(1))

            if not doc_num:
                continue

            score, flags = score_lead(doc_type, base_score)
            leads.append(Lead(
                document_number=doc_num,
                file_date=file_date,
                grantor=grantor,
                grantee=grantee,
                legal_description=legal,
                property_address=legal,
                document_type=doc_type,
                seller_score=score,
                distress_flags=flags,
            ))

        return leads

    def scrape(self) -> list[Lead]:
        """Main entry point — scrape all target document types."""
        all_leads: list[Lead] = []
        seen: set[str] = set()

        log.info("=== Orange County FL Motivated Seller Scraper ===")
        log.info("Date range: %s to %s", DATE_START, DATE_END)

        for doc_type, base_score in TARGET_DOC_TYPES:
            try:
                leads = self._scrape_doc_type(doc_type, base_score)
                for lead in leads:
                    if lead.document_number not in seen:
                        seen.add(lead.document_number)
                        all_leads.append(lead)
                time.sleep(REQUEST_DELAY)
            except Exception as exc:
                log.error("Error scraping %s: %s", doc_type, exc)
                continue

        log.info("Total unique leads: %d", len(all_leads))
        return sorted(all_leads, key=lambda l: l.seller_score, reverse=True)


# ── Output ────────────────────────────────────────────────────────────────────
def save_json(leads: list[Lead]) -> None:
    os.makedirs(os.path.dirname(OUTPUT_PATH) or ".", exist_ok=True)
    payload = {
        "generated_at":   datetime.utcnow().isoformat() + "Z",
        "total_records":  len(leads),
        "date_range":     f"{DATE_START} to {DATE_END}",
        "leads": [asdict(l) for l in leads],
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    log.info("JSON saved → %s (%d records)", OUTPUT_PATH, len(leads))


def save_dashboard(leads: list[Lead]) -> None:
    os.makedirs(os.path.dirname(DASHBOARD_PATH) or ".", exist_ok=True)

    def flag_badges(flags: list) -> str:
        label_map = {
            "lis_pendens":       ("LIS PENDENS",  "#e53e3e"),
            "tax_delinquency":   ("TAX DEED",     "#e53e3e"),
            "multiple_liens":    ("LIEN",         "#3182ce"),
            "judgment":          ("JUDGMENT",     "#d69e2e"),
            "probate":           ("PROBATE",      "#805ad5"),
            "divorce_bankruptcy":("DIVORCE",      "#319795"),
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
        if score >= 25: return "#e53e3e"
        if score >= 15: return "#d69e2e"
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
          <td class="address-col">{lead.legal_description[:100] if lead.legal_description else '—'}</td>
          <td>{badges if badges else '<span class="muted">—</span>'}</td>
        </tr>"""

    generated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    total = len(leads)
    hot = sum(1 for l in leads if l.seller_score >= 25)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OC Motivated Seller Leads</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#0d0f14;--surface:#151820;--border:#1e2330;
    --accent:#e84040;--accent2:#f5a623;--text:#e8eaf0;
    --muted:#5a6070;--card:#181c26;
  }}
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'IBM Plex Mono',monospace;background:var(--bg);color:var(--text);min-height:100vh;padding-bottom:60px}}
  header{{background:linear-gradient(135deg,#0d0f14 0%,#131928 100%);border-bottom:1px solid var(--border);padding:32px 48px 28px;display:flex;align-items:flex-end;gap:32px;flex-wrap:wrap}}
  header h1{{font-family:'Syne',sans-serif;font-weight:800;font-size:2rem;line-height:1;color:#fff;letter-spacing:-1px}}
  header h1 span{{color:var(--accent)}}
  header .meta{{font-size:11px;color:var(--muted);margin-left:auto;text-align:right;line-height:1.8}}
  .stats{{display:flex;gap:16px;padding:28px 48px 0;flex-wrap:wrap}}
  .stat-card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px 28px;min-width:160px;position:relative;overflow:hidden}}
  .stat-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px}}
  .stat-card.red::before{{background:var(--accent)}}
  .stat-card.gold::before{{background:var(--accent2)}}
  .stat-card.green::before{{background:#38a169}}
  .stat-card .num{{font-family:'Syne',sans-serif;font-size:2.4rem;font-weight:800;line-height:1;color:#fff}}
  .stat-card .label{{font-size:10px;color:var(--muted);margin-top:6px;text-transform:uppercase;letter-spacing:1px}}
  .toolbar{{padding:24px 48px 0;display:flex;gap:12px;flex-wrap:wrap;align-items:center}}
  .toolbar input{{background:var(--card);border:1px solid var(--border);border-radius:6px;color:var(--text);font-family:inherit;font-size:13px;padding:10px 16px;width:280px;outline:none}}
  .toolbar input:focus{{border-color:var(--accent)}}
  .toolbar select{{background:var(--card);border:1px solid var(--border);border-radius:6px;color:var(--text);font-family:inherit;font-size:13px;padding:10px 14px;outline:none;cursor:pointer}}
  .toolbar label{{font-size:12px;color:var(--muted)}}
  .table-wrap{{margin:24px 48px 0;border:1px solid var(--border);border-radius:12px;overflow:auto}}
  table{{width:100%;border-collapse:collapse;font-size:12.5px}}
  thead th{{background:var(--surface);padding:14px 16px;text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted);border-bottom:1px solid var(--border);white-space:nowrap;cursor:pointer;user-select:none}}
  thead th:hover{{color:var(--text)}}
  thead th.sorted{{color:var(--accent)}}
  .lead-row{{border-bottom:1px solid var(--border);animation:fadeUp .4s ease both;animation-delay:calc(var(--row-i)*30ms)}}
  @keyframes fadeUp{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:translateY(0)}}}}
  .lead-row:last-child{{border-bottom:none}}
  .lead-row:hover td{{background:#1a1f2e}}
  td{{padding:14px 16px;vertical-align:top;transition:background .15s}}
  .address-col{{max-width:220px}}
  .muted{{color:var(--muted);font-size:11px}}
  small.muted{{display:block;margin-top:3px}}
  .score-cell{{text-align:center}}
  .score-ring{{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;border-radius:50%;border:2px solid var(--c,#38a169);color:var(--c,#38a169);font-family:'Syne',sans-serif;font-weight:800;font-size:15px}}
  footer{{margin-top:40px;text-align:center;font-size:11px;color:var(--muted)}}
</style>
</head>
<body>
<header>
  <div><h1>ORANGE COUNTY <span>FL</span><br>MOTIVATED SELLERS</h1></div>
  <div class="meta">Generated: {generated}<br>Source: Orange County Comptroller Public Records<br>Scored by distress signal analysis</div>
</header>
<div class="stats">
  <div class="stat-card red"><div class="num">{total}</div><div class="label">Total Leads</div></div>
  <div class="stat-card gold"><div class="num">{hot}</div><div class="label">Hot Leads</div></div>
  <div class="stat-card green"><div class="num">{total - hot}</div><div class="label">Warm Leads</div></div>
</div>
<div class="toolbar">
  <input type="text" id="searchBox" placeholder="Search grantor, grantee, legal …" oninput="filterTable()">
  <label>Min score:</label>
  <select id="minScore" onchange="filterTable()">
    <option value="0">All</option>
    <option value="10">10+</option>
    <option value="15">15+</option>
    <option value="20">20+</option>
    <option value="30">30+</option>
  </select>
  <label>Type:</label>
  <select id="flagFilter" onchange="filterTable()">
    <option value="">All types</option>
    <option value="LIS PENDENS">Lis Pendens</option>
    <option value="LIEN">Lien</option>
    <option value="JUDGMENT">Judgment</option>
    <option value="PROBATE">Probate</option>
    <option value="DIVORCE">Divorce</option>
    <option value="TAX DEED">Tax Deed</option>
  </select>
</div>
<div class="table-wrap">
  <table id="leadsTable">
    <thead>
      <tr>
        <th onclick="sortTable(0)">Score</th>
        <th onclick="sortTable(1)">Doc #</th>
        <th onclick="sortTable(2)">File Date</th>
        <th onclick="sortTable(3)">Grantor</th>
        <th onclick="sortTable(4)">Grantee (Seller)</th>
        <th onclick="sortTable(5)">Legal Description</th>
        <th>Type</th>
      </tr>
    </thead>
    <tbody id="tableBody">
{rows_html if rows_html else '<tr><td colspan="7" style="text-align:center;padding:60px;color:#5a6070">No records found — scraper is running</td></tr>'}
    </tbody>
  </table>
</div>
<footer>Orange County Comptroller public records &nbsp;|&nbsp; For investment research only</footer>
<script>
  function filterTable(){{
    const q=document.getElementById('searchBox').value.toLowerCase();
    const minScore=parseInt(document.getElementById('minScore').value)||0;
    const flagFilter=document.getElementById('flagFilter').value;
    document.querySelectorAll('.lead-row').forEach(row=>{{
      const text=row.innerText.toLowerCase();
      const score=parseInt(row.querySelector('.score-ring span').textContent)||0;
      const badges=row.querySelector('td:last-child').innerText;
      row.style.display=(!q||text.includes(q))&&score>=minScore&&(!flagFilter||badges.includes(flagFilter))?'':'none';
    }});
  }}
  let sortDir={{}};
  function sortTable(col){{
    const tbody=document.getElementById('tableBody');
    const rows=Array.from(tbody.querySelectorAll('.lead-row'));
    const asc=!sortDir[col];sortDir={{[col]:asc}};
    rows.sort((a,b)=>{{
      const av=a.cells[col]?.innerText.trim()||'';
      const bv=b.cells[col]?.innerText.trim()||'';
      const an=parseFloat(av),bn=parseFloat(bv);
      if(!isNaN(an)&&!isNaN(bn))return asc?an-bn:bn-an;
      return asc?av.localeCompare(bv):bv.localeCompare(av);
    }});
    rows.forEach(r=>tbody.appendChild(r));
    document.querySelectorAll('thead th').forEach((th,i)=>th.classList.toggle('sorted',i===col));
  }}
</script>
</body>
</html>"""

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("Dashboard saved → %s", DASHBOARD_PATH)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("Starting scrape | %s to %s", DATE_START, DATE_END)
    scraper = OrangeCountyScraper()
    leads = scraper.scrape()
    save_json(leads)
    save_dashboard(leads)
    log.info("Done.")

if __name__ == "__main__":
    main()
