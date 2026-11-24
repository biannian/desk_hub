# Copyright (c) iGroup.
# All rights reserved.
"""独立查询当前登录 Codex 的账户额度。

@author Kai
@date 2026/11/03 14:00
@description 只调用初始化与额度读取，不创建会话或模型任务。
"""

import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def find_codex():
    """定位原生 Codex 程序，不通过 shell 执行命令。

    @return 原生可执行路径或空
    @author Kai
    @date 2026/11/03 14:00
    """
    native = shutil.which("codex.exe")
    if native:
        return native
    shim = shutil.which("codex.cmd")
    if shim:
        root = Path(shim).parent / "node_modules" / "@openai" / "codex"
        for candidate in root.glob("node_modules/@openai/codex-*/vendor/*/bin/codex.exe"):
            return str(candidate)
    return None


def normalize_limits(result):
    """将官方 camelCase 额度响应转换为看板窗口。

    @param result account/rateLimits/read 响应
    @return Codex 额度窗口列表
    @author Kai
    @date 2026/11/03 14:00
    """
    buckets = result.get("rateLimitsByLimitId") or {}
    quota = buckets.get("codex") or result.get("rateLimits") or {}
    if quota.get("limitId") not in (None, "codex"):
        return []
    windows = []
    for key in ("primary", "secondary"):
        item = quota.get(key)
        if not isinstance(item, dict):
            continue
        used, minutes, reset = item.get("usedPercent"), item.get("windowDurationMins"), item.get("resetsAt")
        if type(used) not in (int, float) or not 0 <= used <= 100:
            continue
        if type(minutes) not in (int, float) or minutes <= 0:
            continue
        windows.append({"minutes": minutes, "remaining_percent": round(100 - used, 1),
                        "resets_at": reset if type(reset) in (int, float) else None})
    return windows


async def read_account_limits(executable=None):
    """限时启动 stdio 服务读取额度，并回收子进程。

    @param executable 可选原生程序路径
    @return 官方查询结果
    @author Kai
    @date 2026/11/03 14:00
    """
    executable = executable or find_codex()
    if not executable:
        raise RuntimeError("codex_unavailable")
    process = await asyncio.create_subprocess_exec(
        executable, "app-server", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        limit=1024 * 1024,
    )

    async def send(message):
        """发送协议消息。@param message JSON 消息 @author Kai @date 2026/11/03 14:00"""
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()

    async def receive(request_id):
        """等待指定响应，忽略通知。@param request_id 请求号 @return 结果 @author Kai @date 2026/11/03 14:00"""
        while True:
            line = await process.stdout.readline()
            if not line:
                raise RuntimeError("codex_closed")
            response = json.loads(line)
            if response.get("id") == request_id:
                if "error" in response:
                    # 不输出原始错误，避免认证或账户信息进入日志。
                    raise RuntimeError("quota_query_failed")
                return response.get("result", {})

    try:
        async with asyncio.timeout(20):
            await send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "desk_hub", "version": "1.0"}}})
            await receive(1)
            await send({"method": "initialized"})
            await send({"id": 2, "method": "account/rateLimits/read"})
            return await receive(2)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()


class QuotaReader:
    """独立于任务的一分钟额度轮询器。

    @author Kai
    @date 2026/11/03 14:00
    @description 失败保留上次快照，不用日志活跃度代替账户额度。
    """

    def __init__(self, cache):
        """初始化持久化快照。@param cache 看板缓存路径 @author Kai @date 2026/11/03 14:00"""
        self.cache = cache
        self.snapshot = {}
        self.status = "pending"
        # 由看板按配置决定是否查询；默认允许，关闭后跳过本轮。
        self.allowed = lambda: True
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and isinstance(saved.get("limits"), list):
                self.snapshot = saved
        except (OSError, ValueError):
            pass

    async def refresh(self):
        """刷新真实额度；失败保留历史读数。@author Kai @date 2026/11/03 14:00"""
        try:
            result = await read_account_limits()
            windows = normalize_limits(result)
            if not windows:
                raise RuntimeError("quota_missing")
            self.snapshot = {"limits": windows, "limits_updated_at": datetime.now(timezone.utc).isoformat(), "quota_source": "account_query"}
            self.status = "ok"
            temporary = self.cache.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.snapshot), encoding="utf-8")
            temporary.replace(self.cache)
        except (OSError, ValueError, RuntimeError, TimeoutError, AttributeError, TypeError):
            self.status = "unavailable"

    async def run(self):
        """启动即查询，此后每分钟刷新，不依赖任何任务。@author Kai @date 2026/11/03 14:00"""
        while True:
            if self.allowed():
                await self.refresh()
            await asyncio.sleep(60)

    def merge(self, usage):
        """优先合并较新的账户额度，保留 Token 计数。

        @param usage 日志用量快照
        @return 合并后的快照
        @author Kai
        @date 2026/11/03 14:00
        """
        merged = dict(usage)
        if self.snapshot.get("limits"):
            live = datetime.fromisoformat(self.snapshot["limits_updated_at"].replace("Z", "+00:00"))
            logged = usage.get("limits_updated_at")
            if not logged or live >= datetime.fromisoformat(logged.replace("Z", "+00:00")):
                merged.update(self.snapshot)
        merged["quota_query_status"] = self.status
        return merged
