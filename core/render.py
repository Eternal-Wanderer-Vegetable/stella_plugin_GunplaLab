"""图片卡渲染：Jinja2 模板 + 封面图磁盘缓存。

契约（双框架一致）：
- 模板**源码**交给宿主的 ``Star.html_render(tmpl, data)``，返回本地文件路径；
- Stella 渲染不可用时返回空串（不抛异常）→ 调用方必须保留纯文本降级；
- 封面图下载后 base64 内嵌模板，磁盘按 URL 哈希缓存（LRU/TTL），失败返回 None 不阻塞。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger("gunplalab.render")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_COVER_TIMEOUT = httpx.Timeout(8.0, connect=4.0)


def load_template(name: str) -> str:
    """读取 templates/ 下的模板源码（调用方交给 html_render）。"""
    return (TEMPLATES_DIR / name).read_text("utf-8")


class CoverCache:
    """封面图缓存：文件名 = sha1(url)，TTL 内直接复用。"""

    def __init__(self, cache_dir: Path, ttl_days: int = 7):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(1, int(ttl_days)) * 86400
        self._client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def _path_for(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.img"

    def _fresh(self, path: Path) -> bool:
        try:
            return (time.time() - path.stat().st_mtime) < self.ttl_seconds
        except OSError:
            return False

    async def get_data_uri(self, url: str) -> str | None:
        """封面 → data URI。任何失败都返回 None（卡片以占位块呈现）。"""
        if not url:
            return None
        path = self._path_for(url)
        raw: bytes | None = None
        if self._fresh(path):
            try:
                raw = path.read_bytes()
            except OSError:
                raw = None
        if raw is None:
            raw = await self._download(url)
            if raw is not None:
                tmp = path.with_suffix(".tmp")
                try:
                    tmp.write_bytes(raw)
                    tmp.replace(path)
                except OSError:
                    pass
        if not raw:
            return None
        mime = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else (
            "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/webp"
        )
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    async def _download(self, url: str) -> bytes | None:
        # 不读系统代理：封面 CDN 与数据 API 同属直连场景（同 api_client 的取舍）
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                follow_redirects=True, timeout=_COVER_TIMEOUT, trust_env=False
            )
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            logger.debug("封面下载失败 %s：%s", url, exc)
            return None
        if resp.status_code != 200 or not resp.content:
            return None
        if len(resp.content) > 4 * 1024 * 1024:  # 卡片不需要超大图
            return None
        return resp.content
