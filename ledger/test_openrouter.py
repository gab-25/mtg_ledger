import json

import httpx
from django.test import SimpleTestCase

from .openrouter import SCHEMA, OpenRouterClient, OpenRouterError

READ = {"name": "Counterspell", "set_code": "DMR", "collector_number": "045", "language": "IT", "foil": True}


def completion(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class RecognizeTest(SimpleTestCase):
    def client_for(self, handler):
        return OpenRouterClient("key", model="test/model", transport=httpx.MockTransport(handler))

    def test_recognize_parses_json(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(200, json=completion(json.dumps(READ)))

        rec = self.client_for(handler).recognize_card(b"\xff\xd8jpeg")
        self.assertEqual((rec.name, rec.set_code, rec.collector_number), ("Counterspell", "dmr", "045"))
        self.assertEqual((rec.language, rec.foil), ("IT", True))
        self.assertFalse(hasattr(rec, "color") or hasattr(rec, "type_line"))
        self.assertNotIn("type_line", json.dumps(seen["body"]))
        self.assertEqual(seen["body"]["model"], "test/model")
        self.assertIn("image_url", json.dumps(seen["body"]))
        self.assertEqual(seen["auth"], "Bearer key")
        # Reading a card needs no reasoning: with it, reasoning models (e.g. Qwen) get slow and costly.
        self.assertEqual(seen["body"]["reasoning"], {"enabled": False})

    def test_missing_model_raises(self):
        with self.assertRaises(OpenRouterError):
            OpenRouterClient("key", model="")

    def test_recognize_bad_content_raises(self):
        with self.assertRaises(OpenRouterError):
            self.client_for(lambda r: httpx.Response(200, json=completion("sorry"))).recognize_card(b"x")

    def test_recognize_http_error_raises(self):
        with self.assertRaises(OpenRouterError):
            self.client_for(lambda r: httpx.Response(401, json={"error": {"message": "bad key"}})).recognize_card(b"x")

    def test_recognize_timeout_raises(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        with self.assertRaises(OpenRouterError):
            self.client_for(handler).recognize_card(b"x")

    def test_unsupported_language_blank(self):
        data = dict(READ, language="zh-TW")
        rec = self.client_for(lambda r: httpx.Response(200, json=completion(json.dumps(data)))).recognize_card(b"x")
        self.assertEqual(rec.language, "")

    def test_schema_has_no_color_or_type(self):
        self.assertEqual(SCHEMA["required"], ["name", "set_code", "collector_number", "language", "foil"])
