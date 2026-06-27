"""
快速冒烟测试：game_vocab + render_fast + action_filter 按键边沿检测

运行：D:\anaconda\python.exe smoke_test_vocab.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from backend.fast.event import EventType, GameEvent
from backend.fast.game_vocab import get_vocab
from backend.fast.templates import render_fast
from backend.fast.action_filter import ActionFilter
from backend.nitrogen.parser import PerceptionSignal

OK = "\033[92mPASS\033[0m"
NG = "\033[91mFAIL\033[0m"
results = []

def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  → {detail}" if detail else ""))

def sig(pressed=None, direction=None, intent="NAVIGATE", conf=0.4, mag=0.3):
    return PerceptionSignal(
        primary_intent=intent, confidence=conf,
        move_direction=direction, move_magnitude=mag,
        pressed_buttons=pressed or [],
    )

# ── 1. 词表查找 ───────────────────────────────────────────────────────────
print("\n[1] 词表查找")
check("Wukong vocab id", get_vocab("black_myth_wukong").game_id == "black_myth_wukong")
check("SF6 vocab id",    get_vocab("street_fighter_6").game_id == "street_fighter_6")
check("Forza vocab id",  get_vocab("forza_horizon_5").game_id == "forza_horizon_5")
check("Mario vocab id",  get_vocab("new_super_mario_bros").game_id == "new_super_mario_bros")
check("Unknown → GENERAL", get_vocab("unknown_game").game_id == "_general")
check("None → GENERAL",    get_vocab(None).game_id == "_general")

# ── 2. 按键事件 render_fast ────────────────────────────────────────────────
print("\n[2] render_fast 按键事件")
def btn_event(name):
    return GameEvent(
        type=EventType.BUTTON_PRESS, timestamp=0.0,
        perception=sig(), trigger_fast=True, trigger_slow=False,
        button_name=name,
    )

# Wukong
text = render_fast(btn_event("SOUTH"), "black_myth_wukong")
check("Wukong SOUTH → 起跳！", text == "起跳！", repr(text))
text = render_fast(btn_event("NORTH"), "black_myth_wukong")
check("Wukong NORTH → 重击！", text == "重击！", repr(text))
text = render_fast(btn_event("WEST"), "black_myth_wukong")
check("Wukong WEST → 轻攻！", text == "轻攻！", repr(text))

# Forza
text = render_fast(btn_event("LEFT_TRIGGER"), "forza_horizon_5")
check("Forza LT → 刹车！", text == "刹车！", repr(text))
text = render_fast(btn_event("RIGHT_TRIGGER"), "forza_horizon_5")
check("Forza RT → 踩油门！", text == "踩油门！", repr(text))

# Street Fighter
text = render_fast(btn_event("EAST"), "street_fighter_6")
check("SF6 EAST → 一拳！", text == "一拳！", repr(text))
text = render_fast(btn_event("LEFT_TRIGGER"), "street_fighter_6")
check("SF6 LT → 挡住！", text == "挡住！", repr(text))

# Mario
text = render_fast(btn_event("SOUTH"), "new_super_mario_bros")
check("Mario SOUTH → 起跳！", text == "起跳！", repr(text))
text = render_fast(btn_event("WEST"), "new_super_mario_bros")
check("Mario WEST → 出招！", text == "出招！", repr(text))
text = render_fast(btn_event("NORTH"), "new_super_mario_bros")
check("Mario NORTH → 旋转跳跃！", text == "旋转跳跃！", repr(text))
text = render_fast(btn_event("LEFT_TRIGGER"), "new_super_mario_bros")
check("Mario LT → 左倾斜！", text == "左倾斜！", repr(text))
text = render_fast(btn_event("RIGHT_TRIGGER"), "new_super_mario_bros")
check("Mario RT → 右倾斜！", text == "右倾斜！", repr(text))

# GENERAL → 空串
text = render_fast(btn_event("SOUTH"), "_general")
check("GENERAL SOUTH → 空串", text == "", repr(text))
text = render_fast(btn_event("SOUTH"), None)
check("None game_id SOUTH → 空串", text == "", repr(text))

# ── 3. 方向事件 render_fast ────────────────────────────────────────────────
print("\n[3] render_fast 方向事件")

def dir_event(etype, direction=None):
    return GameEvent(
        type=etype, timestamp=0.0,
        perception=sig(direction=direction), trigger_fast=True, trigger_slow=False,
    )

# Wukong SUDDEN_DODGE 有方向
text = render_fast(dir_event(EventType.SUDDEN_DODGE, "LEFT"), "black_myth_wukong")
check("Wukong dodge+dir → 含'左'", "左" in text, repr(text))

# Wukong SUDDEN_DODGE 无方向
text = render_fast(dir_event(EventType.SUDDEN_DODGE), "black_myth_wukong")
check("Wukong dodge no dir → 翻滚闪避！", "翻滚" in text, repr(text))

# Forza MOVEMENT_SHIFT 有方向
text = render_fast(dir_event(EventType.MOVEMENT_SHIFT, "RIGHT"), "forza_horizon_5")
check("Forza shift+dir → 含'右'", "右" in text, repr(text))

# SF6 SUDDEN_DODGE 有方向
text = render_fast(dir_event(EventType.SUDDEN_DODGE, "LEFT"), "street_fighter_6")
check("SF6 dodge+dir → 往左挡！", text == "往左挡！", repr(text))

# SF6 ATTACK_WINDOW
text = render_fast(dir_event(EventType.ATTACK_WINDOW), "street_fighter_6")
check("SF6 attack window → 搓连段！", text == "搓连段！", repr(text))

# ── 4. ActionFilter 按键边沿检测 ────────────────────────────────────────────
print("\n[4] ActionFilter 按键边沿检测")
af = ActionFilter()

# 无按键 → 不产 BUTTON_PRESS
ev = af.process(sig([]), 1.0)
check("无按键帧 → None", ev is None)

# SOUTH(0.9) 首次出现 → BUTTON_PRESS
ev = af.process(sig(["SOUTH(0.90)"]), 2.0)
check("首次按下 → BUTTON_PRESS", ev is not None and ev.type == EventType.BUTTON_PRESS,
      ev.type.value if ev else "None")
check("button_name == SOUTH", ev is not None and ev.button_name == "SOUTH",
      ev.button_name if ev else "")

# 冷却内持续按住 → 不重复触发
ev2 = af.process(sig(["SOUTH(0.90)"]), 2.5)
check("持续按住冷却内 → None", ev2 is None, str(ev2))

# 低置信度按键被过滤
af2 = ActionFilter()
af2.process(sig([]), 0.0)
ev_low = af2.process(sig(["SOUTH(0.30)"]), 1.0)
check("置信度 0.3 按键被过滤 → None", ev_low is None, str(ev_low))

# ── 5. 悟空组合键 ────────────────────────────────────────────────────────────
print("\n[5] 悟空组合键（RT/LT 法术道具）")

def combo_event(pressed_list):
    """构造带多键 pressed_buttons 的 BUTTON_PRESS 事件"""
    return GameEvent(
        type=EventType.BUTTON_PRESS, timestamp=0.0,
        perception=sig(pressed=pressed_list),
        trigger_fast=True, trigger_slow=False,
        button_name=pressed_list[0].split("(")[0].strip() if pressed_list else "",
    )

# RT + X → 给我定！
text = render_fast(combo_event(["RIGHT_TRIGGER(0.95)", "WEST(0.72)"]), "black_myth_wukong")
check("RT+X → 给我定！", text == "给我定！", repr(text))

# RT + Y → 聚形散气！
text = render_fast(combo_event(["RIGHT_TRIGGER(0.90)", "NORTH(0.85)"]), "black_myth_wukong")
check("RT+Y → 聚形散气！", text == "聚形散气！", repr(text))

# RT + B → 广智救我！
text = render_fast(combo_event(["RIGHT_TRIGGER(0.88)", "EAST(0.78)"]), "black_myth_wukong")
check("RT+B → 广智救我！", text == "广智救我！", repr(text))

# RT + A → 上吧孩儿们！
text = render_fast(combo_event(["RIGHT_TRIGGER(0.91)", "SOUTH(0.80)"]), "black_myth_wukong")
check("RT+A → 上吧孩儿们！", text == "上吧孩儿们！", repr(text))

# LT + ↑ → 驱邪散！
text = render_fast(combo_event(["LEFT_TRIGGER(1.00)", "DPAD_UP(0.90)"]), "black_myth_wukong")
check("LT+↑ → 驱邪散！", text == "驱邪散！", repr(text))

# 仅按 RT（无组合）→ 施法！（fallback 到单键）
text = render_fast(combo_event(["RIGHT_TRIGGER(0.92)"]), "black_myth_wukong")
check("仅 RT → 施法！（单键 fallback）", text == "施法！", repr(text))

# 街霸 RT+EAST 无组合配置 → fallback 单键（中拳！）
text = render_fast(combo_event(["RIGHT_TRIGGER(0.90)", "EAST(0.75)"]), "street_fighter_6")
check("SF6 RT+EAST 无组合 → 单键 fallback 非空", text != "", repr(text))

# ── Summary ────────────────────────────────────────────────────────────────
print("\n" + "=" * 55)
passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"Result: {passed}/{total} checks passed")
if passed < total:
    for name, ok in results:
        if not ok:
            print(f"  FAIL: {name}")
    sys.exit(1)
else:
    print("All checks passed.")
