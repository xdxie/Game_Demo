# ReviewCoach Handoff Notes

This package contains the ReviewCoach module for integration testing with NitroGen action-change output.

## What To Merge

- `review_coach/`: core Python package
- `run_review_demo.py`: local CLI demo
- `README_REVIEW_COACH.md`: module contract and integration notes
- `requirements.txt`: Python dependencies
- `tests/`: regression tests
- `nsmb_vlm_queries/`: sample platformer inputs and screenshots
- `PACKAGE_OVERVIEW_FOR_AI.md`: one-page module overview for teammate or AI-assisted integration review
- `ACTION_SEQUENCE_SUMMARIZER_REQUIREMENTS.md`: upstream requirement doc for converting NitroGen frame-level action predictions into slow/ consumable summaries

Do not commit a local `.env` file or API keys.

## Integration Shape

NitroGen action-change `/predict` output can be passed through after adding three fields:

```python
from review_coach import ReviewCoach, ReviewRequest

payload = {
    **action_change_response,
    "game_type": "platformer",
    "game_name": "New Super Mario Bros.",
    "query": player_query,
}

request = ReviewRequest.from_payload(payload)
result = ReviewCoach().generate(request)
text_for_tts = result["coaching_text"]
```

`ReviewRequest.from_payload()` tolerates NitroGen fields such as `frame_idx`, `session_idx`, `is_change`, `change_info`, `source_image`, and structured `action_summary`.

For DESIGN.md slow-channel integration, use `SlowPath`:

```python
from review_coach import SlowPath

slow_path = SlowPath()
slow_path.observe_action_change(video_time, action_change_response)
slow_path.observe_event(video_time, game_event, fast_text=last_fast_text)

slow_result = slow_path.handle(
    game_event,
    {
        **action_change_response,
        "game_type": "platformer",
        "game_name": "New Super Mario Bros.",
        "image_paths": [current_frame_path],
    },
    user_question=recognized_user_text,
    last_fast_text=last_fast_text,
)

if slow_result:
    # channel: user_answer | slow | slow_summary
    # priority: USER_ANSWER | SLOW_ADVICE | SLOW_SUMMARY
    # interrupt_tts / clear_pending_channels / expire_sec are scheduling hints.
    tts_queue.push(slow_result.text, priority=slow_result.priority)
```

This package implements the slow-channel adapter only. The host application still owns FastPath, ASR, TTS queue execution, WebSocket delivery, NitroGen inference, and video frame extraction.

## Runtime Behavior

ReviewCoach uses a rule-first path for platformer questions:

1. Normalize `query + action_summary`.
2. Extract semantic tags such as jump timing, enemy, reward, power-up, red coin, pit, and rush.
3. Return a local coaching response if a rule matches.
4. Call Gemini only when local rules do not match.

Common platformer feedback returns in milliseconds. Gemini fallback is still available for complex or unseen questions.

## Quick Verification

```powershell
pip install -r requirements.txt
python -m unittest discover -s tests
python -m compileall review_coach run_review_demo.py
python run_review_demo.py --input-dir nsmb_vlm_queries\review_coach --text-only
```

Expected result:

- Unit tests pass.
- Compileall passes.
- The six NSMB sample queries print short Chinese coaching lines.

## Gemini Environment

Local rule matches do not need Gemini. For fallback, configure:

```text
GEMINI_API_KEY=xxx
GEMINI_MODEL=gemini-3.1-flash-lite:stable
GEMINI_ENDPOINT=https://your-provider.example/v1/chat/completions
REVIEW_COACH_MOCK=0
```

Useful optional settings:

```text
REVIEW_COACH_MAX_TOKENS=120
REVIEW_COACH_TIMEOUT_SECONDS=30
REVIEW_COACH_RETRIES=1
REVIEW_COACH_IMAGE_MAX_SIDE=384
REVIEW_COACH_IMAGE_QUALITY=60
```
