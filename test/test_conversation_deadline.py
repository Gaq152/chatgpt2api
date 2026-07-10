from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from services.protocol.conversation import (
    ConversationRequest,
    conversation_events,
    image_stream_error_message,
    is_connection_timeout_error,
    stream_image_outputs,
)


class ConversationDeadlineTests(unittest.TestCase):
    def test_image_conversation_passes_deadline_to_backend_stream(self):
        captured: dict[str, object] = {}

        class Backend:
            def stream_conversation(self, **kwargs):
                captured.update(kwargs)
                return iter(["[DONE]"])

        list(conversation_events(
            Backend(),
            prompt="cat",
            model="gpt-image-2",
            deadline=123.0,
        ))

        self.assertEqual(captured.get("deadline"), 123.0)

    def test_stream_drop_after_conversation_id_falls_back_to_poll(self):
        class Backend:
            def __init__(self) -> None:
                self.poll_timeout_secs: float | None = None

            def stream_conversation(self, **_kwargs):
                yield json.dumps({
                    "type": "server_ste_metadata",
                    "conversation_id": "conv-1",
                    "metadata": {"turn_use_case": "image gen", "tool_invoked": True},
                })
                raise RuntimeError("curl: (92) HTTP/2 stream was not closed cleanly")

            def resolve_conversation_image_urls(
                self,
                conversation_id: str,
                file_ids: list[str],
                sediment_ids: list[str],
                poll_timeout_secs: float | None = None,
            ) -> list[str]:
                self.poll_timeout_secs = poll_timeout_secs
                self.conversation_id = conversation_id
                return ["https://example.test/image.png"]

            def download_image_bytes(self, urls: list[str]) -> list[bytes]:
                self.download_urls = urls
                return [b"fake-image"]

        backend = Backend()
        with patch("services.protocol.conversation.save_image_bytes", return_value="http://local.test/image.png"):
            outputs = list(stream_image_outputs(
                backend,
                ConversationRequest(prompt="cat", model="gpt-image-2", response_format="url"),
                deadline=9999999999.0,
            ))

        self.assertEqual(backend.conversation_id, "conv-1")
        self.assertEqual(backend.download_urls, ["https://example.test/image.png"])
        self.assertEqual(outputs[-1].kind, "result")
        self.assertEqual(outputs[-1].data[0]["url"], "http://local.test/image.png")

    def test_curl_http2_stream_error_is_retryable_and_sanitized(self):
        message = "Failed to perform, curl: (92) HTTP/2 stream 1 was not closed cleanly: INTERNAL_ERROR"

        self.assertTrue(is_connection_timeout_error(message))
        self.assertEqual(
            image_stream_error_message(message),
            "upstream image connection failed, please retry later",
        )


if __name__ == "__main__":
    unittest.main()
