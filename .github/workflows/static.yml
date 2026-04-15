name: Scrape OC Motivated Seller Leads

on:
  schedule:
    - cron: "0 11 * * *"
  workflow_dispatch:

permissions:
  contents: write

jobs:
  scrape:
    name: Scrape & Generate Dashboard
    runs-on: ubuntu-latest
    timeout-minutes: 60

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install requests beautifulsoup4 rapidfuzz gdown

      - name: Create output directories
        run: mkdir -p data dashboard

      - name: Run scraper
        run: python src/scraper.py
        env:
          PYTHONUNBUFFERED: "1"

      - name: Validate outputs
        run: |
          python - <<'EOF'
          import json, sys, os
          path = "data/output.json"
          if not os.path.exists(path):
              print("ERROR: data/output.json not found"); sys.exit(1)
          with open(path) as f:
              data = json.load(f)
          n = data.get("total_records", 0)
          print(f"✓ JSON valid — {n} records")
          if not os.path.exists("dashboard/index.html"):
              print("ERROR: dashboard/index.html not found"); sys.exit(1)
          print("✓ Dashboard HTML present")
          EOF

      - name: Commit results
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git pull --rebase origin main
          git add data/output.json data/output.csv dashboard/index.html
          git diff --cached --quiet || git commit -m "chore: auto-update leads $(date -u '+%Y-%m-%d %H:%M UTC')"
          git push
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      - name: Upload artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: lead-output-${{ github.run_id }}
          path: |
            data/output.json
            data/output.csv
            dashboard/index.html
          retention-days: 30
