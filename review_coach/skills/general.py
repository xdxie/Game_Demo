from review_coach.schemas import ReviewRequest
from review_coach.skills.base import BaseGameSkill


class GeneralReviewSkill(BaseGameSkill):
    name = "general"
    default_event_type = "GENERAL_REVIEW"

    def build_system_prompt(self) -> str:
        return """你是通用游戏复盘教练。你需要结合截图、玩家 query 和可选动作序列，给出保守、可靠、适合直接 TTS 播报的中文建议。

要求：
- 必须只输出 JSON
- 不要输出 Markdown
- 不要编造特定游戏机制
- 如果信息不足，给出低置信度的保守建议
- event_type 默认使用 GENERAL_REVIEW
- coaching_text 必须是中文，60 到 160 字
- 先指出主要问题，再给下一次具体做法"""

    def build_user_prompt(self, request: ReviewRequest, action_summary: str) -> str:
        return f"""请根据截图和以下信息生成复盘 JSON。上游没有提供 event_type，你必须自己判断 event_type。

game_type: {request.game_type}
game_name: {request.game_name}
query: {request.query}
action_summary: {action_summary}
clip_start: {request.clip_start}
clip_end: {request.clip_end}
trigger_reason: {request.trigger_reason}
image_count: {len(request.image_paths or [])}

输出 JSON 字段必须包含：
should_speak, game_type, event_type, problem, coaching_text, confidence"""
