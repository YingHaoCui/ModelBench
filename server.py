"""Local, unauthenticated UI/API. Workers run in isolated Python subprocesses."""
import argparse
import asyncio
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from bench import parser
from workloads import PROFILES, body_for, initial_messages

ROOT = Path(__file__).resolve().parent
DEFAULTS = vars(parser().parse_args(["--model", ""]))
PRIVATE = {"output", "run_id", "dry_run", "replay"}


def validate(payload):
    if not isinstance(payload, dict) or set(payload) - (set(DEFAULTS) - PRIVATE):
        raise ValueError("配置包含未知字段")
    config = {k: v for k, v in DEFAULTS.items() if k not in PRIVATE}
    config.update(payload)
    for key, value in config.items():
        default = DEFAULTS[key]
        if isinstance(default, bool):
            valid = type(value) is bool
        elif isinstance(default, (float, int)):
            valid = type(value) in (int, float) and math.isfinite(value)
            if key not in ("timeout", "duration", "ramp_seconds", "think_seconds"):
                valid = valid and type(value) is int
        else:
            valid = isinstance(value, type(default))
        if not valid:
            raise ValueError(f"{key}: 类型不正确")
    bounds = {"concurrency": (1, 2000), "sessions": (1, 1000000), "turns": (1, 1000),
              "projects": (1, 1000000), "context_chars": (1, 2000000), "tool_chars": (1, 1000000),
              "history_turns": (0, 100), "max_tokens": (1, 1000000), "seed": (0, 2**32 - 1),
              "duration": (0, 86400), "timeout": (1, 3600), "ramp_seconds": (0, 3600),
              "think_seconds": (0, 3600)}
    for key, (low, high) in bounds.items():
        if not low <= config[key] <= high:
            raise ValueError(f"{key}: 应在 {low}–{high} 之间")
    parsed = urlsplit(config["base_url"])
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url 必须是无凭证、无查询参数的 HTTP(S) 地址")
    if not config["model"].strip() or len(config["model"]) > 256:
        raise ValueError("请输入模型名称（最多 256 字符）")
    if not config["profiles"] or any(p not in PROFILES for p in config["profiles"]):
        raise ValueError("至少选择一个有效工具画像")
    if config["prefix_mode"] not in ("shared", "project", "unique") or config["affinity"] not in ("stable", "rotate"):
        raise ValueError("前缀或亲和模式无效")
    for key in ("session_header", "backend_header"):
        if config[key] and not re.fullmatch(r"[A-Za-z0-9-]{1,100}", config[key]):
            raise ValueError(f"{key}: 请求头名称无效")
    if config["session_header"].lower() in ("authorization", "host", "content-length", "content-type", "connection", "transfer-encoding"):
        raise ValueError("不能用保留请求头传递 session")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}", config["api_key_env"]):
        raise ValueError("API Key 环境变量名称无效")
    return config


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_key(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError("API Key 格式无效")
    return value.strip()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created TEXT NOT NULL, status TEXT NOT NULL, config TEXT NOT NULL, summary TEXT, error TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS providers (id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL, model TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL)")
        self.db.execute("PRAGMA user_version=1")
        self.db.execute("UPDATE runs SET status='interrupted', error='服务重启，原任务已中断' WHERE status IN ('running','stopping')")
        self.db.commit()

    def provider(self, pid, secret=False):
        row = self.db.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise web.HTTPNotFound(text="供应商不存在")
        result = dict(row)
        result["has_api_key"] = bool(result["api_key"])
        if not secret:
            result.pop("api_key")
        return result

    def save_provider(self, payload, pid=None):
        if not isinstance(payload, dict) or set(payload) - {"name", "base_url", "model", "api_key", "clear_api_key"}:
            raise ValueError("供应商配置包含未知字段")
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("请输入供应商名称（最多 100 字符）")
        config = validate({"base_url": payload.get("base_url"), "model": payload.get("model")})
        key = validate_key(payload.get("api_key", ""))
        clear = payload.get("clear_api_key", False)
        if type(clear) is not bool or (clear and key):
            raise ValueError("清除密钥与新密钥不能同时设置")
        old = self.provider(pid, secret=True) if pid else None
        if old and old["api_key"] and old["base_url"] != config["base_url"] and not key and not clear:
            raise ValueError("更改 API 地址时请重新输入或清除密钥")
        if old and not key and not clear:
            key = old["api_key"]
        pid = pid or str(uuid.uuid4())
        self.db.execute("INSERT INTO providers VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, base_url=excluded.base_url, model=excluded.model, api_key=excluded.api_key, updated=excluded.updated",
                        (pid, name.strip(), config["base_url"], config["model"], key, now()))
        self.db.commit()
        return self.provider(pid)

    def insert(self, rid, config):
        self.db.execute("INSERT INTO runs VALUES (?,?,?,?,NULL,NULL)", (rid, now(), "running", json.dumps(config)))
        self.db.commit()

    def update(self, rid, status, summary=None, error=None):
        self.db.execute("UPDATE runs SET status=?, summary=?, error=? WHERE id=?", (status, json.dumps(summary) if summary is not None else None, error, rid))
        self.db.commit()

    def get(self, rid):
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise web.HTTPNotFound(text="任务不存在")
        result = dict(row)
        result["config"] = json.loads(result["config"])
        result["summary"] = json.loads(result["summary"]) if result["summary"] else None
        return result


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Manager:
    def __init__(self, data):
        self.data = Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data / "modelbench.sqlite3")
        self.process = None
        self.task = None
        self.active_id = None
        self.lock = asyncio.Lock()

    async def start(self, config, api_key=""):
        async with self.lock:
            if self.active_id:
                raise web.HTTPConflict(text="已有任务运行中，请先停止或等待完成")
            rid = str(uuid.uuid4())
            directory = self.data / rid
            directory.mkdir()
            command = [sys.executable, str(ROOT / "bench.py"), "--output", str(self.data), "--run-id", rid]
            for key, value in config.items():
                flag = "--" + key.replace("_", "-")
                if isinstance(value, bool):
                    if value:
                        command.append(flag)
                elif isinstance(value, list):
                    command.extend([flag, *value])
                else:
                    command.append(f"{flag}={value}")
            self.store.insert(rid, config)
            environment = os.environ.copy()
            if api_key:
                environment[config["api_key_env"]] = api_key
            try:
                with (directory / "worker.log").open("wb") as log:
                    self.process = await asyncio.create_subprocess_exec(*command, cwd=ROOT, stdout=log, stderr=log, env=environment,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            except Exception:
                self.store.update(rid, "failed", error="无法启动工作进程")
                raise web.HTTPInternalServerError(text="无法启动工作进程")
            self.active_id = rid
            self.task = asyncio.create_task(self.watch(rid, self.process))
            return rid

    async def watch(self, rid, process):
        try:
            code = await process.wait()
            row = self.store.get(rid)
            summary = read_json(self.data / rid / "summary.json")
            status = "cancelled" if row["status"] == "stopping" or code == 130 else "completed" if code == 0 else "failed"
            self.store.update(rid, status, summary, f"工作进程退出码 {code}" if status == "failed" else None)
        finally:
            self.active_id = None
            self.process = None

    def stop(self, rid):
        row = self.store.get(rid)
        if row["status"] in ("running", "stopping"):
            (self.data / rid / "stop").touch()
            self.store.update(rid, "stopping")

    async def close(self):
        if self.active_id:
            self.stop(self.active_id)
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=8)
            except asyncio.TimeoutError:
                if self.process and self.process.returncode is None:
                    self.process.terminate()
                await self.task
        self.store.db.close()


@web.middleware
async def local_only(request, handler):
    # No login/auth. Reject cross-origin browser mutations and DNS-rebinding Hosts.
    if request.url.host not in ("127.0.0.1", "localhost", "::1"):
        raise web.HTTPForbidden(text="仅支持本机访问")
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="不允许跨站请求")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="请使用 application/json")
    try:
        return await handler(request)
    except (ValueError, TypeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


def create_app(data=ROOT / "data"):
    app = web.Application(middlewares=[local_only], client_max_size=1024**2)
    manager = Manager(data)
    app[web.AppKey("manager", Manager)] = manager

    async def defaults(request):
        return web.json_response({k: v for k, v in DEFAULTS.items() if k not in PRIVATE})

    async def preview(request):
        config = validate(await request.json())
        args = argparse.Namespace(**config)
        profile = config["profiles"][0]
        return web.json_response(body_for(args, initial_messages(profile, 0, args), profile))

    async def start(request):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("请求必须是对象")
        api_key = validate_key(payload.pop("api_key", ""))
        provider_id = payload.pop("provider_id", None)
        if provider_id is not None:
            if not isinstance(provider_id, str):
                raise ValueError("供应商 ID 无效")
            provider = manager.store.provider(provider_id, secret=True)
            if payload.get("base_url", provider["base_url"]) != provider["base_url"]:
                raise ValueError("地址与所选供应商不一致，请选择手动配置或更新供应商")
            payload.setdefault("base_url", provider["base_url"])
            payload.setdefault("model", provider["model"])
            api_key = api_key or provider["api_key"]
        rid = await manager.start(validate(payload), api_key)
        return web.json_response(manager.store.get(rid), status=201)

    async def providers(request):
        ids = manager.store.db.execute("SELECT id FROM providers ORDER BY name, id").fetchall()
        return web.json_response([manager.store.provider(row["id"]) for row in ids])

    async def save_provider(request):
        return web.json_response(manager.store.save_provider(await request.json(), request.match_info.get("pid")))

    async def delete_provider(request):
        pid = request.match_info["pid"]
        manager.store.provider(pid)
        manager.store.db.execute("DELETE FROM providers WHERE id=?", (pid,))
        manager.store.db.commit()
        return web.json_response({"deleted": True})

    async def listing(request):
        rows = manager.store.db.execute("SELECT id, created, status, error FROM runs ORDER BY created DESC LIMIT 100").fetchall()
        return web.json_response([dict(r) for r in rows])

    async def detail(request):
        row = manager.store.get(request.match_info["rid"])
        row["progress"] = read_json(manager.data / row["id"] / "progress.json")
        return web.json_response(row)

    async def stop(request):
        rid = request.match_info["rid"]
        manager.stop(rid)
        return web.json_response(manager.store.get(rid))

    async def artifact(request):
        row = manager.store.get(request.match_info["rid"])
        name = request.match_info["name"]
        if name not in ("summary.json", "requests.jsonl"):
            raise web.HTTPNotFound()
        path = manager.data / row["id"] / name
        if not path.is_file():
            raise web.HTTPNotFound(text="报告尚未生成")
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def index(request):
        return web.FileResponse(ROOT / "frontend" / "index.html")

    async def cleanup(app):
        await manager.close()

    app.add_routes([web.get("/", index), web.get("/api/defaults", defaults),
                    web.get("/api/providers", providers), web.post("/api/providers", save_provider),
                    web.post("/api/providers/{pid}", save_provider),
                    web.post("/api/providers/{pid}/delete", delete_provider),
                    web.post("/api/preview", preview), web.get("/api/runs", listing),
                    web.post("/api/runs", start), web.get("/api/runs/{rid}", detail),
                    web.post("/api/runs/{rid}/stop", stop),
                    web.get("/api/runs/{rid}/artifacts/{name}", artifact)])
    app.router.add_static("/assets/", ROOT / "frontend")
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--port", type=int, default=8765)
    cli.add_argument("--data", type=Path, default=ROOT / "data")
    options = cli.parse_args()
    web.run_app(create_app(options.data), host="127.0.0.1", port=options.port)
