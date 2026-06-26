# ReviewCoach / Game Review Skill Layer

ReviewCoach converts a player query, optional screenshots/keyframes, optional NitroGen action context, and a game type into a short coaching response for downstream UI or TTS.

This repository contains the local Python module and CLI demo. It does not implement NitroGen Server, frontend, WebSocket, ASR, TTS, automatic video frame extraction, or production service hosting.

## DESIGN.md SlowPath Scope

This module now covers the slow-channel responsibility described in `DESIGN.md`:

- `review_coach.slow.ContextBuffer`: keeps a compact 15-second action/event window.
- `review_coach.slow.should_trigger_slow`: recognizes `USER_QUESTION`, `PATTERN_COMPLETED`, and `trigger_slow=True` events.
- `review_coach.slow.SlowPath`: converts slow events into ReviewCoach requests and returns TTS-ready channel/priority metadata.

It does not own FastPath templates, TTS playback, ASR, WebSocket delivery, NitroGen inference, or video frame extraction.

## Upstream Input

Upstream should provide:

- `game_type`: `platformer`, `racing`, or an unknown value that falls back to `general`
- `game_name`
- `query`
- `image_paths`: zero or more screenshots/keyframes
- Optional `action_summary`
- Optional `nitrogen_actions`
- Optional `clip_start`, `clip_end`, and `trigger_reason`

Upstream does not need to judge `event_type`. ReviewCoach infers it when possible.

## Fast Path

Platformer reviews use a local rule-first path before Gemini:

1. Normalize `query + action_summary`.
2. Extract semantic tags such as `jump_timing`, `enemy`, `reward`, `powerup`, `red_coin`, `pit`, and `rush`.
3. Return a local coaching response when a known rule matches.
4. Fall back to Gemini only when local rules are not confident enough.

This makes common platformer questions return in milliseconds and avoids waiting for Gemini on frequent patterns.

Current platformer rules cover:

- Jump timing near blocks
- Enemy vs reward priority
- Power-up usage
- Red coin / red ring route planning
- Pit-edge enemy handling
- Greedy coin/reward collection
- Rushing into enemies

## SlowPath Integration

Use `SlowPath` when integrating with `GameEvent` / `trigger_slow` flow from `DESIGN.md`:

```python
from review_coach import SlowPath

slow_path = SlowPath()

# Keep recent context updated from NitroGen/action-change observations.
slow_path.observe_action_change(video_time, action_change_payload)
slow_path.observe_event(video_time, game_event, fast_text="向左闪！")

# USER_QUESTION, PATTERN_COMPLETED, or trigger_slow=True.
slow_result = slow_path.handle(
    game_event,
    {
        **action_change_payload,
        "game_type": "platformer",
        "game_name": "New Super Mario Bros.",
        "image_paths": [current_frame_path],
    },
    user_question=recognized_user_text,
    last_fast_text="向左闪！",
)

if slow_result:
    tts_queue.push(slow_result.text, priority=slow_result.priority)
```

`SlowPathResult` fields:

| Field | Meaning |
|---|---|
| `channel` | `user_answer`, `slow`, or `slow_summary` |
| `priority` | `USER_ANSWER`, `SLOW_ADVICE`, or `SLOW_SUMMARY` |
| `text` | TTS-ready coaching text |
| `review` | Full ReviewCoach output dict |
| `context_summary` | ContextBuffer summary sent into the slow request |
| `interrupt_tts` | Whether host TTS should interrupt current speech |
| `clear_pending_channels` | Pending channels the host queue should drop before enqueue |
| `expire_sec` | Suggested max age before dropping this result |

Design alignment:

- User questions map to `USER_ANSWER` and should interrupt lower priority speech in the host TTS queue.
- `PATTERN_COMPLETED` maps to `SLOW_SUMMARY`.
- Other `trigger_slow=True` events map to `SLOW_ADVICE`.
- `last_fast_text` is included in slow context so the model avoids repeating FastPath speech.

Scheduling alignment:

- `trigger_slow=True`: `channel="slow"`, `priority=SLOW_ADVICE`, no interrupt, `expire_sec=8`.
- `USER_QUESTION`: `channel="user_answer"`, `priority=USER_ANSWER`, `interrupt_tts=True`, `clear_pending_channels=("slow", "slow_summary")`, `expire_sec=30`.
- `PATTERN_COMPLETED`: `channel="slow_summary"`, `priority=SLOW_SUMMARY`, no interrupt, `expire_sec=15`.

## NitroGen Action-Change Input

ReviewCoach can also consume JSON shaped like NitroGen action-change `/predict` output. Extra fields are ignored or preserved as optional context:

- `frame_idx`, `session_idx`
- `is_change`
- `change_info`
- structured `action_summary`
- `source_image`
- `client_elapsed_sec`

If `image_paths` is absent and `source_image` exists, `source_image` is used as the single image path. Structured `action_summary` is converted into a compact text summary before rule matching or Gemini fallback, for example:

```text
left_stick=right(0.94,-0.03); buttons=SOUTH(0.72); change_distance=1.06/0.70; left_delta=(1.20,0.10)
```

For integration, add `game_type`, `game_name`, and the player's `query` to the action-change payload, then pass it through `ReviewRequest.from_payload(payload)`.

## Output Contract

Core API returns a full JSON-compatible dict:

```json
{
  "should_speak": true,
  "game_type": "platformer",
  "event_type": "JUMP_TOO_EARLY",
  "problem": "未识别到明确问题",
  "coaching_text": "别担心，确实像起跳时机早了点。下次靠近边缘再按跳，方向键稳住，砖块会更容易顶到。",
  "confidence": 0.92
}
```

Product/TTS integration should usually consume `coaching_text`. `event_type` and `confidence` are useful for logging, analytics, and debugging.

When the local rule path matches, Gemini is not called. When no rule matches, ReviewCoach calls Gemini and still returns the same output shape.

## Example Input

```json
{
  "game_type": "platformer",
  "game_name": "New Super Mario Bros.",
  "query": "我是不是跳太早了，那个砖一直没顶到？",
  "image_paths": ["nsmb_vlm_queries/query_01_screenshot_00m16s.jpg"],
  "action_summary": "JUMPx1 -> RIGHTx3 -> LEFTx1",
  "clip_start": 12.0,
  "clip_end": 16.0,
  "trigger_reason": "manual_review"
}
```

## Run

```bash
pip install -r requirements.txt
python run_review_demo.py --input examples/sample_platformer.json
python run_review_demo.py --input examples/sample_racing.json
python run_review_demo.py --input-dir nsmb_vlm_queries/review_coach --text-only
```

`--text-only` prints only `coaching_text`, which is the recommended output for TTS demos.

## Real Gemini Mode

Create `.env`:

```text
GEMINI_API_KEY=xxx
GEMINI_MODEL=gemini-3.1-flash-lite:stable
GEMINI_ENDPOINT=https://your-provider.example/v1/chat/completions
REVIEW_COACH_MOCK=0
```

Useful latency-related settings:

```text
REVIEW_COACH_MAX_TOKENS=120
REVIEW_COACH_TIMEOUT_SECONDS=30
REVIEW_COACH_RETRIES=1
REVIEW_COACH_IMAGE_MAX_SIDE=384
REVIEW_COACH_IMAGE_QUALITY=60
```

Set `REVIEW_COACH_MOCK=1` to force mock mode even when an API key exists.

## Tests

```bash
python -m unittest discover -s tests
python -m compileall review_coach run_review_demo.py
```
