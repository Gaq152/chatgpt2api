import unittest
from unittest import mock


class MailProviderRateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        from services.register import mail_provider

        mail_provider.tempmail_lol_rate_state.clear()

    def test_rate_limited_tempmail_provider_falls_back_to_next_provider(self) -> None:
        from services.register import mail_provider

        class LimitedProvider(mail_provider.BaseMailProvider):
            name = "tempmail_lol"

            def __init__(self):
                super().__init__({}, "tempmail_lol#1")

            def create_mailbox(self, username=None):
                raise RuntimeError(
                    'TempMail.lol 请求失败: POST /inbox/create, HTTP 429, body={"error":"Rate limited (free)"}'
                )

        class BackupProvider(mail_provider.BaseMailProvider):
            name = "cloudmail_gen"

            def __init__(self):
                super().__init__({}, "cloudmail_gen#2")

            def create_mailbox(self, username=None):
                return {"provider": self.name, "provider_ref": self.provider_ref, "address": "user@example.test"}

        providers = [LimitedProvider(), BackupProvider()]
        with (
            mock.patch.object(mail_provider, "_enabled_entries", return_value=[{"type": "tempmail_lol"}, {"type": "cloudmail_gen"}]),
            mock.patch.object(mail_provider, "_create_provider", side_effect=providers),
        ):
            mailbox = mail_provider.create_mailbox({"providers": []}, "user")

        self.assertEqual(mailbox["provider"], "cloudmail_gen")
        self.assertEqual(mailbox["address"], "user@example.test")

    def test_tempmail_basic_level_allows_25_creates_per_five_minutes(self) -> None:
        from services.register import mail_provider

        provider = mail_provider.TempMailLolProvider(
            {"type": "tempmail_lol", "provider_ref": "tempmail_lol#rate-test", "account_level": "basic"},
            {"request_timeout": 1, "user_agent": "test"},
        )

        responses = [
            {"address": f"user{i}@tempmail.test", "token": f"token-{i}"}
            for i in range(26)
        ]
        with mock.patch.object(provider, "_request", side_effect=responses):
            for _ in range(25):
                provider.create_mailbox()

            with self.assertRaises(RuntimeError) as ctx:
                provider.create_mailbox()

        self.assertIn("冷却", str(ctx.exception))
        self.assertGreater(getattr(ctx.exception, "retry_after_seconds", 0), 0)

    def test_tempmail_rate_limit_is_shared_by_account_key(self) -> None:
        from services.register import mail_provider

        first = mail_provider.TempMailLolProvider(
            {"type": "tempmail_lol", "provider_ref": "tempmail_lol#shared-a", "api_key": "same-key", "account_level": "basic"},
            {"request_timeout": 1, "user_agent": "test"},
        )
        second = mail_provider.TempMailLolProvider(
            {"type": "tempmail_lol", "provider_ref": "tempmail_lol#shared-b", "api_key": "same-key", "account_level": "basic"},
            {"request_timeout": 1, "user_agent": "test"},
        )

        responses = [{"address": f"user{i}@tempmail.test", "token": f"token-{i}"} for i in range(26)]
        with (
            mock.patch.object(first, "_request", side_effect=responses),
            mock.patch.object(second, "_request", side_effect=responses),
        ):
            for i in range(25):
                (first if i % 2 == 0 else second).create_mailbox()

            with self.assertRaises(RuntimeError) as ctx:
                second.create_mailbox()

        self.assertIn("冷却", str(ctx.exception))

    def test_tempmail_without_api_key_uses_basic_limit(self) -> None:
        from services.register import mail_provider

        provider = mail_provider.TempMailLolProvider(
            {"type": "tempmail_lol", "provider_ref": "tempmail_lol#free-plus", "account_level": "plus"},
            {"request_timeout": 1, "user_agent": "test"},
        )

        responses = [{"address": f"user{i}@tempmail.test", "token": f"token-{i}"} for i in range(26)]
        with mock.patch.object(provider, "_request", side_effect=responses):
            for _ in range(25):
                provider.create_mailbox()

            with self.assertRaises(RuntimeError) as ctx:
                provider.create_mailbox()

        self.assertIn("冷却", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
