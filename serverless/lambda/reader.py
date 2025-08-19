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
_REDIS_TIMEOUT_MS = int(os.getenv("REDIS_TIMEOUT_MS", "300"))  # default 300ms

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
        common_kwargs = dict(
            decode_responses=True,
            socket_timeout=_REDIS_TIMEOUT_MS / 1000.0,
            socket_connect_timeout=_REDIS_TIMEOUT_MS / 1000.0,
            retry_on_timeout=False,
            socket_keepalive=True,
            health_check_interval=30,
        )
        if host.startswith("redis://") or host.startswith("rediss://"):
            # If the host is a Redis connection URL, create client directly from it
            _redis_client = redis.Redis.from_url(host, **common_kwargs)
        else:
            # Otherwise, parse "host:port" format manually
            parts = host.split(":", 1)
            h = parts[0]
            p = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 6379
            _redis_client = redis.Redis(host=h, port=p, **common_kwargs)
        # Verify the connection by pinging Redis, raises error if not reachable
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        logging.error(f"get_redis_client error: {e}")
        return None


def _redis_get(client, key):
    """Single GET with error guarding."""
    try:
        return client.get(key)
    except Exception as e:
        logging.error(f"redis get error: {e}")
        return None


def _redis_setex(client, key, ttl_seconds, value):
    """Set key with TTL via pipeline to reduce RTT and partial failures."""
    try:
        pipe = client.pipeline(transaction=False)
        pipe.setex(key, ttl_seconds, value)
        pipe.execute()
    except Exception as e:
        logging.error(f"redis setex error: {e}")

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
        # Keys
        nkey = f"meta404:{pk}"
        ckey = f"meta:{pk}"

        # Fast-path: check positive cache first (common case)
        cval = _redis_get(client, ckey)
        if cval:
            return _resp(200, cval)
        # Then check negative cache
        nval = _redis_get(client, nkey)
        if nval:
            return _json({"error":"not found"}, 404)

        # Fallback to DynamoDB if Redis cache miss
        res = _table().get_item(Key={"pk":pk})
        item = res.get("Item")
        if not item:
            _redis_setex(client, nkey, 30, "1")
            return _json({"error":"not found"}, 404)
        body = json.dumps(item, default=str)

        # Store metadata in Redis with TTL for caching
        _redis_setex(client, ckey, _cache_ttl_from_item(item), body)
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
    elif method == "HEAD":
        # HEAD should mirror GET's status code but without a body.
        try:
            res = _table().get_item(Key={"pk": key})
            item_exists = "Item" in res and res["Item"] is not None
            if not item_exists:
                return _json({"error": "not found"}, 404)
            # Exists: return 200 with no body
            return _resp(200, "", {"Content-Type": "application/json"})
        except Exception as e:
            logging.error(f"meta HEAD error: {e}")
            return _json({"error": "internal server error"}, 500)
    else:
        # 405 with proper Allow header
        return _resp(405, json.dumps({"error": "invalid method"}), {"Allow": "GET, HEAD"})
    
# =========================
# IMG branch - handling image retrieval and redirection
# =========================
# Generate a presigned S3 URL for image access
def _img_get_via_s3(key):
    try:
        url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": DST_BUCKET, "Key": key},
            ExpiresIn=300  # 5 mins
        )
        # Align with CloudFront behavior: always redirect to the final content URL
        return _resp(302, "", {"Location": url})
    except Exception as e:
        logging.error(f"img_get_via_s3 error: {e}")
        return _json({"error": "internal server error"}, 500)

# Redirect to CloudFront URL for image access
def _img_get_via_cf(key):
    if not CF_DOMAIN:
        return _json({"error": "cf domain not configured"}, 500)
    cf_path = key.lstrip('/')
    url = f"https://{CF_DOMAIN}/{cf_path}"
    return _resp(302, "", {"Location": url})
 
# Resolve thumbnail key
def _resolve_thumb_key(pk):
    # Try Redis first
    client = _get_redis_client()
    nkey = f"meta404:{pk}"
    ckey = f"meta:{pk}"
    if client:
        try:
            # Positive first, then negative
            cval = _redis_get(client, ckey)
            if cval:
                try:
                    meta = json.loads(cval)
                    thumb_key = meta.get("thumb")
                    if thumb_key:
                        return thumb_key, None
                except Exception:
                    pass
            nval = _redis_get(client, nkey)
            if nval:
                return None, _json({"error":"not found"}, 404)
        except Exception as e:
            logging.error(f"resolve_thumb_key redis error: {e}")

    # Fallback to DynamoDB
    try:
        res = _table().get_item(Key={"pk":pk})
        item = res.get("Item")
        if not item:
            if client:
                _redis_setex(client, nkey, 30, "1")
            return None, _json({"error":"not found"}, 404)
        thumb_key = item.get("thumb")
        if client:
            body = json.dumps(item, default=str)
            _redis_setex(client, ckey, _cache_ttl_from_item(item), body)
        return thumb_key, None
    except Exception as e:
        logging.error(f"resolve_thumb_key error: {e}")
        return None, _json({"error":"internal server error"}, 500)

# Handle image requests, supporting GET and HEAD methods
def _handle_img(key, method):
    if method not in ("GET", "HEAD"):
        # 405 with proper Allow header
        return _resp(405, json.dumps({"error": "invalid method"}), {"Allow": "GET, HEAD"})

    thumb_key, err = _resolve_thumb_key(key)
    if err is not None:
        return err
    if not thumb_key:
        return _json({"error": "not found"}, 404)

    resp = _img_get_via_cf(thumb_key) if ENABLE_CLOUDFRONT else _img_get_via_s3(thumb_key)

    # For HEAD, do not include a body; keep status code (302 for redirects). If 200 body ever returned, convert to 204.
    if method == "HEAD":
        resp["body"] = ""
        if resp.get("statusCode") == 200:
            resp["statusCode"] = 204
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
