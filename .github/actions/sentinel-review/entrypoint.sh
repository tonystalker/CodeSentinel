#!/bin/bash
set -e

DIFF_REF="${INPUT_DIFF_REF:-}"
AUTO_FIX="${INPUT_AUTO_FIX:-false}"
SARIF_OUTPUT="${INPUT_SARIF_OUTPUT:-sentinel-review.sarif}"
AGENTS="${INPUT_AGENTS:-bug,security}"

# Build command
CMD="sentinel review . --sarif ${SARIF_OUTPUT} --agents ${AGENTS} --output sentinel-review.json"

if [ -n "$DIFF_REF" ]; then
  CMD="$CMD --diff $DIFF_REF"
fi

if [ "$AUTO_FIX" = "true" ]; then
  CMD="$CMD --auto-fix"
fi

echo "Running: $CMD"
eval $CMD

# Set outputs for GitHub Actions
FINDINGS=$(python -c "import json; d=json.load(open('sentinel-review.json')); print(d['summary']['total_findings'])" 2>/dev/null || echo "0")
SCORE=$(python -c "import json; d=json.load(open('sentinel-review.json')); print(d['summary'].get('review_score', 0))" 2>/dev/null || echo "0")

echo "findings-count=${FINDINGS}" >> "$GITHUB_OUTPUT"
echo "review-score=${SCORE}" >> "$GITHUB_OUTPUT"
echo "sarif-file=${SARIF_OUTPUT}" >> "$GITHUB_OUTPUT"
