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
    return _json({"todo":"meta_get_via_redis"})

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
    return _json({"todo":"img_get_via_cf"})
 
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

