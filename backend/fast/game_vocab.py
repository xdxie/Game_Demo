"""
游戏专属快路径词表。

每个游戏有一个 GameVocab 实例，包含：
  templates: 4 个意图事件类型的（有方向, 无方向）渲染函数对
  button_to_text: 按键名 → TTS 文本（BUTTON_PRESS 事件使用）
  fallback: 无模板时的兜底文本

用 get_vocab(game_id) 查找；未注册 game_id 返回 GENERAL。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable

from backend.fast.event import EventType

_Fn = Callable  # (PerceptionSignal) -> str
_Pair = tuple[_Fn, _Fn]  # (有方向函数, 无方向函数)

DIRECTION_ZH: dict[str | None, str] = {
    "LEFT":    "左",
    "RIGHT":   "右",
    "FORWARD": "前",
    "BACK":    "后",
    None:      "",
}


@dataclass
class GameVocab:
    game_id: str
    templates: dict[EventType, _Pair]
    button_to_text: dict[str, str] = field(default_factory=dict)
    # 组合键 → 文本，key = 两键名按字母序用 '+' 拼接（如 "RIGHT_TRIGGER+WEST"）
    combo_to_text: dict[str, str] = field(default_factory=dict)
    fallback: str = "注意！"


# ── GENERAL（通用，兜底所有未注册游戏）─────────────────────────────────
GENERAL = GameVocab(
    game_id="_general",
    templates={
        EventType.SUDDEN_DODGE: (
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}闪！",
            lambda s: "快闪！",
        ),
        EventType.ATTACK_WINDOW: (
            lambda s: "有机会，打！",
            lambda s: "出手！",
        ),
        EventType.SUSTAINED_DANGER: (
            lambda s: f"危险，往{DIRECTION_ZH[s.move_direction]}跑！",
            lambda s: "危险！快拉开距离！",
        ),
        EventType.MOVEMENT_SHIFT: (
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}走",
            lambda s: "换个方向走",
        ),
    },
    button_to_text={},  # 通用不配置按键文本，避免乱说
)


# ── MARIO（新超级马里奥兄弟）────────────────────────────────────────────
# Xbox 按键对照（NitroGen 方位名 → Xbox键 → 功能，Wii 版为主）：
#   SOUTH=A 跳跃  EAST=B 跳跃(备用, DS)  WEST=X 奔跑/火球/抱物  NORTH=Y 旋转跳/晃手柄
#   LT=LEFT_TRIGGER 左倾斜  RT=RIGHT_TRIGGER 右倾斜
#   LB=LEFT_SHOULDER 进泡泡(多人)  LS/RS 无  START/BACK 系统键不播报
MARIO = GameVocab(
    game_id="new_super_mario_bros",
    templates={
        EventType.SUDDEN_DODGE: (
            lambda s: "起跳！",
            lambda s: "跳！",
        ),
        EventType.ATTACK_WINDOW: (
            lambda s: "踩它！",
            lambda s: "顶一下！",
        ),
        EventType.SUSTAINED_DANGER: (
            lambda s: f"前面有怪，往{DIRECTION_ZH[s.move_direction]}跳",
            lambda s: "小心怪，跳开",
        ),
        EventType.MOVEMENT_SHIFT: (
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}跑",
            lambda s: "换边跑",
        ),
    },
    button_to_text={
        # ── 核心三键 ──────────────────────────────────────────────────
        "SOUTH":         "起跳！",      # A = 跳跃
        "EAST":          "起跳！",      # B = 跳跃（DS 备用）
        "WEST":          "出招！",      # X = 奔跑/加速/火球/抱物
        "NORTH":         "旋转跳跃！",    # Y = 晃手柄 Spin/旋转跳
        # ── 倾斜（Wii 可选）──────────────────────────────────────────
        "LEFT_TRIGGER":  "左倾斜！",    # LT
        "RIGHT_TRIGGER": "右倾斜！",    # RT
        # ── 多人可选 ─────────────────────────────────────────────────
        "LEFT_SHOULDER": "",            # LB = 进泡泡，单人模式不播报
        # ── 无功能 / 系统键 ───────────────────────────────────────────
        "LEFT_THUMB":    "",            # LS 无
        "RIGHT_THUMB":   "",            # RS 无
        "START":         "",            # Menu
        "BACK":          "",            # View
    },
)


# ── WUKONG（黑神话：悟空）──────────────────────────────────────────────
# Xbox 按键对照（NitroGen 方位名 → Xbox键 → 功能）：
#   SOUTH=A 跳跃  EAST=B 闪避/翻滚  WEST=X 轻攻  NORTH=Y 重攻
#   LB=LEFT_SHOULDER 葫芦  LT=LEFT_TRIGGER 棍花/旋棍
#   RB=RIGHT_SHOULDER 奔跑  RT=RIGHT_TRIGGER 法术(按住)
#   LS=LEFT_THUMB 疾跑  RS=RIGHT_THUMB 锁定
#   DPAD_UP=立棍  DPAD_LEFT=劈棍  DPAD_RIGHT=戳棍
# 组合技（待组合键检测实装后启用）：
#   RT+WEST=定身术  RT+NORTH=聚形散气  RT+EAST=赤潮  RT+SOUTH=身外身法
#   LT+DPAD_UP=驱邪散  LT+DPAD_LEFT=避雷散  LT+DPAD_RIGHT=虎伏丹  LT+DPAD_DOWN=人参丸
WUKONG = GameVocab(
    game_id="black_myth_wukong",
    templates={
        EventType.SUDDEN_DODGE: (
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}翻滚！",
            lambda s: "翻滚闪避！",
        ),
        EventType.ATTACK_WINDOW: (
            lambda s: "反击！出手！",
            lambda s: "有机会，打！",
        ),
        EventType.SUSTAINED_DANGER: (
            lambda s: "稳住，别贪刀",
            lambda s: "拉开距离",
        ),
        EventType.MOVEMENT_SHIFT: (
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}走位",
            lambda s: "换位置",
        ),
    },
    button_to_text={
        # ── 面部按键 ──────────────────────────────────────────────
        "SOUTH":          "起跳！",      # A = 跳跃
        "EAST":           "翻滚闪避！",  # B = 闪避/翻滚
        "WEST":           "轻攻！",      # X = 轻攻击
        "NORTH":          "重击！",      # Y = 重攻击/蓄力攻击
        # ── 肩键/扳机 ─────────────────────────────────────────────
        "LEFT_SHOULDER":  "喝葫芦！",   # LB = 葫芦回血
        "LEFT_TRIGGER":   "旋棍！",     # LT = 棍花/旋棍（道具快捷键修饰键）
        "RIGHT_SHOULDER": "冲跑！",     # RB = 奔跑
        "RIGHT_TRIGGER":  "施法！",     # RT = 法术快捷键（单按进法术界面）
        # ── 摇杆按下 ──────────────────────────────────────────────
        "LEFT_THUMB":     "疾跑！",     # LS = 冲刺/疾跑
        "RIGHT_THUMB":    "锁定！",     # RS = 锁定/取消锁定目标
        # ── 十字键（棍势变招）─────────────────────────────────────
        "DPAD_UP":        "立棍！",     # ↑ = 立棍
        "DPAD_LEFT":      "劈棍！",     # ← = 劈棍
        "DPAD_RIGHT":     "戳棍！",     # → = 戳棍
        "DPAD_DOWN":      "",           # ↓ = 无明确技能，不播报
        # ── 系统键（不播报）──────────────────────────────────────
        "START":          "",           # Menu = 菜单，不播报
        "BACK":           "",           # View = 地图，不播报
    },
    combo_to_text={
        # RT（法术快捷键）按住 + 面部按键 → 四大法术
        "RIGHT_TRIGGER+WEST":   "给我定！",    # RT + X
        "NORTH+RIGHT_TRIGGER":  "聚形散气！",  # RT + Y
        "EAST+RIGHT_TRIGGER":   "广智救我！",      # RT + B
        "RIGHT_TRIGGER+SOUTH":  "上吧孩儿们！",  # RT + A
        # LT（道具快捷键）按住 + 十字键 → 四种道具
        "DPAD_UP+LEFT_TRIGGER":    "驱邪散！",  # LT + ↑
        "DPAD_LEFT+LEFT_TRIGGER":  "避雷散！",  # LT + ←
        "DPAD_RIGHT+LEFT_TRIGGER": "虎伏丹！",  # LT + →
        "DPAD_DOWN+LEFT_TRIGGER":  "人参丸！",  # LT + ↓
    },
)


# ── FORZA（极限竞速：地平线 5）──────────────────────────────────────────
FORZA = GameVocab(
    game_id="forza_horizon_5",
    templates={
        EventType.SUDDEN_DODGE: (
            lambda s: f"向{DIRECTION_ZH[s.move_direction]}打方向盘！",
            lambda s: "急打方向盘！",
        ),
        EventType.ATTACK_WINDOW: (
            lambda s: "踩油门超车！",
            lambda s: "全力加速！",
        ),
        EventType.SUSTAINED_DANGER: (
            lambda s: f"弯道，靠{DIRECTION_ZH[s.move_direction]}走",
            lambda s: "减速过弯",
        ),
        EventType.MOVEMENT_SHIFT: (
            lambda s: f"向{DIRECTION_ZH[s.move_direction]}转向",
            lambda s: "切线路",
        ),
    },
    button_to_text={
        "RIGHT_TRIGGER":  "踩油门！",
        "LEFT_TRIGGER":   "刹车！",
        "RIGHT_SHOULDER": "换挡升！",
        "LEFT_SHOULDER":  "换挡降！",
        "SOUTH":          "手刹！",    # A = 手刹
    },
)


# ── STREET_FIGHTER（街头霸王 6）──────────────────────────────────────────
STREET_FIGHTER = GameVocab(
    game_id="street_fighter_6",
    templates={
        EventType.SUDDEN_DODGE: (
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}挡！",
            lambda s: "挡住！",
        ),
        EventType.ATTACK_WINDOW: (
            lambda s: "压上去打！",
            lambda s: "搓连段！",
        ),
        EventType.SUSTAINED_DANGER: (
            lambda s: f"拉开点，往{DIRECTION_ZH[s.move_direction]}挪",
            lambda s: "稳住别贪刀",
        ),
        EventType.MOVEMENT_SHIFT: (
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}挪",
            lambda s: "换个角度！",
        ),
    },
    button_to_text={
        "SOUTH":          "戳一下！",    # A 轻拳
        "EAST":           "一拳！",      # B 中拳
        "WEST":           "踢一下！",    # X 轻脚
        "NORTH":          "踹他！",      # Y 中脚
        "LEFT_SHOULDER":  "重拳砸！",    # LB 重拳
        "RIGHT_SHOULDER": "重脚扫！",    # RB 重脚
        "LEFT_TRIGGER":   "挡住！",      # LT 防御/投技
        "RIGHT_TRIGGER":  "冲上去！",    # RT 驱动冲刺
    },
)


# ── 注册表 ────────────────────────────────────────────────────────────────
_REGISTRY: dict[str, GameVocab] = {
    GENERAL.game_id:        GENERAL,
    MARIO.game_id:          MARIO,
    WUKONG.game_id:         WUKONG,
    FORZA.game_id:          FORZA,
    STREET_FIGHTER.game_id: STREET_FIGHTER,
}


def get_vocab(game_id: str | None) -> GameVocab:
    """按 game_id 查词表；未注册时返回 GENERAL。"""
    if not game_id:
        return GENERAL
    return _REGISTRY.get(game_id, GENERAL)
