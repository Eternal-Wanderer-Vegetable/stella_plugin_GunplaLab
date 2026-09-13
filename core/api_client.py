"""胶情局开放数据 API 客户端。

特性：
- 统一响应外壳（envelope）解析，业务错误映射为 GunplaApiError；
- ETag / If-None-Match 协商缓存（manifest、snapshot 均可用）；
- 限流感知：读取 X-RateLimit-Remaining，429 时进入冷却窗口并快速失败；
- item_id 含特殊字符（如 ``HGUC#21``、``ROBOT魂``），路径参数一律 URL 编码。
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class GunplaApiError(RuntimeError):
    """开放数据 API 业务错误（HTTP >= 400 或 envelope ok=false）。"""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.api_message = message
        self.status = status


class RateLimitedError(GunplaApiError):
    """429 限流。reset_seconds 为服务端滑动窗口重置倒计时。"""

    def __init__(self, reset_seconds: int, limit: int | None = None):
        super().__init__(
            "RATE_LIMIT_EXCEEDED",
            f"请求频率超出配额（{limit or '?'} 次/分钟），冷却 {reset_seconds}s 后恢复",
            429,
        )
        self.reset_seconds = max(1, reset_seconds)
        self.limit = limit


@dataclass
class ApiResponse:
    """一次 API 响应的解包结果。304 时 data 为 None 且 not_modified=True。"""

    status: int
    data: Any | None
    etag: str | None
    not_modified: bool
    data_as_of: str | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset: int | None = None


class GunplaLabClient:
    """对 /api/v1/data/* 的薄封装。一个实例对应一个部署点，可安全并发。"""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        min_interval: float = 1.5,
        transport: httpx.AsyncBaseTransport | None = None,
        trust_env: bool = False,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = (api_key or "").strip()
        self._min_interval = min_interval
        self._transport = transport  # 测试注入点（httpx.MockTransport）
        # 默认不读系统代理：数据 API 通常是本机/局域网地址，
        # Windows 注册表代理（如 Clash）会把回环请求发往代理导致 502
        self._trust_env = trust_env
        self._throttle_lock = asyncio.Lock()
        self._next_allowed = 0.0
        self._cooldown_until = 0.0
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Accept": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=True,
                transport=self._transport,
                trust_env=self._trust_env,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _throttle(self) -> None:
        """串行化远程请求：尊重 429 冷却窗口 + 最小请求间隔。"""
        async with self._throttle_lock:
            now = time.monotonic()
            if now < self._cooldown_until:
                raise RateLimitedError(int(self._cooldown_until - now) + 1)
            if now < self._next_allowed:
                await asyncio.sleep(self._next_allowed - now)
            self._next_allowed = time.monotonic() + self._min_interval

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        if_none_match: str | None = None,
    ) -> ApiResponse:
        await self._throttle()
        client = await self._ensure_client()
        headers = {}
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        try:
            resp = await client.get(path, params=params or None, headers=headers)
        except httpx.HTTPError as exc:
            raise GunplaApiError("NETWORK_ERROR", f"无法连接胶情局数据服务：{exc}") from exc

        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")

        if resp.status_code == 304:
            return ApiResponse(
                status=304,
                data=None,
                etag=if_none_match,
                not_modified=True,
                rate_limit_remaining=_to_int(remaining),
                rate_limit_reset=_to_int(reset),
            )
        if resp.status_code == 429:
            reset_seconds = _to_int(reset) or 60
            self._cooldown_until = time.monotonic() + max(reset_seconds, 5)
            raise RateLimitedError(reset_seconds, _to_int(resp.headers.get("X-RateLimit-Limit")))

        if resp.status_code >= 400:
            raise _envelope_error(resp)

        payload = _parse_envelope(resp)
        if payload.get("ok") is not True:
            err = payload.get("error") or {}
            raise GunplaApiError(
                str(err.get("code", "UNKNOWN_ERROR")),
                str(err.get("message", "未知错误")),
                resp.status_code,
            )
        meta = payload.get("meta") or {}
        return ApiResponse(
            status=resp.status_code,
            data=payload.get("data"),
            etag=resp.headers.get("ETag"),
            not_modified=False,
            data_as_of=str(meta.get("data_as_of") or "") or None,
            rate_limit_remaining=_to_int(remaining),
            rate_limit_reset=_to_int(reset),
        )

    # ------------------------------------------------------------------
    # 业务端点
    # ------------------------------------------------------------------

    async def get_manifest(self, if_none_match: str | None = None) -> ApiResponse:
        return await self._request("/manifest", if_none_match=if_none_match)

    async def download_snapshot(
        self, if_none_match: str | None = None
    ) -> tuple[bytes | None, str | None]:
        """下载全量快照。

        服务端固定返回 ``Content-Encoding: gzip``，httpx 会透明解压，
        因此这里拿到的是**解压后的 JSON 字节**；本地落盘前自行压缩。
        返回 ``(json_bytes_or_None, etag)``，304 时 json_bytes 为 None。
        """
        await self._throttle()
        client = await self._ensure_client()
        headers = {"If-None-Match": if_none_match} if if_none_match else {}
        try:
            resp = await client.get("/snapshot", headers=headers)
        except httpx.HTTPError as exc:
            raise GunplaApiError("NETWORK_ERROR", f"无法连接胶情局数据服务：{exc}") from exc

        if resp.status_code == 304:
            return None, if_none_match
        if resp.status_code == 429:
            reset_seconds = _to_int(resp.headers.get("X-RateLimit-Reset")) or 60
            self._cooldown_until = time.monotonic() + max(reset_seconds, 5)
            raise RateLimitedError(reset_seconds, _to_int(resp.headers.get("X-RateLimit-Limit")))
        if resp.status_code >= 400:
            raise _envelope_error(resp)
        return resp.content, resp.headers.get("ETag")

    async def search_items(self, q: str, page_size: int = 5) -> list[dict]:
        """远程模糊检索（服务端 LIKE 覆盖别名与日文名），用于本地未命中时的兜底。"""
        resp = await self._request("/items", params={"q": q, "page_size": page_size})
        items = (resp.data or {}).get("items") or []
        return items

    async def get_item(self, item_id: str) -> dict:
        """单款模型全景档案（含 market_observation 现货行情）。"""
        path = "/items/" + urllib.parse.quote(item_id, safe="")
        resp = await self._request(path)
        return resp.data or {}

    async def get_item_prices(self, item_id: str) -> dict:
        """单款模型价格观测（含来源商品链接 source_url）。"""
        path = "/items/" + urllib.parse.quote(item_id, safe="") + "/prices"
        resp = await self._request(path)
        return resp.data or {}

    async def get_item_history(self, item_id: str, days: int = 180) -> dict:
        """单款模型时序价格走势（days 上限 365，服务端强制）。"""
        days = min(max(1, int(days)), 365)
        path = "/items/" + urllib.parse.quote(item_id, safe="") + "/history"
        resp = await self._request(path, params={"days": days})
        return resp.data or {}

    async def get_release_events(self, month: str = "", limit: int = 100) -> tuple[dict, str]:
        """官方发售/再版排期日历。month 形如 2026-09，留空返回全部。返回 (data, data_as_of)。"""
        params: dict[str, Any] = {"limit": limit}
        if month:
            params["month"] = month
        resp = await self._request("/release-events", params=params)
        return resp.data or {}, resp.data_as_of or ""

    async def get_intel_events(self, limit: int = 50) -> tuple[dict, str]:
        """官方最新情报雷达。返回 (data, data_as_of)。"""
        resp = await self._request("/intel-events", params={"limit": limit})
        return resp.data or {}, resp.data_as_of or ""


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_envelope(resp: httpx.Response) -> dict:
    try:
        payload = resp.json()
    except ValueError as exc:
        raise GunplaApiError(
            "BAD_RESPONSE", f"响应不是合法 JSON（HTTP {resp.status_code}）", resp.status_code
        ) from exc
    if not isinstance(payload, dict):
        raise GunplaApiError("BAD_RESPONSE", "响应外壳不是对象", resp.status_code)
    return payload


def _envelope_error(resp: httpx.Response) -> GunplaApiError:
    code_map = {
        400: "PARAM_MISSING",
        401: "UNAUTHORIZED",
        404: "ITEM_NOT_FOUND",
    }
    try:
        err = resp.json().get("error") or {}
        code = str(err.get("code") or code_map.get(resp.status_code, "HTTP_ERROR"))
        message = str(err.get("message") or resp.text[:120])
    except ValueError:
        code = code_map.get(resp.status_code, "HTTP_ERROR")
        message = f"HTTP {resp.status_code}"
    return GunplaApiError(code, message, resp.status_code)
