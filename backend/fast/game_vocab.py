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


# ── MARIO（新超级马里奥兄弟 / 超级马力欧：惊奇）────────────────────────────
# Xbox 按键对照（Switch→Xbox 映射，Nintendo B=Xbox A，Nintendo A=Xbox B）：
#   SOUTH=A 跳跃（Nintendo B）
#   EAST=B 跳跃备用（Nintendo A，部分作品功能不同）
#   WEST=X 奔跑/加速/使用道具（Nintendo Y）
#   NORTH=Y 表情/旋转跳（Nintendo X）
#   LEFT_TRIGGER=LT 蹲下/下砸  RIGHT_TRIGGER=RT 旋转跳/奔跑备用
#   LEFT_SHOULDER=LB 多人泡泡，单人不播报
MARIO = GameVocab(
    game_id="new_super_mario_bros",
    suppress_directional_fast=True,
    templates={
        EventType.SUDDEN_DODGE: (
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}跳！",
            lambda s: "快跳开！",
        ),
        EventType.ATTACK_WINDOW: (
            lambda s: "踩它！",
            lambda s: "跳上去踩！",
        ),
        EventType.SUSTAINED_DANGER: (
            lambda s: f"前有危险，往{DIRECTION_ZH[s.move_direction]}跳",
            lambda s: "小心，跳开！",
        ),
        EventType.MOVEMENT_SHIFT: (
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}跑",
            lambda s: "换边跑！",
        ),
    },
    button_variants={
        "SOUTH": ["跳！", "踩它！", "跳上去！"],
        "EAST":  ["跳！", "踩一下！"],
    },
    button_to_text={
        "SOUTH":          "",            # A = 跳跃（由 button_variants 轮播）
        "EAST":           "",            # B = 跳跃备用（由 button_variants 轮播）
        "WEST":           "冲刺！",      # X = 奔跑/加速/使用道具
        "NORTH":          "旋转跳！",    # Y = 旋转跳/表情
        "RIGHT_TRIGGER":  "旋转跳！",    # RT = 旋转跳
        "LEFT_TRIGGER":   "",            # LT = 蹲下/下砸，不主动提示
        "LEFT_SHOULDER":  "",            # LB = 进泡泡（多人），单人不播报
        "RIGHT_SHOULDER": "",
        "LEFT_THUMB":     "",
        "RIGHT_THUMB":    "",
        "START":          "",
        "BACK":           "",
    },
    variant_texts={
        EventType.ATTACK_WINDOW: [
            "踩它！",
            "跳上去！",
            "顶一下！",
        ],
        EventType.SUSTAINED_DANGER: [
            "小心，跳开！",
            "前有危险！",
            "避开！",
        ],
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
# Xbox 按键对照：
#   RT=RIGHT_TRIGGER 油门（几乎全程按住，不播报）
#   LT=LEFT_TRIGGER 刹车
#   SOUTH=A 手刹（漂移用）
#   EAST=B 升挡（手动档）
#   WEST=X 降挡（手动档）
#   NORTH=Y 倒带（纠错，不播报）
#   LEFT_SHOULDER=LB 离合器（手动档）/ 换挡降
#   RIGHT_SHOULDER=RB 切换摄像头（不播报）
#   RIGHT_THUMB=RS按下 喇叭
#   DPAD_UP 拍照模式（不播报）
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
            lambda s: "注意弯道，减速！",
        ),
        EventType.MOVEMENT_SHIFT: (
            lambda s: f"向{DIRECTION_ZH[s.move_direction]}转向",
            lambda s: "切线路！",
        ),
    },
    button_to_text={
        "LEFT_TRIGGER":   "刹车！",      # LT = 刹车
        "SOUTH":          "手刹！",      # A = 手刹/漂移
        "EAST":           "升挡！",      # B = 升挡（手动档）
        "WEST":           "降挡！",      # X = 降挡（手动档）
        "LEFT_SHOULDER":  "换挡降！",    # LB = 离合/换挡降
        "RIGHT_THUMB":    "按喇叭！",    # RS按下 = 喇叭
        # ── 以下不播报 ────────────────────────────────────────────────
        "RIGHT_TRIGGER":  "",            # RT = 油门，全程按住，不播报
        "NORTH":          "",            # Y = 倒带，纠错操作不播报
        "RIGHT_SHOULDER": "",            # RB = 切换摄像头，不播报
        "LEFT_THUMB":     "",
        "DPAD_UP":        "",
        "DPAD_DOWN":      "",
        "DPAD_LEFT":      "",
        "DPAD_RIGHT":     "",
        "START":          "",
        "BACK":           "",
    },
    button_variants={
        "SOUTH": ["手刹！", "漂移！"],
        "LEFT_TRIGGER": ["刹车！", "减速！"],
    },
    variant_texts={
        EventType.SUSTAINED_DANGER: [
            "注意弯道！",
            "减速过弯！",
            "收油了！",
        ],
        EventType.ATTACK_WINDOW: [
            "踩油门！",
            "加速超车！",
            "全力冲！",
        ],
    },
)


# ── STREET_FIGHTER（街头霸王 6，经典模式六键布局）──────────────────────────
# Xbox 按键对照（经典模式默认）：
#   WEST=X 轻拳(LP)  NORTH=Y 中拳(MP)  RIGHT_SHOULDER=RB 重拳(HP)
#   SOUTH=A 轻脚(LK)  EAST=B 中脚(MK)  RIGHT_TRIGGER=RT 重脚(HK)
#   LB/LT 经典模式默认未分配，现代模式 LB=斗气冲击 LT=投技 RB=斗气反击
#   斗气冲击(RB+RT同时) / 斗气反击(Y+B同时) — NitroGen 组合键识别有限，不做 combo
#   格挡靠后拨摇杆实现，无专用按键
STREET_FIGHTER = GameVocab(
    game_id="street_fighter_6",
    suppress_directional_fast=True,
    templates={
        EventType.SUDDEN_DODGE: (
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}挡！",
            lambda s: "挡住！",
        ),
        EventType.ATTACK_WINDOW: (
            lambda s: "压上去！",
            lambda s: "搓连段！",
        ),
        EventType.SUSTAINED_DANGER: (
            lambda s: f"拉开距离，往{DIRECTION_ZH[s.move_direction]}挪",
            lambda s: "稳住！",
        ),
        EventType.MOVEMENT_SHIFT: (
            lambda s: f"往{DIRECTION_ZH[s.move_direction]}挪",
            lambda s: "保持移动！",
        ),
    },
    button_to_text={
        # ── 六键经典布局 ──────────────────────────────────────────────
        "WEST":           "轻拳！",      # X = 轻拳 LP
        "NORTH":          "中拳！",      # Y = 中拳 MP
        "RIGHT_SHOULDER": "重拳！",      # RB = 重拳 HP
        "SOUTH":          "轻脚！",      # A = 轻脚 LK
        "EAST":           "中脚！",      # B = 中脚 MK
        "RIGHT_TRIGGER":  "重脚！",      # RT = 重脚 HK
        # ── 经典模式 LB/LT 默认空，现代模式映射以下功能 ──────────────
        "LEFT_SHOULDER":  "斗气冲击！",  # LB = 斗气冲击（现代模式）
        "LEFT_TRIGGER":   "投技！",      # LT = 投技（现代模式）
        # ── 系统键不播报 ──────────────────────────────────────────────
        "LEFT_THUMB":     "",
        "RIGHT_THUMB":    "",
        "START":          "",
        "BACK":           "",
    },
    button_variants={
        "WEST":  ["轻拳！", "出拳！"],
        "SOUTH": ["轻脚！", "踢他！"],
        "EAST":  ["中脚！", "踹他！"],
    },
    variant_texts={
        EventType.ATTACK_WINDOW: [
            "压上去！",
            "搓连段！",
            "反击！",
        ],
        EventType.SUSTAINED_DANGER: [
            "稳住！",
            "拉开距离！",
            "别贪招！",
        ],
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
