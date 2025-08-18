

#!/bin/bash
# Benchmark script for Redis vs DDB latency

if [ -z "$API_URL" ] || [ -z "$REDIS_URL" ]; then
  echo "❌ Please export API_URL and REDIS_URL first."
  echo "Example:"
  echo "  export API_URL=https://your-api.execute-api.us-east-1.amazonaws.com/Prod"
  echo "  export REDIS_URL=rediss://:password@host:port"
  exit 1
fi

KEY="sample.jpg"

echo "🔄 Clearing Redis key: $KEY ..."
redis-cli -u "$REDIS_URL" --tls DEL "$KEY" >/dev/null

echo "⚡ First request (expect miss -> DDB)"
time curl -s "$API_URL/meta/$KEY" > /dev/null

echo "⚡ Second request (expect hit -> Redis)"
time curl -s "$API_URL/meta/$KEY" > /dev/null