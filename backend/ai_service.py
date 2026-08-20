"""
Generic AI wrapper — Gemini free-tier client with rate-limit retry.

Every module that needs an LLM call (M2 qualification now; M3 employee
matching and M5 email generation later) goes through `generate_text()` so the
retry/backoff behavior lives in exactly one place instead of being
reimplemented per caller.

Model choice: gemini-3.6-flash's free tier is capped at 20 requests/day
(confirmed via a live 429 RESOURCE_EXHAUSTED response body during real
testing), which one demo campaign alone can exceed. Both gemini-2.5-flash-lite
and gemini-2.0-flash-lite returned a live 404 for this key ("no longer
available") — confirmed by direct API calls, not assumed. Google's own 404
body for both pointed to gemini-3.5-flash-lite, and a direct call confirmed
that model actually responds (200) for this key, so that's what's used here.
ai.google.dev no longer publishes a static per-model free-tier RPM/RPD table
on its rate-limits page (it now points to the authenticated per-project
aistudio.google.com/rate-limit page instead) — so treat any quota number for
this model as unverified until we see a real 429 body; the quotaValue in an
actual 429 response is the authoritative source for this key.
"""

import time
from typing import Optional

import requests

from backend.config import settings

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.5-flash-lite"

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 2  # attempt 1 -> 2s, attempt 2 -> 4s


class AIServiceError(Exception):
    """Raised when a call to the AI service fails (bad config, non-retryable
    HTTP error, or a rate limit that didn't clear within MAX_RETRIES)."""


def _is_rate_limit_error(response: requests.Response) -> bool:
    if response.status_code == 429:
        return True
    try:
        body = response.json()
    except ValueError:
        return False
    return body.get("error", {}).get("status") == "RESOURCE_EXHAUSTED"


def generate_text(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Calls Gemini with `prompt` and returns the raw text of its response.

    On a 429 / RESOURCE_EXHAUSTED response, retries up to MAX_RETRIES times
    with exponential backoff (2s, 4s) before raising. Any other HTTP error
    raises immediately without retrying, since retrying won't help a bad
    request or an auth failure.
    """
    if not settings.GEMINI_API_KEY:
        raise AIServiceError("GEMINI_API_KEY is not set — add it to .env before calling the AI service.")

    last_status: Optional[int] = None
    last_body: str = ""

    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.post(
            GEMINI_URL.format(model=model),
            params={"key": settings.GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )

        if response.status_code == 200:
            data = response.json()
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError) as exc:
                raise AIServiceError(f"Unexpected Gemini response shape: {data}") from exc

        last_status, last_body = response.status_code, response.text

        if _is_rate_limit_error(response) and attempt < MAX_RETRIES:
            time.sleep(BASE_BACKOFF_SECONDS**attempt)
            continue

        raise AIServiceError(f"Gemini call failed: {response.status_code} {response.text}")

    raise AIServiceError(f"Gemini call failed after {MAX_RETRIES} attempts: {last_status} {last_body}")
