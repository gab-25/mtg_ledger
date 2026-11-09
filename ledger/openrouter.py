"""OpenRouter client: asks a vision model what card is in a photo.

API: https://openrouter.ai/docs (OpenAI-compatible chat completions).
"""

import base64
import json

import httpx

from .models import Color, Language
from .recognition import Recognition

BASE_URL = "https://openrouter.ai/api/v1"

PROMPT = (
    "You are looking at a photo of one Magic: The Gathering card. Read it and answer with JSON only.\n"
    "- name: the card's ENGLISH name, even when the card is printed in another language.\n"
    "- set_code: the expansion code printed at the bottom left (e.g. DSK), or empty if not visible.\n"
    "- collector_number: the collector number printed at the bottom left without the total "
    "(\"0187/0286\" -> \"0187\"), or empty if not visible.\n"
    f"- language: the language the card is printed in, one of {', '.join(Language.values)}; "
    "empty if unreadable or another language.\n"
    "- foil: true if the card is foil (holographic sheen, star next to the set code).\n"
    "- type_line: the type line in English (e.g. \"Creature — Elf Druid\").\n"
    f"- color: one of {', '.join(Color.values)} (M for multicolor, C for colorless).\n"
    "Never guess a collector number you cannot read. "
    "If the photo shows no Magic card (or it is unreadable), answer with an empty name and empty fields."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "set_code": {"type": "string"},
        "collector_number": {"type": "string"},
        "language": {"type": "string"},
        "foil": {"type": "boolean"},
        "type_line": {"type": "string"},
        "color": {"type": "string"},
    },
    "required": ["name", "set_code", "collector_number", "language", "foil", "type_line", "color"],
    "additionalProperties": False,
}


class OpenRouterError(Exception):
    pass


class OpenRouterClient:
    def __init__(self, key, *, model, base_url=BASE_URL, transport=None, timeout=20.0):
        if not key:
            raise OpenRouterError("OpenRouter API key is not configured.")
        self.model = model
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}", "X-Title": "MTG Ledger"},
            timeout=timeout,
            transport=transport,
        )

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, method, path, **kwargs):
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"OpenRouter unreachable: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.is_error:
            message = ((data or {}).get("error") or {}).get("message") if isinstance(data, dict) else None
            raise OpenRouterError(f"OpenRouter failed ({resp.status_code}): {message or resp.reason_phrase}")
        return data

    def check_key(self):
        """Raises OpenRouterError if the key is not valid."""
        return self._request("GET", "/key")

    def recognize_card(self, image_jpeg):
        image = base64.b64encode(image_jpeg).decode("ascii")
        data = self._request("POST", "/chat/completions", json={
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
            ]}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "card", "strict": True, "schema": SCHEMA}},
            "temperature": 0,
            # Reading a card needs no reasoning; reasoning models (e.g. Qwen) would get slow and costly.
            "reasoning": {"enabled": False},
        })
        try:
            content = data["choices"][0]["message"]["content"]
            read = json.loads(content.strip().removeprefix("```json").removesuffix("```"))
            if not isinstance(read, dict):
                raise ValueError("not an object")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise OpenRouterError("OpenRouter returned an unreadable answer") from exc

        def text(key):
            value = read.get(key)
            return value.strip() if isinstance(value, str) else ""

        language = text("language").upper()
        color = text("color").upper()
        return Recognition(
            name=text("name"),
            set_code=text("set_code").lower(),
            collector_number=text("collector_number"),
            language=language if language in Language.values else "",
            foil=read.get("foil") is True,
            type_line=text("type_line"),
            color=color if color in Color.values else "",
        )
