#!/bin/bash
# Download FXCM's public M1 candles (bid + ask) into data/fxcm/<SYMBOL>/<year>-<week>.csv.gz.
# Resumable: existing files are skipped. Missing weeks are saved as empty files.
#   ./fetch_fxcm.sh                      # all six pairs, 2023 → this year
#   ./fetch_fxcm.sh EURUSD GBPUSD        # chosen pairs
# Gold (XAUUSD) is not in FXCM's public archive.
set -u
cd "$(dirname "$0")"
SYMBOLS=${*:-"EURUSD GBPUSD AUDUSD USDCHF NZDUSD USDCAD"}
FIRST_YEAR=2023
LAST_YEAR=$(date +%Y)
for sym in $SYMBOLS; do
  mkdir -p "data/fxcm/$sym"
  for y in $(seq $FIRST_YEAR $LAST_YEAR); do
    for w in $(seq 1 53); do
      out="data/fxcm/$sym/$y-$(printf %02d $w).csv.gz"
      [ -e "$out" ] && continue
      code=$(curl -s --max-time 60 -o "$out.part" -w "%{http_code}" \
             "https://candledata.fxcorporate.com/m1/$sym/$y/$w.csv.gz")
      case "$code" in
        200) mv "$out.part" "$out" ;;
        404) rm -f "$out.part"; : > "$out" ;;
        *)   rm -f "$out.part"; echo "$sym $y week $w: HTTP $code (will retry next run)"; sleep 10 ;;
      esac
      sleep 0.3
    done
    echo "$(date +%T) $sym $y done"
  done
done
# Parsed caches are rebuilt from the new files on the next run
rm -f data/cache/*.npz
