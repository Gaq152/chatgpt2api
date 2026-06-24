from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from services.account_service import account_service
from services.config import DATA_DIR
from services.register import openai_register


REGISTER_FILE = DATA_DIR / "register.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict:
    return {**openai_register.config, "mode": "total", "target_quota": 100, "target_available": 10, "check_interval": 5, "auto_replenish": False, "replenish_interval": 30, "enabled": False, "stats": {"success": 0, "fail": 0, "done": 0, "running": 0, "threads": openai_register.config["threads"], "elapsed_seconds": 0, "avg_seconds": 0, "success_rate": 0, "current_quota": 0, "current_available": 0, "phase": "idle"}}


def _normalize(raw: dict) -> dict:
    cfg = _default_config()
    cfg.update({k: v for k, v in raw.items() if k not in {"stats", "logs"}})
    cfg["total"] = max(1, int(cfg.get("total") or 1))
    cfg["threads"] = max(1, int(cfg.get("threads") or 1))
    cfg["mode"] = str(cfg.get("mode") or "total").strip() if str(cfg.get("mode") or "total").strip() in {"total", "quota", "available"} else "total"
    cfg["target_quota"] = max(1, int(cfg.get("target_quota") or 1))
    cfg["target_available"] = max(1, int(cfg.get("target_available") or 1))
    cfg["check_interval"] = max(1, int(cfg.get("check_interval") or 5))
    cfg["auto_replenish"] = bool(cfg.get("auto_replenish"))
    cfg["replenish_interval"] = max(1, int(cfg.get("replenish_interval") or 30))
    cfg["proxy"] = str(cfg.get("proxy") or "").strip()
    cfg["enabled"] = bool(cfg.get("enabled"))
    stats = {**_default_config()["stats"], **(raw.get("stats") if isinstance(raw.get("stats"), dict) else {}),
             "threads": cfg["threads"]}
    cfg["stats"] = stats
    return cfg


class RegisterService:
    def __init__(self, store_file: Path):
        self._store_file = store_file
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._logs: list[dict] = []
        openai_register.register_log_sink = self._append_log
        self._config = self._load()
        if self._config["enabled"]:
            self.start()

    def _load(self) -> dict:
        try:
            return _normalize(json.loads(self._store_file.read_text(encoding="utf-8")))
        except Exception:
            return _normalize({})

    def _save(self) -> None:
        self._store_file.parent.mkdir(parents=True, exist_ok=True)
        self._store_file.write_text(json.dumps(self._config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def get(self) -> dict:
        with self._lock:
            return json.loads(json.dumps({**self._config, "logs": self._logs[-300:]}, ensure_ascii=False))

    def get_json(self) -> str:
        # 直接返回 JSON 字符串，仅一次序列化（避免 get() 的 loads(dumps()) 双重转换）。
        # SSE 端点用 asyncio.to_thread 调用此方法，使序列化与锁等待不阻塞事件循环。
        with self._lock:
            return json.dumps({**self._config, "logs": self._logs[-300:]}, ensure_ascii=False)

    def _inject_proxy_to_mail(self) -> None:
        proxy = str(self._config.get("proxy") or "").strip()
        if proxy and isinstance(self._config.get("mail"), dict):
            self._config["mail"]["proxy"] = proxy

    def update(self, updates: dict) -> dict:
        with self._lock:
            self._config = _normalize({**self._config, **updates})
            self._inject_proxy_to_mail()
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
            self._save()
            return self.get()

    def start(self) -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                self._config["enabled"] = True
                self._save()
                return self.get()
            self._config["enabled"] = True
            self._inject_proxy_to_mail()
            self._logs = []
            metrics = self._pool_metrics()
            self._config["stats"] = {"job_id": uuid.uuid4().hex, "success": 0, "fail": 0, "done": 0, "running": 0, "threads": self._config["threads"], **metrics, "phase": "registering", "started_at": _now(), "updated_at": _now()}
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
            self._save()
            self._runner = threading.Thread(target=self._run, daemon=True, name="openai-register")
            self._runner.start()
            ar_label = "，自动补充=开启" if self._config.get("auto_replenish") else ""
            self._append_log(f"注册任务启动，模式={self._config['mode']}，线程数={self._config['threads']}{ar_label}", "yellow")
            if self._config.get("auto_replenish") and self._config["mode"] == "total":
                self._append_log("注意：注册总数模式下自动补充无效，完成后将停止", "yellow")
            return self.get()

    def stop(self) -> dict:
        with self._lock:
            self._config["enabled"] = False
            self._config["stats"]["updated_at"] = _now()
            self._save()
            self._append_log("已请求停止注册任务，正在等待当前运行任务结束", "yellow")
            return self.get()

    def reset(self) -> dict:
        with self._lock:
            self._logs = []
            self._config["stats"] = {"success": 0, "fail": 0, "done": 0, "running": 0, "threads": self._config["threads"], "elapsed_seconds": 0, "avg_seconds": 0, "success_rate": 0, **self._pool_metrics(), "updated_at": _now()}
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": 0.0})
            self._save()
            return self.get()

    def _append_log(self, text: str, color: str = "") -> None:
        with self._lock:
            self._logs.append({"time": _now(), "text": str(text), "level": str(color or "info")})
            self._logs = self._logs[-300:]

    def _pool_metrics(self) -> dict:
        items = account_service.list_accounts()
        normal = [item for item in items if item.get("status") == "正常"]
        return {
            "current_quota": sum(int(item.get("quota") or 0) for item in normal if not item.get("image_quota_unknown")),
            "current_available": len(normal),
        }

    def _target_reached(self, cfg: dict, submitted: int) -> bool:
        mode = str(cfg.get("mode") or "total")
        metrics = self._pool_metrics()
        self._bump(**metrics)
        if mode == "quota":
            reached = metrics["current_quota"] >= int(cfg.get("target_quota") or 1)
            self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，当前剩余额度={metrics['current_quota']}，目标额度={cfg.get('target_quota')}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        if mode == "available":
            reached = metrics["current_available"] >= int(cfg.get("target_available") or 1)
            self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，目标账号={cfg.get('target_available')}，当前剩余额度={metrics['current_quota']}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        return submitted >= int(cfg.get("total") or 1)

    def _bump(self, **updates) -> None:
        with self._lock:
            self._config["stats"].update(updates)
            stats = self._config["stats"]
            started_at = str(stats.get("started_at") or "")
            if started_at:
                try:
                    elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
                except Exception:
                    elapsed = 0.0
                done = int(stats.get("done") or 0)
                success = int(stats.get("success") or 0)
                fail = int(stats.get("fail") or 0)
                stats["elapsed_seconds"] = round(elapsed, 1)
                stats["avg_seconds"] = round(elapsed / success, 1) if success else 0
                stats["success_rate"] = round(success * 100 / max(1, success + fail), 1)
            self._config["stats"]["updated_at"] = _now()
            self._save()

    def _run(self) -> None:
        threads = int(self.get()["threads"])
        total_done, total_success, total_fail = 0, 0, 0

        while True:
            cfg = self.get()
            if not cfg["enabled"]:
                break

            self._bump(phase="registering")
            submitted, done, success, fail = 0, 0, 0, 0
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})

            with ThreadPoolExecutor(max_workers=threads) as executor:
                futures: set = set()
                while True:
                    cfg = self.get()
                    while cfg["enabled"] and not self._target_reached(cfg, submitted) and len(futures) < threads:
                        submitted += 1
                        futures.add(executor.submit(openai_register.worker, submitted))
                    self._bump(running=len(futures), done=total_done + done, success=total_success + success, fail=total_fail + fail)
                    if not futures and (not cfg["enabled"] or self._target_reached(cfg, submitted)):
                        break
                    if not futures:
                        time.sleep(max(1, int(cfg.get("check_interval") or 5)))
                        continue
                    finished, futures = wait(futures, return_when=FIRST_COMPLETED)
                    for future in finished:
                        done += 1
                        try:
                            result = future.result()
                            success += 1 if result.get("ok") else 0
                            fail += 0 if result.get("ok") else 1
                        except Exception:
                            fail += 1

            total_done += done
            total_success += success
            total_fail += fail
            self._bump(running=0, done=total_done, success=total_success, fail=total_fail)

            cfg = self.get()
            if not cfg["enabled"] or not cfg.get("auto_replenish") or cfg["mode"] == "total":
                break

            interval_minutes = max(1, int(cfg.get("replenish_interval") or 30))
            self._bump(phase="monitoring")
            self._append_log(f"注册批次完成（成功{success}，失败{fail}），进入监控模式，每 {interval_minutes} 分钟检查一次号池", "yellow")

            for _ in range(interval_minutes * 60):
                if not self.get()["enabled"]:
                    break
                time.sleep(1)

            if not self.get()["enabled"]:
                break

            metrics = self._pool_metrics()
            self._bump(**metrics)
            cfg = self.get()

            if self._target_reached(cfg, 0):
                self._append_log(f"号池检查：目标已满足（额度={metrics['current_quota']}，可用={metrics['current_available']}），继续监控", "yellow")
                self._bump(phase="monitoring")
                while self.get()["enabled"]:
                    interval_minutes = max(1, int(self.get().get("replenish_interval") or 30))
                    for _ in range(interval_minutes * 60):
                        if not self.get()["enabled"]:
                            break
                        time.sleep(1)
                    if not self.get()["enabled"]:
                        break
                    metrics = self._pool_metrics()
                    self._bump(**metrics)
                    if not self._target_reached(self.get(), 0):
                        self._append_log(f"号池检查：目标未满足（额度={metrics['current_quota']}，可用={metrics['current_available']}），启动补充注册", "yellow")
                        break
                    self._append_log(f"号池检查：目标已满足（额度={metrics['current_quota']}，可用={metrics['current_available']}），继续监控", "yellow")
                if not self.get()["enabled"]:
                    break
            else:
                self._append_log(f"号池检查：目标未满足（额度={metrics['current_quota']}，可用={metrics['current_available']}），启动补充注册", "yellow")

        self._bump(running=0, done=total_done, success=total_success, fail=total_fail, phase="idle", finished_at=_now())
        with self._lock:
            self._config["enabled"] = False
            self._save()
        self._append_log(f"注册任务结束，累计成功{total_success}，累计失败{total_fail}", "yellow")


register_service = RegisterService(REGISTER_FILE)
