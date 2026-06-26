"""VLM 用户消息构造（Claude / OpenAI 兼容网关共用）。"""

from __future__ import annotations

from backend.fast.event import EventType, GameEvent
from backend.nitrogen.fast_api_parser import format_perception_for_vlm

SYSTEM_PROMPT = """你是一个游戏陪玩，正在实时陪伴玩家观看游戏视频录像。

你的职责：
- 主要依据当前画面与玩家提问，给出简短有价值的建议或回答
- 1~2 句话，不超过 40 字，口语化
- 不要重复快通道刚说过的内容
- 不要假设一定是赛车/开车类游戏，以画面内容为准

约束：不用列表/Markdown；不超过 40 字。"""

SYSTEM_PROMPT_WITH_NITROGEN = """你是一个游戏陪玩，正在实时陪伴玩家观看游戏视频录像。
旁边有一个快系统从视频帧预测手柄层面的高层动作提示（非驾驶/方向盘语义）。

你的职责：
- 结合当前画面、关键动作时间线与快系统提示，给出简短有价值的建议或回答
- 扳机/摇杆信息表示「攻击/防御/移动」等通用手柄语义，不要当成赛车油门刹车
- 1~2 句话，不超过 40 字，口语化
- 不要重复快通道刚说过的内容

约束：不用列表/Markdown；不超过 40 字。"""


def system_prompt(include_nitrogen: bool = False) -> str:
    if include_nitrogen:
        return SYSTEM_PROMPT_WITH_NITROGEN
    return SYSTEM_PROMPT


def build_task_section(
    event: GameEvent,
    user_question: str,
    *,
    include_nitrogen: bool = False,
) -> tuple[str, str]:
    if user_question:
        hint = "结合画面" + ("与动作时间线" if include_nitrogen else "")
        return (
            f"玩家提问：{user_question}",
            f"直接回答玩家问题，{hint}。",
        )
    if event.type == EventType.PATTERN_COMPLETED:
        return (
            "触发原因：玩家刚结束一段操作",
            "总结刚才操作，给一句点评。",
        )
    if event.type == EventType.ATTACK_WINDOW:
        return (
            "触发原因：检测到进攻窗口",
            "说明为何此时可进攻。",
        )
    return (
        f"触发原因：{event.type.value}",
        "给出当前局面下最有价值的一句建议。",
    )


def build_user_text(
    event: GameEvent,
    ctx_summary: str,
    last_fast_text: str,
    actions_timeline_text: str,
    user_question: str = "",
    *,
    include_nitrogen: bool = False,
) -> str:
    task_desc, guidance = build_task_section(
        event, user_question, include_nitrogen=include_nitrogen,
    )

    parts: list[str] = []

    if include_nitrogen:
        if ctx_summary and ctx_summary != "无近期动作记录":
            parts.append(ctx_summary)
        if actions_timeline_text:
            parts.append(actions_timeline_text)
        signal: PerceptionSignal = event.perception
        parts.append(format_perception_for_vlm(signal))

    parts.append(f"快通道刚才已播报：\"{last_fast_text}\"")
    parts.append(f"{task_desc}\n{guidance}")

    return "\n\n".join(parts)
