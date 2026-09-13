"""同步流程测试（MockTransport）+ 真实快照集成测试。"""

import gzip
import json
import shutil
from pathlib import Path

import httpx
import pytest

from core.api_client import GunplaLabClient
from core.snapshot import SnapshotStore, sync_once

REAL_SNAPSHOT = Path(r"E:\GunplaLab\GunplaLab\backend\data\snapshot.json.gz")

SNAPSHOT_V1 = {
    "meta": {"generated_at": "2026-09-13T08:00:00+08:00", "total_items": 1},
    "items": [
        {
            "id": "78dm_ct_9",
            "name": "测试模型",
            "displayName": "[测试] 测试模型",
            "num": "HG01",
            "jpy": 1000,
            "conv4": 40.0,
            "conv5": 50.0,
        }
    ],
}


def make_client(handlers: dict, state: dict) -> GunplaLabClient:
    def router(request: httpx.Request) -> httpx.Response:
        state.setdefault("calls", []).append(
            (request.url.path, dict(request.headers).get("if-none-match"))
        )
        if request.url.path.endswith("/manifest"):
            fn = handlers["manifest"]
        elif request.url.path.endswith("/snapshot"):
            fn = handlers["snapshot"]
        else:
            fn = handlers.get("other", lambda req: httpx.Response(404))
        return fn(request)

    return GunplaLabClient(
        "http://t.local/api/v1/data", min_interval=0, transport=httpx.MockTransport(router)
    )


def manifest_response(sha: str | None, etag: str):
    if sha is None:
        return httpx.Response(304, headers={"ETag": etag})
    data = {"total_items": 1, "snapshot": {"sha256": sha}}
    return httpx.Response(200, json={"ok": True, "meta": {}, "data": data}, headers={"ETag": etag})


def snapshot_response(etag: str):
    body = json.dumps(SNAPSHOT_V1, ensure_ascii=False).encode("utf-8")
    return httpx.Response(200, content=body, headers={"ETag": etag})


@pytest.mark.asyncio
async def test_sync_flow(tmp_path):
    store = SnapshotStore(tmp_path / "g")
    state: dict = {}
    client = make_client(
        {
            "manifest": lambda req: manifest_response("sha-1", '"m1"'),
            "snapshot": lambda req: snapshot_response('"s1"'),
        },
        state,
    )

    # 首轮：无本地数据 → 全量下载
    assert await sync_once(store, client) == "updated"
    assert store.loaded and len(store.items) == 1
    assert store.snapshot_sha256 == "sha-1"
    # 落盘校验
    assert (store.data_dir / "snapshot.json.gz").is_file()

    # 第二轮：manifest 304 → fresh
    client2 = make_client(
        {"manifest": lambda req: manifest_response(None, '"m1"'), "snapshot": lambda req: snapshot_response('"s1"')},
        state,
    )
    store.manifest_etag = '"m1"'
    assert await sync_once(store, client2) == "fresh"

    # 第三轮：sha 相同 → no-change
    client3 = make_client(
        {"manifest": lambda req: manifest_response("sha-1", '"m1"'), "snapshot": lambda req: snapshot_response('"s1"')},
        state,
    )
    assert await sync_once(store, client3) == "no-change"

    # 第四轮：sha 变化但快照 304 → no-change（sha 记录被刷新）
    client4 = make_client(
        {
            "manifest": lambda req: manifest_response("sha-2", '"m2"'),
            "snapshot": lambda req: httpx.Response(304, headers={"ETag": '"s1"'}),
        },
        state,
    )
    assert await sync_once(store, client4) == "no-change"
    assert store.snapshot_sha256 == "sha-2"

    # 第五轮：sha 变化 + 快照更新 → updated
    client5 = make_client(
        {
            "manifest": lambda req: manifest_response("sha-3", '"m3"'),
            "snapshot": lambda req: snapshot_response('"s2"'),
        },
        state,
    )
    assert await sync_once(store, client5) == "updated"
    assert store.snapshot_etag == '"s2"'


@pytest.mark.asyncio
async def test_sync_snapshot_missing_keeps_local(tmp_path):
    """快照尚未生成（404）时应保留本地数据。"""
    store = SnapshotStore(tmp_path / "g")
    # 预置本地数据
    body = json.dumps(SNAPSHOT_V1, ensure_ascii=False).encode("utf-8")
    store.manifest_etag = None
    store.snapshot_sha256 = "sha-0"
    store._install(SNAPSHOT_V1["items"], SNAPSHOT_V1["meta"])
    store._save_to_disk(gzip.compress(body, mtime=0))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/manifest"):
            return manifest_response("sha-9", '"m9"')
        return httpx.Response(
            404, json={"ok": False, "error": {"code": "SNAPSHOT_NOT_FOUND", "message": "生成中"}}
        )

    client = GunplaLabClient("http://t.local/api/v1/data", min_interval=0,
                             transport=httpx.MockTransport(handler))
    assert await sync_once(store, client) == "no-change"
    assert store.loaded


@pytest.mark.skipif(not REAL_SNAPSHOT.is_file(), reason="本机未找到 GunplaLab 真实快照")
class TestRealSnapshot:
    def test_load_and_search(self, tmp_path):
        store = SnapshotStore(tmp_path / "g")
        shutil.copy(REAL_SNAPSHOT, store._gz_path)
        assert store.load_from_disk() is True
        assert len(store.items) == 19070

        # 俗称（未学习时不在索引中，但名字包含「海牛」的条目可被子串命中）
        hits = store.search("海牛")
        assert hits, "「海牛」应能子串命中 Hi-ν 相关条目"

        # 精确名称
        hits = store.search("新安洲")
        assert hits and "新安洲" in hits[0]["name"]

        # 编号
        hits = store.search("RG32")
        assert hits and hits[0]["num"] == "RG32"

        # 全角/大小写/比例归一化
        hits = store.search("ＲＧ３３")
        assert hits and hits[0]["id"].startswith("78dm")

        stats = store.stats()
        assert stats["total_items"] == 19070
        assert stats["generated_at"].startswith("2026-09")
