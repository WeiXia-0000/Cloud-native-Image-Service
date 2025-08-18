import importlib
import types
import os
import pathlib
import sys
import pytest


# ---- Helpers to load reader.py and build API Gateway-like events ----

def http_event(path: str, method: str = "GET", key: str | None = None):
    """HTTP API (v2) style event."""
    evt = {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "pathParameters": {},
    }
    if key is not None:
        evt["pathParameters"]["key"] = key
    return evt


def rest_event(path: str, method: str = "GET", key: str | None = None):
    """REST API (v1) style event."""
    evt = {
        "path": path,
        "httpMethod": method,
        "pathParameters": {},
    }
    if key is not None:
        evt["pathParameters"]["key"] = key
    return evt


def setup_min_env(monkeypatch):
    """Set minimal env vars expected by reader.py."""
    monkeypatch.setenv("TABLE", "dummy-table")
    monkeypatch.setenv("DST_BUCKET", "dummy-bucket")
    # Default: features off unless explicitly enabled per test
    monkeypatch.delenv("ENABLE_CLOUDFRONT", raising=False)
    monkeypatch.delenv("ENABLE_REDIS", raising=False)
    monkeypatch.delenv("CF_DOMAIN", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)


def load_reader_module():
    """Dynamically load serverless/lambda/reader.py as module 'reader'."""
    proj_root = pathlib.Path(__file__).parents[1]
    mod_path = proj_root / "serverless" / "lambda" / "reader.py"
    assert mod_path.exists(), f"reader.py not found at {mod_path}"
    if "reader" in sys.modules:
        del sys.modules["reader"]
    spec = importlib.util.spec_from_file_location("reader", mod_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)  # type: ignore
    return module


# --------------------- Tests ---------------------

# Health check returns 200 and ok true in body
def test_health_check(monkeypatch):
    setup_min_env(monkeypatch)
    reader = load_reader_module()

    resp = reader.handler(http_event("/health", method="GET"), None)
    assert resp["statusCode"] == 200
    assert '"ok": true' in resp["body"]


# Meta 404 returns 404 status when item missing
def test_meta_404_returns_404(monkeypatch):
    setup_min_env(monkeypatch)
    reader = load_reader_module()

    # Mock DynamoDB: return no Item
    class _T:
        def get_item(self, Key):
            return {}
    monkeypatch.setattr(reader, "_table", lambda: _T())

    evt = http_event("/meta/missing.jpg", method="GET", key="missing.jpg")
    res = reader.handler(evt, None)
    assert res["statusCode"] == 404
    assert "not found" in res["body"]


# Meta method not allowed returns 405
def test_meta_method_not_allowed(monkeypatch):
    setup_min_env(monkeypatch)
    reader = load_reader_module()

    res = reader.handler(http_event("/meta/sample.jpg", method="POST", key="sample.jpg"), None)
    assert res["statusCode"] == 405


# Img with CloudFront domain missing returns 500
def test_img_cf_domain_missing_returns_500(monkeypatch):
    setup_min_env(monkeypatch)
    # Enable CloudFront but leave CF_DOMAIN empty
    monkeypatch.setenv("ENABLE_CLOUDFRONT", "true")
    reader = load_reader_module()

    # Make resolver succeed so we reach CF branch
    monkeypatch.setattr(reader, "_resolve_thumb_key", lambda pk: ("resized/sample-800.jpg", None))

    res = reader.handler(http_event("/img/sample.jpg", key="sample.jpg"), None)
    assert res["statusCode"] == 500
    assert "cf domain not configured" in res["body"]


# Meta Redis disabled falls back to DynamoDB
def test_meta_redis_disabled_falls_back_to_ddb(monkeypatch):
    setup_min_env(monkeypatch)
    # Explicitly disable Redis
    monkeypatch.setenv("ENABLE_REDIS", "false")
    reader = load_reader_module()

    # Simulate DDB returning an item
    class _T:
        def get_item(self, Key):
            return {"Item": {"pk": "sample.jpg", "thumb": "resized/sample-800.jpg"}}
    monkeypatch.setattr(reader, "_table", lambda: _T())

    res = reader.handler(http_event("/meta/sample.jpg", key="sample.jpg"), None)
    assert res["statusCode"] == 200
    assert "sample.jpg" in res["body"]


# Missing key returns 400 error
def test_missing_key_returns_400(monkeypatch):
    setup_min_env(monkeypatch)
    reader = load_reader_module()

    res = reader.handler(http_event("/meta/", key=None), None)
    assert res["statusCode"] == 400
    assert "missing key" in res["body"]


# Img HEAD returns 204 no content
def test_img_head_returns_204(monkeypatch):
    setup_min_env(monkeypatch)
    reader = load_reader_module()

    # Force S3 branch (CF disabled)
    monkeypatch.setenv("ENABLE_CLOUDFRONT", "false")

    # Make resolver return a valid thumb so we hit _img_get_via_s3
    monkeypatch.setattr(reader, "_resolve_thumb_key", lambda pk: ("resized/sample-800.jpg", None))

    # Monkeypatch S3 client presign result to deterministic body
    def fake_presign(ClientMethod, Params, ExpiresIn):  # noqa: N803
        return "https://example.com/presigned"
    monkeypatch.setattr(reader, "s3", types.SimpleNamespace(generate_presigned_url=fake_presign))

    res = reader.handler(http_event("/img/sample.jpg", method="HEAD", key="sample.jpg"), None)
    assert res["statusCode"] == 204
    assert res["body"] == ""


# Baseline: CF disabled, Redis disabled. /img should return 200 JSON with presigned URL.
def test_baseline_img_returns_presigned_url(monkeypatch):
    setup_min_env(monkeypatch)
    monkeypatch.setenv("ENABLE_CLOUDFRONT", "false")
    monkeypatch.setenv("ENABLE_REDIS", "false")
    reader = load_reader_module()

    # Resolve thumb key successfully
    monkeypatch.setattr(reader, "_resolve_thumb_key", lambda pk: ("resized/sample-800.jpg", None))

    # Fake S3 presign URL
    def fake_presign(ClientMethod, Params, ExpiresIn):  # noqa: N803
        assert Params["Bucket"] == "dummy-bucket"
        assert Params["Key"] == "resized/sample-800.jpg"
        return "https://example.com/presigned"
    monkeypatch.setattr(reader, "s3", types.SimpleNamespace(generate_presigned_url=fake_presign))

    res = reader.handler(http_event("/img/sample.jpg", method="GET", key="sample.jpg"), None)
    assert res["statusCode"] == 200
    assert "https://example.com/presigned" in res["body"]
    # Should not be a redirect
    assert res["headers"].get("Location") is None


# Better: CF enabled with domain; /img should 302 to CF URL.
def test_better_img_returns_302_cloudfront(monkeypatch):
    setup_min_env(monkeypatch)
    monkeypatch.setenv("ENABLE_CLOUDFRONT", "true")
    monkeypatch.setenv("CF_DOMAIN", "d111111abcdef8.cloudfront.net")
    reader = load_reader_module()

    monkeypatch.setattr(reader, "_resolve_thumb_key", lambda pk: ("resized/sample-800.jpg", None))

    res = reader.handler(http_event("/img/sample.jpg", method="GET", key="sample.jpg"), None)
    assert res["statusCode"] == 302
    assert res["headers"]["Location"] == "https://d111111abcdef8.cloudfront.net/resized/sample-800.jpg"


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.get_calls = []
        self.setex_calls = []
    def get(self, k):
        self.get_calls.append(k)
        return self.store.get(k)
    def setex(self, k, ttl, v):
        self.setex_calls.append((k, ttl))
        self.store[k] = v
    def ping(self):
        return True


# Redis enabled: First request fills cache from DDB; second serves from Redis.
def test_redis_meta_hit_and_fill(monkeypatch):
    setup_min_env(monkeypatch)
    monkeypatch.setenv("ENABLE_REDIS", "true")
    reader = load_reader_module()

    # Inject fake redis client
    fake = _FakeRedis()
    monkeypatch.setattr(reader, "_get_redis_client", lambda: fake)

    # DDB returns an item on miss
    class _T:
        calls = 0
        def get_item(self, Key):
            _T.calls += 1
            return {"Item": {"pk": "sample.jpg", "thumb": "resized/sample-800.jpg", "ttl": 9999999999}}
    monkeypatch.setattr(reader, "_table", lambda: _T())

    # First request -> miss -> DDB -> cache fill
    r1 = reader.handler(http_event("/meta/sample.jpg", key="sample.jpg"), None)
    assert r1["statusCode"] == 200
    assert any(k.startswith("meta:") for k in fake.store.keys())
    assert _T.calls == 1

    # Second request -> hit from Redis (no extra DDB calls)
    r2 = reader.handler(http_event("/meta/sample.jpg", key="sample.jpg"), None)
    assert r2["statusCode"] == 200
    assert _T.calls == 1


# Redis negative cache: 404 cached, second request should not hit DDB again.
def test_redis_meta_negative_cache(monkeypatch):
    setup_min_env(monkeypatch)
    monkeypatch.setenv("ENABLE_REDIS", "true")
    reader = load_reader_module()

    fake = _FakeRedis()
    monkeypatch.setattr(reader, "_get_redis_client", lambda: fake)

    class _T:
        calls = 0
        def get_item(self, Key):
            _T.calls += 1
            return {}  # not found
    monkeypatch.setattr(reader, "_table", lambda: _T())

    # First -> 404 from DDB and set negative cache
    r1 = reader.handler(http_event("/meta/missing.jpg", key="missing.jpg"), None)
    assert r1["statusCode"] == 404
    assert any(k.startswith("meta404:") for k in fake.store.keys())
    first_calls = _T.calls

    # Second -> served from negative cache, no extra DDB calls
    r2 = reader.handler(http_event("/meta/missing.jpg", key="missing.jpg"), None)
    assert r2["statusCode"] == 404
    assert _T.calls == first_calls