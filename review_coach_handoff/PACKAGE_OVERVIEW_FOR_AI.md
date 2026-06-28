# ReviewCoach Slow Module Package Overview

一句概括：本模块实现了游戏语音教练 Demo 的慢通道适配层，消费 ActionFilter/ASR 产生的慢通道事件与 NitroGen action-change 上下文，维护短时上下文，执行规则优先/Gemini 兜底的文本建议生成，并返回 TTS 可调度的文本与优先级元信息；不负责事件检测、FastPath、ASR、TTS 播放、WebSocket、视频抽帧或 NitroGen 推理。

## 交付内容

- `review_coach/`: Python package, core implementation.
- `review_coach/slow/`: slow-channel adapter required by `DESIGN.md`.
- `review_coach/skills/platformer.py`: platformer-specific rule-first coaching logic.
- `run_review_demo.py`: local CLI smoke-test/demo runner.
- `tests/`: unit tests for rules, upstream payload parsing, and slow path scheduling.
- `nsmb_vlm_queries/`: sample NSMB inputs and screenshots for local verification.
- `README_REVIEW_COACH.md`: public module contract and run commands.
- `HANDOFF_REVIEW_COACH.md`: integration handoff notes.
- `PACKAGE_OVERVIEW_FOR_AI.md`: this AI-readable overview.
- `ACTION_SEQUENCE_SUMMARIZER_REQUIREMENTS.md`: upstream requirement doc for converting NitroGen frame-level action predictions into `action_summary` / `action_features`.

## 已实现范围

- Consume `USER_QUESTION`, `PATTERN_COMPLETED`, and `trigger_slow=True` events.
- Maintain compact recent context with `ContextBuffer`.
- Convert NitroGen action-change payloads into `ReviewRequest`.
- Generate short Chinese coaching text through local platformer rules first.
- Fall back to Gemini for complex or unseen cases.
- Return scheduling metadata for host TTS queue:
  - `channel`
  - `priority`
  - `interrupt_tts`
  - `clear_pending_channels`
  - `expire_sec`
- Compress image input before Gemini fallback through environment settings.
- Support text-only CLI output for TTS demos.

## 未实现范围

- Does not decide when `trigger_slow=True` should be produced. That belongs to upstream `ActionFilter` or event detector.
- Does not produce the upstream event `type`; it only consumes it.
- Does not implement FastPath templates or fast-channel speech.
- Does not implement ASR, TTS playback, TTS queue execution, interrupt execution, WebSocket delivery, video frame extraction, or NitroGen inference.
- Does not run as a production HTTP service by itself.

## Main Runtime Flow

```text
ActionFilter / ASR / EventDetector
    -> event: USER_QUESTION | PATTERN_COMPLETED | trigger_slow=True
    -> host calls SlowPath.handle(...)
    -> ContextBuffer adds recent action/event context
    -> ReviewCoach.generate(...)
    -> platformer local rules first
    -> Gemini fallback if rules do not match
    -> SlowPathResult returned to host TTS queue
```

## SlowPath Integration Example

```python
from review_coach import SlowPath

slow_path = SlowPath()

slow_path.observe_action_change(video_time, action_change_payload)
slow_path.observe_event(video_time, game_event, fast_text=last_fast_text)

slow_result = slow_path.handle(
    game_event,
    {
        **action_change_payload,
        "game_type": "platformer",
        "game_name": "New Super Mario Bros.",
        "image_paths": [current_frame_path],
    },
    user_question=recognized_user_text,
    last_fast_text=last_fast_text,
)

if slow_result:
    # Host owns actual queue behavior.
    # Use slow_result.priority, interrupt_tts, clear_pending_channels, expire_sec.
    tts_queue.push(slow_result.text, priority=slow_result.priority)
```

## Event Contract

Recommended event fields:

```json
{
  "type": "USER_QUESTION",
  "trigger_slow": true,
  "video_time": 16.2,
  "reason": "player_asked_question"
}
```

Supported trigger behavior:

| Input event | Output channel | Priority | TTS hint |
|---|---|---|---|
| `USER_QUESTION` | `user_answer` | `USER_ANSWER` | interrupt current TTS and clear pending `slow` / `slow_summary` |
| `PATTERN_COMPLETED` | `slow_summary` | `SLOW_SUMMARY` | enqueue as medium-priority summary |
| `trigger_slow=True` | `slow` | `SLOW_ADVICE` | enqueue after fast speech if still fresh |

## Output Contract

`SlowPath.handle(...)` returns `None` when the event should not trigger slow path. Otherwise it returns:

```python
SlowPathResult(
    channel="user_answer",
    priority="USER_ANSWER",
    text="...",
    review={...},
    context_summary="...",
    interrupt_tts=True,
    clear_pending_channels=("slow", "slow_summary"),
    expire_sec=30,
)
```

The host should normally speak `text` and log `review`.

## Verification Commands

```powershell
pip install -r requirements.txt
python -m unittest discover -s tests
python -m compileall review_coach run_review_demo.py
python run_review_demo.py --input-dir nsmb_vlm_queries\review_coach --text-only
```

Expected result:

- All unit tests pass.
- Compileall succeeds.
- Demo prints short Chinese coaching lines.

## Gemini Configuration

Local rule matches do not require Gemini. Gemini is only used for fallback.

```text
GEMINI_API_KEY=xxx
GEMINI_MODEL=gemini-3.1-flash-lite:stable
GEMINI_ENDPOINT=https://your-provider.example/v1/chat/completions
REVIEW_COACH_MOCK=0
REVIEW_COACH_MAX_TOKENS=120
REVIEW_COACH_TIMEOUT_SECONDS=30
REVIEW_COACH_RETRIES=1
REVIEW_COACH_IMAGE_MAX_SIDE=384
REVIEW_COACH_IMAGE_QUALITY=60
```
