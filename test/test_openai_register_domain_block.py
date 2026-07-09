import unittest
from unittest import mock


class FakeResponse:
    status_code = 400

    def json(self):
        return {
            "error": {
                "message": "Sorry, we cannot create your account with the given information.",
                "type": "invalid_request_error",
                "param": None,
                "code": "registration_disallowed",
            }
        }


class OpenAIRegisterDomainBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        from services.register import openai_register

        with openai_register._blocked_domains_lock:
            openai_register._blocked_domains.clear()
            openai_register._blocked_domains_loaded = True

    def test_registration_disallowed_blocks_current_email_domain(self) -> None:
        from services.register import openai_register

        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.session = object()
        registrar.device_id = "device"
        registrar._current_email = "user@blocked.example"

        with (
            mock.patch.object(openai_register, "build_sentinel_token", return_value="sentinel"),
            mock.patch.object(openai_register, "request_with_local_retry", return_value=(FakeResponse(), "")),
            mock.patch.object(openai_register, "_persist_blocked_domains"),
        ):
            with self.assertRaises(RuntimeError):
                registrar._create_account("Test User", "2000-01-01", 1)

        blocked = openai_register.list_blocked_domains()
        self.assertEqual([item["domain"] for item in blocked], ["blocked.example"])
        self.assertIn("registration_disallowed", blocked[0]["reason"])


if __name__ == "__main__":
    unittest.main()
