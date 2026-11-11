#!/usr/bin/env bash
# One-command setup + demo of the auto-crawler on real official sources:
#   Run 1  archive replay: Wayback Machine copies of the official pages as they were on ARCHIVE_DATE
#   Run 2  live crawl of the same official sources + discovery of new ones  -> change & stale detection
#   Run 3  short live re-verification run (shows a repeat run: unchanged / changed / stale)
# Then exports sample_data/. Logs go to logs/.
set -euo pipefail
cd "$(dirname "$0")"
ARCHIVE_DATE="${ARCHIVE_DATE:-20251001}"
mkdir -p logs data

[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Optional: headless Chromium for JavaScript-only portals (NSP, USIEF ...). Skipped quietly if it fails.
if [ "${WITH_JS:-1}" = "1" ]; then
  (pip install -q playwright && python -m playwright install chromium >/dev/null 2>&1 && echo "Playwright ready") || echo "Playwright not installed - JS-only pages will be skipped"
fi

if [ "${FRESH:-1}" = "1" ] && [ -f data/scholarships.db ]; then
  mv data/scholarships.db "data/previous_$(date +%Y%m%d_%H%M%S).db"
fi

echo ">>> Run 1: archive replay ($ARCHIVE_DATE)"
python run_crawler.py --archive "$ARCHIVE_DATE" --max-pages "${ARCHIVE_PAGES:-160}" 2>&1 | tee logs/run1_archive.log
echo ">>> Run 2: live crawl"
python run_crawler.py --max-pages "${LIVE_PAGES:-550}" 2>&1 | tee logs/run2_live.log
echo ">>> Run 3: live re-verification"
python run_crawler.py --max-pages "${REVERIFY_PAGES:-160}" --no-search 2>&1 | tee logs/run3_reverify.log
python scripts/export_records.py
echo
echo "Done. Open the dashboard with:"
echo "  source .venv/bin/activate && streamlit run app/dashboard.py"
