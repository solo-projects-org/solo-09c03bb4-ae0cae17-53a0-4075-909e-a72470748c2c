"""End-to-end tests through the HTTP layer with a manually driven worker."""

import csv
import io
import time

import pytest

from app import service


def csv_bytes(header, rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


NET_HEADER = ["路段号", "起点x", "起点y", "终点x", "终点y"]
PTS_HEADER = ["时刻", "x", "y"]

PARALLEL_NETWORK = [
    ["N1", 0, 0, 100, 0],
    ["N2", 100, 0, 200, 0],
    ["S1", 0, 5, 100, 5],
    ["S2", 100, 5, 200, 5],
]


def upload_network(client, rows, note=""):
    return client.post(
        "/network",
        files={"file": ("net.csv", csv_bytes(NET_HEADER, rows), "text/csv")},
        data={"note": note},
        follow_redirects=True,
    )


def upload_points(client, rows, note=""):
    return client.post(
        "/points",
        files={"file": ("pts.csv", csv_bytes(PTS_HEADER, rows), "text/csv")},
        data={"note": note},
        follow_redirects=True,
    )


def run_pending(main_module):
    from app import db

    conn = db.connect(main_module.DB_PATH)
    try:
        job = service.claim_job(conn)
        assert job is not None
        result_id = service.execute_job(conn, job)
        return result_id
    finally:
        conn.close()


def test_dashboard_requires_uploads(worker_client):
    client, _ = worker_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert "未上传" in resp.text


def test_full_flow_parallel_network(worker_client):
    client, main_module = worker_client
    resp = upload_network(client, PARALLEL_NETWORK)
    assert resp.status_code == 200
    assert "路网修订 1 已受理" in resp.text

    resp = upload_points(
        client,
        [
            ["2026-11-18T00:00:00Z", 40, 0.4],
            ["2026-11-18T00:00:05Z", 160, 1.0],
        ],
    )
    assert "定位点修订 1 已受理" in resp.text
    # No worker thread: the page must show a pending state, not a result.
    assert "等待重算" in resp.text or "尚" in resp.text

    result_id = run_pending(main_module)
    assert result_id is not None

    page = client.get(f"/results/{result_id}")
    assert page.status_code == 200
    assert "N1 → N2" in page.text
    assert "已发布唯一轨迹" in page.text

    dash = client.get("/")
    assert "N1 → N2" in dash.text


def test_ambiguous_result_is_review(worker_client):
    client, main_module = worker_client
    upload_network(client, [["U", 0, 0, 100, 0], ["V", 0, 5, 100, 5]])
    upload_points(
        client,
        [["2026-11-18T00:00:00Z", 30, 0], ["2026-11-18T00:00:05Z", 70, 5]],
    )
    result_id = run_pending(main_module)
    page = client.get(f"/results/{result_id}")
    assert "待核" in page.text
    assert "多条轨迹" in page.text
    assert "路段序列" not in page.text


def test_no_connected_path_is_review(worker_client):
    client, main_module = worker_client
    upload_network(
        client, [["A", 0, 0, 100, 0], ["B", 100, 0, 200, 0]]
    )
    upload_points(
        client,
        [["2026-11-18T00:00:00Z", 150, 0], ["2026-11-18T00:00:05Z", 50, 0]],
    )
    result_id = run_pending(main_module)
    page = client.get(f"/results/{result_id}")
    assert "不存在满足行驶方向的可连通路径" in page.text


def test_correction_sequence_invalidates_old_then_republishes(worker_client):
    client, main_module = worker_client
    upload_network(client, [["A", 0, 0, 100, 0], ["B", 100, 0, 200, 0]])
    upload_points(
        client,
        [["2026-11-18T00:00:00Z", 50, 0], ["2026-11-18T00:00:05Z", 150, 0]],
    )
    first_id = run_pending(main_module)

    # Points correction: a third point far off the road -> review.
    upload_points(
        client,
        [
            ["2026-11-18T00:00:00Z", 50, 0],
            ["2026-11-18T00:00:05Z", 150, 0],
            ["2026-11-18T00:00:10Z", 200, 500],
        ],
    )
    dash = client.get("/")
    assert "已失效结果" in dash.text
    assert "定位点换版" in dash.text
    assert "待重算" in dash.text
    old_page = client.get(f"/results/{first_id}")
    assert "不得用于路段运行时间分析" in old_page.text

    second_id = run_pending(main_module)
    assert second_id != first_id
    page = client.get(f"/results/{second_id}")
    assert "半径内没有任何路段" in page.text

    # Network correction in the other direction: add the missing segment.
    upload_network(
        client,
        [
            ["A", 0, 0, 100, 0],
            ["B", 100, 0, 200, 0],
            ["C", 200, 0, 200, 600],
        ],
    )
    third_id = run_pending(main_module)
    page = client.get(f"/results/{third_id}")
    assert "已发布唯一轨迹" in page.text
    assert "A → B → C" in page.text


def test_missing_column_is_rejected_without_new_revision(worker_client):
    client, main_module = worker_client
    resp = client.post(
        "/network",
        files={
            "file": (
                "bad.csv",
                csv_bytes(["路段号", "起点x", "起点y"], [["X", 0, 0]]),
                "text/csv",
            )
        },
        follow_redirects=True,
    )
    assert "缺少列" in resp.text
    from app import db

    conn = db.connect(main_module.DB_PATH)
    try:
        assert db.get_active_revision_id(conn, "network") is None
    finally:
        conn.close()


def test_duplicate_segment_id_rejected(worker_client):
    client, main_module = worker_client
    resp = upload_network(
        client,
        [["A", 0, 0, 100, 0], ["A", 100, 0, 200, 0]],
    )
    assert "路段号重复" in resp.text


def test_out_of_order_points_rejected(worker_client):
    client, main_module = worker_client
    upload_network(client, [["A", 0, 0, 100, 0]])
    resp = upload_points(
        client,
        [
            ["2026-11-18T00:00:10Z", 10, 0],
            ["2026-11-18T00:00:05Z", 20, 0],
        ],
    )
    assert "升序" in resp.text


def test_radius_endpoint_requeues_and_recomputes(worker_client):
    client, main_module = worker_client
    upload_network(client, [["A", 0, 0, 100, 0]])
    upload_points(client, [["2026-11-18T00:00:00Z", 50, 8]])
    first_id = run_pending(main_module)
    assert "已发布唯一轨迹" in client.get(f"/results/{first_id}").text

    resp = client.post("/radius", data={"radius": "5"}, follow_redirects=True)
    assert "匹配半径已设为 5 米" in resp.text
    second_id = run_pending(main_module)
    page = client.get(f"/results/{second_id}")
    assert "半径内没有任何路段" in page.text
    assert "不得用于路段运行时间分析" in client.get(f"/results/{first_id}").text


def test_radius_validation(worker_client):
    client, _ = worker_client
    resp = client.post("/radius", data={"radius": "-1"}, follow_redirects=True)
    assert "必须大于 0" in resp.text
    resp = client.post("/radius", data={"radius": "abc"}, follow_redirects=True)
    assert "必须是数字" in resp.text


def test_recompute_without_sources(worker_client):
    client, _ = worker_client
    resp = client.post("/recompute", follow_redirects=True)
    assert "请先上传路网和定位点" in resp.text


def test_health(worker_client):
    client, _ = worker_client
    assert client.get("/health").json() == {"status": "ok"}
