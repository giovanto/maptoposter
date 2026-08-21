#!/bin/bash
# Multi-city poster series at identical scale.
# Same --distance and same -W/-H for every city = identical metres-per-pixel,
# so the posters are directly comparable side by side.
cd "$(dirname "$0")/.."

THEME="terracotta"
DISTANCE=6000   # metres — keep constant across the series

declare -a CITIES=(
  "Amsterdam|Netherlands"
  "Bologna|Italy"
  "Valencia|Spain"
)

for entry in "${CITIES[@]}"; do
  IFS="|" read -r city country <<< "$entry"
  echo "=== $city, $country ==="
  uv run ./create_map_poster.py \
    --city "$city" --country "$country" \
    --theme "$THEME" --distance "$DISTANCE" \
    --output-directory "posters/series"
done
