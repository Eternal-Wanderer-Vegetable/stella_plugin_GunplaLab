"""快照存储与检索：下载、gzip 缓存、内存索引、别名学习与搜索。

对应方案「模式 A」：全量快照常驻本地内存，群聊查询零网络请求。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from .api_client import ApiResponse, GunplaApiError, GunplaLabClient

logger = logging.getLogger("gunplalab.snapshot")

SNAPSHOT_SCHEMA = "gunplalab_snapshot_v1"
ALIAS_CACHE_LIMIT = 5000

# 常见比例写法，归一化时剥离（如 1/144、1／100）
_SCALE_RE = re.compile(r"1\s*[/／:：]\s*\d{2,4}")
# 装饰性标点与括号
_PUNCT_RE = re.compile(r"[【】\[\]()（）「」『』·・,，。.\-—_~～+'\"“”‘’!?！？]")
_GRADE_TOKENS = (
    "mgsd", "hguc", "hgce", "hgac", "msgg", "s.h.f", "shf", "figma",
    "robot", "rg", "mg", "hg", "pg", "eg", "sd", "bb", "fm",
)


def normalize(text: Any) -> str:
    """检索归一化：NFKC 全半角、小写、去空白/比例/装饰标点。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).lower()
    s = _SCALE_RE.sub("", s)
    s = _PUNCT_RE.sub("", s)
    return re.sub(r"\s+", "", s)


class SnapshotStore:
    """内存索引 + 磁盘缓存。所有公开方法均可在无网络环境下工作。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._gz_path = self.data_dir / "snapshot.json.gz"
        self._meta_path = self.data_dir / "snapshot_meta.json"
        self._alias_path = self.data_dir / "alias_cache.json"

        self.items: dict[str, dict] = {}
        self.meta: dict = {}
        self.manifest_etag: str | None = None
        self.snapshot_etag: str | None = None
        self.snapshot_sha256: str | None = None

        # 归一化索引
        self._norm_name: dict[str, str] = {}       # item_id -> 归一化 name
        self._norm_display: dict[str, str] = {}    # item_id -> 归一化 displayName
        self._norm_num: dict[str, list[str]] = {}  # 归一化 num -> [item_id]
        self._jan: dict[str, str] = {}             # jan -> item_id
        self._alias: dict[str, list[str]] = {}     # 归一化别名 -> [item_id]

    # ------------------------------------------------------------------
    # 磁盘缓存
    # ------------------------------------------------------------------

    def load_from_disk(self) -> bool:
        """加载本地缓存。成功返回 True；缓存缺失或损坏返回 False。"""
        if not self._gz_path.is_file():
            return False
        try:
            raw = gzip.decompress(self._gz_path.read_bytes())
            payload = json.loads(raw.decode("utf-8"))
            items = payload.get("items")
            if not isinstance(items, list) or not items:
                return False
            meta = json.loads(self._meta_path.read_text("utf-8")) if self._meta_path.is_file() else {}
            self._install(items, payload.get("meta") or {})
            self.manifest_etag = meta.get("manifest_etag")
            self.snapshot_etag = meta.get("snapshot_etag")
            self.snapshot_sha256 = meta.get("sha256")
            self._load_alias_cache()
            return True
        except Exception as exc:  # 缓存损坏不应阻塞启动
            logger.warning("本地快照缓存加载失败，将重新同步：%s", exc)
            return False

    def _save_to_disk(self, gz_bytes: bytes) -> None:
        tmp = self._gz_path.with_suffix(".tmp")
        tmp.write_bytes(gz_bytes)
        os.replace(tmp, self._gz_path)
        self._write_meta()

    def _write_meta(self) -> None:
        meta = {
            "manifest_etag": self.manifest_etag,
            "snapshot_etag": self.snapshot_etag,
            "sha256": self.snapshot_sha256,
            "fetched_at": self.meta.get("generated_at", ""),
        }
        tmp = self._meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self._meta_path)

    # ------------------------------------------------------------------
    # 索引构建
    # ------------------------------------------------------------------

    def _install(self, items: list[dict], meta: dict) -> None:
        items_by_id: dict[str, dict] = {}
        norm_name: dict[str, str] = {}
        norm_display: dict[str, str] = {}
        norm_num: dict[str, list[str]] = {}
        jan_map: dict[str, str] = {}
        for item in items:
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            items_by_id[item_id] = item
            norm_name[item_id] = normalize(item.get("name"))
            norm_display[item_id] = normalize(item.get("displayName"))
            num = normalize(item.get("num"))
            if num:
                norm_num.setdefault(num, []).append(item_id)
            jan = str(item.get("jan") or "").strip()
            if jan:
                jan_map[jan] = item_id

        self.items = items_by_id
        self._norm_name = norm_name
        self._norm_display = norm_display
        self._norm_num = norm_num
        self._jan = jan_map
        self.meta = dict(meta)

    def _load_alias_cache(self) -> None:
        cache: dict[str, list[str]] = {}
        if self._alias_path.is_file():
            try:
                cache = json.loads(self._alias_path.read_text("utf-8"))
            except Exception:
                cache = {}
        # 重放到新索引上，丢弃指向已下架条目的映射
        self._alias = {
            key: [i for i in ids if i in self.items]
            for key, ids in cache.items()
            if isinstance(ids, list) and ids
        }
        self._alias = {key: ids for key, ids in self._alias.items() if ids}

    def _persist_alias_cache(self) -> None:
        try:
            tmp = self._alias_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._alias, ensure_ascii=False), "utf-8")
            os.replace(tmp, self._alias_path)
        except OSError as exc:
            logger.warning("别名缓存写盘失败：%s", exc)

    def learn_aliases(self, pairs: list[tuple[str, str]]) -> None:
        """批量记录「俗称 -> item_id」映射并持久化。"""
        changed = False
        for alias, item_id in pairs:
            key = normalize(alias)
            if not key or item_id not in self.items:
                continue
            bucket = self._alias.setdefault(key, [])
            if item_id not in bucket:
                bucket.insert(0, item_id)
                changed = True
        if changed:
            if len(self._alias) > ALIAS_CACHE_LIMIT:
                for stale in list(self._alias)[: len(self._alias) - ALIAS_CACHE_LIMIT]:
                    self._alias.pop(stale, None)
            self._persist_alias_cache()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return bool(self.items)

    def get(self, item_id: str) -> dict | None:
        return self.items.get(item_id)

    def stats(self) -> dict:
        return {
            "total_items": len(self.items),
            "generated_at": self.meta.get("generated_at", ""),
            "alias_count": len(self._alias),
        }

    def search(self, keyword: str, limit: int = 5) -> list[dict]:
        """按相关度返回条目（不带层级信息）。"""
        return [item for item, _ in self.search_ranked(keyword, limit)]

    def search_ranked(self, keyword: str, limit: int = 5) -> list[tuple[dict, int]]:
        """别名/编号精确 -> 名称精确/别名子串/去级别词 -> 名称前缀 -> 名称子串。

        返回 [(条目, 命中层级)]；层级 0~1 视为确定性命中，2~3 为模糊命中。
        同层内按「评分 × 票数热度」加权，避免低票满分件压过热门件。
        """
        query = normalize(keyword)
        if not query:
            return []

        scored: dict[str, tuple[int, float]] = {}

        def add(item_id: str, tier: int) -> None:
            item = self.items[item_id]
            rating = float(item.get("rating") or 0.0)
            votes = float(item.get("ratingCount") or 0)
            weight = rating * (1.0 + math.log10(votes + 1.0))
            rank = (tier, -weight)
            if item_id not in scored or rank < scored[item_id]:
                scored[item_id] = rank

        # 1. 别名精确（含本地学习的俗称）
        for item_id in self._alias.get(query, []):
            add(item_id, 0)
        # 2. JAN 精确
        jan_digits = re.sub(r"\D", "", keyword)
        if jan_digits and len(jan_digits) >= 8:
            hit = self._jan.get(jan_digits)
            if hit:
                add(hit, 0)
        # 3. 编号 / 名称精确
        for item_id in self._norm_num.get(query, []):
            add(item_id, 1)
        for item_id, norm in self._norm_name.items():
            if norm == query or self._norm_display.get(item_id) == query:
                add(item_id, 1)
        # 4. 别名子串（官方/学习到的俗称，视为确定性命中）
        if not scored:
            for key, ids in self._alias.items():
                if query in key:
                    for item_id in ids:
                        add(item_id, 1)
        # 5. 剥离级别词再试（如「rg海牛」->「海牛」）
        if not scored:
            stripped = _strip_grade(query)
            if stripped and stripped != query:
                for item_id in self._alias.get(stripped, []):
                    add(item_id, 1)
                for item_id, norm in self._norm_name.items():
                    if norm == stripped:
                        add(item_id, 1)
                    elif norm.startswith(stripped):
                        add(item_id, 2)
        # 6. 名称前缀
        if not scored:
            for item_id, norm in self._norm_name.items():
                if norm.startswith(query) or self._norm_display.get(item_id, "").startswith(query):
                    add(item_id, 2)
        # 7. 名称/编号子串兜底（线性扫描，1.9 万条毫秒级）
        if not scored:
            for item_id, norm in self._norm_name.items():
                if query in norm or query in self._norm_display.get(item_id, ""):
                    add(item_id, 3)
            if not scored:
                for num_key, ids in self._norm_num.items():
                    if query in num_key:
                        for item_id in ids:
                            add(item_id, 3)

        ranked = sorted(scored.items(), key=lambda kv: kv[1])[: max(1, limit)]
        return [(self.items[item_id], tier) for item_id, (tier, _) in ranked]


def _strip_grade(query: str) -> str:
    for token in sorted(_GRADE_TOKENS, key=len, reverse=True):
        if query.startswith(token):
            rest = query[len(token):]
            if len(rest) >= 2:  # 避免剥出单字噪音
                return rest
    return query


# ----------------------------------------------------------------------
# 每日增量同步（模式 A）
# ----------------------------------------------------------------------

async def sync_once(store: SnapshotStore, client: GunplaLabClient) -> str:
    """执行一轮同步。返回 'updated' | 'fresh' | 'no-change'，异常向上抛。"""
    manifest: ApiResponse = await client.get_manifest(
        if_none_match=store.manifest_etag if store.loaded else None
    )
    if manifest.not_modified:
        return "fresh"

    remote_sha = ((manifest.data or {}).get("snapshot") or {}).get("sha256")
    if store.loaded and remote_sha and remote_sha == store.snapshot_sha256:
        # 数据未变，仅刷新清册 ETag
        store.manifest_etag = manifest.etag or store.manifest_etag
        store._write_meta()
        return "no-change"

    try:
        return await _download_and_install(
            store,
            client,
            if_none_match=store.snapshot_etag if store.loaded else None,
            sha256=remote_sha,
        )
    except GunplaApiError as exc:
        if exc.status == 404 and store.loaded:
            # 快照尚未生成：保留本地数据
            return "no-change"
        raise


async def _download_and_install(
    store: SnapshotStore,
    client: GunplaLabClient,
    *,
    if_none_match: str | None,
    sha256: str | None = None,
) -> str:
    json_bytes, etag = await client.download_snapshot(if_none_match=if_none_match)
    if json_bytes is None:
        # snapshot 304：数据没变，只刷新 sha 记录
        if sha256:
            store.snapshot_sha256 = sha256
            store._write_meta()
        return "no-change"

    payload = json.loads(json_bytes.decode("utf-8"))
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise GunplaApiError("BAD_RESPONSE", "快照缺少 items 字段")

    # httpx 已透明解压 Content-Encoding: gzip，落盘前自行压缩
    gz_bytes = gzip.compress(json_bytes, mtime=0)
    store.manifest_etag = None  # 旧清册 ETag 不再可信，下轮重新协商
    store.snapshot_etag = etag
    store.snapshot_sha256 = sha256
    store._install(items, payload.get("meta") or {})
    store._save_to_disk(gz_bytes)
    store._load_alias_cache()
    logger.info("快照已更新：%d 款模型", len(items))
    return "updated"


async def load_store_async(store: SnapshotStore) -> bool:
    """线程池中加载磁盘快照，避免阻塞事件循环。"""
    return await asyncio.to_thread(store.load_from_disk)
