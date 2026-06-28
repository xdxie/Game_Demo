import base64
import io
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from review_coach.schemas import ReviewRequest


class GeminiClient:
    def __init__(self) -> None:
        self._load_env_if_available()
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.endpoint = os.getenv("GEMINI_ENDPOINT") or os.getenv("GEMINI_BASE_URL")
        self.max_tokens = int(os.getenv("REVIEW_COACH_MAX_TOKENS", "120"))
        self.timeout = int(os.getenv("REVIEW_COACH_TIMEOUT_SECONDS", "30"))
        self.retries = int(os.getenv("REVIEW_COACH_RETRIES", "1"))
        self.image_max_side = int(os.getenv("REVIEW_COACH_IMAGE_MAX_SIDE", "384"))
        self.image_quality = int(os.getenv("REVIEW_COACH_IMAGE_QUALITY", "60"))
        self.force_mock = os.getenv("REVIEW_COACH_MOCK", "0").strip() == "1"
        self.mock_mode = self.force_mock or not self.api_key
        self._image_data_url_cache: dict[tuple[str, float, int, int, int], str] = {}

    @staticmethod
    def _load_env_if_available() -> None:
        try:
            from dotenv import load_dotenv
        except Exception:
            return
        load_dotenv()

    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        image_paths: list[str],
        request: ReviewRequest,
    ) -> dict[str, Any]:
        if self.mock_mode:
            return self._mock_response(request.game_type)

        if self.endpoint:
            return self._generate_chat_completions_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_paths=image_paths,
                request=request,
            )

        images = self._load_images(image_paths)
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.api_key)
            response = client.models.generate_content(
                model=self.model,
                contents=[user_prompt, *images],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=0.2,
                    max_output_tokens=self.max_tokens,
                ),
            )
        except Exception as exc:
            raise RuntimeError(f"Gemini request failed: {exc}") from exc

        parsed = self._parse_json(getattr(response, "text", "") or "")
        if parsed is None:
            return self._fallback_response(request.game_type)
        return parsed

    def _generate_chat_completions_json(
        self,
        system_prompt: str,
        user_prompt: str,
        image_paths: list[str],
        request: ReviewRequest,
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": self._build_multimodal_content(user_prompt, image_paths)},
            ],
            "temperature": 0.2,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http_request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        response_body = self._post_with_retry(http_request)

        try:
            response_json = json.loads(response_body)
            content = response_json["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Gemini endpoint returned unexpected response: {response_body}") from exc

        parsed = self._parse_json(content)
        if parsed is None:
            return self._fallback_response(request.game_type)
        return parsed

    def _post_with_retry(self, http_request: urllib.request.Request) -> str:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(http_request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Gemini endpoint request failed: HTTP {exc.code}: {error_body}") from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
        raise RuntimeError(f"Gemini endpoint request failed: {last_error}") from last_error

    def _build_multimodal_content(self, user_prompt: str, image_paths: list[str]) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        for image_path in image_paths or []:
            path = Path(image_path)
            if not path.exists():
                print(f"Warning: image path does not exist: {image_path}", file=sys.stderr)
                continue
            data_url = self._image_data_url(path, image_path)
            if not data_url:
                continue
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                }
            )
        return content

    def _image_data_url(self, path: Path, image_path: str) -> str | None:
        try:
            stat = path.stat()
        except OSError as exc:
            print(f"Warning: failed to stat image {image_path}: {exc}", file=sys.stderr)
            return None

        cache_key = (
            str(path.resolve()),
            stat.st_mtime,
            self.image_max_side,
            self.image_quality,
            stat.st_size,
        )
        cached = self._image_data_url_cache.get(cache_key)
        if cached:
            return cached

        data_url = self._compressed_image_data_url(path, image_path)
        if data_url is None:
            try:
                image_bytes = path.read_bytes()
            except OSError as exc:
                print(f"Warning: failed to read image {image_path}: {exc}", file=sys.stderr)
                return None
            mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            encoded = base64.b64encode(image_bytes).decode("ascii")
            data_url = f"data:{mime_type};base64,{encoded}"

        self._image_data_url_cache[cache_key] = data_url
        return data_url

    def _compressed_image_data_url(self, path: Path, image_path: str) -> str | None:
        try:
            from PIL import Image
        except Exception:
            return None

        try:
            with Image.open(path) as image:
                image = image.convert("RGB")
                image.thumbnail((self.image_max_side, self.image_max_side))
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=self.image_quality, optimize=True)
        except Exception as exc:
            print(f"Warning: failed to compress image {image_path}: {exc}", file=sys.stderr)
            return None

        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    def _load_images(self, image_paths: list[str]) -> list[Any]:
        try:
            from PIL import Image
        except Exception as exc:
            raise RuntimeError(f"Pillow is required for real Gemini image mode: {exc}") from exc

        images: list[Any] = []
        for image_path in image_paths or []:
            path = Path(image_path)
            if not path.exists():
                print(f"Warning: image path does not exist: {image_path}", file=sys.stderr)
                continue
            try:
                images.append(Image.open(path))
            except Exception as exc:
                print(f"Warning: failed to open image {image_path}: {exc}", file=sys.stderr)
        return images

    def _parse_json(self, text: str) -> dict[str, Any] | None:
        cleaned = self._clean_json_text(text)
        for candidate in (cleaned, self._extract_json_object(cleaned)):
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    @staticmethod
    def _clean_json_text(text: str) -> str:
        cleaned = text.strip()
        fence_match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL)
        if fence_match:
            return fence_match.group(1).strip()
        return cleaned

    @staticmethod
    def _extract_json_object(text: str) -> str | None:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        return text[start : end + 1]

    @staticmethod
    def _mock_response(game_type: str) -> dict[str, Any]:
        if game_type == "racing":
            return {
                "should_speak": True,
                "game_type": "racing",
                "event_type": "LATE_BRAKE",
                "problem": "左弯前全油门保持太久，入弯前没有提前减速",
                "coaching_text": "刚才这个左弯前你全油保持得太久，车速已经偏高。下次进这种山路弯，提前松油或轻点刹车，让车头先对准弯心，再逐步给油出弯。",
                "confidence": 0.78,
            }
        if game_type == "platformer":
            return {
                "should_speak": True,
                "game_type": "platformer",
                "event_type": "SHELL_RISK",
                "problem": "顶方块后出现移动龟壳，需要先确认方向",
                "coaching_text": "刚才这类问号块顶出龟壳后，先别急着继续往前冲。下次可以停半拍看龟壳往哪边滑，如果朝你过来就跳过它，等它离开后再继续推进。",
                "confidence": 0.75,
            }
        return GeminiClient._fallback_response(game_type)

    @staticmethod
    def _fallback_response(game_type: str) -> dict[str, Any]:
        return {
            "should_speak": True,
            "game_type": game_type,
            "event_type": "GENERAL_REVIEW",
            "problem": "模型输出不可解析，已切换为保守复盘",
            "coaching_text": "刚才这段信息不够稳定，我会先给保守建议：下次遇到类似情况，先放慢一拍确认局面，再做主要操作，避免因为急着推进而扩大失误。",
            "confidence": 0.5,
        }
