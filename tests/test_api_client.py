"""API 客户端测试：envelope 解析、错误映射、304/429、URL 编码（httpx.MockTransport）。"""

import json

import httpx
import pytest

from core.api_client import GunplaApiError, GunplaLabClient, RateLimitedError

BASE = "http://test.local/api/v1/data"


def envelope(data=None, ok=True, error=None):
    payload = {"ok": ok, "meta": {"api_version": "v1"}}
    if ok:
        payload["data"] = data
    else:
        payload["error"] = error
    return payload


def ok_handler(data, etag='"abc"', status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json=envelope(data),
            headers={"ETag": etag, "X-RateLimit-Remaining": "29", "X-RateLimit-Reset": "42"},
        )
    return handler


@pytest.mark.asyncio
async def test_manifest_success_and_etag():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["inm"] = request.headers.get("If-None-Match")
        return httpx.Response(
            200,
            json=envelope({"total_items": 19070}),
            headers={"ETag": '"m1"', "X-RateLimit-Remaining": "29", "X-RateLimit-Reset": "42"},
        )

    client = GunplaLabClient(BASE, min_interval=0, transport=httpx.MockTransport(handler))
    resp = await client.get_manifest(if_none_match='"m0"')
    assert resp.data["total_items"] == 19070
    assert resp.etag == '"m1"'
    assert seen["inm"] == '"m0"'
    assert resp.rate_limit_remaining == 29
    await client.close()


@pytest.mark.asyncio
async def test_manifest_304():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("If-None-Match") == '"m1"'
        return httpx.Response(304, headers={"ETag": '"m1"'})

    client = GunplaLabClient(BASE, min_interval=0, transport=httpx.MockTransport(handler))
    resp = await client.get_manifest(if_none_match='"m1"')
    assert resp.not_modified is True
    assert resp.data is None
    await client.close()


@pytest.mark.asyncio
async def test_item_not_found_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        # httpx 的 request.url.path 是解码后的；线上 URL 必须是 %23 编码
        assert "HGUC%2321" in str(request.url), str(request.url)
        return httpx.Response(
            404, json=envelope(ok=False, error={"code": "ITEM_NOT_FOUND", "message": "未找到模型"})
        )

    client = GunplaLabClient(BASE, min_interval=0, transport=httpx.MockTransport(handler))
    with pytest.raises(GunplaApiError) as excinfo:
        await client.get_item("HGUC#21")  # 特殊字符必须被 URL 编码
    assert excinfo.value.code == "ITEM_NOT_FOUND"
    await client.close()


@pytest.mark.asyncio
async def test_unauthorized_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer tok-1"
        return httpx.Response(
            401, json=envelope(ok=False, error={"code": "UNAUTHORIZED", "message": "无效令牌"})
        )

    client = GunplaLabClient(BASE, api_key="tok-1", min_interval=0, transport=httpx.MockTransport(handler))
    with pytest.raises(GunplaApiError) as excinfo:
        await client.get_manifest()
    assert excinfo.value.code == "UNAUTHORIZED"
    await client.close()


@pytest.mark.asyncio
async def test_rate_limit_cooldown():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429,
            json=envelope(ok=False, error={"code": "RATE_LIMIT_EXCEEDED", "message": "超频"}),
            headers={"X-RateLimit-Reset": "37", "X-RateLimit-Limit": "30"},
        )

    client = GunplaLabClient(BASE, min_interval=0, transport=httpx.MockTransport(handler))
    with pytest.raises(RateLimitedError) as excinfo:
        await client.get_manifest()
    assert excinfo.value.reset_seconds == 37
    # 冷却窗口内的第二次请求应快速失败且不再发起网络请求
    with pytest.raises(RateLimitedError) as excinfo2:
        await client.get_manifest()
    assert excinfo2.value.reset_seconds <= 37
    assert calls["n"] == 1
    await client.close()


@pytest.mark.asyncio
async def test_snapshot_download_returns_decompressed_json():
    raw_json = json.dumps({"meta": {}, "items": [{"id": "a"}]}, ensure_ascii=False).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        # 服务端固定 Content-Encoding: gzip；httpx 透明解压后应拿到原始 JSON
        return httpx.Response(200, content=raw_json, headers={"ETag": '"s1"'})

    client = GunplaLabClient(BASE, min_interval=0, transport=httpx.MockTransport(handler))
    body, etag = await client.download_snapshot()
    assert json.loads(body.decode("utf-8"))["items"][0]["id"] == "a"
    assert etag == '"s1"'
    await client.close()


@pytest.mark.asyncio
async def test_search_items_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["q"] = request.url.params.get("q")
        seen["page_size"] = request.url.params.get("page_size")
        return httpx.Response(200, json=envelope({"items": [{"item_id": "78dm_ct_1"}]}))

    client = GunplaLabClient(BASE, min_interval=0, transport=httpx.MockTransport(handler))
    rows = await client.search_items("海牛", page_size=5)
    assert seen["q"] == "海牛"
    assert seen["page_size"] == "5"
    assert rows[0]["item_id"] == "78dm_ct_1"
    await client.close()
