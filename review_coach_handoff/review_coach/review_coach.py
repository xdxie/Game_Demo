from review_coach.action_summarizer import summarize_action_context, summarize_action_features, summarize_actions
from review_coach.gemini_client import GeminiClient
from review_coach.schemas import ReviewRequest
from review_coach.skills.base import BaseGameSkill
from review_coach.skills.general import GeneralReviewSkill
from review_coach.skills.platformer import PlatformerReviewSkill
from review_coach.skills.racing import RacingReviewSkill


class ReviewCoach:
    def __init__(self, gemini_client: GeminiClient | None = None):
        self.gemini_client = gemini_client or GeminiClient()
        self.skills: dict[str, BaseGameSkill] = {
            "platformer": PlatformerReviewSkill(),
            "racing": RacingReviewSkill(),
            "general": GeneralReviewSkill(),
        }

    def generate(self, request: ReviewRequest) -> dict:
        skill = self._select_skill(request.game_type)
        action_parts = [
            summarize_action_context(request.action_summary, request.change_info),
            summarize_action_features(request.action_features),
        ]
        action_summary = "\n".join(part for part in action_parts if part)
        if not action_summary:
            action_summary = summarize_actions(request.nitrogen_actions)
        rule_result = skill.build_rule_response(request, action_summary)
        if rule_result is not None:
            return skill.postprocess(rule_result, request)

        system_prompt = skill.build_system_prompt()
        user_prompt = skill.build_user_prompt(request, action_summary)
        raw_result = self.gemini_client.generate_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_paths=request.image_paths,
            request=request,
        )
        return skill.postprocess(raw_result, request)

    def _select_skill(self, game_type: str) -> BaseGameSkill:
        return self.skills.get((game_type or "").lower(), self.skills["general"])
