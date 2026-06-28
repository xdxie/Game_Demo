"""
游戏专属快路径词表。

每个游戏有一个 GameVocab 实例，包含：
  templates: 4 个意图事件类型的（有方向, 无方向）渲染函数对
  button_to_text: 按键名 → TTS 文本（BUTTON_PRESS 事件使用）
  button_variants: 按键名 → 多条文案（BUTTON_PRESS 轮播，优先于 button_to_text）
  variant_texts: 事件类型 → 多条文案（render_fast 轮播输出）
  fallback: 无模板时的兜底文本

用 get_vocab(game_id) 查找；未注册 game_id 返回 GENERAL。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable

from backend.fast.event import EventType

WUKONG_GAME_ID = "black_myth_wukong"

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
    button_variants: dict[str, list[str]] = field(default_factory=dict)
    combo_to_text: dict[frozenset[str], str] = field(default_factory=dict)
    variant_texts: dict[EventType, list[str]] = field(default_factory=dict)
    fallback: str = "注意！"
    suppress_directional_fast: bool = False

    def lookup_combo(self, buttons: set[str]) -> str:
        """按键集合命中 combo_to_text 的 frozenset 子集时返回 TTS 文本。"""
        for keys, text in self.combo_to_text.items():
            if keys <= buttons:
                return text
        return ""


class WukongSpeakPolicy:
    """黑猴快通道播报分级：P0 法术 > P1 回血 > P2 单键；MUTE 永不播。"""

    GAME_ID = WUKONG_GAME_ID

    TIER2_BUTTONS = frozenset({"LEFT_SHOULDER"})
    TIER3_BUTTONS = frozenset({
        "EAST", "NORTH", "SOUTH",
        "LEFT_THUMB", "RIGHT_THUMB",
        "DPAD_UP", "DPAD_LEFT", "DPAD_RIGHT",
    })
    DPAD_KEYS = frozenset({"DPAD_UP", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_DOWN"})
    MUTE_BUTTONS = frozenset({
        "WEST", "RIGHT_TRIGGER", "LEFT_TRIGGER", "RIGHT_SHOULDER",
        "START", "BACK", "DPAD_DOWN",
    })

    @classmethod
    def is_allowed_button(cls, name: str) -> bool:
        if name in cls.MUTE_BUTTONS:
            return False
        return name in cls.TIER2_BUTTONS or name in cls.TIER3_BUTTONS

    @classmethod
    def button_tier(cls, name: str) -> int:
        if name in cls.TIER2_BUTTONS:
            return 1
        if name in cls.TIER3_BUTTONS:
            return 2
        return 99


# ── GENERAL（通用，兜底所有未注册游戏）─────────────────────────────────
GENERAL = GameVocab(
    game_id="_general",
    suppress_directional_fast=True,
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
    suppress_directional_fast=True,
    templates={
        EventType.SUDDEN_DODGE: (
            lambda s: "顶一下！",
            lambda s: "踩一下！",
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
    button_variants={
        "SOUTH": ["顶一下！", "踩一下！", "踩它！", "顶一下！"],
        "EAST":  ["顶一下！", "踩一下！", "踩它！", "顶一下！"],
    },
    button_to_text={
        # ── 核心三键（SOUTH/EAST 由 button_variants 轮播）────────────
        "SOUTH":         "",
        "EAST":          "",
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
    suppress_directional_fast=True,
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
        "EAST":           "闪！",  # B = 闪避/翻滚
        "WEST":           "",           # X = 轻攻击，不播报（减少连播）
        "NORTH":          "重击！",      # Y = 重攻击/蓄力攻击
        # ── 肩键/扳机 ─────────────────────────────────────────────
        "LEFT_SHOULDER":  "回口血！",   # LB = 葫芦回血
        "LEFT_TRIGGER":   "棍花！",     # LT = 棍花/旋棍（道具快捷键修饰键）
        # "RIGHT_SHOULDER": "冲！",     # RB = 奔跑
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
        # RT + 面部键（无序并集，RT→face / face→RT 等价）
        frozenset({"RIGHT_TRIGGER", "WEST"}):   "给我定！",
        frozenset({"RIGHT_TRIGGER", "NORTH"}):  "聚形散气！",
        frozenset({"RIGHT_TRIGGER", "EAST"}):   "广智救我！",
        frozenset({"RIGHT_TRIGGER", "SOUTH"}):  "上吧孩儿们！",
        # RT + LT 精魄/化身
        frozenset({"RIGHT_TRIGGER", "LEFT_TRIGGER"}): "化身！",
        # LT + 十字键
        frozenset({"LEFT_TRIGGER", "DPAD_UP"}):    "驱邪散！",
        frozenset({"LEFT_TRIGGER", "DPAD_LEFT"}):  "避雷散！",
        frozenset({"LEFT_TRIGGER", "DPAD_RIGHT"}): "虎伏丹！",
        frozenset({"LEFT_TRIGGER", "DPAD_DOWN"}):  "人参丸！",
    },
    variant_texts={
        EventType.SUSTAINED_DANGER: [
            "拉开距离",
            "稳住别贪刀",
            "小心快慢刀",
        ],
    },
)


# ── FORZA（极限竞速：地平线 5）──────────────────────────────────────────
FORZA = GameVocab(
    game_id="forza_horizon_5",
    suppress_directional_fast=True,
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
    suppress_directional_fast=True,
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
            lambda s: "稳住",
        ),
        EventType.MOVEMENT_SHIFT: (
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}挪",
            lambda s: "保持移动！",
        ),
    },
    button_to_text={
        "SOUTH":          "踢一下！",    # A 轻脚
        "EAST":           "踹他！",      # B 中脚
        "WEST":           "戳一下！",    # X 轻拳
        "NORTH":          "给一拳！",      # Y 中拳
        "LEFT_SHOULDER":  "斗气格挡！",    # LB 斗气格挡
        "RIGHT_SHOULDER": "重拳砸！",    # RB 重拳
        "LEFT_TRIGGER":   "斗气冲击！",      # LT 防御/投技
        "RIGHT_TRIGGER":  "重脚扫！",    # RT 重脚
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
