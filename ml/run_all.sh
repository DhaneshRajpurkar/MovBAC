#!/usr/bin/env bash
# Overnight run: download + train every region. FIRMS downloads go one region at a time (they share
# the key's 5000-per-10-minutes budget); weather + training for a region run while the next one downloads.
# Usage (from the project folder):  bash ml/run_all.sh australia brazil africa
cd "$(dirname "$0")/.."
mkdir -p ml/data
for r in "$@"; do
  mkdir -p "ml/data/$r"
  echo "$(date +%T) === $r: FIRMS ==="
  python -u ml/download.py firms "$r" > "ml/data/$r/firms.log" 2>&1 || { echo "$(date +%T) $r FIRMS FAILED (see ml/data/$r/firms.log)"; continue; }
  tail -1 "ml/data/$r/firms.log"
  ( python -u ml/download.py weather "$r" > "ml/data/$r/weather.log" 2>&1 \
      && python -u ml/train.py "$r" > "ml/data/$r/train.log" 2>&1 \
      && echo "$(date +%T) === $r: DONE ===" && grep -E "test|forecast issued" "ml/data/$r/train.log" \
      || echo "$(date +%T) $r weather/train FAILED (see ml/data/$r/*.log)" ) &
done
wait
echo "$(date +%T) all regions finished"
