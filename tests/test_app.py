"""存储与 Web 全流程测试。

覆盖：导入取数、更正整份更新、过期版本拒绝、无解回滚、CSV、文件上传，
以及规则升级后的旧回次迁移（可解整份替换 / 无解冲突保留更正入口 /
中断重启续算 / 下载门禁）。
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    # 延迟导入，确保 Store 使用临时目录
    import importlib

    import app.main as main
    importlib.reload(main)
    return TestClient(main.app, follow_redirects=False)


DEMO_PAYLOAD = {
    "run_id": "RUN-T",
    "start_depth": 10000,
    "end_depth": 12500,
    "segments": [
        {"length": 500, "samples": [
            {"sample_no": "S1", "start": 20, "end": 100, "assay": "0.12"},
        ]},
        {"length": 600, "samples": [
            {"sample_no": "S2", "start": 50, "end": 120, "assay": "0.31"},
            {"sample_no": "S3", "start": 400, "end": 460, "assay": "0.27"},
        ]},
        {"length": 700, "samples": [
            {"sample_no": "S4", "start": 500, "end": 650, "assay": "1.05"},
        ]},
        {"length": 400, "samples": [
            {"sample_no": "S5", "start": 50, "end": 200, "assay": "0.66"},
        ]},
    ],
    "gaps": [
        {"low": 100, "high": 300},
        {"low": 50, "high": 200},
        {"low": 50, "high": 150},
    ],
    "boundaries": [11100],
    "layers": ["A", "B"],
    "measured_starts": [],
}


def _import(client, payload=DEMO_PAYLOAD):
    return client.post("/import", data={"payload_text": json.dumps(payload, ensure_ascii=False)})


def test_full_import_and_query_flow(client):
    resp = _import(client)
    assert resp.status_code == 303 and resp.headers["location"] == "/runs/RUN-T"

    list_resp = client.get("/")
    assert "RUN-T" in list_resp.text and "v1" in list_resp.text and "生效中" in list_resp.text

    detail = client.get("/runs/RUN-T")
    assert detail.status_code == 200
    # S1 入仓（A 层），S3 跨 11100 分界暂不入仓并展示可行范围
    assert "写进入仓 CSV" in detail.text
    assert "暂不入仓" in detail.text and "11100" in detail.text
    assert "[11000, 11100]" in detail.text
    # 段实长计入孔深：段 1 起点最小为 10000 + 500 + 100
    assert "10600" in detail.text

    csv_resp = client.get("/runs/RUN-T/csv")
    assert csv_resp.status_code == 200
    text = csv_resp.text
    assert "样品编号" in text and "S1" in text and "S3" not in text
    # 稳定同层样品 S1、S2、S4、S5 写入；跨层的 S3 不写入
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 5  # 表头 + 4 个样品
    assert all(no in text for no in ("S1", "S2", "S4", "S5"))


def test_correction_updates_whole_supply_and_bumps_version(client):
    _import(client)
    # 钉死段 1 起点 10700（绝对孔深）：S3 固定为 [11100,11160)，左闭右开归入 B 层
    resp = client.post(
        "/runs/RUN-T/correct",
        data={
            "expected_version": 1,
            "seg_index": "1",
            "depth": "10700",
            "note": "补测段 1 起点",
        },
    )
    assert resp.status_code == 303
    detail = client.get("/runs/RUN-T").text
    assert "v2" in detail and "补测段 1 起点" in detail
    # 段 1 起点固定 10700，S3 现在可入仓
    csv_text = client.get("/runs/RUN-T/csv").text
    assert "S3" in csv_text and "B" in csv_text
    assert "10700" in detail and "11160" in detail


def test_stale_version_rejected_and_supply_kept(client):
    _import(client)
    # 先做一次成功更正 → 版本 2
    client.post(
        "/runs/RUN-T/correct",
        data={"expected_version": 1, "seg_index": "1", "depth": "10700", "note": "第一次"},
    )
    # 再拿过期的版本 1 提交
    resp = client.post(
        "/runs/RUN-T/correct",
        data={"expected_version": 1, "seg_index": "1", "depth": "10750", "note": "过期提交"},
    )
    assert resp.status_code == 303
    flash_page = client.get("/runs/RUN-T").text
    assert "页面版本已过期" in flash_page
    # 库内仍是第一次更正的结果：实测点表展示 10700，且更正历史只有 v1→v2 一条
    assert "10700" in flash_page
    assert "v1 → v2" in flash_page and "v2 → v3" not in flash_page
    assert "v2" in flash_page


def test_infeasible_correction_rolls_back(client):
    _import(client)
    csv_before = client.get("/runs/RUN-T/csv").text
    # 旧语义补录的 10200 早于段 1 最早位置 10500，岩芯重叠 → 无解
    resp = client.post(
        "/runs/RUN-T/correct",
        data={"expected_version": 1, "seg_index": "1", "depth": "10200", "note": "坏数据"},
    )
    assert resp.status_code == 303
    page = client.get("/runs/RUN-T").text
    assert "约束无解" in page and "重叠" in page and "当前供数未做任何改动" in page
    # 版本未增长、供数未变、无更正历史
    assert "v1" in page
    assert client.get("/runs/RUN-T/csv").text == csv_before
    assert "暂无更正记录" in page


def test_import_bad_payload_persists_nothing(client):
    bad = dict(DEMO_PAYLOAD)
    bad["gaps"] = [
        {"low": 999, "high": 1000},
        {"low": 0, "high": 0},
        {"low": 0, "high": 0},
    ]  # 下限就放不下
    resp = client.post("/import", data={"payload_text": json.dumps(bad)})
    assert resp.status_code == 303
    home = client.get("/").text
    assert "RUN-T" not in home
    # 正常数据仍可导入
    assert _import(client).status_code == 303


def test_json_file_upload(client):
    files = {"payload_file": ("run.json", json.dumps(DEMO_PAYLOAD).encode(), "application/json")}
    resp = client.post("/import", data={"payload_text": ""}, files=files)
    assert resp.status_code == 303
    assert client.get("/runs/RUN-T").status_code == 200


# --------------------------------------------------------------------------- #
# 规则升级迁移：旧规则库（rules_version=1）启动后按新规则整份重算
# --------------------------------------------------------------------------- #
def _seed_legacy_db(db_path: str, payload: dict, supply_rows: list[tuple],
                    measured: list[tuple] | None = None) -> None:
    """绕过应用逻辑，直接构造一个规则 v1 的老库。"""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, version INTEGER NOT NULL, "
        "rules_version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active', "
        "conflict_note TEXT NOT NULL DEFAULT '', start_depth INTEGER NOT NULL, "
        "end_depth INTEGER NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE measured_starts (run_id TEXT NOT NULL, seg_index INTEGER NOT NULL, "
        "depth INTEGER NOT NULL, PRIMARY KEY (run_id, seg_index))"
    )
    conn.execute(
        "CREATE TABLE supply_rows (run_id TEXT NOT NULL, sample_no TEXT NOT NULL, "
        "assay TEXT NOT NULL, layer TEXT NOT NULL, seg_index INTEGER NOT NULL, "
        "start_lo INTEGER NOT NULL, start_hi INTEGER NOT NULL, end_lo INTEGER NOT NULL, "
        "end_hi INTEGER NOT NULL, PRIMARY KEY (run_id, sample_no))"
    )
    conn.execute(
        "CREATE TABLE correction_log (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
        "from_version INTEGER NOT NULL, to_version INTEGER NOT NULL, note TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO runs (run_id, version, rules_version, status, conflict_note, "
        "start_depth, end_depth, payload, updated_at) VALUES (?, 1, 1, 'active', '', ?, ?, ?, ?)",
        (payload["run_id"], payload["start_depth"], payload["end_depth"],
         json.dumps(payload, ensure_ascii=False), "2026-01-01T00:00:00+00:00"),
    )
    conn.executemany(
        "INSERT INTO supply_rows (run_id, sample_no, assay, layer, seg_index, "
        "start_lo, start_hi, end_lo, end_hi) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        supply_rows,
    )
    for seg_index, depth in measured or []:
        conn.execute(
            "INSERT INTO measured_starts (run_id, seg_index, depth) VALUES (?, ?, ?)",
            (payload["run_id"], seg_index, depth),
        )
    conn.execute(
        "INSERT INTO correction_log (run_id, from_version, to_version, note, created_at) "
        "VALUES (?, 1, 2, '历史更正', '2026-01-02T00:00:00+00:00')",
        (payload["run_id"],),
    )
    conn.commit()
    conn.close()


def _reload_app(tmp_path):
    import importlib
    import os

    os.environ["APP_DATA_DIR"] = str(tmp_path)
    import app.main as main
    importlib.reload(main)
    return main


def test_legacy_run_recomputed_and_supply_replaced(tmp_path):
    db_path = tmp_path / "runs.db"
    _seed_legacy_db(
        db_path,
        DEMO_PAYLOAD,
        # 旧规则漏算段实长，S3 曾被错误地整份放进 A 层供数
        [("RUN-T", "S3", "0.27", "A", 1, 10450, 10450, 10510, 10510)],
    )
    main = _reload_app(tmp_path)
    client = TestClient(main.app, follow_redirects=False)

    detail = client.get("/runs/RUN-T").text
    assert "规则 v2" in detail and "已生效" in detail
    # 旧的错误供数被整份替换：S3 新规则下跨层，不应出现在 CSV
    csv_text = client.get("/runs/RUN-T/csv").text
    assert "S3" not in csv_text and "S1" in csv_text
    # 原始录入与历史更正均保留
    assert "历史更正" in detail
    # 重算记录可查
    assert "v1 → v2" in detail

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT rules_version, status FROM runs WHERE run_id = 'RUN-T'"
        ).fetchone()
        assert row == (2, "active")
        log = conn.execute("SELECT status FROM recompute_log").fetchall()
        assert log == [("active",)]


def test_legacy_conflicting_run_blocked_until_corrected(tmp_path):
    db_path = tmp_path / "runs.db"
    payload = dict(DEMO_PAYLOAD)
    # 旧规则下录入的实测起点（按“起始+缺失”解释），新规则下导致岩芯重叠
    payload["measured_starts"] = [{"seg_index": 1, "depth": 10200}]
    _seed_legacy_db(
        db_path,
        payload,
        [("RUN-T", "S1", "0.12", "A", 0, 10020, 10020, 10100, 10100)],
        measured=[(1, 10200)],
    )
    main = _reload_app(tmp_path)
    client = TestClient(main.app, follow_redirects=False)

    # 迁移后标记冲突，旧供数清空，下载被门禁拦截
    detail = client.get("/runs/RUN-T").text
    assert "冲突待更正" in detail and "重叠" in detail
    assert "下载已暂停" in detail
    csv_resp = client.get("/runs/RUN-T/csv")
    assert csv_resp.status_code == 303
    assert "冲突" in client.get("/runs/RUN-T").text
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM supply_rows").fetchone()[0] == 0
        # 原始录入、实测点、更正历史保留
        assert conn.execute("SELECT COUNT(*) FROM measured_starts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM correction_log").fetchone()[0] == 1
        assert conn.execute(
            "SELECT status FROM runs WHERE run_id = 'RUN-T'"
        ).fetchone()[0] == "conflict"

    # 更正为合法绝对孔深后恢复下载，版本 +1
    ok = client.post(
        "/runs/RUN-T/correct",
        data={"expected_version": 1, "seg_index": "1", "depth": "10700", "note": "补录正确起点"},
    )
    assert ok.status_code == 303
    detail2 = client.get("/runs/RUN-T").text
    # 状态已恢复：下载按钮回来、冲突横幅消失；重算历史中保留“冲突待更正”记录
    assert "下载生效入仓 CSV" in detail2
    assert "历史实测起点与新孔深换算规则冲突" not in detail2
    assert "v2" in detail2
    csv_text = client.get("/runs/RUN-T/csv").text
    assert csv_text.startswith("﻿") and "S1" in csv_text
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT rules_version, status, version FROM runs WHERE run_id = 'RUN-T'"
        ).fetchone()
        assert row == (2, "active", 2)


def test_migration_resumes_after_interrupted_restart(tmp_path):
    """模拟首次迁移中断：回次仍停留在 rules_version=1，重启后续算。"""
    from app.storage import RULES_VERSION, Store

    db_path = tmp_path / "runs.db"
    _seed_legacy_db(
        db_path,
        DEMO_PAYLOAD,
        [("RUN-T", "S1", "0.12", "A", 0, 1, 1, 2, 2)],
    )
    # 第一次启动：完整迁移
    Store(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT rules_version FROM runs WHERE run_id = 'RUN-T'"
        ).fetchone()[0] == RULES_VERSION

    # 人为把回次退回未迁移状态并塞回旧供数，模拟“迁移中断后重启”
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE runs SET rules_version = 1, status = 'active', conflict_note = '' "
            "WHERE run_id = 'RUN-T'"
        )
        conn.execute("DELETE FROM supply_rows")
        conn.execute(
            "INSERT INTO supply_rows (run_id, sample_no, assay, layer, seg_index, "
            "start_lo, start_hi, end_lo, end_hi) "
            "VALUES ('RUN-T', 'S1', '0.12', 'A', 0, 1, 1, 2, 2)"
        )
        conn.commit()

    # 未迁移完成前，下载门禁必须拒绝（不允许返回旧规则供数）
    from app.storage import SupplyUnavailableError

    blocked_store = Store.__new__(Store)
    blocked_store.db_path = str(db_path)
    with pytest.raises(SupplyUnavailableError):
        blocked_store.supply_csv_bytes("RUN-T")

    # 重新实例化（等价于进程重启），迁移续算成功
    Store(db_path)
    main = _reload_app(tmp_path)
    client = TestClient(main.app, follow_redirects=False)
    csv_text = client.get("/runs/RUN-T/csv").text
    assert "S1" in csv_text and "S3" not in csv_text
    with sqlite3.connect(db_path) as conn:
        logs = conn.execute(
            "SELECT status FROM recompute_log ORDER BY id"
        ).fetchall()
        assert logs == [("active",), ("active",)]


def test_active_run_shows_download_link_on_list_page(client):
    _import(client)
    home = client.get("/").text
    assert "/runs/RUN-T/csv" in home and "生效中" in home
