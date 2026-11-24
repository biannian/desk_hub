# Copyright (c) iGroup.
# All rights reserved.
"""Agent 事件转换与转发。@author Kai @date 2026/10/23 15:51 @description 只提取生命周期，不上传提示词和工具输入。"""

import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime

from .core import TZ


def convert(tool, raw):
    """将真实上游事件转换为统一状态。@param tool 工具 @param raw 原始事件 @return 统一事件或 None @author Kai @date 2026/10/23 15:51"""
    # Cursor stop 的 aborted/error 与完成严格分开；未知事件不猜测状态。
    params = raw.get("params", {}) if tool == "codex" else raw
    name = raw.get("method") if tool == "codex" else raw.get("hook_event_name")
    state = None
    task = params.get("threadId") or raw.get("conversation_id") or raw.get("session_id")
    if tool == "cursor":
        if name in {"sessionStart", "beforeSubmitPrompt"}:
            state = "running"
        elif name == "stop":
            state = {"completed": "completed", "aborted": "interrupted", "error": "failed"}.get(raw.get("status"), "unknown")
    elif tool == "codex":
        if name == "turn/started":
            state = "running"
        elif name == "turn/completed":
            state = {"completed": "completed", "failed": "failed", "interrupted": "interrupted"}.get(params.get("turn", {}).get("status"), "unknown")
        elif name in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/tool/requestUserInput"}:
            state = "waiting_user"
    if not task or not state:
        return None
    # 任务以一次执行轮次区分，避免下一轮执行沿用上一轮完成提醒去重。
    turn = params.get("turnId") or params.get("turn", {}).get("id") or raw.get("generation_id") or "session"
    return {
        "event_id": tool + ":" + hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest(),
        "task_id": str(task) + ":" + str(turn), "tool": tool,
        "title": tool + " · " + str(task)[:12], "state": state,
        "occurred_at": datetime.now(TZ).isoformat(), "source": "official-event",
        "confidence": "observed", "summary": name,
        "reason": "需要在原工具中确认" if state == "waiting_user" else "",
    }


def main():
    """从 stdin 接收单个 Hooks / App Server 事件并授权转发。@author Kai @date 2026/10/23 15:51"""
    event = convert(sys.argv[1], json.load(sys.stdin))
    if event:
        request = urllib.request.Request(
            os.environ.get("DESK_HUB_URL", "http://127.0.0.1:8765") + "/api/agents/events",
            data=json.dumps(event).encode(),
            headers={"Authorization": "Bearer " + os.environ["DESK_HUB_ADAPTER_TOKEN"], "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read()


if __name__ == "__main__":
    # 转发失败不得阻塞 Agent 本身；错误仅写 stderr，不输出工具内容。
    try:
        main()
    except Exception as exc:
        print("Desk Hub 转发不可用：" + type(exc).__name__, file=sys.stderr)
