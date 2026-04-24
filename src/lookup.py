"""
lookup.py — Live property lookup for NONE-confidence leads
Uses Claude API with web search to find property addresses for leads
that couldn't be matched against the NAL dataset.

Run after reenrich.py. Only processes NONE leads that have a grantee name.
Rate-limited to avoid overloading. Saves progress incrementally.
"""
import json, logging, os, time, re
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

OUTPUT_PATH = "data/output.json"

# Only process this many NONE leads per run to keep runtime under 10 min
MAX_LOOKUPS = 200
# Delay between API calls in seconds
RATE_LIMIT  = 2.0

LENDER_NOISE = re.compile(
    r'\b(JPMORGAN|JMORGAN|PMORGAN|CHASE|BANK\s+OF|WELLS\s+FARGO|'
    r'CITIBANK|COUNTRYWIDE|NATIONSTAR|OCWEN|SETERUS|PHH|'
    r'QUICKEN|ROCKET|PENNYMAC|FREEDOM|SERVICING|SERVICER|'
    r'FEDERAL\s+NATIONAL|FANNIE|FREDDIE|SECRETARY|HUD\b|'
    r'HOMEOWNERS\s+ASSOCIATION|HOA\b|ASSOCIATION\s+INC)\b',
    re.IGNORECASE
)


def clean_name(raw):
    if not raw:
        return ""
    text = LENDER_NOISE.sub('', raw.upper().strip())
    text = re.sub(r'[^A-Z\s,]', ' ', text)
    parts = [p.strip() for p in text.split(',') if p.strip() and len(p.strip()) >= 4]
    return ', '.join(parts[:2])


def build_prompt(lead):
    grantee  = clean_name(lead.get("grantee", "") or "")
    grantor  = clean_name(lead.get("grantor", "") or "")
    legal    = (lead.get("legal_description", "") or "").strip()
    doc_type = (lead.get("document_type", "") or "").strip()

    owner_name = grantee or grantor
    if not owner_name:
        return None

    prompt = f"""Find the property address in Orange County, Florida for this public record filing.

Document type: {doc_type}
Owner/Grantee: {grantee or "unknown"}
Grantor/Lender: {grantor or "unknown"}
Legal description: {legal or "not available"}

Search for the property address associated with this owner in Orange County FL.
Use the owner name and legal description to find the exact street address.

Reply in this exact format only, no other text:
ADDRESS: [full street address, city, FL zip]
CONFIDENCE: [HIGH/MEDIUM/LOW]
SOURCE: [where you found it]

If you cannot find a reliable address, reply:
ADDRESS: NOT FOUND
CONFIDENCE: NONE
SOURCE: none"""

    return prompt, owner_name


def call_claude_with_search(prompt):
    """Call Claude API with web search enabled."""
    try:
        import requests as req
        response = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 200,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        if response.status_code != 200:
            log.warning("API error %d: %s", response.status_code, response.text[:200])
            return None

        data = response.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        return text.strip()

    except Exception as e:
        log.error("Claude API call failed: %s", e)
        return None


def parse_response(text):
    if not text:
        return None

    address    = ""
    confidence = "LOW"
    source     = ""

    for line in text.split('\n'):
        line = line.strip()
        if line.startswith("ADDRESS:"):
            address = line[8:].strip()
        elif line.startswith("CONFIDENCE:"):
            confidence = line[11:].strip().upper()
        elif line.startswith("SOURCE:"):
            source = line[7:].strip()

    if not address or address.upper() in ("NOT FOUND", "UNKNOWN", ""):
        return None

    if "FL" not in address.upper() and "FLORIDA" not in address.upper():
        if re.search(r'\d+.*\w+.*\d{5}', address):
            pass
        else:
            return None

    return {
        "property_address": address,
        "match_confidence": "MEDIUM" if confidence == "HIGH" else "LOW",
        "match_reason":     f"Claude web search: {source[:80]}",
        "match_score":      60 if confidence == "HIGH" else 35,
        "mailing_address":  "",
        "owner_name":       "",
        "assessed_value":   "",
    }


def main():
    log.info("=== Live lookup for NONE leads ===")

    if not os.path.exists(OUTPUT_PATH):
        log.error("No output.json found")
        return

    with open(OUTPUT_PATH, encoding="utf-8") as f:
        data = json.load(f)

    leads = data.get("leads", [])
    log.info("Loaded %d leads", len(leads))

    # Find NONE leads with a usable name
    none_leads = []
    for i, lead in enumerate(leads):
        if lead.get("match_confidence") != "NONE":
            continue
        grantee = lead.get("grantee", "") or ""
        grantor = lead.get("grantor", "") or ""
        clean_g  = clean_name(grantee)
        clean_gr = clean_name(grantor)
        if not clean_g and not clean_gr:
            continue
        legal = (lead.get("legal_description", "") or "").strip()
        if not clean_g and not legal:
            continue
        none_leads.append((i, lead))

    log.info("Found %d NONE leads eligible for lookup", len(none_leads))
    to_process = none_leads[:MAX_LOOKUPS]
    log.info("Processing %d leads this run (max %d)", len(to_process), MAX_LOOKUPS)

    found  = 0
    failed = 0

    for idx, (lead_idx, lead) in enumerate(to_process):
        result = build_prompt(lead)
        if not result:
            continue
        prompt, owner_name = result

        log.info("[%d/%d] Looking up: %s", idx+1, len(to_process), owner_name[:50])

        response_text = call_claude_with_search(prompt)
        parsed = parse_response(response_text)

        if parsed:
            leads[lead_idx]["property_address"] = parsed["property_address"]
            leads[lead_idx]["mailing_address"]   = parsed.get("mailing_address", "")
            leads[lead_idx]["owner_name"]        = parsed.get("owner_name", "")
            leads[lead_idx]["assessed_value"]    = parsed.get("assessed_value", "")
            leads[lead_idx]["match_confidence"]  = parsed["match_confidence"]
            leads[lead_idx]["match_score"]       = parsed["match_score"]
            leads[lead_idx]["match_reason"]      = parsed["match_reason"]
            leads[lead_idx]["needs_enrichment"]  = True
            found += 1
            log.info("  Found: %s", parsed["property_address"])
        else:
            failed += 1
            log.info("  Not found")

        # Save progress every 25 leads
        if (idx + 1) % 25 == 0:
            data["leads"] = leads
            with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log.info("Progress saved (%d found so far)", found)

        time.sleep(RATE_LIMIT)

    # Final save
    data["generated_at"]  = datetime.utcnow().isoformat() + "Z"
    data["total_records"] = len(leads)
    data["leads"]         = leads

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    log.info("Done: %d found | %d not found | %d processed",
             found, failed, len(to_process))


if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()
