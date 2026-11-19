"""End-to-end dwell-check tests through the HTTP layer."""

import csv
import io

from app import service


def csv_bytes(header, rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


NET_HEADER = ["路段号", "起点x", "起点y", "终点x", "终点y"]
PTS_HEADER = ["时刻", "x", "y"]

NET = [["S1", 0, 0, 500, 0]]

DWELL_POINTS = [
    ["2026-11-18T00:00:00Z", 20, 1],
    ["2026-11-18T00:00:10Z", 95, 1],
    ["2026-11-18T00:00:20Z", 100, 1],
    ["2026-11-18T00:00:30Z", 105, 1],
    ["2026-11-18T00:00:40Z", 100, 1],
    ["2026-11-18T00:00:50Z", 102, 1],
    ["2026-11-18T00:01:00Z", 104, 1],
    ["2026-11-18T00:01:10Z", 100, 1],
    ["2026-11-18T00:01:20Z", 300, 1],
]

GAP_POINTS = [
    ["2026-11-18T00:00:00Z", 100, 1],
    ["2026-11-18T00:00:10Z", 100, 1],
    ["2026-11-18T00:00:20Z", 100, 1],
    ["2026-11-18T00:01:00Z", 100, 1],
    ["2026-11-18T00:01:10Z", 100, 1],
    ["2026-11-18T00:01:20Z", 100, 1],
]

CHECK_FORM = {
    "segment_id": "S1",
    "x": "100",
    "y": "0",
    "dwell_radius": "15",
    "min_dwell": "60",
    "max_gap": "30",
    "note": "测试站",
}


def upload_network(client, rows=NET):
    return client.post(
        "/network",
        files={"file": ("net.csv", csv_bytes(NET_HEADER, rows), "text/csv")},
        follow_redirects=True,
    )


def upload_points(client, rows):
    return client.post(
        "/points",
        files={"file": ("pts.csv", csv_bytes(PTS_HEADER, rows), "text/csv")},
        follow_redirects=True,
    )


def run_pending_match(main_module):
    from app import db

    conn = db.connect(main_module.DB_PATH)
    try:
        job = service.claim_job(conn)
        assert job is not None
        return service.execute_job(conn, job)
    finally:
        conn.close()


def run_pending_dwell(main_module):
    from app import db

    conn = db.connect(main_module.DB_PATH)
    try:
        job = service.claim_dwell_job(conn)
        assert job is not None
        return service.execute_dwell_job(conn, job)
    finally:
        conn.close()


def create_check(client, **overrides):
    form = dict(CHECK_FORM)
    form.update(overrides)
    return client.post(
        "/dwell-checks", data=form, follow_redirects=True
    )


def setup_confirmed(client, main_module):
    upload_network(client)
    upload_points(client, DWELL_POINTS)
    run_pending_match(main_module)
    resp = create_check(client)
    return resp


def test_dashboard_shows_dwell_panel(worker_client):
    client, _ = worker_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert "站点驻留核对" in resp.text
    assert "尚未登记驻留核对" in resp.text


def test_confirmed_flow_shows_window_and_evidence(worker_client):
    client, main_module = worker_client
    resp = setup_confirmed(client, main_module)
    assert "驻留核对 #1 已登记" in resp.text

    conclusion_id = run_pending_dwell(main_module)
    assert conclusion_id is not None

    page = client.get("/dwell-checks/1")
    assert page.status_code == 200
    assert '<span class="badge confirmed">已驻留</span>' in page.text
    assert "2026-11-18T00:00:10Z" in page.text
    assert "2026-11-18T00:01:10Z" in page.text
    assert "证据点" in page.text
    # Binding: trajectory result, revisions and matching radius are shown.
    assert "绑定轨迹结果" in page.text
    assert "依据来源" in page.text
    assert "路网 r1" in page.text and "定位点 r1" in page.text

    dash = client.get("/")
    assert "已驻留" in dash.text


def test_broken_sampling_not_concluded_as_dwell(worker_client):
    client, main_module = worker_client
    upload_network(client)
    upload_points(client, GAP_POINTS)
    run_pending_match(main_module)
    create_check(client)
    run_pending_dwell(main_module)

    page = client.get("/dwell-checks/1")
    assert "证据不足" in page.text
    assert "断采样不能拼成驻留" in page.text
    # The two clusters span 80 s in total, but no 已驻留 badge may appear.
    assert '<span class="badge confirmed">已驻留</span>' not in page.text


def test_review_trajectory_gives_unavailable_not_confirmed(worker_client):
    client, main_module = worker_client
    upload_network(client, [["P1", 0, 0, 100, 0], ["P2", 0, 5, 100, 5]])
    upload_points(
        client,
        [["2026-11-18T00:00:00Z", 30, 0], ["2026-11-18T00:00:10Z", 70, 5]],
    )
    run_pending_match(main_module)
    create_check(client, segment_id="P1")
    run_pending_dwell(main_module)

    page = client.get("/dwell-checks/1")
    assert "无法核对" in page.text
    assert "待核" in page.text
    assert '<span class="badge confirmed">已驻留</span>' not in page.text


def test_points_replacement_revokes_conclusion_on_pages(worker_client):
    client, main_module = worker_client
    setup_confirmed(client, main_module)
    run_pending_dwell(main_module)
    assert '<span class="badge confirmed">已驻留</span>' in client.get("/dwell-checks/1").text

    upload_points(client, GAP_POINTS)
    dash = client.get("/")
    assert "已撤销的驻留结论" in dash.text
    assert "定位点换版" in dash.text
    detail = client.get("/dwell-checks/1")
    assert '<span class="badge confirmed">已驻留</span>' not in detail.text
    assert "已撤销" in detail.text  # history row keeps the revoked conclusion

    # Recompute publishes the new trajectory; the check is re-queued and
    # concludes 证据不足 against the gapped points.
    run_pending_match(main_module)
    run_pending_dwell(main_module)
    detail = client.get("/dwell-checks/1")
    assert "证据不足" in detail.text


def test_radius_change_revokes_conclusion(worker_client):
    client, main_module = worker_client
    setup_confirmed(client, main_module)
    run_pending_dwell(main_module)

    client.post("/radius", data={"radius": "25"}, follow_redirects=True)
    dash = client.get("/")
    assert "已撤销的驻留结论" in dash.text
    assert "匹配半径变更" in dash.text

    run_pending_match(main_module)
    run_pending_dwell(main_module)
    detail = client.get("/dwell-checks/1")
    assert '<span class="badge confirmed">已驻留</span>' in detail.text
    assert "25 m" in detail.text


def test_check_waits_for_first_trajectory(worker_client):
    client, main_module = worker_client
    upload_network(client)
    upload_points(client, DWELL_POINTS)
    create_check(client)
    # No matching result yet: conclusion stays pending, nothing confirmed.
    page = client.get("/dwell-checks/1")
    assert '<span class="badge confirmed">已驻留</span>' not in page.text
    assert "核对保持待核" in page.text

    run_pending_match(main_module)
    run_pending_dwell(main_module)
    page = client.get("/dwell-checks/1")
    assert '<span class="badge confirmed">已驻留</span>' in page.text


def test_recheck_endpoint(worker_client):
    client, main_module = worker_client
    setup_confirmed(client, main_module)
    run_pending_dwell(main_module)
    resp = client.post("/dwell-checks/1/recheck", follow_redirects=True)
    assert "已按当前轨迹重新安排核对" in resp.text
    run_pending_dwell(main_module)
    assert '<span class="badge confirmed">已驻留</span>' in client.get("/dwell-checks/1").text


def test_form_validation(worker_client):
    client, _ = worker_client
    resp = create_check(client, dwell_radius="-5")
    assert "判定半径必须大于 0" in resp.text
    resp = create_check(client, x="abc")
    assert "站点平面坐标 x必须是数字" in resp.text
    resp = create_check(client, max_gap="0")
    assert "最大采样间隔必须大于 0" in resp.text
    resp = create_check(client, segment_id="  ")
    assert "路段号不能为空" in resp.text


def test_unknown_check_redirects_with_error(worker_client):
    client, _ = worker_client
    resp = client.get("/dwell-checks/999", follow_redirects=True)
    assert "驻留核对不存在" in resp.text
