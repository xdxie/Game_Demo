import argparse
import json
import sys
from pathlib import Path

from review_coach import ReviewCoach, ReviewRequest


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Run ReviewCoach demo from input JSON files.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", help="Path to sample input JSON.")
    group.add_argument("--input-dir", help="Directory containing query_*.json input files.")
    parser.add_argument("--text-only", action="store_true", help="Print only coaching_text.")
    args = parser.parse_args()

    input_paths = [Path(args.input)] if args.input else sorted(Path(args.input_dir).glob("query_*.json"))
    if not input_paths:
        print("Error: no input files found.", file=sys.stderr)
        return 1

    coach = ReviewCoach()
    exit_code = 0
    for input_path in input_paths:
        if len(input_paths) > 1:
            print(f"--- {input_path.name}")
        exit_code = max(exit_code, _run_one(input_path, coach, text_only=args.text_only))
    return exit_code


def _run_one(input_path: Path, coach: ReviewCoach, text_only: bool) -> int:
    if not input_path.exists():
        print(f"Error: input file does not exist: {input_path}", file=sys.stderr)
        return 1

    with input_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    for image_path in payload.get("image_paths") or []:
        if not Path(image_path).exists():
            print(f"Warning: image path does not exist: {image_path}", file=sys.stderr)

    request = ReviewRequest.from_payload(payload)
    result = coach.generate(request)
    if text_only:
        print(result.get("coaching_text", ""))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
