from review_coach.schemas import ReviewRequest
from review_coach.skills.base import BaseGameSkill


class PlatformerReviewSkill(BaseGameSkill):
    name = "platformer"
    default_event_type = "GENERAL_PLATFORMER_REVIEW"

    def build_system_prompt(self) -> str:
        return """你是马里奥类 2D 平台游戏复盘教练。结合截图、玩家提问和操作摘要，给一句短建议。
关注：跳跃时机/助跑/落点、敌人碰撞、掉坑、道具/金币取舍、是否太急。
只输出 JSON：{"coaching_text":"..."}。
coaching_text 用中文 30-55 字，像语音提示：先安抚，再指出关键问题，最后给一个具体动作。不要解释过程。"""

    def build_user_prompt(self, request: ReviewRequest, action_summary: str) -> str:
        return _build_common_user_prompt(request, action_summary)

    def build_rule_response(self, request: ReviewRequest, action_summary: str) -> dict | None:
        text = _normalize_text(f"{request.query} {action_summary}")
        tags = _extract_tags(text)
        rule = _match_rule(tags)
        if rule:
            event_type, coaching_text, confidence = rule
            return {
                "should_speak": True,
                "event_type": event_type,
                "coaching_text": coaching_text,
                "confidence": confidence,
            }
        return None


def _build_common_user_prompt(request: ReviewRequest, action_summary: str) -> str:
    lines = [
        "生成一句播报文本。",
        f"query: {request.query}",
    ]
    if action_summary:
        lines.append(f"action: {action_summary}")
    return "\n".join(lines)


def _normalize_text(text: str) -> str:
    return "".join(text.lower().split())


def _extract_tags(text: str) -> set[str]:
    tag_keywords = {
        "jump_timing": (
            "起跳早",
            "跳早",
            "太早跳",
            "早跳",
            "跳太早",
            "起跳晚",
            "跳晚",
            "够不着",
            "没够到",
            "没顶到",
            "顶不到",
            "跳不到",
            "距离不够",
            "差点够不着",
        ),
        "block": (
            "砖",
            "问号砖",
            "砖块",
            "顶砖",
            "顶那个砖",
            "顶方块",
            "方块",
            "奖励砖",
        ),
        "enemy": (
            "怪",
            "敌人",
            "小怪",
            "怪物",
            "蘑菇怪",
            "栗宝宝",
            "乌龟",
            "龟壳",
            "脚底",
            "下面",
            "下方",
            "碰到",
            "撞到",
            "被撞",
            "被碰",
            "偷袭",
        ),
        "reward": (
            "奖励",
            "道具",
            "金币",
            "红币",
            "拿",
            "收集",
            "吃",
            "顶砖",
            "问号砖",
        ),
        "powerup": (
            "超级蘑菇",
            "蘑菇道具",
            "冒出蘑菇",
            "蘑菇出现",
            "变大",
            "道具",
            "吃了",
            "吃到",
            "回头吃",
            "停一下",
            "先吃",
        ),
        "red_coin": (
            "红圈",
            "红币",
            "红色圆环",
            "红环",
            "挑战",
            "乱飞",
            "路线",
        ),
        "pit": (
            "坑",
            "沟",
            "沟壑",
            "掉下去",
            "掉坑",
            "掉进",
            "落点",
            "平台边",
        ),
        "rush": (
            "太急",
            "急着",
            "着急",
            "贪",
            "贪心",
            "冲",
            "乱跳",
            "赶路",
            "没看清",
            "先看清",
            "观察",
            "等一下",
        ),
    }

    tags: set[str] = set()
    for tag, keywords in tag_keywords.items():
        if any(keyword in text for keyword in keywords):
            tags.add(tag)
    return tags


def _match_rule(tags: set[str]) -> tuple[str, str, float] | None:
    if {"jump_timing", "block"} <= tags:
        return (
            "JUMP_TOO_EARLY",
            "别担心，确实像起跳时机早了点。下次靠近边缘再按跳，方向键稳住，砖块会更容易顶到。",
            0.92,
        )

    if "jump_timing" in tags:
        return (
            "JUMP_TOO_EARLY",
            "别担心，主要是起跳时机没卡准。下次先稳住方向，接近边缘再起跳。",
            0.72,
        )

    if "red_coin" in tags:
        return (
            "RUSH_TOO_FAST",
            "别慌，红币挑战本来节奏快。下次触发后先停半拍看轨迹，再按顺序跳着收集。",
            0.9,
        )

    if {"pit", "enemy"} <= tags:
        return (
            "ENEMY_COLLISION",
            "刚才确实有点惊险。坑边别急着处理怪，先稳住落点，没把握就直接跳过去。",
            0.86,
        )

    if {"reward", "rush"} <= tags:
        return (
            "RUSH_TOO_FAST",
            "别灰心，奖励不值得冒险。下次先观察敌人和落点，安全了再过去收集。",
            0.78,
        )

    if "powerup" in tags:
        return (
            "POWERUP_USAGE",
            "别急着赶路，蘑菇和道具能提高容错率。下次先停一下，吃到再继续推进。",
            0.88,
        )

    if {"enemy", "reward"} <= tags:
        return (
            "ENEMY_COLLISION",
            "你的判断是对的，先保命更重要。下次先清掉脚下威胁，确认安全后再拿奖励。",
            0.86,
        )

    if {"enemy", "rush"} <= tags:
        return (
            "RUSH_TOO_FAST",
            "别急，刚才主要是节奏被小怪打乱了。下次先等半拍看清走位，再起跳通过。",
            0.76,
        )

    if {"jump_timing", "pit"} <= tags:
        return (
            "MISSED_PLATFORM",
            "别慌，问题主要在落点判断。下次起跳前先确认平台边缘，宁可晚半拍也要稳住落地。",
            0.74,
        )

    return None
