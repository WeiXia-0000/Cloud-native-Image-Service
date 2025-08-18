# Lambda handler for Cloud-native Image Service

import os, json
import logging
from decimal import Decimal
import boto3
import time
try:
    import redis
    HAS_REDIS = True
except Exception:
    redis = None
    HAS_REDIS = False


# ---------- Feature switches ----------
# Enable CloudFront URL redirection for image access
ENABLE_CLOUDFRONT = os.getenv("ENABLE_CLOUDFRONT", "false").lower() == "true"
# Enable Redis caching for metadata retrieval
ENABLE_REDIS      = os.getenv("ENABLE_REDIS", "false").lower() == "true"

# DynamoDB table name for storing metadata
TABLE = os.environ["TABLE"]
# Destination S3 bucket for images
DST_BUCKET = os.environ["DST_BUCKET"]
# CloudFront domain for serving images
CF_DOMAIN = os.environ.get("CF_DOMAIN", "").strip()

# ---------- AWS clients ----------
ddb = boto3.resource("dynamodb")
s3  = boto3.client("s3")


_redis_client = None
_DEF_META_TTL = 300

# Return the DynamoDB table resource
def _table():
    return ddb.Table(TABLE)

# Construct HTTP response with status, body, headers, and content type
def _resp(status, body="", headers=None, ctype="application/json"):
    h = {"Content-Type": ctype}
    if headers: 
        h.update(headers)
    return {"statusCode": status, "headers": h, "body": body or ""}

# Return JSON response with given data and status code
def _json(data, status=200):
    return _resp(status, json.dumps(data, default=str))

# Extract path, key, and HTTP method from Lambda event
def _extract(event):
    path = event.get("rawPath") or event.get("path", "")
    method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod") or "GET"
    path_params = event.get("pathParameters") or {}
    key = path_params.get("key") or path_params.get('proxy')
    return path, key, method

# Initialize and return Redis client if enabled and available
def _get_redis_client():
    global _redis_client
    if not ENABLE_REDIS or not HAS_REDIS:
        return None
    if _redis_client is not None:
        return _redis_client
    
    host = os.getenv("REDIS_HOST", "").strip() or os.getenv("REDIS_URL", "").strip()
    if not host:
        return None
    try:
        if host.startswith("redis://") or host.startswith("rediss://"):
            # If the host is a Redis connection URL, create client directly from it
            _redis_client = redis.Redis.from_url(host, decode_responses=True)
        else:
            # Otherwise, parse "host:port" format manually
            parts = host.split(":", 1)
            h = parts[0]
            p = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 6379
            _redis_client = redis.Redis(host=h, port=p, decode_responses=True)
        # Verify the connection by pinging Redis, raises error if not reachable
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        logging.error(f"get_redis_client error: {e}")
        return None

# Determine TTL (time to live) for cache from a DynamoDB item
def _cache_ttl_from_item(item):
    try:
        now = int(time.time())
        raw = item.get("ttl")
        if raw is None:
            return _DEF_META_TTL
        ttl_epoch = int(raw) if not isinstance(raw, Decimal) else int(raw)
        # Constrain the TTL to be at least 60 seconds and at most 3600 seconds
        secs = max(60, min(ttl_epoch - now, 3600))
        return secs
    except Exception as e:
        logging.error(f"cache_ttl_from_item error: {e}")
        return _DEF_META_TTL

# =========================
# META branch - handling metadata retrieval and caching
# =========================

# Get metadata directly from DynamoDB
def _meta_get_via_ddb(pk):
    try:
        res = _table().get_item(Key={"pk":pk})
        item = res.get("Item")
        if not item:
            return _json({"error":"not found"}, 404)
        return _json(item)
    except Exception as e:
        logging.error(f"meta_get_via_ddb error: {e}")
        return _json({"error":"internal server error"}, 500)

# Get metadata using Redis cache, fallback to DynamoDB
def _meta_get_via_redis(pk):
    client = _get_redis_client()
    if not client:
        return _meta_get_via_ddb(pk)
    try:
        # Cache key for 404 (not found) results to avoid repeated DB hits
        nkey = f"meta404:{pk}"
        if client.get(nkey):
            return _json({"error":"not found"}, 404)

        # Cache key for valid metadata results
        ckey = f"meta:{pk}"

        # If metadata found in Redis cache, return it immediately
        cached = client.get(ckey)
        if cached:
            return _resp(200, cached)

        # Fallback to DynamoDB if Redis cache miss
        res = _table().get_item(Key={"pk":pk})
        item = res.get("Item")
        if not item:
            try:
                client.setex(nkey, 30, "1")
            except Exception as e:
                pass
            return _json({"error":"not found"}, 404)
        body = json.dumps(item, default=str)

        # Store metadata in Redis with TTL for caching
        try:
            client.setex(ckey, _cache_ttl_from_item(item), body)
        except Exception as e:
            pass
        return _resp(200, body)
    except Exception as e:
        logging.error(f"meta_get_via_redis error: {e}")
        return _meta_get_via_ddb(pk)

# Handle metadata requests based on HTTP method and caching settings
def _handle_meta(key, method):
    if method == "GET":
        if ENABLE_REDIS:
            return _meta_get_via_redis(key)
        return _meta_get_via_ddb(key)
    else:
        return _json({"error":"invalid method"}, 405)
    
# =========================
# IMG branch - handling image retrieval and redirection
# =========================
# Generate a presigned S3 URL for image access
def _img_get_via_s3(key):
    try:
        url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket":DST_BUCKET, "Key":key},
            ExpiresIn=300 # 5 mins
        )
        return _json({"url":url}, 200)
    except Exception as e:
        logging.error(f"img_get_via_s3 error: {e}")
        return _json({"error":"internal server error"}, 500)

# Redirect to CloudFront URL for image access
def _img_get_via_cf(key):
    if not CF_DOMAIN:
        return _json({"error": "cf domain not configured"}, 500)
    cf_path = key.lstrip('/')
    url = f"https://{CF_DOMAIN}/{cf_path}"
    return _resp(302, "", {"Location": url})
 
# Resolve thumbnail key from metadata in DynamoDB
def _resolve_thumb_key(pk):
    try:
        res = _table().get_item(Key={"pk":pk})
        item = res.get("Item")
        if not item:
            return None, _json({"error":"not found"}, 404)
        return item.get("thumb"), None
    except Exception as e:
        logging.error(f"resolve_thumb_key error: {e}")
        return None, _json({"error":"internal server error"}, 500)

# Handle image requests, supporting GET and HEAD methods
def _handle_img(key, method):
    if method not in ("GET", "HEAD"):
        return _json({"error":"invalid method"}, 405)

    thumb_key, err = _resolve_thumb_key(key)
    if not thumb_key:
        return _json({"error":"not found"}, 404)
    
    if ENABLE_CLOUDFRONT:
        resp = _img_get_via_cf(thumb_key)
    else:
        resp = _img_get_via_s3(thumb_key)
    
    if method == "HEAD":
        resp["body"] = ""
        if resp.get("statusCode") == 200:
            resp["statusCode"] = 204
        headers = resp.get("headers", {})
        resp["headers"] = headers
    return resp

# Main Lambda handler function
def handler(event, context):

    try:
        path, key, method = _extract(event)

        if path.startswith("/health") and method == "GET":
            return _json({"ok":True})

        if not key:
            return _json({"error":"missing key"}, 400)
        
        if path.startswith("/meta"):
            return _handle_meta(key, method)

        if path.startswith("/img"):
            return _handle_img(key, method)
        
        return _json({"error":"not found"}, 404)

    except Exception as e:
        logging.error(f"extract error: {e}")
        return _json({"error":"invalid request"}, 400)
