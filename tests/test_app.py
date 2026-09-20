"""字幕定稿服务行为测试：乱序、修订、缺口释放、并发事务。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client():
    app = create_app(":memory:")
    with TestClient(app) as c:
        yield c


def submit(client, channel, seq, revision, text="t"):
    return client.post(
        f"/channels/{channel}/fragments",
        json={"seq": seq, "revision": revision, "text": text},
    )


def test_out_of_order_release_on_gap_fill(client):
    assert submit(client, "ch", 2, 1, "b").json()["watermark"] == 0
    assert submit(client, "ch", 3, 1, "c").json()["watermark"] == 0
    r = submit(client, "ch", 1, 1, "a").json()
    assert r["accepted"] is True
    assert r["watermark"] == 3
    assert [f["seq"] for f in r["released"]] == [1, 2, 3]

    fin = client.get("/channels/ch/finalized").json()
    assert fin["watermark"] == 3
    assert [(f["seq"], f["text"]) for f in fin["fragments"]] == [
        (1, "a"),
        (2, "b"),
        (3, "c"),
    ]


def test_gap_blocks_waterline_until_filled(client):
    for seq in (1, 2, 4, 5):
        submit(client, "ch", seq, 1, f"s{seq}")
    assert client.get("/channels/ch").json()["watermark"] == 2

    r = submit(client, "ch", 3, 1, "s3").json()
    assert r["watermark"] == 5
    assert [f["seq"] for f in r["released"]] == [3, 4, 5]


def test_higher_revision_replaces_pending(client):
    submit(client, "ch", 2, 1, "old")
    assert submit(client, "ch", 2, 1, "dup").json()["accepted"] is False
    assert submit(client, "ch", 2, 0, "lower").json()["accepted"] is False
    assert submit(client, "ch", 2, 2, "new").json()["accepted"] is True

    submit(client, "ch", 1, 1, "first")
    fin = client.get("/channels/ch/finalized").json()
    assert [(f["seq"], f["revision"], f["text"]) for f in fin["fragments"]] == [
        (1, 1, "first"),
        (2, 2, "new"),
    ]


def test_finalized_is_immutable(client):
    r = submit(client, "ch", 1, 1, "final").json()
    assert r["watermark"] == 1
    # 已定稿序号即使修订号更高也不再生效
    r = submit(client, "ch", 1, 99, "mutated").json()
    assert r["accepted"] is False
    assert r["watermark"] == 1
    fin = client.get("/channels/ch/finalized").json()
    assert fin["fragments"][0]["text"] == "final"


def test_channels_are_isolated(client):
    submit(client, "a", 1, 1, "a1")
    submit(client, "b", 2, 1, "b2")
    assert client.get("/channels/a").json()["watermark"] == 1
    assert client.get("/channels/b").json()["watermark"] == 0


def test_finalized_query_pagination(client):
    for seq in range(1, 6):
        submit(client, "ch", seq, 1, f"s{seq}")
    r = client.get("/channels/ch/finalized", params={"from_seq": 3, "limit": 2}).json()
    assert [f["seq"] for f in r["fragments"]] == [3, 4]
    assert r["watermark"] == 5


def test_validation(client):
    assert submit(client, "ch", 0, 1).status_code == 422
    assert submit(client, "ch", 1, -1).status_code == 422


def test_concurrent_out_of_order_submissions(client):
    """多线程乱序提交 1..N：最终水位线必须为 N，定稿恰好为 1..N 各一次。"""
    n = 60
    barrier = threading.Barrier(n)

    def worker(seq: int) -> None:
        barrier.wait()
        r = submit(client, "ch", seq, 1, f"s{seq}")
        assert r.status_code == 200

    threads = [
        threading.Thread(target=worker, args=(seq,))
        for seq in reversed(range(1, n + 1))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    fin = client.get("/channels/ch/finalized").json()
    assert fin["watermark"] == n
    assert [f["seq"] for f in fin["fragments"]] == list(range(1, n + 1))


def test_concurrent_duplicate_and_revision_race(client):
    """并发同序号竞速（seq=2 被缺口挡在暂存区）：
    同修订号至多生效一次；竞速结束后最高修订号 9 必然胜出并定稿。"""
    m = 20
    barrier = threading.Barrier(m)
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def worker(revision: int) -> None:
        barrier.wait()
        r = submit(client, "ch", 2, revision, f"rev{revision}").json()
        with lock:
            results.append((revision, r))

    threads = [threading.Thread(target=worker, args=(rev,)) for rev in [5] * 10 + list(range(10))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 同修订号(5)并发：至多一个生效
    dup_accepted = [r for rev, r in results if rev == 5 and r["accepted"]]
    assert len(dup_accepted) <= 1
    # 最高修订号 9 必然生效（提交时库中修订号必然 < 9）
    assert any(rev == 9 and r["accepted"] for rev, r in results)
    # 缺口未补齐，水位线不动
    assert all(r["watermark"] == 0 for _, r in results)

    # 补齐 seq=1：seq=2 以最高修订号定稿，且只定稿一次
    r = submit(client, "ch", 1, 1, "s1").json()
    assert [f["seq"] for f in r["released"]] == [1, 2]
    fin = client.get("/channels/ch/finalized").json()
    assert [(f["seq"], f["revision"], f["text"]) for f in fin["fragments"]] == [
        (1, 1, "s1"),
        (2, 9, "rev9"),
    ]
