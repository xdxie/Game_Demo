from review_coach.schemas import ReviewRequest
from review_coach.skills.base import BaseGameSkill


class StreetFighter6ReviewSkill(BaseGameSkill):
    name = "street_fighter6"
    default_event_type = "GENERAL_FIGHTING_REVIEW"

    def build_system_prompt(self) -> str:
        return """你是 Street Fighter 6 慢系统复盘教练，只用于训练、复盘、自定义房间或线下对战后的主动提问。
你不自动操作、不读内存、不提供作弊式实时指令；你只根据截图、玩家问题和可选上下文给出短复盘。

街霸6知识点，优先围绕这些判断：
- 立回：中距离不要无意义前走，先占安全距离，观察对手前跳、前冲、波动和差合习惯。
- 对空：对手前跳时，优先准备角色稳定对空选项；没反应时不要用慢按钮硬抢。
- 差合：自己挥空后被打，重点是按钮距离太短或按得太满；下次用更长按钮或少按等对手挥空。
- 确反：被防后还按，通常是负帧继续抢；下次防住后先确认对方是否轮到你。
- Drive Impact：版边和重攻击后特别容易吃 DI；出招别太满，看到闪光优先反 DI 或取消应对。
- Drive Rush：绿色冲刺后的压力要区分真压和偷回合；不确定时先防，不要每次乱抢。
- 投/拆投/shimmy：被投循环时先延迟拆投；被 shimmy 打时减少自动拆投，改成防守观察。
- 起身：不要每次起身升龙或乱按；根据对手是否爱投、爱压、爱等升龙选择防御、延迟拆投或偶尔无敌技。
- 版边：版边先保命和换位，不要急着抢大按钮；等对手压制间隙用轻拳、跳出、反 DI 或无敌技。
- 资源：Drive 低时少乱用 DR/DI；血少或有 SA 时再考虑用资源换回合或收尾。

可选 event_type：
ANTI_AIR_MISSED
JUMP_IN_HIT
WHIFF_PUNISH_MISSED
UNSAFE_ON_BLOCK
DRIVE_IMPACT_HIT
DRIVE_RUSH_PRESSURE
THROW_LOOP
SHIMMY_CAUGHT
BAD_WAKEUP_OPTION
CORNER_PRESSURE
METER_MISUSED
COMBO_DROP
GOOD_DEFENSE
GENERAL_FIGHTING_REVIEW
NO_MAJOR_EVENT

输出要求：
- 必须只输出 JSON，不要 Markdown
- coaching_text 用中文，30 到 75 字，适合直接语音播报
- 语气像街霸高手复盘：短、具体、能马上训练
- 先指出关键问题，再给下一次具体处理
- 不要说“我看不清”；信息不足时给保守建议并降低 confidence
- 不要虚构角色专属帧数、招式名或画面中无法确认的细节"""

    def build_user_prompt(self, request: ReviewRequest, action_summary: str) -> str:
        lines = [
            "请根据街霸6截图和玩家问题生成复盘 JSON。上游没有 event_type，你必须自己判断。",
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
                ("跳", "对空"),
                "ANTI_AIR_MISSED",
                "刚才重点不是抢按钮，而是先守住对空距离。下次看到他前跳，先停住别前走，用升龙或稳定对空处理。",
            ),
            (
                ("跳入",),
                "JUMP_IN_HIT",
                "你被跳入主要是站位没留对空反应。下次中距离先稳住，看见起跳就停手对空，别继续前走抢按钮。",
            ),
            (
                ("起身", "升龙"),
                "BAD_WAKEUP_OPTION",
                "起身别每次都赌升龙。对手贴身压你时，先多用防御和延迟拆投，确认他爱投再偶尔反打。",
            ),
            (
                ("起身",),
                "BAD_WAKEUP_OPTION",
                "刚才起身选择太急了。下次先防住第一拍，观察他是投、压还是等升龙，再决定拆投或反打。",
            ),
            (
                ("投",),
                "THROW_LOOP",
                "你现在像被投循环牵着走。下次版边先稳防，少乱按，观察他投后走位，再用延迟拆投或后跳破解。",
            ),
            (
                ("shimmy",),
                "SHIMMY_CAUGHT",
                "这里像是被 shimmy 抓了拆投。下次别自动拆，先多防一拍，等他走回来再用轻拳或低风险按钮止住。",
            ),
            (
                ("di",),
                "DRIVE_IMPACT_HIT",
                "这里要预留 Drive Impact 反应。你出招别太满，看到版边闪光优先反 DI，别继续按慢招。",
            ),
            (
                ("driveimpact",),
                "DRIVE_IMPACT_HIT",
                "这里要预留 Drive Impact 反应。你出招别太满，看到版边闪光优先反 DI，别继续按慢招。",
            ),
            (
                ("版边",),
                "CORNER_PRESSURE",
                "版边先别急着抢大按钮。下次先稳防一拍，找轻拳止压、跳出换边或反 DI，目标是先活着出来。",
            ),
            (
                ("连段",),
                "COMBO_DROP",
                "这波重点是确认后别急着接复杂连段。下次先用稳定收尾，保证伤害和倒地，再慢慢加高难路线。",
            ),
        ]
        for keywords, event_type, coaching_text in rules:
            if all(keyword in text for keyword in keywords):
                return {
                    "should_speak": True,
                    "event_type": event_type,
                    "problem": coaching_text[:28],
                    "coaching_text": coaching_text,
                    "confidence": 0.74,
                }
        return None


def _normalize(text: str) -> str:
    return "".join(text.lower().split())
