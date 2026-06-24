import json
import tempfile
import unittest
from pathlib import Path


class RegisterServiceGetJsonTests(unittest.TestCase):
    def _make_service(self):
        # 写入 enabled=false 的临时配置，避免构造时启动真实注册线程
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        store = Path(tmp_dir.name) / "register.json"
        store.write_text(json.dumps({"enabled": False}), encoding="utf-8")

        from services.register_service import RegisterService

        return RegisterService(store)

    def test_get_json_matches_get(self) -> None:
        svc = self._make_service()
        svc._append_log("hello", "yellow")
        svc._append_log("world")

        # get_json 单次序列化的结果，反序列化后应与 get() 完全等价
        self.assertEqual(json.loads(svc.get_json()), svc.get())

        data = json.loads(svc.get_json())
        self.assertIn("logs", data)
        texts = [item["text"] for item in data["logs"]]
        self.assertEqual(texts[-2:], ["hello", "world"])

    def test_get_json_caps_logs_at_300(self) -> None:
        svc = self._make_service()
        for i in range(350):
            svc._append_log(f"log-{i}")

        data = json.loads(svc.get_json())
        self.assertEqual(len(data["logs"]), 300)
        # 应保留最近 300 条
        self.assertEqual(data["logs"][-1]["text"], "log-349")
        self.assertEqual(data["logs"][0]["text"], "log-50")


if __name__ == "__main__":
    unittest.main()
