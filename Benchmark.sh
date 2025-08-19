#!/bin/bash
set -euo pipefail

: "${REQS:=200}"
: "${CONC:=20}"
: "${SKIP_CACHE_CHECK:=false}"

# Quick benchmark for Image Service
# Requirements: curl, jq

# Load environment variables if .stack_env exists
if [[ -f .stack_env ]]; then
  source .stack_env
fi

# Guard: Ensure API_URL is set
if [[ -z "${API_URL:-}" ]]; then
  echo "[ERROR] API_URL is not set. Export API_URL or run deploy.sh first." >&2
  exit 1
fi

IMG_KEY="${IMG_KEY:-sample.jpg}"

# API endpoints
URL_IMG="$API_URL/img/${IMG_KEY}"
URL_META="$API_URL/meta/${IMG_KEY}"

# Resolve the actual delivery URL behind the API 302 (CF for better/redis, S3 presign for baseline)
resolve_img_url() {
  local api_img="$1"
  local resolved
  resolved=$(curl -s -I "$api_img" | awk 'tolower($1)=="location:" {print $2}' | tr -d '\r')
  if [[ -z "$resolved" ]]; then
    echo "$api_img"  # fallback to original URL if no redirect
  else
    echo "$resolved"
  fi
}

# Resolve delivery URL for benchmarking
RESOLVED_IMG=$(resolve_img_url "$URL_IMG")

echo "IMG URL (API):      $URL_IMG"
echo "IMG URL (delivery): $RESOLVED_IMG"
echo "META URL:           $URL_META"

host_of_url() {
  # Extract host from URL like https://host/path
  echo "$1" | awk -F/ '{print $3}'
}
is_cloudfront_url() {
  local host
  host="$(host_of_url "$1")"
  # Only consider it CloudFront if it's a clean CloudFront URL, not S3 with presigned parameters
  [[ "$host" == *"cloudfront.net" || "$host" == *".cloudfront."* ]] && [[ "$1" != *"AWSAccessKeyId"* ]]
}
# REQS=10
# CONC=20 

calc_stats() {
  awk '
  {
    a[NR]=$1; sum+=$1
  }
  END {
    n=NR
    if (n==0) { print "0 0 0 0"; exit }
    # NOTE: input must be pre-sorted ascending (caller runs: sort -n | calc_stats)
    # Percentile indices (1-based). Fallback to last element if index is 0.
    i50=int(0.50*n); if (i50<1) i50=1
    i95=int(0.95*n); if (i95<1) i95=1
    i99=int(0.99*n); if (i99<1) i99=1
    p50=a[i50]; if (p50=="") p50=a[n]
    p95=a[i95]; if (p95=="") p95=a[n]
    p99=a[i99]; if (p99=="") p99=a[n]
    rps = n / sum
    printf "%.6f %.6f %.6f %.2f\n", p50, p95, p99, rps
  }'
}

benchmark() {
  local URL=$1
  local PREFIX=$2
  local DEPLOY_MODE=$3

  echo "🔄 Running benchmark on $URL ..." >&2
  echo >&2

  # 1. Cold request (simulate miss)
  echo "⚡ First request (expect cache miss) for $PREFIX" >&2
  echo "  Testing URL: $URL" >&2
  time curl -s -o /dev/null -w "HTTP: %{http_code}, Time: %{time_total}s, Size: %{size_download} bytes\n" "$URL" 1>&2
  echo >&2

  # 2. Warm-up (fill cache)
  echo "⚡ Warm-up requests (filling cache) for $PREFIX..." >&2
  for i in {1..5}; do curl -s -o /dev/null "$URL"; done
  echo "Warm-up done." >&2
  echo >&2

  # 3. Benchmark latency (p50/p95/p99)
  echo "⚡ Measuring latency with curl for $PREFIX..." >&2

  LAT_FILE=$(mktemp)

  # Measure wall-clock time for the whole concurrent batch
  START_TS=$(date +%s)

  # Fire REQS requests with CONC concurrency. Always print one numeric line per request
  # even if curl fails (in which case record a large sentinel like 9.999 seconds).
  seq 1 "$REQS" | xargs -P "$CONC" -I{} bash -c '
    t=$(curl -s -o /dev/null -w "%{time_total}" --max-time 10 "$0" 2>/dev/null || echo "FAIL")
    if [ -z "$t" ] || [ "$t" = "FAIL" ]; then
      echo 9.999
    else
      echo "$t"
    fi
  ' "$URL" >> "$LAT_FILE"

  END_TS=$(date +%s)
  WALL_SEC=$((END_TS - START_TS))
  if [ "$WALL_SEC" -le 0 ]; then WALL_SEC=1; fi

  # Compute percentiles and single-connection theoretical RPS (1/mean)
  read P50 P95 P99 RPS_SINGLE < <(sort -n "$LAT_FILE" | calc_stats)
  rm -f "$LAT_FILE"

  # Observed throughput considering concurrency (overall N / wall time)
  RPS_WALL=$(awk -v n="$REQS" -v t="$WALL_SEC" 'BEGIN { if (t<=0) t=1; printf "%.2f", n/t }')

  # Log both
  echo "Latency Results (sec) for $PREFIX:" >&2
  echo "p50: $P50" >&2
  echo "p95: $P95" >&2
  echo "p99: $P99" >&2
  echo "RPS (single-conn 1/mean): $RPS_SINGLE" >&2
  echo "RPS (observed wall-clock): $RPS_WALL" >&2

  # Use observed RPS for downstream reporting
  RPS=$RPS_WALL

  # 4. CloudFront Hit Ratio (only meaningful for CloudFront URLs)
  local HITS=0
  local MISSES=0
  local TOTAL=0
  local HIT_RATIO=0.0

  # Skip CloudFront check for baseline mode
  if is_cloudfront_url "$URL" && [[ "$SKIP_CACHE_CHECK" != "true" ]] && [[ "$DEPLOY_MODE" != "baseline" ]]; then
    echo "⚡ Checking CloudFront cache status for $PREFIX (CF detected)..." >&2
    # Use a smaller sample size for cache checking to avoid overwhelming the service
    local CACHE_SAMPLE_SIZE=$((REQS / 10))
    if [[ $CACHE_SAMPLE_SIZE -lt 10 ]]; then CACHE_SAMPLE_SIZE=10; fi
    if [[ $CACHE_SAMPLE_SIZE -gt 50 ]]; then CACHE_SAMPLE_SIZE=50; fi
    
    for i in $(seq 1 $CACHE_SAMPLE_SIZE); do
      # Show progress every 10 requests
      if [[ $((i % 10)) -eq 0 ]]; then
        echo "  Cache check progress: $i/$CACHE_SAMPLE_SIZE" >&2
      fi
      
      local STATUS
      STATUS=$(curl -s -o /dev/null -D - --max-time 5 "$URL" \
                | tr '[:upper:]' '[:lower:]' \
                | awk '/^x-cache:/ {print $2; exit}' \
                | tr -d "\r")
      if [[ "$STATUS" == "hit" ]]; then
        ((HITS++))
      else
        ((MISSES++))
      fi
    done
    TOTAL=$((HITS+MISSES))
    HIT_RATIO=$(awk -v h="$HITS" -v t="$TOTAL" 'BEGIN{ if (t==0) {print 0.0} else {printf "%.1f", (100.0*h)/t} }')
    echo "CloudFront Hits:   $HITS" >&2
    echo "CloudFront Misses: $MISSES" >&2
    echo "Hit Ratio:         ${HIT_RATIO}%" >&2
  else
    echo "⚡ Skipping CloudFront cache check for $PREFIX (non-CF URL)." >&2
  fi
  echo >&2

  echo "$P50" "$P95" "$P99" "$RPS" "$HITS" "$MISSES" "$TOTAL" "$HIT_RATIO"
}

# For IMG endpoint, test the resolved delivery URL directly (CloudFront/S3) instead of API redirect
DEPLOY_MODE="${1:-baseline}"
read P50_IMG P95_IMG P99_IMG RPS_IMG HITS_IMG MISSES_IMG TOTAL_IMG HIT_RATIO_IMG < <(benchmark "$RESOLVED_IMG" "img" "$DEPLOY_MODE")
read P50_META P95_META P99_META RPS_META HITS_META MISSES_META TOTAL_META HIT_RATIO_META < <(benchmark "$URL_META" "meta" "$DEPLOY_MODE")

echo "Benchmark complete!"

# Write benchmark results to file
OUTPUT_FILE="bench_${1}_$(date +%Y%m%d_%H%M%S).txt"
{
  echo "Benchmark results for implementation: $1"
  echo

  echo "=== IMG Endpoint ==="
  echo "Timestamp: $(date)"
  echo "Latency Results (sec):"
  echo "p50: $P50_IMG"
  echo "p95: $P95_IMG"
  echo "p99: $P99_IMG"
  echo "RPS: $RPS_IMG"
  echo "CloudFront Hits: $HITS_IMG"
  echo "CloudFront Misses: $MISSES_IMG"
  echo "Total Requests: $TOTAL_IMG"
  echo "Hit Ratio: ${HIT_RATIO_IMG}%"
  echo

  echo "=== META Endpoint ==="
  echo "Timestamp: $(date)"
  echo "Latency Results (sec):"
  echo "p50: $P50_META"
  echo "p95: $P95_META"
  echo "p99: $P99_META"
  echo "RPS: $RPS_META"
  echo "CloudFront Hits: $HITS_META"
  echo "CloudFront Misses: $MISSES_META"
  echo "Total Requests: $TOTAL_META"
  echo "Hit Ratio: ${HIT_RATIO_META}%"
} > "$OUTPUT_FILE"