"""
分析 session.log 里的黑神话悟空动作数据。

支持两种日志格式：
  格式A（新版，已修复）：含 [模型原始] / [翻译] / [快提示] 行
  格式B（旧版）：从 ActionFilter 10s统计 行提取意图/方向聚合，从 ActionFilter 触发 行提取事件

用法：
  python tools/analyze_wukong_log.py [session.log路径]
"""

import re
import sys
import json
from collections import Counter, defaultdict
from pathlib import Path

# ── 悟空词表（与 game_vocab.py 同步）──────────────────────────────────────
WUKONG_BUTTON_TO_TEXT = {
    "SOUTH":          "翻滚闪避！",
    "EAST":           "轻击！",
    "NORTH":          "重击！",
    "WEST":           "化身！",
    "LEFT_TRIGGER":   "棍势蓄力！",
    "RIGHT_TRIGGER":  "识破！",
    "LEFT_SHOULDER":  "切法术！",
    "RIGHT_SHOULDER": "切棍法！",
}
WUKONG_TEMPLATE_EVENTS = {
    "sudden_dodge":    "往X翻滚！ / 翻滚闪避！",
    "attack_window":   "出招！ / 重击！",
    "sustained_danger":"稳住，别贪刀 / 拉开距离",
    "movement_shift":  "往X走位 / 换位置",
}

# ── regex ────────────────────────────────────────────────────────────────
RE_RAW      = re.compile(r"\[模型原始\]\s+intent=(\S+)\s+conf=[\d.]+\s+dir=(\S+)\s+buttons=(\[.*?\])")
RE_TRANSLATE= re.compile(r"\[翻译\]\s+\[black_myth_wukong\]\s+(\S+)\s+->\s+'([^']*)'")
RE_EVENT    = re.compile(r"ActionFilter 触发:\s+(\S+)\s+@")
RE_10S      = re.compile(r"ActionFilter 10s统计:.*?intents=\[([^\]]+)\].*?dir=(\S+)")


def parse_log(log_path: Path):
    btn_counts:    Counter = Counter()
    btn_conf_sum:  defaultdict = defaultdict(float)
    intent_counts: Counter = Counter()
    direction_counts: Counter = Counter()
    event_counts:  Counter = Counter()
    phrase_counts: Counter = Counter()
    event_btn_map: dict[str, str] = {}   # event_key -> sample button (for BUTTON_PRESS)
    has_new_format = False

    text = log_path.read_text(encoding="utf-8", errors="ignore")

    for line in text.splitlines():
        # ── 格式A：[模型原始] ──
        m = RE_RAW.search(line)
        if m:
            has_new_format = True
            intent, direction, buttons_raw = m.group(1), m.group(2), m.group(3)
            intent_counts[intent] += 1
            direction_counts[direction] += 1
            try:
                btns = json.loads(buttons_raw.replace("'", '"'))
            except Exception:
                btns = re.findall(r"[\w]+(?:\([\d.]+\))?", buttons_raw)
            for entry in btns:
                name = entry.split("(")[0].strip()
                try:
                    conf = float(entry.split("(")[1].rstrip(")")) if "(" in entry else 1.0
                except (IndexError, ValueError):
                    conf = 1.0
                if name:
                    btn_counts[name] += 1
                    btn_conf_sum[name] += conf
            continue

        # ── 格式A：[翻译] ──
        m = RE_TRANSLATE.search(line)
        if m:
            has_new_format = True
            evt_key, phrase = m.group(1), m.group(2)
            if phrase:
                phrase_counts[phrase] += 1
            continue

        # ── 格式B：ActionFilter 触发 ──
        m = RE_EVENT.search(line)
        if m:
            event_counts[m.group(1)] += 1
            continue

        # ── 格式B：ActionFilter 10s统计 ──
        m = RE_10S.search(line)
        if m and not has_new_format:
            intents_str = m.group(1)
            direction = m.group(2)
            for part in intents_str.split():
                if ":" in part:
                    k, v = part.split(":")
                    intent_counts[k] += int(v)
            direction_counts[direction] += 1

    return (btn_counts, btn_conf_sum, intent_counts,
            direction_counts, event_counts, phrase_counts, has_new_format)


def print_table(headers, rows, col_widths=None):
    if not rows:
        print("  （无数据）")
        return
    if not col_widths:
        col_widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
                      for i, h in enumerate(headers)]
    sep = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
    fmt = "| " + " | ".join(f"{{:<{w}}}" for w in col_widths) + " |"
    print(sep)
    print(fmt.format(*[str(h) for h in headers]))
    print(sep)
    for row in rows:
        print(fmt.format(*[str(c) for c in row]))
    print(sep)


def main():
    if len(sys.argv) > 1:
        log_path = Path(sys.argv[1])
    else:
        log_path = Path(__file__).resolve().parent.parent / "session.log"

    if not log_path.exists():
        print(f"[ERROR] 找不到日志文件: {log_path}")
        sys.exit(1)

    size_kb = log_path.stat().st_size // 1024
    print(f"\n分析日志: {log_path}  ({size_kb} KB)\n")

    (btn_counts, btn_conf_sum, intent_counts, direction_counts,
     event_counts, phrase_counts, has_new_format) = parse_log(log_path)

    if not has_new_format:
        print("⚠  本次日志为旧格式（无 [模型原始]/[翻译] 行）")
        print("   意图/方向数据来自 ActionFilter 10s统计聚合，无按键级别数据。")
        print("   重启服务后再跑一次即可获得完整数据。\n")

    total_frames = sum(intent_counts.values()) or 1

    # ── A. NitroGen 原始按键分布 ──────────────────────────────────────────
    print("=" * 62)
    print("A. NitroGen 原始按键分布（出现过的按键）")
    print("=" * 62)
    if btn_counts:
        rows = [(name, cnt, f"{btn_conf_sum[name]/cnt:.2f}")
                for name, cnt in sorted(btn_counts.items(), key=lambda x: -x[1])]
        print_table(["按键名", "出现帧次", "平均置信度"], rows)
    else:
        print("  （需新格式日志；请重启服务后重跑视频）")

    # ── B. 主导意图分布 ────────────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("B. 主导意图分布")
    print("=" * 62)
    rows = [(k, v, f"{v/total_frames*100:.1f}%")
            for k, v in sorted(intent_counts.items(), key=lambda x: -x[1])]
    print_table(["意图", "帧数/信号数", "占比"], rows)

    # ── C. 摇杆方向分布 ───────────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("C. 摇杆方向分布")
    print("=" * 62)
    total_dir = sum(direction_counts.values()) or 1
    rows = [(k, v, f"{v/total_dir*100:.1f}%")
            for k, v in sorted(direction_counts.items(), key=lambda x: -x[1])]
    print_table(["方向", "次数", "占比"], rows)

    # ── D. 快通道事件类型分布 ─────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("D. 触发的快通道事件分布")
    print("=" * 62)
    if event_counts:
        rows = [(k, v) for k, v in sorted(event_counts.items(), key=lambda x: -x[1])]
        print_table(["事件类型", "触发次数"], rows)
    else:
        print("  （无快事件触发记录）")

    # ── E. 实际播报短语 ────────────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("E. 实际播报短语（悟空游戏）")
    print("=" * 62)
    if phrase_counts:
        rows = [(ph, cnt) for ph, cnt in phrase_counts.most_common(20) if ph]
        print_table(["短语", "播报次数"], rows)
    else:
        print("  （需新格式日志；请重启服务后重跑视频）")

    # ── F. 词表对账：按键事件 ─────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("F. 悟空词表对账（按键事件）")
    print("=" * 62)
    rows = []
    for btn, text in WUKONG_BUTTON_TO_TEXT.items():
        hit   = btn_counts.get(btn, 0)
        played = phrase_counts.get(text, 0)
        if not has_new_format:
            status = "? 需新格式"
        elif played > 0:
            status = "✓ 命中"
        elif hit > 0:
            status = "△ NitroGen有/未播"
        else:
            status = "✗ 从未出现"
        rows.append((btn, text, hit, played, status))
    print_table(["按键", "目标短语", "NitroGen帧次", "实际播报次", "状态"],
                rows, col_widths=[16, 12, 12, 10, 18])

    # ── G. 词表对账：意图事件 ─────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("G. 悟空词表对账（意图事件）")
    print("=" * 62)
    rows = []
    for evt_key, desc in WUKONG_TEMPLATE_EVENTS.items():
        played = event_counts.get(evt_key, 0)
        status = "✓ 命中" if played > 0 else "✗ 未触发"
        rows.append((evt_key, desc, played, status))
    print_table(["事件类型", "目标短语", "触发次数", "状态"],
                rows, col_widths=[18, 28, 8, 8])

    # ── H. 断链点 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("H. 断链点（NitroGen 有按键但词表未覆盖）")
    print("=" * 62)
    unmapped = [(name, cnt) for name, cnt in sorted(btn_counts.items(), key=lambda x: -x[1])
                if name not in WUKONG_BUTTON_TO_TEXT]
    if unmapped:
        print_table(["未覆盖按键", "出现帧次"], unmapped)
        print("\n  → 建议将高频按键加入 WUKONG.button_to_text")
    elif btn_counts:
        print("  （所有出现的按键都已覆盖）")
    else:
        print("  （需新格式日志）")

    print()
    if not has_new_format:
        print("★ 结论：旧格式日志仅有聚合统计，无按键/翻译级别数据。")
        print("  请重启服务（python run.py）后再播放一次视频，即可得到完整分析。")
    print("分析完成。\n")


if __name__ == "__main__":
    main()
