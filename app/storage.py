"""SQLite 持久化与入仓 CSV 生成。

规则版本
--------
孔深换算规则修复（段实长计入孔深、实测起点按绝对孔深解释）后，规则版本
升级为 RULES_VERSION。应用启动时自动把旧规则回次按新规则**整份重算**：

- 可解：在单事务内整份替换 supply_rows，runs.rules_version 升到当前版本，
  状态置为 active；原始录入 payload、measured_starts 与 correction_log 保留；
- 无解：状态置为 conflict，旧供数表清空并记录冲突原因，详情页展示冲突并
  保留更正入口；数据员更正成功后状态恢复 active，下载随之恢复。

每个回次独立提交，迁移中断或进程重启后，未完成（rules_version 仍为旧值）
的回次会在下次启动时继续重算；CSV 下载只允许 active 且规则版本最新的回次，
因此任何时刻下载到的都是按新规则完整生效的供数。

更正实测起点后同样整份回次重新计算，单事务内整份替换供数表；
若新约束无解，事务不提交，当前供数原样保留。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .engine import EngineError, RunInput, RunResult, parse_payload, solve

# 孔深换算规则版本：v2 = 段实长计入孔深、实测起点按绝对孔深解释
RULES_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    version       INTEGER NOT NULL,
    rules_version INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'active',
    conflict_note TEXT NOT NULL DEFAULT '',
    start_depth   INTEGER NOT NULL,
    end_depth     INTEGER NOT NULL,
    payload       TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measured_starts (
    run_id     TEXT NOT NULL,
    seg_index  INTEGER NOT NULL,
    depth      INTEGER NOT NULL,
    PRIMARY KEY (run_id, seg_index),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS supply_rows (
    run_id      TEXT NOT NULL,
    sample_no   TEXT NOT NULL,
    assay       TEXT NOT NULL,
    layer       TEXT NOT NULL,
    seg_index   INTEGER NOT NULL,
    start_lo    INTEGER NOT NULL,
    start_hi    INTEGER NOT NULL,
    end_lo      INTEGER NOT NULL,
    end_hi      INTEGER NOT NULL,
    PRIMARY KEY (run_id, sample_no),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS correction_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    from_version INTEGER NOT NULL,
    to_version   INTEGER NOT NULL,
    note        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recompute_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    from_rules    INTEGER NOT NULL,
    to_rules      INTEGER NOT NULL,
    status        TEXT NOT NULL,
    note          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""

CSV_HEADER = [
    "回次编号",
    "样品编号",
    "化验值",
    "层位",
    "段序号",
    "起点孔深最小_mm",
    "起点孔深最大_mm",
    "终点孔深最小_mm",
    "终点孔深最大_mm",
]


class StaleVersionError(Exception):
    """页面携带的版本号与库内不一致，更正请求被拒绝。"""


class SupplyUnavailableError(Exception):
    """回次不是按当前规则完整生效的可下载供数（迁移未完成或冲突未更正）。"""


@dataclass
class RunRecord:
    run_id: str
    version: int
    rules_version: int
    status: str
    conflict_note: str
    start_depth: int
    end_depth: int
    payload: dict
    updated_at: str
    result: RunResult | None  # conflict 状态下无可行解，为 None
    measured_starts: list[tuple[int, int]]

    @property
    def is_active(self) -> bool:
        return self.status == "active" and self.rules_version == RULES_VERSION


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_columns(conn)
            conn.commit()
        # 启动/建库后立即重算所有旧规则回次；逐回次独立事务，可安全重入
        self.migrate_legacy_runs()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        """老库（初始提交版本）没有 rules_version/status/conflict_note 列，补齐。"""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        if "rules_version" not in existing:
            conn.execute("ALTER TABLE runs ADD COLUMN rules_version INTEGER NOT NULL DEFAULT 1")
        if "status" not in existing:
            conn.execute("ALTER TABLE runs ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "conflict_note" not in existing:
            conn.execute("ALTER TABLE runs ADD COLUMN conflict_note TEXT NOT NULL DEFAULT ''")

    # ------------------------------------------------------------------ #
    def list_runs(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT run_id, version, rules_version, status, conflict_note, "
                    "start_depth, end_depth, updated_at "
                    "FROM runs ORDER BY run_id"
                )
            )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload"])
            measured = list(
                conn.execute(
                    "SELECT seg_index, depth FROM measured_starts "
                    "WHERE run_id = ? ORDER BY seg_index",
                    (run_id,),
                )
            )
        result = None
        if row["status"] == "active":
            # active 回次必须可解；若求解失败说明数据损坏，直接抛出便于发现问题
            result = solve(parse_payload(payload))
        return RunRecord(
            run_id=row["run_id"],
            version=row["version"],
            rules_version=row["rules_version"],
            status=row["status"],
            conflict_note=row["conflict_note"],
            start_depth=row["start_depth"],
            end_depth=row["end_depth"],
            payload=payload,
            updated_at=row["updated_at"],
            result=result,
            measured_starts=[(m["seg_index"], m["depth"]) for m in measured],
        )

    # ------------------------------------------------------------------ #
    def save_new_run(self, payload: dict) -> RunRecord:
        """导入新回次：先全量校验与求解，再在事务内写入。"""
        run_input = parse_payload(payload)
        result = solve(run_input)  # 无解会抛 InfeasibleError，库内不产生任何数据
        now = _now()
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_input.run_id,)
            ).fetchone()
            if exists:
                raise ValueError(f"回次 {run_input.run_id} 已存在，请在详情页更正")
            self._insert_run(
                conn, run_input, result, payload,
                version=1, now=now, status="active", conflict_note="",
            )
            conn.commit()
        record = self.get_run(run_input.run_id)
        assert record is not None
        return record

    def correct_run(
        self, run_id: str, expected_version: int, measured_starts: list[dict], note: str
    ) -> RunRecord:
        """更正实测段起点并整份重算供数。

        expected_version 与库内版本不一致时抛 StaleVersionError；
        新约束无解时抛 InfeasibleError，事务回滚、旧供数保留。
        若回次此前处于 conflict 状态，更正成功后恢复为 active，下载随之恢复。
        """
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["version"] != expected_version:
                raise StaleVersionError(
                    f"页面版本已过期：页面基于版本 {expected_version}，"
                    f"库内当前版本为 {row['version']}，请刷新详情页后重新更正。"
                    "当前供数未做任何改动。"
                )

            payload = json.loads(row["payload"])
            # 复用 parse_payload 的全部校验逻辑
            payload["measured_starts"] = measured_starts
            run_input = parse_payload(payload)
            result = solve(run_input)  # 无解 -> 抛异常 -> 事务回滚

            now = _now()
            new_version = expected_version + 1
            conn.execute("DELETE FROM supply_rows WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM measured_starts WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            self._insert_run(
                conn, run_input, result, payload,
                version=new_version, now=now, status="active", conflict_note="",
            )
            conn.execute(
                "INSERT INTO correction_log (run_id, from_version, to_version, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, expected_version, new_version, note or "更正实测起点", now),
            )
            conn.commit()
        record = self.get_run(run_id)
        assert record is not None
        return record

    def correction_history(self, run_id: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM correction_log WHERE run_id = ? ORDER BY id",
                    (run_id,),
                )
            )

    def recompute_history(self, run_id: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM recompute_log WHERE run_id = ? ORDER BY id",
                    (run_id,),
                )
            )

    # ------------------------------------------------------------------ #
    def migrate_legacy_runs(self) -> list[str]:
        """把所有非当前规则版本的回次按新规则整份重算，逐回次独立事务。

        可解：整份替换供数，状态 active；不可解：清空旧供数，状态 conflict。
        中断/重启后凭 runs.rules_version 判定，未完成的回次继续重算。
        返回本次实际重算的回次编号列表。
        """
        migrated: list[str] = []
        with self._connect() as conn:
            rows = list(
                conn.execute(
                    "SELECT run_id FROM runs WHERE rules_version < ? ORDER BY run_id",
                    (RULES_VERSION,),
                )
            )
        for row in rows:
            run_id = row["run_id"]
            self._recompute_one(run_id)
            migrated.append(run_id)
        return migrated

    def _recompute_one(self, run_id: str) -> None:
        """在独立事务内重算单个回次并更新规则版本/状态。"""
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row["rules_version"] >= RULES_VERSION:
                return
            from_rules = row["rules_version"]
            payload = json.loads(row["payload"])
            try:
                run_input = parse_payload(payload)
                result = solve(run_input)
            except EngineError as exc:
                # 旧实测起点在新规则下无解：保留原始录入与更正历史，
                # 清空按旧规则算出的供数，标记冲突等待数据员更正
                conn.execute("DELETE FROM supply_rows WHERE run_id = ?", (run_id,))
                conn.execute(
                    "UPDATE runs SET rules_version = ?, status = 'conflict', "
                    "conflict_note = ?, updated_at = ? WHERE run_id = ?",
                    (RULES_VERSION, str(exc), now, run_id),
                )
                conn.execute(
                    "INSERT INTO recompute_log "
                    "(run_id, from_rules, to_rules, status, note, created_at) "
                    "VALUES (?, ?, ?, 'conflict', ?, ?)",
                    (run_id, from_rules, RULES_VERSION, str(exc), now),
                )
                conn.commit()
                return

            conn.execute("DELETE FROM supply_rows WHERE run_id = ?", (run_id,))
            conn.executemany(
                "INSERT INTO supply_rows "
                "(run_id, sample_no, assay, layer, seg_index, start_lo, start_hi, end_lo, end_hi) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        s.sample_no,
                        s.assay,
                        s.layer,
                        s.seg_index,
                        s.start_lo,
                        s.start_hi,
                        s.end_lo,
                        s.end_hi,
                    )
                    for s in result.samples
                    if s.layer is not None
                ],
            )
            conn.execute(
                "UPDATE runs SET rules_version = ?, status = 'active', conflict_note = '', "
                "updated_at = ? WHERE run_id = ?",
                (RULES_VERSION, now, run_id),
            )
            conn.execute(
                "INSERT INTO recompute_log "
                "(run_id, from_rules, to_rules, status, note, created_at) "
                "VALUES (?, ?, ?, 'active', ?, ?)",
                (
                    run_id,
                    from_rules,
                    RULES_VERSION,
                    f"按规则 v{RULES_VERSION} 整份重算并替换供数",
                    now,
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------ #
    def _insert_run(
        self, conn, run_input: RunInput, result: RunResult, payload: dict,
        version: int, now: str, status: str, conflict_note: str,
    ):
        conn.execute(
            "INSERT INTO runs (run_id, version, rules_version, status, conflict_note, "
            "start_depth, end_depth, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_input.run_id,
                version,
                RULES_VERSION,
                status,
                conflict_note,
                run_input.start_depth,
                run_input.end_depth,
                json.dumps(payload, ensure_ascii=False),
                now,
            ),
        )
        conn.executemany(
            "INSERT INTO measured_starts (run_id, seg_index, depth) VALUES (?, ?, ?)",
            [(run_input.run_id, m.seg_index, m.depth) for m in run_input.measured_starts],
        )
        conn.executemany(
            "INSERT INTO supply_rows "
            "(run_id, sample_no, assay, layer, seg_index, start_lo, start_hi, end_lo, end_hi) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_input.run_id,
                    s.sample_no,
                    s.assay,
                    s.layer,
                    s.seg_index,
                    s.start_lo,
                    s.start_hi,
                    s.end_lo,
                    s.end_hi,
                )
                for s in result.samples
                if s.layer is not None
            ],
        )

    # ------------------------------------------------------------------ #
    def supply_csv_bytes(self, run_id: str) -> bytes:
        """读取库内生效供数表并生成入仓 CSV（utf-8-sig，便于 Excel 打开）。

        仅当回次为 active 且规则版本最新时允许下载；迁移未完成或冲突未更正
        时抛 SupplyUnavailableError，保证下载内容一定按新规则完整生效。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, rules_version FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["rules_version"] < RULES_VERSION:
                raise SupplyUnavailableError(
                    f"回次 {run_id} 正在按新规则重算，重算完成前暂不提供下载，请稍后重试。"
                )
            if row["status"] != "active":
                raise SupplyUnavailableError(
                    f"回次 {run_id} 的历史实测起点与新孔深规则冲突，暂无可下载供数；"
                    "请在详情页更正实测起点，更正成功后恢复下载。"
                )
            rows = list(
                conn.execute(
                    "SELECT * FROM supply_rows WHERE run_id = ? ORDER BY sample_no",
                    (run_id,),
                )
            )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(CSV_HEADER)
        for r in rows:
            writer.writerow(
                [
                    r["run_id"],
                    r["sample_no"],
                    r["assay"],
                    r["layer"],
                    r["seg_index"],
                    r["start_lo"],
                    r["start_hi"],
                    r["end_lo"],
                    r["end_hi"],
                ]
            )
        return "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")

    def supply_csv(self, run_id: str) -> str:
        return self.supply_csv_bytes(run_id).decode("utf-8-sig")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
