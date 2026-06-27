from typing import Any


def summarize_actions(actions: list[dict] | None) -> str:
    if not actions:
        return "无动作序列"

    collapsed: list[tuple[str, int]] = []
    for item in actions:
        action = "UNKNOWN"
        if isinstance(item, dict):
            raw_action = item.get("action")
            if raw_action:
                action = str(raw_action)

        if collapsed and collapsed[-1][0] == action:
            previous_action, count = collapsed[-1]
            collapsed[-1] = (previous_action, count + 1)
        else:
            collapsed.append((action, 1))

    if not collapsed:
        return "无动作序列"

    return " -> ".join(f"{action}x{count}" for action, count in collapsed)


def summarize_action_context(action_summary: Any, change_info: dict[str, Any] | None = None) -> str:
    if isinstance(action_summary, str):
        return action_summary
    if not isinstance(action_summary, dict):
        return ""

    parts: list[str] = []
    left_stick = action_summary.get("left_stick_mean")
    if _is_pair(left_stick):
        parts.append(f"left_stick={_stick_label(left_stick)}({left_stick[0]:.2f},{left_stick[1]:.2f})")

    buttons = action_summary.get("buttons_avg_pressed")
    if isinstance(buttons, list) and buttons:
        pressed = [str(button) for button in buttons if str(button) != "(none)"]
        if pressed:
            parts.append("buttons=" + ",".join(pressed[:3]))

    triggers = action_summary.get("trigger_means")
    if isinstance(triggers, dict):
        active = [
            f"{name}={float(value):.2f}"
            for name, value in triggers.items()
            if _is_number(value) and float(value) > 0.1
        ]
        if active:
            parts.append("triggers=" + ",".join(active))

    if isinstance(change_info, dict):
        distance = change_info.get("distance")
        threshold = change_info.get("threshold")
        if _is_number(distance) and _is_number(threshold):
            parts.append(f"change_distance={float(distance):.2f}/{float(threshold):.2f}")
        delta = change_info.get("delta")
        if isinstance(delta, dict) and _is_pair(delta.get("left_stick")):
            left_delta = delta["left_stick"]
            parts.append(f"left_delta=({left_delta[0]:.2f},{left_delta[1]:.2f})")

    return "; ".join(parts) if parts else "无动作摘要"


def summarize_action_features(action_features: dict[str, Any] | None) -> str:
    if not isinstance(action_features, dict) or not action_features:
        return ""

    parts: list[str] = []
    main_movement = action_features.get("main_movement")
    if main_movement:
        parts.append(f"main_movement={main_movement}")

    jump_count = action_features.get("jump_count")
    if _is_number(jump_count):
        parts.append(f"jump_count={int(float(jump_count))}")

    dominant_pattern = action_features.get("dominant_pattern")
    if dominant_pattern:
        parts.append(f"dominant_pattern={dominant_pattern}")

    if action_features.get("direction_reversal") is True:
        parts.append("direction_reversal=true")

    risk_tags = action_features.get("risk_tags")
    if isinstance(risk_tags, list) and risk_tags:
        parts.append("risk_tags=" + ",".join(str(tag) for tag in risk_tags[:5]))

    return "; ".join(parts)


def _is_pair(value: Any) -> bool:
    return isinstance(value, list) and len(value) >= 2 and _is_number(value[0]) and _is_number(value[1])


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _stick_label(stick: list[Any]) -> str:
    x = float(stick[0])
    y = float(stick[1])
    horizontal = "right" if x > 0.35 else "left" if x < -0.35 else "center"
    vertical = "up" if y > 0.35 else "down" if y < -0.35 else "neutral"
    return horizontal if vertical == "neutral" else f"{horizontal}/{vertical}"
