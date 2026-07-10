from __future__ import annotations

import threading
import time
import unittest

from utils.helper import iter_sse_payloads


class IterSSEPayloadTests(unittest.TestCase):
    def test_deadline_closes_hanging_response(self):
        class HangingResponse:
            def __init__(self) -> None:
                self.closed = threading.Event()

            def iter_lines(self):
                while not self.closed.wait(timeout=0.01):
                    pass
                return
                yield b""

            def close(self) -> None:
                self.closed.set()

        response = HangingResponse()

        with self.assertRaises(TimeoutError):
            list(iter_sse_payloads(response, deadline=time.time() + 0.05))

        self.assertTrue(response.closed.is_set())

    def test_deadline_masks_close_error_as_timeout(self):
        class ErrorOnCloseResponse:
            def __init__(self) -> None:
                self.closed = threading.Event()

            def iter_lines(self):
                while not self.closed.wait(timeout=0.01):
                    pass
                raise RuntimeError("curl: (92) HTTP/2 stream was not closed cleanly")
                yield b""

            def close(self) -> None:
                self.closed.set()

        response = ErrorOnCloseResponse()

        with self.assertRaises(TimeoutError) as ctx:
            list(iter_sse_payloads(response, deadline=time.time() + 0.05))

        self.assertIn("超时", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
