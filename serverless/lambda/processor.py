# processor.py
import os
import io
import time
import json
import urllib.parse

import boto3
from PIL import Image

s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb")

TABLE = os.environ["TABLE"]
DST_BUCKET = os.environ["DST_BUCKET"]

IMG_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff")

def _resize_to_jpeg(src_bytes, max_size=(800, 800)):
    with Image.open(io.BytesIO(src_bytes)) as img:
        img.thumbnail(max_size)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", optimize=True, quality=85)
        out.seek(0)
        return out

def handler(event, context):
    # S3 事件可能包含多条记录
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"]
        key = urllib.parse.unquote(rec["s3"]["object"]["key"])

        # 只处理图片后缀，避免非图片或子目录触发
        lower = key.lower()
        if not lower.endswith(IMG_SUFFIXES):
            print(f"skip non-image object: {key}")
            continue

        base = os.path.splitext(os.path.basename(key))[0]
        out_key = f"resized/{base}-800.jpg"

        # 下载源图
        obj = s3.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()

        # 处理成 800px JPEG
        out_buf = _resize_to_jpeg(body, max_size=(800, 800))

        # 上传到目标桶
        s3.put_object(
            Bucket=DST_BUCKET,
            Key=out_key,
            Body=out_buf.getvalue(),
            ContentType="image/jpeg",
        )

        # 写入/更新元数据（DynamoDB）
        table = ddb.Table(TABLE)
        now = int(time.time())
        item = {
            "pk": key,                
            "thumb": out_key,          
            "formats": ["jpg"],
            "updatedAt": now,
            "ttl": now + 7*24*3600    
        }
        table.put_item(Item=item)

        print(json.dumps({"ok": True, "src": key, "dst": out_key}))

    return {"status": "done"}