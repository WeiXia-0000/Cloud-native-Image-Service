#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./deploy.sh baseline|better|redis
#
# Behavior:
#   - Builds & deploys the chosen stack.
#   - Tolerates "No changes to deploy" (continues to fetch outputs).
#   - Exports API_URL / SRC_BUCKET / DST_BUCKET / CF_DOMAIN in this process.
#   - Persists them to .stack_env so you can:   source .stack_env
#   - Prints next-step commands for upload & benchmark.

IMPL="${1:-baseline}"  # baseline | better | redis
STACK="image-service-${IMPL}"
REGION="${AWS_REGION:-us-east-1}"
ACC="$(aws sts get-caller-identity --query Account --output text)"

SRC="img-src-${IMPL}-${ACC}-${REGION}"
DST="img-dst-${IMPL}-${ACC}-${REGION}"

echo "STACK=$STACK"
echo "SRC_BUCKET_CANDIDATE=$SRC"
echo "DST_BUCKET_CANDIDATE=$DST"

PARAMS=( "SourceBucketName=$SRC" "ProcessedBucketName=$DST" )

# Enable/disable Redis & CloudFront by implementation
if [[ "$IMPL" == "redis" ]]; then
  PARAMS+=("EnableRedis=true" "RedisHost=fun-iguana-22660.upstash.io:6379")
else
  PARAMS+=("EnableRedis=false")
fi

if [[ "$IMPL" == "better" || "$IMPL" == "redis" ]]; then
  PARAMS+=("EnableCloudFront=true")
else
  PARAMS+=("EnableCloudFront=false")
fi

# If the stack previously failed, delete it first so we can re-create.
STATUS="$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "NOT_FOUND")"

if [[ "$STATUS" == "ROLLBACK_COMPLETE" ]]; then
  echo "Stack $STACK is in ROLLBACK_COMPLETE. Deleting it first..."
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK"
  echo "Deleted. Proceeding to deploy..."
fi

# Build & deploy (tolerate 'No changes to deploy')
sam build

set +e
sam deploy \
  --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM \
  --resolve-s3 \
  --parameter-overrides "${PARAMS[@]}"
DEPLOY_RC=$?
set -e
if [[ "$DEPLOY_RC" -ne 0 ]]; then
  echo "sam deploy returned $DEPLOY_RC (possibly 'No changes to deploy'); continuing to fetch outputs..."
fi

# Fetch outputs regardless
API_URL="$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)"
SRC_BUCKET="$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='SourceBucket'].OutputValue" --output text)"
DST_BUCKET="$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ProcessedBucket'].OutputValue" --output text)"
CF_DOMAIN="$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomain'].OutputValue" --output text 2>/dev/null || true)"

echo "API_URL=$API_URL"
echo "SRC_BUCKET=$SRC_BUCKET"
echo "DST_BUCKET=$DST_BUCKET"
echo "CF_DOMAIN=${CF_DOMAIN:-<none>}"

# Derive convenient URLs for testing/benchmark
IMG_KEY="${IMG_KEY:-sample.jpg}"
IMG_API_URL="${API_URL}/img/${IMG_KEY}"
META_URL="${API_URL}/meta/${IMG_KEY}"

# Try to resolve delivery URL (302 Location) from the API img endpoint.
# Works for both baseline (S3 presign) and better/redis (CloudFront).
# If it fails, leave DELIVERY_URL empty.
DELIVERY_URL="$(curl -s -I "${IMG_API_URL}" | tr -d '\r' | awk 'tolower($1)=="location:"{print $2; exit}')"

export API_URL SRC_BUCKET DST_BUCKET CF_DOMAIN

# Persist to a file you can source in your current shell
cat > .stack_env <<EOF
export API_URL="$API_URL"
export SRC_BUCKET="$SRC_BUCKET"
export DST_BUCKET="$DST_BUCKET"
export CF_DOMAIN="${CF_DOMAIN}"
export IMG_KEY="${IMG_KEY:-sample.jpg}"
export IMG_API_URL="${IMG_API_URL}"
export META_URL="${META_URL}"
export DELIVERY_URL="${DELIVERY_URL}"
EOF

echo "Wrote .stack_env — load via:  source .stack_env"

# Warn if better/redis expected CF but output is missing
if [[ "$IMPL" == "better" || "$IMPL" == "redis" ]]; then
  if [[ -z "${CF_DOMAIN}" || "${CF_DOMAIN}" == "<none>" ]]; then
    echo "WARNING: EnableCloudFront=true but CloudFrontDomain output is empty."
    echo "         Check your template to ensure CloudFrontDistribution is created and CloudFrontDomain is in Outputs."
  fi
fi

# Show the derived URLs for convenience
echo
echo "Derived URLs:"
echo "  IMG_API_URL:   ${IMG_API_URL}"
echo "  META_URL:      ${META_URL}"
echo "  DELIVERY_URL:  ${DELIVERY_URL:-<unknown until first 302>}"

echo
echo "Next steps:"
echo "  aws s3 cp sample.jpg \"s3://\$SRC_BUCKET/sample.jpg\""
echo "  sleep 5 && ./Benchmark.sh"
echo "  # (Optional) open the resolved delivery URL:"
echo "  echo \$DELIVERY_URL"