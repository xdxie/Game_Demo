"""
NitroGen 原始动作数据查看器。

读取由 NITROGEN_DUMP_PATH 生成的 JSONL 文件，输出：
  A. 元信息（总帧数、时长、fps、change 占比）
  B. 摇杆分布（lx/ly/rx/ry min/mean/max + 方向频次）
  C. 扳机分布（LT/RT 直方图）
  D. 按键频次（每键出现次数、平均/最高置信度）
  E. 逐帧明细（--detail 开启，--head N 限行数）

用法：
  python tools/dump_wukong_viewer.py wukong_actions.jsonl
  python tools/dump_wukong_viewer.py wukong_actions.jsonl --detail --head 30
  python tools/dump_wukong_viewer.py wukong_actions.jsonl --filter button=NORTH
  python tools/dump_wukong_viewer.py wukong_actions.jsonl --filter change=1
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


# ── 解析工具 ──────────────────────────────────────────────────────────────

def _parse_buttons(pressed: list | None) -> list[tuple[str, float]]:
    """从 ['SOUTH(0.92)', 'EAST(0.41)'] 解析为 [('SOUTH', 0.92), ...]"""
    if not pressed:
        return []
    result = []
    for entry in pressed:
        name = entry.split("(")[0].strip()
        try:
            conf = float(entry.split("(")[1].rstrip(")")) if "(" in entry else 1.0
        except (IndexError, ValueError):
            conf = 1.0
        if name:
            result.append((name, conf))
    return result


def load_jsonl(path: Path, filter_arg: str | None = None):
    records = []
    with path.open(encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if filter_arg:
                if not _match_filter(rec, filter_arg):
                    continue
            records.append(rec)
    return records


def _match_filter(rec: dict, filt: str) -> bool:
    """支持 button=NORTH 或 change=1"""
    if "=" not in filt:
        return True
    key, val = filt.split("=", 1)
    key = key.strip().lower()
    val = val.strip()
    if key == "button":
        pressed = (rec.get("action_summary") or {}).get("buttons_avg_pressed") or []
        names = [e.split("(")[0].strip() for e in pressed]
        return val.upper() in names
    if key == "change":
        return str(int(rec.get("is_change", False))) == val
    return True


# ── 统计 ──────────────────────────────────────────────────────────────────

def _stat(values: list[float]) -> dict:
    if not values:
        return {"min": 0.0, "mean": 0.0, "max": 0.0, "n": 0}
    return {
        "min":  round(min(values), 3),
        "mean": round(sum(values) / len(values), 3),
        "max":  round(max(values), 3),
        "n":    len(values),
    }


def analyze(records: list[dict]):
    total = len(records)
    if total == 0:
        return None

    ts_list = [r["ts"] for r in records if "ts" in r]
    duration = (max(ts_list) - min(ts_list)) if len(ts_list) >= 2 else 0.0
    fps_actual = total / duration if duration > 0 else 0.0
    change_count = sum(1 for r in records if r.get("is_change"))

    # 摇杆
    lx_vals, ly_vals, rx_vals, ry_vals = [], [], [], []
    for r in records:
        s = r.get("action_summary") or {}
        left  = s.get("left_stick_mean") or [0.0, 0.0]
        right = s.get("right_stick_mean") or [0.0, 0.0]
        if len(left) >= 2:
            lx_vals.append(float(left[0]))
            ly_vals.append(float(left[1]))
        if len(right) >= 2:
            rx_vals.append(float(right[0]))
            ry_vals.append(float(right[1]))

    # 左摇杆方向分布（幅度>0.2才算）
    dir_counts: Counter = Counter()
    for lx, ly in zip(lx_vals, ly_vals):
        mag = (lx**2 + ly**2) ** 0.5
        if mag < 0.2:
            dir_counts["None"] += 1
        elif abs(lx) >= abs(ly):
            dir_counts["RIGHT" if lx > 0 else "LEFT"] += 1
        else:
            dir_counts["FORWARD" if ly > 0 else "BACK"] += 1

    # 扳机
    lt_vals, rt_vals = [], []
    for r in records:
        s = r.get("action_summary") or {}
        triggers = s.get("trigger_means") or {}
        lt_vals.append(float(triggers.get("LEFT_TRIGGER", 0.0)))
        rt_vals.append(float(triggers.get("RIGHT_TRIGGER", 0.0)))

    def _hist(vals):
        bins = {"0.0-0.2": 0, "0.2-0.5": 0, "0.5-0.8": 0, "0.8-1.0": 0}
        for v in vals:
            if v < 0.2:   bins["0.0-0.2"] += 1
            elif v < 0.5: bins["0.2-0.5"] += 1
            elif v < 0.8: bins["0.5-0.8"] += 1
            else:         bins["0.8-1.0"] += 1
        return bins

    # 按键
    btn_count: Counter = Counter()
    btn_conf_sum: defaultdict = defaultdict(float)
    btn_conf_max: defaultdict = defaultdict(float)
    for r in records:
        s = r.get("action_summary") or {}
        pressed = s.get("buttons_avg_pressed") or []
        for name, conf in _parse_buttons(pressed):
            btn_count[name] += 1
            btn_conf_sum[name] += conf
            btn_conf_max[name] = max(btn_conf_max[name], conf)

    return {
        "total": total,
        "duration": duration,
        "fps": fps_actual,
        "change_count": change_count,
        "lx": _stat(lx_vals), "ly": _stat(ly_vals),
        "rx": _stat(rx_vals), "ry": _stat(ry_vals),
        "dir_counts": dir_counts,
        "lt": _stat(lt_vals), "rt": _stat(rt_vals),
        "lt_hist": _hist(lt_vals), "rt_hist": _hist(rt_vals),
        "btn_count": btn_count,
        "btn_conf_sum": dict(btn_conf_sum),
        "btn_conf_max": dict(btn_conf_max),
    }


# ── 打印工具 ──────────────────────────────────────────────────────────────

def _ptable(headers, rows, col_widths=None):
    if not rows:
        print("  （无数据）")
        return
    if not col_widths:
        col_widths = [
            max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
            for i, h in enumerate(headers)
        ]
    sep = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
    fmt = "| " + " | ".join(f"{{:<{w}}}" for w in col_widths) + " |"
    print(sep)
    print(fmt.format(*[str(h) for h in headers]))
    print(sep)
    for row in rows:
        print(fmt.format(*[str(c) for c in row]))
    print(sep)


def _bar(val: float, width: int = 20) -> str:
    filled = int(round(val * width))
    return "█" * filled + "░" * (width - filled)


# ── 报告输出 ──────────────────────────────────────────────────────────────

def print_report(stats: dict):
    total = stats["total"]

    # A. 元信息
    print("\n" + "=" * 64)
    print("A. 元信息")
    print("=" * 64)
    print(f"  总帧数:      {total}")
    print(f"  录制时长:    {stats['duration']:.1f} 秒")
    print(f"  实际 fps:    {stats['fps']:.2f}")
    print(f"  动作变化帧:  {stats['change_count']}  ({stats['change_count']/total*100:.1f}%)")

    # B. 摇杆分布
    print("\n" + "=" * 64)
    print("B. 摇杆分布")
    print("=" * 64)
    stk_rows = [
        ("左摇杆 X (lx)", stats["lx"]["min"], stats["lx"]["mean"], stats["lx"]["max"]),
        ("左摇杆 Y (ly)", stats["ly"]["min"], stats["ly"]["mean"], stats["ly"]["max"]),
        ("右摇杆 X (rx)", stats["rx"]["min"], stats["rx"]["mean"], stats["rx"]["max"]),
        ("右摇杆 Y (ry)", stats["ry"]["min"], stats["ry"]["mean"], stats["ry"]["max"]),
    ]
    _ptable(["轴", "min", "mean", "max"], stk_rows)

    print("\n  左摇杆方向频次：")
    dir_rows = [
        (k, v, f"{v/total*100:.1f}%", _bar(v/total))
        for k, v in sorted(stats["dir_counts"].items(), key=lambda x: -x[1])
    ]
    _ptable(["方向", "帧次", "占比", "分布"], dir_rows)

    # C. 扳机分布
    print("\n" + "=" * 64)
    print("C. 扳机分布")
    print("=" * 64)
    trig_rows = [
        ("LEFT_TRIGGER",  stats["lt"]["mean"], stats["lt"]["max"]),
        ("RIGHT_TRIGGER", stats["rt"]["mean"], stats["rt"]["max"]),
    ]
    _ptable(["扳机", "均值", "最大值"], trig_rows)

    print("\n  LT 直方图：")
    for bucket, cnt in stats["lt_hist"].items():
        print(f"    {bucket}  {_bar(cnt/total, 30)}  {cnt} ({cnt/total*100:.1f}%)")
    print("  RT 直方图：")
    for bucket, cnt in stats["rt_hist"].items():
        print(f"    {bucket}  {_bar(cnt/total, 30)}  {cnt} ({cnt/total*100:.1f}%)")

    # D. 按键频次
    print("\n" + "=" * 64)
    print("D. 按键频次（出现过的按键）")
    print("=" * 64)
    if stats["btn_count"]:
        btn_rows = []
        for name, cnt in sorted(stats["btn_count"].items(), key=lambda x: -x[1]):
            avg_c = stats["btn_conf_sum"][name] / cnt
            max_c = stats["btn_conf_max"][name]
            btn_rows.append((name, cnt, f"{cnt/total*100:.1f}%", f"{avg_c:.2f}", f"{max_c:.2f}"))
        _ptable(["按键", "帧次", "帧占比", "均值置信度", "最高置信度"], btn_rows)
    else:
        print("  （未检测到按键数据）")


def print_detail(records: list[dict], head: int | None = None):
    print("\n" + "=" * 64)
    print(f"E. 逐帧明细（共 {len(records)} 帧{f'，显示前 {head}' if head else ''}）")
    print("=" * 64)
    shown = records[:head] if head else records
    for i, r in enumerate(shown):
        s = r.get("action_summary") or {}
        left  = s.get("left_stick_mean") or [0, 0]
        right = s.get("right_stick_mean") or [0, 0]
        trig  = s.get("trigger_means") or {}
        pressed = s.get("buttons_avg_pressed") or []
        btns_str = ", ".join(pressed[:4]) + ("…" if len(pressed) > 4 else "")
        lt = trig.get("LEFT_TRIGGER", 0.0)
        rt = trig.get("RIGHT_TRIGGER", 0.0)
        chg = "★" if r.get("is_change") else " "
        ts_offset = r["ts"] - records[0]["ts"] if "ts" in r and "ts" in records[0] else 0
        print(
            f"  [{i+1:4d}] {chg} t+{ts_offset:6.1f}s  "
            f"lx={left[0]:+.2f} ly={left[1]:+.2f}  "
            f"LT={lt:.2f} RT={rt:.2f}  "
            f"btns=[{btns_str}]"
        )


# ── 主程序 ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NitroGen 原始动作数据查看器")
    parser.add_argument("jsonl", help="JSONL 文件路径")
    parser.add_argument("--detail", action="store_true", help="输出逐帧明细")
    parser.add_argument("--head", type=int, default=None, help="逐帧明细只显示前 N 帧")
    parser.add_argument("--filter", default=None,
                        help="过滤条件，例如 button=NORTH 或 change=1")
    args = parser.parse_args()

    path = Path(args.jsonl)
    if not path.exists():
        print(f"[ERROR] 找不到文件: {path}")
        sys.exit(1)

    records = load_jsonl(path, filter_arg=args.filter)
    print(f"\n读取文件: {path}  ({path.stat().st_size // 1024} KB)")
    if args.filter:
        print(f"过滤条件: {args.filter} → {len(records)} 条匹配")

    if not records:
        print("  （无记录或过滤后为空）")
        sys.exit(0)

    stats = analyze(records)
    print_report(stats)
    if args.detail:
        print_detail(records, args.head)

    print("\n查看完毕。\n")


if __name__ == "__main__":
    main()
