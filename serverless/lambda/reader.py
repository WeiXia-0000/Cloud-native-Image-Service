import os, json
from decimal import Decimal

import boto3

TABLE = os.environ["TABLE"]
DST_BUCKET = os.environ["DST_BUCKET"]
CF_DOMAIN = os.environ.get("CF_DOMAIN", "").strip()

ddb = boto3.resource("dynamodb")

def _resp(status, body="", headers=None):
    h = {"Content-Type": "application/json"}
    if headers: h.update(headers)
    return {"statusCode": status, "headers": h, "body": body}

def _json(data, status=200):
    return _resp(status, json.dumps(data, default=str))

def handler(event, context):
    # 兼容 REST API 的 event 形态
    raw_path = event.get("rawPath") or event.get("path", "")
    path_params = event.get("pathParameters") or {}
    key = path_params.get("key")

    # /health
    if raw_path.endswith("/health"):
        return _json({"ok": True})

    # /meta/{key}
    if raw_path.startswith("/meta/"):
        if not key:
            return _json({"error":"missing key"}, 400)
        item = _get_meta(key)
        if not item:
            return _json({"error":"not found"}, 404)
        return _json(item)

    # /img/{key} -> 302 重定向到 CloudFront（若配置）或 S3 公有 URL
    if raw_path.startswith("/img/"):
        if not key:
            return _json({"error":"missing key"}, 400)
        item = _get_meta(key)
        if not item:
            return _json({"error":"not found"}, 404)

        thumb_key = item["thumb"]
        if CF_DOMAIN:
            url = f"https://{CF_DOMAIN}/{thumb_key}"
        else:
            # 简单起见：直接用 S3 公有 URL（后续可改成预签名或 CloudFront）
            url = f"https://{DST_BUCKET}.s3.amazonaws.com/{thumb_key}"
        return _resp(302, "", {"Location": url})

    return _json({"error":"not found"}, 404)

def _get_meta(pk):
    table = ddb.Table(TABLE)
    res = table.get_item(Key={"pk": pk})
    return res.get("Item")