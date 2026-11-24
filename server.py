# Copyright (c) iGroup.
# All rights reserved.
"""独立局域网看板服务。

@author Kai
@date 2026/10/23 15:51
@description 令牌认证、只读文件桥、SQLite 和 WebSocket；不修改助手功能。
"""

import asyncio
import hashlib
import hmac
import secrets
import json
import os
import sys
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .core import STATES, TZ, Store, attendance
from .cursor_quota import CursorQuota
from .local_codex import LocalCodex
from .local_cursor import LocalCursor
from .quota_reader import QuotaReader

# 独立数据目录与配置，避免触碰原助手配置和数据库。
ROOT = Path(__file__).resolve().parent
# @author Kai @date 2026/10/23 16:57：打包后资源只读，数据保存在 EXE 旁的独立目录。
RUNTIME = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT
DATA = Path(os.environ.get("DESK_HUB_DATA", RUNTIME / "desk_hub-data" if getattr(sys, "frozen", False) else ROOT / "data"))
DATA.mkdir(parents=True, exist_ok=True)
SOURCE = Path(os.environ.get("DESK_HUB_SOURCE", RUNTIME if getattr(sys, "frozen", False) else ROOT.parent / "windows" / "dist"))
TOKEN_FILE = DATA / "access-token.txt"
if not TOKEN_FILE.exists():
    TOKEN_FILE.write_text(secrets.token_urlsafe(32), encoding="utf-8")
TOKEN = os.environ.get("DESK_HUB_TOKEN") or TOKEN_FILE.read_text().strip()
ADAPTER_TOKEN = os.environ.get("DESK_HUB_ADAPTER_TOKEN", TOKEN)
STORE = Store(DATA / "hub.sqlite3")
# 本机观察器只读取生命周期，绝不写入 Codex 会话文件。
# 服务重启后恢复最近额度；持久化仅写看板数据目录。
# @author Kai @date 2026/11/03 08:48
OBSERVER = LocalCodex(usage_cache=DATA / "usage-snapshot.json")
# 本机 Cursor 会话与账户余量，和 Codex 一样只读，不写回 Cursor。
# @author Kai @date 2026/11/20 09:25
CURSOR_ROOT = os.environ.get("DESK_HUB_CURSOR_ROOT")
CURSOR = LocalCursor(Path(CURSOR_ROOT) if CURSOR_ROOT else None)
CURSOR_QUOTA = CursorQuota(DATA / "cursor-quota.json")
# 账户额度查询不依赖任务日志。
QUOTA = QuotaReader(DATA / "account-quota.json")
# 配置可随时关掉某个 AI，查询循环按当时的选择跳过。
# @author Kai @date 2026/11/20 09:40
QUOTA.allowed = lambda: "codex" in enabled_providers()
CURSOR_QUOTA.allowed = lambda: "cursor" in enabled_providers()


def configured_token() -> str:
    """读取助手设置中的看板访问凭据。

    @return 用户配置的令牌，未配置时沿用默认令牌
    @author Kai
    @date 2026/10/30 10:44
    """
    # 每次认证读取配置，使设置保存后立即生效；环境变量仍优先。
    if os.environ.get("DESK_HUB_TOKEN"):
        return os.environ["DESK_HUB_TOKEN"]
    try:
        config = json.loads((SOURCE / "config.json").read_text(encoding="utf-8-sig"))
        value = str(config.get("desk_hub_token", "")).strip()
        return value or TOKEN
    except (OSError, ValueError, TypeError, AttributeError):
        return TOKEN


class AgentEvent(BaseModel):
    """统一授权事件。@author Kai @date 2026/10/23 15:51 @description 只收摘要，不收提示词和代码。"""

    # 标识字段控制幂等和不同工具间的任务隔离。
    event_id: str = Field(min_length=1, max_length=160)
    task_id: str = Field(min_length=1, max_length=160)
    tool: str = Field(min_length=1, max_length=40)
    title: str = Field(default="未命名任务", max_length=160)
    state: str
    occurred_at: datetime
    # 摘要由调用方主动提供，默认不传原始内容。
    summary: str = Field(default="", max_length=500)
    reason: str = Field(default="", max_length=300)
    source: str = Field(default="manual", max_length=100)
    confidence: str = Field(default="reported", max_length=40)

    @field_validator("state")
    @classmethod
    def valid_state(cls, value):
        """拒绝未定义状态。@param value 状态 @return 校验值 @author Kai @date 2026/10/23 15:51"""
        if value not in STATES:
            raise ValueError("未知任务状态")
        return value

    @field_validator("occurred_at")
    @classmethod
    def valid_time(cls, value):
        """规范时间以稳定排序。@param value 事件时间 @return 中国时区时间 @author Kai @date 2026/10/23 15:51"""
        if value.tzinfo is None:
            raise ValueError("事件时间必须带时区")
        return value.astimezone(TZ)


# 看板可选的本机 AI。未配置时两个都监听。
PROVIDER_IDS = ("codex", "cursor")


def enabled_providers(source=None):
    """读取助手配置里要监听的 AI。

    @param source 可选助手目录
    @return 启用的提供方标识；配置缺失时返回全部
    @author Kai
    @date 2026/11/20 09:40
    """
    root = Path(source) if source else SOURCE
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
        chosen = config.get("desk_hub_providers", list(PROVIDER_IDS))
        if not isinstance(chosen, list):
            return list(PROVIDER_IDS)
        return [item for item in chosen if item in PROVIDER_IDS]
    except (OSError, ValueError, TypeError, AttributeError):
        return list(PROVIDER_IDS)


def visible_tasks(tasks, enabled):
    """隐藏已关闭 AI 的任务，保留手动和其他来源。

    @param tasks 任务或事件列表
    @param enabled 启用的提供方
    @return 仍应展示的记录
    @author Kai
    @date 2026/11/20 09:40
    """
    selected = {item.lower() for item in enabled}
    visible = []
    for task in tasks:
        tool = str(task.get("tool", "")).lower()
        if tool in PROVIDER_IDS and tool not in selected:
            continue
        visible.append(task)
    return visible


def snapshot():
    """读取最新看板快照。@return 完整数据 @author Kai @date 2026/10/23 15:51"""
    # 用量按来源分组，后续接入方提供各自窗口，不假定所有 AI 都有 5h/周额度。
    # @author Kai @date 2026/11/02 13:52
    enabled = enabled_providers()
    usages = []
    if "codex" in enabled:
        usages.append({**QUOTA.merge(OBSERVER.usage), "provider_id": "codex", "provider_name": "Codex",
                       "trend": OBSERVER.trend.snapshot})
    # Cursor 使用自己的账期窗口，不套用 Codex 的 5 小时和周额度。
    # @author Kai @date 2026/11/20 09:25
    if "cursor" in enabled:
        # 旧缓存里的「套餐额度」不再下发，Cursor 右侧只显示剩余比例。
        # @author Kai @date 2026/11/20 09:58
        limits = []
        for item in CURSOR_QUOTA.snapshot.get("limits") or []:
            if isinstance(item, dict):
                limits.append({key: value for key, value in item.items() if not (key == "label" and value in ("", "套餐额度"))})
        usages.append({**CURSOR_QUOTA.snapshot, "limits": limits, "provider_id": "cursor", "provider_name": "Cursor",
                       "quota_query_status": CURSOR_QUOTA.status, "trend": {}})
    usage = usages[0] if usages else {"provider_id": "", "provider_name": "", "limits": [], "trend": {}}
    cursor_adapter = "本机日志监听"
    if "cursor" not in enabled:
        cursor_adapter = "已关闭"
    elif CURSOR_QUOTA.status == "signed_out":
        cursor_adapter = "本机日志监听 · 未登录"
    elif CURSOR_QUOTA.status == "unavailable" and not CURSOR_QUOTA.snapshot.get("limits"):
        cursor_adapter = "本机日志监听 · 额度暂不可用"
    stored = STORE.snapshot()
    return {"usage": usage, "usages": usages, "providers": enabled, "attendance": attendance(SOURCE),
            "agents": visible_tasks(stored["agents"], enabled), "events": visible_tasks(stored["events"], enabled),
            "server_time": datetime.now(TZ).isoformat(),
            "adapters": {"cursor": cursor_adapter, "codex": "本机日志监听" if "codex" in enabled else "已关闭", "chatgpt": "未接入"}}


@asynccontextmanager
async def lifespan(app):
    """管理连接和推送任务。@param app 服务 @author Kai @date 2026/10/23 15:51"""
    app.state.clients = set()
    task = asyncio.create_task(broadcast())
    # 独立任务不阻塞两秒广播，退出时取消查询并回收子进程。
    # @author Kai @date 2026/11/03 14:00
    quota_task = asyncio.create_task(QUOTA.run())
    cursor_quota_task = asyncio.create_task(CURSOR_QUOTA.run())
    try:
        yield
    finally:
        task.cancel()
        quota_task.cancel()
        cursor_quota_task.cancel()
        for pending in (task, quota_task, cursor_quota_task):
            with suppress(asyncio.CancelledError):
                await pending



async def broadcast():
    """监听本地文件及事件库变化，不请求上游考勤。@author Kai @date 2026/10/23 15:51"""
    previous = ""
    while True:
        # @author Kai @date 2026/10/23 16:05：真实任务变化写入独立库后统一推送。
        enabled = enabled_providers()
        if "codex" in enabled:
            await asyncio.to_thread(OBSERVER.poll, STORE)
        if "cursor" in enabled:
            await asyncio.to_thread(CURSOR.poll, STORE)
        data = snapshot()
        digest = hashlib.sha256(json.dumps({k: v for k, v in data.items() if k != "server_time"}, sort_keys=True).encode()).hexdigest()
        message = {"type": "snapshot", "data": data} if digest != previous else {"type": "heartbeat"}
        previous = digest
        for client in tuple(app.state.clients):
            try:
                await asyncio.wait_for(client.send_json(message), timeout=3)
            except Exception:
                app.state.clients.discard(client)
        await asyncio.sleep(2)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """统一设置本地看板响应缓存策略。@param request 请求 @param call_next 下游 @return 响应 @author Kai @date 2026/10/23 15:51"""
    if request.url.path.startswith("/api/"):
        expected = os.environ.get("DESK_HUB_ADAPTER_TOKEN", configured_token()) if request.method == "POST" and request.url.path != "/api/agents/review" else configured_token()
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + expected):
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "未授权"}, status_code=401)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/")
async def index():
    """返回独立页面。@return HTML @author Kai @date 2026/10/23 15:51"""
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/{resource}")
async def get_resource(resource: str):
    """获取授权快照。@param resource 资源名 @return 快照或资源 @author Kai @date 2026/10/23 15:51"""
    data = snapshot()
    if resource == "dashboard":
        return data
    if resource not in {"attendance", "agents", "events"}:
        raise HTTPException(404)
    return data[resource]


@app.post("/api/agents/events")
async def ingest(event: AgentEvent):
    """保存授权适配器事件。@param event 事件 @return 幂等结果 @author Kai @date 2026/10/23 15:51"""
    return {"accepted": STORE.ingest(event.model_dump(mode="json"))}


class ReviewRequest(BaseModel):
    """用户查看确认。@author Kai @date 2026/10/23 16:38 @description 按完成事件确认，不影响新轮次。"""

    # 精确确认一次执行结果，避免确认另一轮的新完成提醒。
    event_id: str = Field(min_length=1, max_length=160)


@app.post("/api/agents/review")
async def review_result(event: ReviewRequest):
    """保存跨设备确认。@param event 结果事件 @return 确认状态 @author Kai @date 2026/10/23 16:38"""
    return {"reviewed": STORE.review(event.event_id)}


@app.websocket("/ws")
async def socket(ws: WebSocket):
    """连接后直接推送局域网看板快照。@param ws 连接 @author Kai @date 2026/10/23 15:51"""
    await ws.accept()
    try:
        auth = await asyncio.wait_for(ws.receive_json(), timeout=5)
        if not hmac.compare_digest(str(auth.get("token", "")), configured_token()):
            await ws.close(code=1008)
            return
        app.state.clients.add(ws)
        await ws.send_json({"type": "snapshot", "data": snapshot()})
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        app.state.clients.discard(ws)
