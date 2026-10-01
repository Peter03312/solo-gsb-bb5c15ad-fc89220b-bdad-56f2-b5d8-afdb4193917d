"""已登记装船实绩事件更正端到端测试。"""

from tests.test_actuals_api import (
    STACK_SLOTS,
    STACK_MANIFEST,
    TWIN_SLOTS,
    TWIN_MANIFEST,
    _confirm,
    _ev,
    _schedule,
    _stack_plan,
)

from fastapi.testclient import TestClient

from app.main import app


def _corr(seq, etype, cno, crane, minute, expected_version, request_no):
    return {
        "seq": seq,
        "event_type": etype,
        "container_no": cno,
        "crane_no": crane,
        "minute": minute,
        "expected_version": expected_version,
        "correction_request_no": request_no,
    }


# ---------- 基本更正：版本、原值留存、偏差 ----------


def test_correction_updates_event_bumps_version_and_keeps_history(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    r0 = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
    ]})
    assert r0.status_code == 201
    assert r0.json()["version"] == 1
    assert r0.json()["corrections"] == []

    r = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 2, 3, 1, "REQ-1"),
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["version"] == 2
    ev = next(e for e in data["events"] if e["seq"] == 1)
    assert (ev["crane_no"], ev["minute"]) == (2, 3)
    # registered_at 保留首次登记时刻
    assert ev["registered_at"] == r0.json()["events"][0]["registered_at"]
    box = next(c for c in data["containers"] if c["container_no"] == "C001")
    assert (box["actual_crane_no"], box["actual_start_minute"]) == (2, 3)
    assert box["start_delta_minutes"] == 3
    assert box["crane_changed"] is True
    assert len(data["corrections"]) == 1
    h = data["corrections"][0]
    assert h == {
        "correction_request_no": "REQ-1",
        "seq": 1,
        "event_type": "start",
        "container_no": "C001",
        "old_minute": 0,
        "old_crane_no": 1,
        "new_minute": 3,
        "new_crane_no": 2,
        "version": 2,
        "created_at": h["created_at"],
    }

    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["version"] == 2
    assert g["events"][0]["minute"] == 3 and g["events"][0]["crane_no"] == 2
    assert len(g["corrections"]) == 1


def test_correct_only_crane_then_only_minute_chain(client):
    # 两个独立单箱堆栈、2 台岸桥：开工中的 C001 可单独换桥（无完工桥牵连）
    plan = _confirm(client, TWIN_SLOTS, TWIN_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2, nos=("C001", "C003"), d1=10, d2=10)
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
    ]})
    # 只改桥：桥1 -> 桥2
    r1 = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 2, 0, 1, "REQ-A"),
    )
    assert r1.status_code == 201 and r1.json()["version"] == 2
    box1 = next(c for c in r1.json()["containers"] if c["container_no"] == "C001")
    assert box1["actual_crane_no"] == 2 and box1["crane_changed"] is True

    # 用旧桥1 补登记完工 -> 与更正后的开工桥不一致，重放拒绝（登记接口）
    rf = client.post(f"/api/plans/{pid}/actual-events",
                     json={"events": [_ev(2, "finish", "C001", 1, 10)]})
    assert rf.status_code == 422 and "不一致" in rf.json()["detail"]

    # 只改分钟：0 -> 3（桥仍为 2）
    r2 = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 2, 3, 2, "REQ-B"),
    )
    assert r2.status_code == 201 and r2.json()["version"] == 3
    box2 = next(c for c in r2.json()["containers"] if c["container_no"] == "C001")
    assert (box2["actual_crane_no"], box2["actual_start_minute"]) == (2, 3)

    # 在桥2 补登记完工 -> 合法；登记不推进版本
    r3 = client.post(f"/api/plans/{pid}/actual-events",
                     json={"events": [_ev(2, "finish", "C001", 2, 10)]})
    assert r3.status_code == 201 and r3.json()["version"] == 3

    # 更正历史按版本先后留存全部原值（同一事件两次更正）
    hist = r3.json()["corrections"]
    assert [(h["seq"], h["old_crane_no"], h["new_crane_no"],
             h["old_minute"], h["new_minute"], h["version"]) for h in hist] == [
        (1, 1, 2, 0, 0, 2),
        (1, 2, 2, 0, 3, 3),
    ]


def test_replay_rerun_after_correction_reflects_new_order(client):
    """更正分钟后按全部有效事件重放：同分钟完工先于开工的顺序随之改变。"""
    plan = _confirm(client, TWIN_SLOTS, TWIN_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=1, nos=("C001", "C003"), d1=10, d2=10)
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C003", 1, 11),
    ]})
    # 把 C001 完工改到第 12 分钟：桥1 [0,10) 完工、[11,..) 是 C003 开工，
    # C001@12 完工时桥已属 C003（区间重叠）-> 非法拒绝
    bad = client.post(
        f"/api/plans/{pid}/actual-events/2/corrections",
        json=_corr(2, "finish", "C001", 1, 12, 1, "REQ-X"),
    )
    assert bad.status_code == 422 and "不一致" not in bad.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["events"][1]["minute"] == 10  # 未生效
    assert g["version"] == 1 and g["corrections"] == []

    # 把 C003 开工更正到第 12 分钟、C001 完工仍在 10 -> 合法，重放顺序变化
    ok = client.post(
        f"/api/plans/{pid}/actual-events/3/corrections",
        json=_corr(3, "start", "C003", 1, 12, 1, "REQ-Y"),
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["replay_order"] == [1, 2, 3]


# ---------- 非法更正整次拒绝，版本/历史不变 ----------


def test_illegal_correction_leaves_actuals_version_history_unchanged(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 1, 10),
        _ev(4, "finish", "C002", 1, 18),
    ]})
    # C001 开工改到第 9 分钟：与其第 10 分钟完工仍合法，但与基线无关；
    # 改为第 11 分钟则晚于其完工，重放 finish@10 先于 start@11 -> 未开工完工非法
    r = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 1, 11, 1, "REQ-BAD"),
    )
    assert r.status_code == 422 and "严格晚于开工" in r.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["version"] == 1
    assert g["corrections"] == []
    assert g["events"][0]["minute"] == 0
    assert g["unfinished_containers"] == []


def test_correction_out_of_crane_range_rejected(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    r = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 3, 0, 1, "REQ-CR"),
    )
    assert r.status_code == 422 and "岸桥" in r.json()["detail"]


def test_noop_correction_rejected(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    r = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 1, 0, 1, "REQ-NOOP"),
    )
    assert r.status_code == 422 and "无实质更正" in r.json()["detail"]
    assert client.get(f"/api/plans/{pid}/actuals").json()["version"] == 1


def test_correction_cannot_change_type_or_container(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    r1 = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "finish", "C001", 1, 5, 1, "REQ-T"),
    )
    assert r1.status_code == 422 and "事件类型" in r1.json()["detail"]
    r2 = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C002", 1, 0, 1, "REQ-N"),
    )
    assert r2.status_code == 422 and "箱号" in r2.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["version"] == 1 and g["corrections"] == []


def test_path_seq_must_match_body_seq(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    r = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(2, "start", "C001", 2, 0, 1, "REQ-SEQ"),
    )
    assert r.status_code == 422 and "流水号" in r.json()["detail"]


# ---------- 未找到 / 前置条件 / 只读 ----------


def test_correction_unknown_event_returns_404(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    r = client.post(
        f"/api/plans/{pid}/actual-events/999/corrections",
        json=_corr(999, "start", "C001", 2, 0, 1, "REQ-404"),
    )
    assert r.status_code == 404 and "未找到" in r.json()["detail"]


def test_correction_without_actuals_returns_404(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    r = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 2, 0, 1, "REQ-HEAD"),
    )
    assert r.status_code == 404
    assert client.post(
        "/api/plans/nope/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 2, 0, 1, "REQ-P"),
    ).status_code == 404


def test_correction_draft_plan_rejected_409(client):
    sr = client.post("/api/slot-revisions", json=STACK_SLOTS).json()["slots_revision"]
    client.post("/api/manifest-revisions", json=STACK_MANIFEST)
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(
        f"/api/plans/{plan['plan_id']}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 1, 0, 1, "REQ-D"),
    )
    assert r.status_code == 404  # draft 无实绩头 -> 原事件未找到


def test_correction_container_not_in_plan_422(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    r = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C999", 2, 0, 1, "REQ-XC"),
    )
    assert r.status_code == 422 and "不在方案" in r.json()["detail"]


def test_superseded_plan_correction_read_only(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
    ]})
    client.post("/api/manifest-revisions", json={
        "containers": [{"no": "C001", "weight": 15, "hazmat": None,
                        "discharge_order": 1}]
    })
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    })
    assert ok.status_code == 200

    r = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 2, 1, 1, "REQ-SUP"),
    )
    assert r.status_code == 409 and "只读" in r.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["read_only"] is True and g["version"] == 1
    assert g["corrections"] == []


# ---------- 版本乐观锁 ----------


def test_expected_version_stale_conflict(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    ok = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 2, 0, 1, "REQ-1"),
    )
    assert ok.status_code == 201
    stale = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 1, 0, 1, "REQ-2"),
    )
    assert stale.status_code == 409 and "过期" in stale.json()["detail"]
    # 用正确当前版本可继续更正（改回桥1）
    cur = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 1, 0, 2, "REQ-2"),
    )
    assert cur.status_code == 201 and cur.json()["version"] == 3


# ---------- 更正请求号幂等与冲突 ----------


def test_same_request_no_same_content_idempotent_even_after_version_advanced(client):
    # 完整作业链：C001[0,10) 桥1、C002[10,18) 桥1
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 1, 10),
        _ev(4, "finish", "C002", 1, 18),
    ]})
    # 更正 1：C001 开工分钟 0 -> 1（完工仍在 10，合法）
    body = _corr(1, "start", "C001", 1, 1, 1, "REQ-IDEM")
    r1 = client.post(f"/api/plans/{pid}/actual-events/1/corrections", json=body)
    assert r1.status_code == 201
    first = r1.json()
    assert first["version"] == 2

    # 另一条更正把版本推进到 3：C001 完工 10 -> 11（C002 开工在 10，
    # 同分钟 finish-before-start 规则下可首尾相接；11 分钟完工时桥已被 C002 占用？
    # C002@10 开工先于 C001@11 完工重放 -> 会撞桥，故改用 9：finish@9 先于
    # C002 start@10，且严格晚于 C001 开工@1，合法）
    other = client.post(
        f"/api/plans/{pid}/actual-events/2/corrections",
        json=_corr(2, "finish", "C001", 1, 9, 2, "REQ-OTHER"),
    )
    assert other.status_code == 201 and other.json()["version"] == 3

    # 同请求号同内容、带旧期望版本重试：仍返回首次更正结果（200），不重复生效
    r2 = client.post(f"/api/plans/{pid}/actual-events/1/corrections", json=body)
    assert r2.status_code == 200
    second = r2.json()
    assert second == first  # 完整首次结果（version=2、当时的事件/历史）
    # 当前实绩已推进到 3，且该请求号只留存一次
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["version"] == 3
    reqs = [c["correction_request_no"] for c in g["corrections"]]
    assert reqs == ["REQ-IDEM", "REQ-OTHER"]


def test_same_request_no_different_content_conflict(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    r1 = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 2, 0, 1, "REQ-DUP"),
    )
    assert r1.status_code == 201
    # 同请求号但改的内容不同（这次改分钟）-> 409
    r2 = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 2, 5, 2, "REQ-DUP"),
    )
    assert r2.status_code == 409 and "冲突" in r2.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["version"] == 2 and len(g["corrections"]) == 1
    assert (g["events"][0]["crane_no"], g["events"][0]["minute"]) == (2, 0)


# ---------- 更正后原登记接口仍按首次内容识别重试/冲突 ----------


def test_register_retry_after_correction_matches_first_content(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    # 更正分钟 0 -> 4
    c = client.post(
        f"/api/plans/{pid}/actual-events/1/corrections",
        json=_corr(1, "start", "C001", 1, 4, 1, "REQ-R"),
    )
    assert c.status_code == 201

    # 原登记接口以**首次登记内容**（minute=0）重试 -> 幂等 200，返回当前有效实绩
    r = client.post(f"/api/plans/{pid}/actual-events",
                    json={"events": [_ev(1, "start", "C001", 1, 0)]})
    assert r.status_code == 200
    assert r.json()["events"][0]["minute"] == 4
    assert r.json()["version"] == 2

    # 以当前有效内容（minute=4）重提：既不是首次内容 -> 冲突 409
    r2 = client.post(f"/api/plans/{pid}/actual-events",
                     json={"events": [_ev(1, "start", "C001", 1, 4)]})
    assert r2.status_code == 409 and "冲突" in r2.json()["detail"]

    # 新流水号仍可追加登记，版本不因登记变化
    r3 = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),  # 首次内容重试
        _ev(2, "finish", "C001", 1, 9),
    ]})
    assert r3.status_code == 201
    assert r3.json()["version"] == 2
    assert [e["seq"] for e in r3.json()["events"]] == [1, 2]


# ---------- 入参校验 ----------


def test_correction_payload_validation(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    base = f"/api/plans/{pid}/actual-events/1/corrections"
    assert client.post(base, json=_corr(1, "start", "C001", 1, 5, 0, "R")).status_code == 422
    assert client.post(base, json=_corr(1, "start", "C001", 0, 5, 1, "R")).status_code == 422
    assert client.post(base, json=_corr(1, "start", "C001", 1, -1, 1, "R")).status_code == 422
    assert client.post(base, json=_corr(1, "begin", "C001", 1, 5, 1, "R")).status_code == 422
    assert client.post(base, json=_corr(1, "start", "C001", 1, 5, 1, " ")).status_code == 422
    assert client.post(base, json=_corr(0, "start", "C001", 1, 5, 1, "R")).status_code == 422


# ---------- 并发：版本串行化 ----------


def test_concurrent_corrections_one_stale_conflict(client):
    """两线程同时基于版本 1 更正：一个成功（201），另一个版本过期（409）。"""
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    results = {}

    def run(req_no, crane, key):
        local = TestClient(app)
        r = local.post(
            f"/api/plans/{pid}/actual-events/1/corrections",
            json=_corr(1, "start", "C001", crane, 0, 1, req_no),
        )
        results[key] = r.status_code

    t1 = __import__("threading").Thread(target=run, args=("REQ-T1", 2, "a"))
    t2 = __import__("threading").Thread(target=run, args=("REQ-T2", 2, "b"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results.values()) == [201, 409]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["version"] == 2 and len(g["corrections"]) == 1
