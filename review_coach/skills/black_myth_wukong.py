from review_coach.schemas import ReviewRequest
from review_coach.skills.base import BaseGameSkill


class BlackMythWukongReviewSkill(BaseGameSkill):
    name = "black_myth_wukong"
    default_event_type = "GENERAL_ACTION_REVIEW"

    def build_system_prompt(self) -> str:
        return """你是《黑神话：悟空》慢系统复盘教练，只根据截图、玩家问题和可选上下文做战斗复盘。
你不提供自动操作，不读游戏内存，不虚构画面外细节；你给的是训练建议，不是剧情百科。

黑神话战斗知识点，优先围绕这些判断：
- 闪避时机：不要连续乱闪；观察敌人出手节奏，延迟一拍躲关键段。
- 棍势资源：有棍势时优先把重击打在硬直、破绽或安全窗口里，不要满资源乱交。
- 定身术：适合打断、争取输出或喝药，不要在敌人远离或即将转阶段时浪费。
- 铜头铁臂/防反：适合读准单段重击或明显前摇，不适合多段攻击里硬顶。
- 身外身法/变身：适合处理高压阶段、抢输出或收尾，但不要在低收益窗口空放。
- 喝药：喝药前先拉开距离或用定身/地形争取时间，别在敌人连段正中喝。
- 贪刀：黑神话常见失误是多贪最后一棍；打完安全两三下就撤，等下一轮破绽。
- 锁定/视角：大型 boss 或快速位移敌人容易丢视角；先稳住位置再输出。
- 法力/神力/葫芦：资源不足时优先保命，别为了小伤害把关键技能交空。
- 小怪群战：先拉开、分割、处理远程或高威胁目标，不要站在包围圈中间硬打。

可选 event_type：
DODGE_TOO_EARLY
DODGE_TOO_LATE
GREEDY_ATTACK
BAD_HEAL_TIMING
SPELL_MISUSED
IMMOBILIZE_WINDOW
COUNTER_TIMING
FOCUS_MISUSED
TRANSFORMATION_TIMING
MANA_LOW
CAMERA_LOST
CROWD_CONTROL
GOOD_PUNISH
GENERAL_ACTION_REVIEW
NO_MAJOR_EVENT

输出要求：
- 必须只输出 JSON，不要 Markdown
- coaching_text 用中文，30 到 75 字，适合直接语音播报
- 语气像动作游戏高手复盘：短、具体、能马上训练
- 先指出关键问题，再给下一次具体处理
- 不要说“我看不清”；信息不足时给保守建议并降低 confidence
- 不要编造 boss 名称、招式名称或截图无法确认的机制"""

    def build_user_prompt(self, request: ReviewRequest, action_summary: str) -> str:
        lines = [
            "请根据《黑神话：悟空》截图和玩家问题生成复盘 JSON。上游没有 event_type，你必须自己判断。",
            f"game_type: {request.game_type}",
            f"game_name: {request.game_name}",
            f"query: {request.query}",
            f"image_count: {len(request.image_paths or [])}",
        ]
        if action_summary:
            lines.append(f"context: {action_summary}")
        if request.client_elapsed_sec is not None:
            lines.append(f"client_elapsed_sec: {request.client_elapsed_sec}")
        lines.append("输出 JSON 字段必须包含：should_speak, game_type, event_type, problem, coaching_text, confidence")
        return "\n".join(lines)

    def build_rule_response(self, request: ReviewRequest, action_summary: str) -> dict | None:
        text = _normalize(f"{request.query} {action_summary}")
        rules = [
            (
                ("闪避",),
                "DODGE_TOO_EARLY",
                "刚才问题多半是闪避交太早。下次先看敌人真正出手点，别一看前摇就滚，留半拍躲关键伤害。",
            ),
            (
                ("贪",),
                "GREEDY_ATTACK",
                "这里像是贪了最后一棍。下次打完安全两三下就收手，先躲反击，再等硬直窗口补重击。",
            ),
            (
                ("喝药",),
                "BAD_HEAL_TIMING",
                "喝药时机太危险了。下次先拉开距离，或用定身争取空档，再喝葫芦，别在敌人连段里硬喝。",
            ),
            (
                ("定身",),
                "SPELL_MISUSED",
                "定身术别随手交。下次等敌人贴近、出招后摇或你需要喝药时再用，收益会比远距离空放高。",
            ),
            (
                ("棍势",),
                "FOCUS_MISUSED",
                "棍势要打在破绽里。下次别满势就急着重击，先等敌人后摇或定身窗口，再把重击砸实。",
            ),
            (
                ("视角",),
                "CAMERA_LOST",
                "这波先别急着输出，视角乱了就容易吃招。下次先拉开重锁定，确认敌人位置后再进场打。",
            ),
            (
                ("小怪",),
                "CROWD_CONTROL",
                "小怪多时别站中间硬换血。下次先拉开分割，优先处理远程或最烦的目标，再逐个收掉。",
            ),
        ]
        for keywords, event_type, coaching_text in rules:
            if all(keyword in text for keyword in keywords):
                return {
                    "should_speak": True,
                    "event_type": event_type,
                    "problem": coaching_text[:28],
                    "coaching_text": coaching_text,
                    "confidence": 0.72,
                }
        return None


def _normalize(text: str) -> str:
    return "".join(text.lower().split())
