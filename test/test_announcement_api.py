import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.system as system_module


class AnnouncementApiTests(unittest.TestCase):
    def test_announcement_endpoint_allows_non_admin_identity(self) -> None:
        fake_config = SimpleNamespace(
            get_public_announcement_settings=lambda: {
                "enabled": True,
                "message": "系统维护中，请稍后再试",
            },
        )
        with (
            mock.patch.object(system_module, "config", fake_config),
            mock.patch.object(system_module, "require_identity", return_value={"id": "user-1", "role": "user"}),
        ):
            app = FastAPI()
            app.include_router(system_module.create_router("test"))
            client = TestClient(app)

            response = client.get("/api/announcement", headers={"Authorization": "Bearer user-key"})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "announcement": {
                    "enabled": True,
                    "message": "系统维护中，请稍后再试",
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
