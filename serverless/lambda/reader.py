import os, json
import logging
from decimal import Decimal

import boto3


# ---------- Feature switches ----------
ENABLE_CLOUDFRONT = os.getenv("ENABLE_CLOUDFRONT", "false").lower() == "true"
ENABLE_REDIS      = os.getenv("ENABLE_REDIS", "false").lower() == "true"

TABLE = os.environ["TABLE"]
DST_BUCKET = os.environ["DST_BUCKET"]
CF_DOMAIN = os.environ.get("CF_DOMAIN", "").strip()

# ---------- AWS clients ----------
ddb = boto3.resource("dynamodb")
s3  = boto3.client("s3")

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

def _table():
    return ddb.Table(TABLE)

# =========================
# META branch (CRUD)
# =========================
def _meta_get_via_ddb(pk):
    return _json({"todo":"meta_get_via_ddb"})

def _meta_get_via_redis(pk):
    return _json({"todo":"meta_get_via_redis"})

def _meta_post_create(key):
    return _json({"todo":"meta_post_create"})

def _meta_put_update(pk):
    return _json({"todo":"meta_put_update"})

def _meta_delete(pk):
    return _json({"todo":"meta_delete"})

def _handle_meta(key, method):
    if method == "GET":
        if ENABLE_REDIS:
            return _meta_get_via_redis(key)
        return _meta_get_via_ddb(key)
    elif method == "POST":
        return _meta_post_create(key)
    elif method == "PUT":
        return _meta_put_update(key)
    elif method == "DELETE":
        return _meta_delete(key)
    else:
        return _json({"error":"invalid method"}, 405)
    
# =========================
# IMG branch (read)
# =========================
def _img_get_via_s3(key):
    return _json({"todo":"img_get_via_s3"})

def _img_get_via_cf(key):
    return _json({"todo":"img_get_via_cf"})
 
def _resolve_thumb_key(pk):
    return None, _json({"todo":"resolve_thumb_key"})

def _handle_img(key, method):
    if method not in ("GET", "HEAD"):
        return _json({"error":"invalid method"}, 405)

    thumb_key, err = _resolve_thumb_key(key)
    if not thumb_key:
        return _json({"error":"not found"}, 404)
    
    if ENABLE_CLOUDFRONT:
        return _img_get_via_cf(thumb_key)
    else:
        return _img_get_via_s3(thumb_key)

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

