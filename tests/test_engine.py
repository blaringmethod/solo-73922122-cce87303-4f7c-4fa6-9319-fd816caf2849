"""引擎求解器单元测试：可行区间、实测锚点、地层判定、无解与重叠场景。

规则 v2：段起点孔深 = 回次起始 + 前段岩芯实长 C_j + 前段缺失 P_j；
实测起点为绝对孔深，等价于 P_j = 实测孔深 - 回次起始 - C_j。
"""

import pytest

from app.engine import (
    InfeasibleError,
    ValidationError,
    parse_payload,
    solve,
)

DEMO = {
    "run_id": "R1",
    "start_depth": 10000,
    "end_depth": 12500,
    "segments": [
        {"length": 500, "samples": [
            {"sample_no": "S1", "start": 20, "end": 100, "assay": "0.12"},
            {"sample_no": "S2", "start": 400, "end": 480, "assay": "0.08"},
        ]},
        {"length": 600, "samples": [
            {"sample_no": "S3", "start": 50, "end": 120, "assay": "0.31"},
            {"sample_no": "S4", "start": 400, "end": 460, "assay": "0.27"},
        ]},
        {"length": 700, "samples": [
            {"sample_no": "S5", "start": 500, "end": 650, "assay": "1.05"},
        ]},
        {"length": 400, "samples": [
            {"sample_no": "S6", "start": 50, "end": 200, "assay": "0.66"},
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


def test_open_bounds_ranges():
    result = solve(parse_payload(DEMO))
    # 段起点 = 10000 + C_j + P_j，C = [0, 500, 1100, 1800]
    # P1 ∈ [100,200]（受尾预算 300 反向收紧）；P2 ∈ [150,250]；P3 ∈ [200,300]
    assert result.core_offsets == [0, 500, 1100, 1800]
    assert result.seg_start_lo == [10000, 10600, 11250, 12000]
    assert result.seg_start_hi == [10000, 10700, 11350, 12100]
    # 段间绝不重叠：每段起点下界 = 前段起点下界 + 前段实长 + 缺失下限
    for j in range(1, 4):
        assert result.seg_start_lo[j] >= result.seg_end_lo[j - 1] + DEMO["gaps"][j - 1]["low"]
    assert result.total_gap_high == 300

    by_no = {s.sample_no: s for s in result.samples}
    # S1 在段 0，位置固定：[10020,10100)
    assert (by_no["S1"].start_lo, by_no["S1"].start_hi) == (10020, 10020)
    assert (by_no["S1"].end_lo, by_no["S1"].end_hi) == (10100, 10100)
    # S4 在段 1 段内 [400,460)：段起点 [10600,10700]
    assert (by_no["S4"].start_lo, by_no["S4"].start_hi) == (11000, 11100)
    assert (by_no["S4"].end_lo, by_no["S4"].end_hi) == (11060, 11160)
    # S6 在段 3，段内 [50,200)：段起点 [12000,12100]
    assert (by_no["S6"].start_lo, by_no["S6"].start_hi) == (12050, 12150)
    assert (by_no["S6"].end_lo, by_no["S6"].end_hi) == (12200, 12300)


def test_layer_classification_crossing_and_fixed():
    result = solve(parse_payload(DEMO))
    by_no = {s.sample_no: s for s in result.samples}
    # S1/S2/S3 永远在 A；S6 永远在 B；S4 跨越 11100 分界，层位不唯一
    assert by_no["S1"].layer == "A" and by_no["S2"].layer == "A"
    assert by_no["S3"].layer == "A"
    assert by_no["S6"].layer == "B"
    assert by_no["S4"].layer is None and "11100" in by_no["S4"].reason
    # S5 整体位于 B 层（起点最小 11800 > 11200）
    assert by_no["S5"].layer == "B"


def test_measured_anchor_narrows_and_fixes_layer():
    payload = dict(DEMO)
    # 实测段 1 起点 10700（绝对孔深）→ P1 = 10700-10000-500 = 200
    payload["measured_starts"] = [{"seg_index": 1, "depth": 10700}]
    result = solve(parse_payload(payload))
    assert result.seg_start_lo[1] == result.seg_start_hi[1] == 10700
    # 段 2：10000 + 1100 + (200 + g2)，g2 ∈ [50,200]，再为 g3 保留下限 50 后
    # 受尾预算（P3 ≤ 300）收紧：P2 ≤ 300-50 = 250，段 2 起点固定为 11350
    assert result.seg_start_lo[2] == 11350 and result.seg_start_hi[2] == 11350
    by_no = {s.sample_no: s for s in result.samples}
    # S4 固定段 1：[11100,11160)，起点恰为分界，左闭右开下完整落入 B
    assert by_no["S4"].layer == "B"

    # 再钉段 2 起点 11350 → P2 = 11350-10000-1100 = 250，即 g2 = 50
    payload["measured_starts"] = [
        {"seg_index": 1, "depth": 10700},
        {"seg_index": 2, "depth": 11350},
    ]
    result = solve(parse_payload(payload))
    by_no = {s.sample_no: s for s in result.samples}
    assert result.seg_start_lo[1] == result.seg_start_hi[1] == 10700
    assert result.seg_start_lo[2] == result.seg_start_hi[2] == 11350
    # 段 3：g3 = P3 - P2 = 300 - 250 = 50，段 3 固定在 12100
    assert result.seg_start_lo[3] == result.seg_start_hi[3] == 12100
    assert by_no["S4"].layer == "B" and by_no["S6"].layer == "B"


def test_infeasible_anchor_between_two_measurements():
    payload = dict(DEMO)
    # P1 = 11000-10000-500 = 500，超出 g1 ∈ [100,300]
    payload["measured_starts"] = [{"seg_index": 1, "depth": 11000}]
    with pytest.raises(InfeasibleError, match="约束无解"):
        solve(parse_payload(payload))


def test_legacy_measured_start_now_conflicts():
    """旧规则把实测孔深当成“起始+缺失”，按旧语义补录的值在新规则下无解。"""
    payload = dict(DEMO)
    # 旧规则下 P1 会被当成 200（10200-10000）；新规则 P1 = 200-500 = -300，
    # 实测点早于“起始+前段实长 10500”，岩芯重叠
    payload["measured_starts"] = [{"seg_index": 1, "depth": 10200}]
    with pytest.raises(InfeasibleError, match="重叠"):
        solve(parse_payload(payload))


def test_infeasible_tail_overflow():
    payload = dict(DEMO)
    # 锚点本身落在等式块允许范围内（P2 = 11350-10000-1100 = 250），
    # 但其后 g3 至少 50：P2 + 50 = 300 恰好等于总缺失上限 300，可行；
    # 改钉段 2 起点 11400（P2 = 300），再加 g3 下限 50 → 超出总缺失上限，
    # 末端开放块容不下，按终孔容量报无解
    payload["measured_starts"] = [{"seg_index": 2, "depth": 11400}]
    with pytest.raises(InfeasibleError, match="终孔"):
        solve(parse_payload(payload))


def test_equality_block_partial_sums_are_tight():
    """两个实测点之间总量固定时，中间前缀同时受前后变量上下限挤占。"""
    payload = {
        "run_id": "R4",
        "start_depth": 2000,
        "end_depth": 3000,
        "segments": [{"length": 100}, {"length": 100}, {"length": 100}],
        "gaps": [{"low": 10, "high": 50}, {"low": 20, "high": 80}],
        "boundaries": [],
        "layers": ["X"],
        # 钉死 P2 = 60（g1+g2=60）：段 2 绝对起点 = 2000 + C2(200) + 60 = 2260
        "measured_starts": [{"seg_index": 2, "depth": 2260}],
    }
    result = solve(parse_payload(payload))
    # P1=g1 ∈ [max(10,60-80)=10, min(50,60-20)=40]
    # 段 1 起点 = 2000 + 100 + P1 ∈ [2110, 2140]
    assert result.seg_start_lo == [2000, 2110, 2260]
    assert result.seg_start_hi == [2000, 2140, 2260]


def test_single_segment_run():
    """只有一个岩芯段时没有缺失变量，所有孔深完全确定。"""
    payload = {
        "run_id": "R5",
        "start_depth": 5000,
        "end_depth": 5800,
        "segments": [{"length": 300, "samples": [
            {"sample_no": "U1", "start": 0, "end": 300, "assay": "9"},
        ]}],
        "gaps": [],
        "boundaries": [5300],
        "layers": ["上", "下"],
        "measured_starts": [],
    }
    result = solve(parse_payload(payload))
    assert result.seg_start_lo == result.seg_start_hi == [5000]
    assert result.seg_end_lo == result.seg_end_hi == [5300]
    # 样品 [5000,5300) 终点恰为分界，左闭右开下完整属“上”层
    assert result.samples[0].layer == "上"
    assert result.total_gap_high == 500  # 顶部 500 mm 自由缺失


def test_zero_gap_bounds_allowed():
    """缺失上下限均为 0 是合法录入，表示段间无缺失，岩芯首尾相接。"""
    payload = {
        "run_id": "R6",
        "start_depth": 0,
        "end_depth": 500,
        "segments": [{"length": 100}, {"length": 100}],
        "gaps": [{"low": 0, "high": 0}],
        "boundaries": [],
        "layers": ["X"],
        "measured_starts": [],
    }
    result = solve(parse_payload(payload))
    assert result.seg_start_lo == result.seg_start_hi == [0, 100]
    assert result.seg_end_lo == result.seg_end_hi == [100, 200]


def test_partial_sum_bounds_are_tight():
    """构造 3 个缺失变量，验证部分和上下限可同时取到（紧边界）。"""
    payload = {
        "run_id": "R2",
        "start_depth": 0,
        "end_depth": 1000,
        "segments": [{"length": 100}, {"length": 100}, {"length": 100}, {"length": 100}],
        "gaps": [
            {"low": 10, "high": 50},
            {"low": 20, "high": 80},
            {"low": 30, "high": 120},
        ],
        "boundaries": [],
        "layers": ["X"],
        "measured_starts": [],
    }
    result = solve(parse_payload(payload))
    # 总缺失上限 = 1000-400 = 600；C = [0,100,200,300]
    # P1: [10,50]，P2: [30,130]，P3: [60,250]
    assert result.seg_start_lo == [0, 110, 230, 360]
    assert result.seg_start_hi == [0, 150, 330, 550]


def test_boundary_value_uses_half_open_intervals():
    """样品终点恰为分界点时不算跨层（左闭右开）。"""
    payload = {
        "run_id": "R3",
        "start_depth": 0,
        "end_depth": 500,
        "segments": [{"length": 200, "samples": [
            {"sample_no": "T1", "start": 100, "end": 200, "assay": "x"},
        ]}],
        "gaps": [],
        "boundaries": [200],
        "layers": ["L1", "L2"],
        "measured_starts": [],
    }
    result = solve(parse_payload(payload))
    assert result.samples[0].layer == "L1"
    # 样品起点恰为分界点时归入 L2
    payload["segments"][0]["samples"][0] = {
        "sample_no": "T1", "start": 0, "end": 200, "assay": "x"
    }
    payload["boundaries"] = [100]
    result = solve(parse_payload(payload))
    assert result.samples[0].layer is None  # [0,200) 跨越 100


def test_measured_depth_outside_run_rejected():
    """实测起点落在回次区间之外属于录入校验错误。"""
    payload = dict(DEMO)
    payload["measured_starts"] = [{"seg_index": 1, "depth": 12500}]  # 等于终孔
    with pytest.raises(ValidationError, match="不小于回次终止孔深"):
        parse_payload(payload)
    payload["measured_starts"] = [{"seg_index": 1, "depth": 9999}]  # 小于起始
    with pytest.raises(ValidationError, match="小于回次起始孔深"):
        parse_payload(payload)


def test_validation_errors():
    with pytest.raises(ValidationError):
        parse_payload({**DEMO, "segments": []})
    with pytest.raises(ValidationError):
        parse_payload({**DEMO, "gaps": [{"low": 300, "high": 100}, {"low": 0, "high": 0},
                                        {"low": 0, "high": 0}]})
    bad = dict(DEMO)
    bad["segments"] = [dict(DEMO["segments"][0])]
    bad["gaps"] = []
    bad["boundaries"] = []
    bad["layers"] = ["A", "B"]  # 无分界却给两个层位
    with pytest.raises(ValidationError):
        parse_payload(bad)
    bad2 = dict(DEMO)
    bad2["segments"] = [{**seg, "samples": [
        {"sample_no": "BAD", "start": 400, "end": 400, "assay": "1"}
    ]} for seg in [DEMO["segments"][0]]]
    bad2["gaps"] = []
    with pytest.raises(ValidationError):
        parse_payload(bad2)
