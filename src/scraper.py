"""
scraper.py — Orange County FL Automated Motivated Seller Scraper

Matching architecture (2-stage retrieval + weighted scoring):

Stage 1 — Candidate generation using inverted indexes (no O(n^2)):
  lot_index / unit_index / block_index / subdiv token index / surname index

Stage 2 — Weighted scoring per candidate:
  +40  legal type match
  +30  exact lot match
  +30  exact unit match
  +25  exact block match
  +35  strong subdivision token containment (>=80%)
  +20  moderate subdivision token containment (>=50%)
  +10  weak subdivision token containment (>=25%)
  +20  fuzzy subdivision similarity >= 90
  +10  fuzzy subdivision similarity 80-89
  +20  owner surname overlap
  +10  co-owner overlap (2+ surnames match)
  +10  exact normalized legal match
  -35  lead=subdivision but NAL=metes_bounds
  -20  multiple competing candidates with close scores (ambiguous)
  -15  no parcel anchor (no lot, no unit)

Labels:
  HIGH   = score >= 85 AND parcel anchor AND subdivision confirmed
  MEDIUM = score 65-84
  LOW    = score 40-64
  NONE   = score < 40
"""
import json, logging, os, csv, io, requests, time, re, urllib.parse
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from rapidfuzz import fuzz

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_URL    = "https://selfservice.or.occompt.com"
SEARCH_URL  = f"{BASE_URL}/ssweb/searchPost/DOCSEARCH2950S1"
RESULTS_URL = f"{BASE_URL}/ssweb/search/DOCSEARCH2950S1"
CSV_URL     = f"{BASE_URL}/ssweb/viewSearchResultsReport/DOCSEARCH2950S1/CSV"
OUTPUT_PATH = "data/output.json"

OC_APPRAISER_SEARCH = "https://www.ocpafl.org/searches/ParcelSearch.aspx"

NAL_GDRIVE_ID  = "1X1nZkK07FJV3BmUFHUFvpZA1hLEl4UP9"
NAL_LOCAL_PATH = "/tmp/NAL_orange.csv"

END_DATE   = datetime.today()
START_DATE = END_DATE - timedelta(days=7)
DATE_START = START_DATE.strftime("%m/%d/%Y")
DATE_END   = END_DATE.strftime("%m/%d/%Y")

TARGET_DOC_TYPES = [
    ("Lis Pendens",             "LP",   30),
    ("Lien",                    "LN",   15),
    ("Judgment",                "J",    15),
    ("Probate Court Paper",     "PRCP", 20),
    ("Domestic Relations Deed", "DRD",  10),
]

DOC_TYPE_PRIMARY_NAME = {
    "lis pendens": "grantee",
    "lp":          "grantee",
    "lien":        "grantor",
    "ln":          "grantor",
    "judgment":    "grantor",
    "j":           "grantor",
    "probate":     "both",
    "prcp":        "both",
    "domestic":    "both",
    "drd":         "both",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "Referer": RESULTS_URL,
}

_LEGAL_ABBREV = [
    (r'\bBLK\b',    'BLOCK'),
    (r'\bSEC\b',    'SECTION'),
    (r'\bSUBD\b',   'SUBDIVISION'),
    (r'\bSUB\b',    'SUBDIVISION'),
    (r'\bADD\b',    'ADDITION'),
    (r'\bESTS\b',   'ESTATES'),
    (r'\bEST\b',    'ESTATES'),
    (r'\bHTS\b',    'HEIGHTS'),
    (r'\bHGTS\b',   'HEIGHTS'),
    (r'\bCONDM\b',  'CONDOMINIUM'),
    (r'\bCONDO\b',  'CONDOMINIUM'),
    (r'\bCOND\b',   'CONDOMINIUM'),
    (r'\bVIL\b',    'VILLAS'),
    (r'\bVLS\b',    'VILLAS'),
    (r'\b1ST\b',    'FIRST'),
    (r'\b2ND\b',    'SECOND'),
    (r'\b3RD\b',    'THIRD'),
    (r'\bPK\b',     'PARK'),
    (r'\bGDNS\b',   'GARDENS'),
    (r'\bGARD\b',   'GARDENS'),
    (r'\bLOT:\s*',  'LOT '),
    (r'\bUNIT:\s*', 'UNIT '),
    (r'\bBLOCK:\s*','BLOCK '),
]

_STOPWORDS = {
    'THE', 'OF', 'A', 'AN', 'AND', 'OR', 'IN', 'AT', 'TO', 'FOR',
    'PT', 'PB', 'PG', 'PLAT', 'BOOK', 'PAGE', 'THEREOF', 'THENCE',
    'BEARING', 'DEGREES', 'FEET', 'NORTH', 'SOUTH', 'EAST', 'WEST',
}

_METES_PATTERN = re.compile(
    r'\b(THE
