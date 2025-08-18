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
ENABLE_CLOUDFRONT = os.getenv("ENABLE_CLOUDFRONT", "false").lower() == "true"
ENABLE_REDIS      = os.getenv("ENABLE_REDIS", "false").lower() == "true"

TABLE = os.environ["TABLE"]
DST_BUCKET = os.environ["DST_BUCKET"]
CF_DOMAIN = os.environ.get("CF_DOMAIN", "").strip()

# ---------- AWS clients ----------
ddb = boto3.resource("dynamodb")
s3  = boto3.client("s3")


_redis_client = None
_DEF_META_TTL = 300

def _table():
    return ddb.Table(TABLE)

def _resp(status, body="", headers=None, ctype="application/json"):
    h = {"Content-Type": ctype}
    if headers: 
        h.update(headers)
    return {"statusCode": status, "headers": h, "body": body or ""}

def _json(data, status=200):
    return _resp(status, json.dumps(data, default=str))

def _extract(event):
    path = event.get("rawPath") or event.get("path", "")
    method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod") or "GET"
    path_params = event.get("pathParameters") or {}
    key = path_params.get("key") or path_params.get('proxy')
    return path, key, method

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
            _redis_client = redis.Redis.from_url(host, decode_responses=True)
        else:
            parts = host.split(":", 1)
            h = parts[0]
            p = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 6379
            _redis_client = redis.Redis(host=h, port=p, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        logging.error(f"get_redis_client error: {e}")
        return None

def _cache_ttl_from_item(item):
    try:
        now = int(time.time())
        raw = item.get("ttl")
        if raw is None:
            return _DEF_META_TTL
        ttl_epoch = int(raw) if not isinstance(raw, Decimal) else int(raw)
        secs = max(60, min(ttl_epoch - now, 3600))
        return secs
    except Exception as e:
        logging.error(f"cache_ttl_from_item error: {e}")
        return _DEF_META_TTL

# =========================
# META branch
# =========================
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

def _meta_get_via_redis(pk):
    client = _get_redis_client()
    if not client:
        return _meta_get_via_ddb(pk)
    try:
        nkey = f"meta404:{pk}"
        if client.get(nkey):
            return _json({"error":"not found"}, 404)
        ckey = f"meta:{pk}"
        cached = client.get(ckey)
        if cached:
            return _resp(200, cached)
        res = _table().get_item(Key={"pk":pk})
        item = res.get("Item")
        if not item:
            try:
                client.setex(nkey, 30, "1")
            except Exception as e:
                pass
            return _json({"error":"not found"}, 404)
        body = json.dumps(item, default=str)
        try:
            client.setex(ckey, _cache_ttl_from_item(item), body)
        except Exception as e:
            pass
        return _resp(200, body)
    except Exception as e:
        logging.error(f"meta_get_via_redis error: {e}")
        return _meta_get_via_ddb(pk)

def _handle_meta(key, method):
    if method == "GET":
        if ENABLE_REDIS:
            return _meta_get_via_redis(key)
        return _meta_get_via_ddb(key)
    else:
        return _json({"error":"invalid method"}, 405)
    
# =========================
# IMG branch
# =========================
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

def _img_get_via_cf(key):
    if not CF_DOMAIN:
        return _json({"error": "cf domain not configured"}, 500)
    cf_path = key.lstrip('/')
    url = f"https://{CF_DOMAIN}/{cf_path}"
    return _resp(302, "", {"Location": url})
 
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

