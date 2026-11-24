# Copyright (c) iGroup.
# All rights reserved.
"""本机 Codex 生命周期只读观察。@author Kai @date 2026/10/23 16:05 @description 仅提取真实事件元数据，不转发对话内容。"""

import json
import sqlite3
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .usage_trend import UsageTrend


class LocalCodex:
    """增量读取当天会话日志。@author Kai @date 2026/10/23 16:05 @description 日志格式非稳定公共协议，明确标注来源。"""

    def __init__(self, root=None, usage_cache=None):
        """初始化观察器及持久化额度快照。

        @param root 可选日志根目录
        @param usage_cache 看板自身的数值快照文件
        @author Kai
        @date 2026/11/03 08:48
        """
        self.root = root or Path.home() / ".codex" / "sessions"
        # 文件读取游标仅在内存保存，重启由 SQLite 事件 ID 去重。
        self.positions = {}
        # 最新会话用量快照只保存数值和时间，不读取凭据或消息正文。
        self.usage = {}
        # 仅持久化看板数值快照，不修改 Codex 原始日志。
        self.usage_cache = usage_cache
        self.usage_bootstrapped = False
        if usage_cache:
            try:
                cached = json.loads(usage_cache.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and isinstance(cached.get("limits"), list):
                    self.usage = cached
            except (OSError, ValueError):
                pass
        # 七日趋势独立缓存，避免两秒广播反复扫描日志正文。
        self.trend = UsageTrend(self.root)
        # 当前文件的轮次归属用于关联用户主动消息，不采集其他角色正文。
        self.turns = {}
        # 只承认曾明确观测到的未读 → 已读转换，不把启动时无记录当成已读。
        self.unread_threads = set()
        # 提问后等待用户回答，不能把本轮结束误显示为开发已完成。
        self.waiting = set()
        # 跨日继续旧会话时，文件仍在创建日期目录；缓存发现结果以避免每次遍历全部日志。
        self.recent_files = set()
        self.last_discovery = 0.0

    def poll(self, store):
        """提取当天明确的生命周期事件。@param store 独立状态库 @author Kai @date 2026/10/23 16:05"""
        # @author Kai @date 2026/10/26 11:32：扫描创建于旧日期但今天仍在更新的会话，修复打包版跨日漏任务。
        if not self.usage_bootstrapped:
            self.restore_recent_usage()
            self.usage_bootstrapped = True
        now = datetime.now()
        folder = self.root / now.strftime("%Y/%m/%d")
        if time.monotonic() - self.last_discovery >= 30 or not self.last_discovery:
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            discovered = set()
            for candidate in self.root.rglob("*.jsonl"):
                try:
                    if candidate.stat().st_mtime >= midnight:
                        discovered.add(candidate)
                except OSError:
                    continue
            self.recent_files = discovered
            self.last_discovery = time.monotonic()
        paths = self.recent_files | set(folder.glob("*.jsonl"))
        for path in paths:
            try:
                offset, thread = self.positions.get(str(path), (0, None))
                if path.stat().st_size < offset:
                    offset, thread = 0, None
                with path.open("rb") as stream:
                    stream.seek(offset)
                    while True:
                        start = stream.tell()
                        line = stream.readline()
                        if not line or not line.endswith(b"\n"):
                            stream.seek(start)
                            break
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        payload = record.get("payload", {})
                        if record.get("type") == "session_meta":
                            thread = payload.get("id")
                        if record.get("type") == "response_item" and payload.get("type") == "function_call" and thread:
                            tool = str(payload.get("name", ""))
                            active = self.turns.get(str(path))
                            if active and tool.endswith(("request_user_input_async", "request_user_input")):
                                try:
                                    args = json.loads(payload.get("arguments", "{}"))
                                    questions = args.get("questions", [])
                                    question = questions[0].get("title") or questions[0].get("question") if questions else "等待回答"
                                    self.waiting.add((thread, active))
                                    store.ingest({"event_id": "question:" + str(payload.get("call_id", f"{thread}:{active}")), "task_id": f"{thread}:{active}", "tool": "Codex", "title": "Codex · " + str(thread)[:8], "state": "waiting_user", "occurred_at": record["timestamp"], "source": "local-session-log", "confidence": "observed-question", "summary": "等待你回答问题", "reason": str(question)[:160]})
                                except (ValueError, TypeError, AttributeError):
                                    pass
                        if record.get("type") == "response_item" and payload.get("role") == "user" and thread:
                            message = " ".join(item.get("text", "") for item in payload.get("content", []) if item.get("type") == "input_text")
                            # @author Kai @date 2026/10/26 10:27：用户回答后解除等待，后续结束才可正常显示完成。
                            active = self.turns.get(str(path))
                            if active and (thread, active) in self.waiting and ("<send_user_message_question_reply>" in message or "## My request:" in message):
                                self.waiting.discard((thread, active))
                                store.ingest({"event_id": f"answer:{thread}:{active}:{record['timestamp']}", "task_id": f"{thread}:{active}", "tool": "Codex", "title": "Codex · " + str(thread)[:8], "state": "running", "occurred_at": record["timestamp"], "source": "local-session-log", "confidence": "observed-answer", "summary": "已收到回答，继续执行", "reason": ""})
                            # 仅取明确的用户请求部分，剔除自动附加的浏览器上下文与环境说明。
                            if "## My request:" in message:
                                message = message.rsplit("## My request:", 1)[1]
                            elif message.lstrip().startswith(("<", "# AGENTS", "# Desk Hub", "The following is the Codex agent history")):
                                continue
                            snippet = re.sub(r"\s+", " ", message).strip()[:100]
                            active = self.turns.get(str(path))
                            if snippet and active:
                                self.save_request(store, thread, active, snippet)
                        if record.get("type") != "event_msg" or not thread:
                            continue
                        name = payload.get("type")
                        if name == "token_count":
                            self.capture_usage(payload, record.get("timestamp", ""))
                        state = {"task_started": "running", "task_complete": "completed", "turn_aborted": "interrupted"}.get(name)
                        turn = payload.get("root_turn_id") or payload.get("turn_id")
                        if not state or not turn:
                            continue
                        if name == "task_complete" and (thread, turn) in self.waiting:
                            continue
                        self.turns[str(path)] = turn
                        # 明确结束事件才标结束，不按静默时间推断成功或失败。
                        store.ingest({"event_id": f"local:{thread}:{turn}:{name}", "task_id": f"{thread}:{turn}", "tool": "Codex", "title": "Codex · " + str(thread)[:8], "state": state, "occurred_at": record["timestamp"], "source": "local-session-log", "confidence": "observed-lifecycle", "summary": {"running": "正在处理任务", "completed": "本轮执行已结束", "interrupted": "本轮执行已中断"}[state], "reason": ""})
                    self.positions[str(path)] = (stream.tell(), thread)
            except (OSError, ValueError, TypeError, KeyError):
                # 某个文件暂不可读不影响其他来源，不伪造任务结果。
                continue
        self.trend.poll()
        self.refresh_titles(store)
        self.sync_read_state(store)

    def sync_read_state(self, store):
        """按 Codex 明确未读转已读同步确认。@param store 看板库 @author Kai @date 2026/10/23 16:43"""
        try:
            path = self.root.parent / ".codex-global-state.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            state = raw.get("electron-thread-read-state-v1")
            if not isinstance(state, dict) or not isinstance(state.get("unreadByIdentity"), dict):
                return
            unread = set()
            # 当前本地版本按身份、主机保存未读会话 ID 列表；未知结构不推断已读。
            for hosts in state["unreadByIdentity"].values():
                if not isinstance(hosts, dict):
                    return
                for ids in hosts.values():
                    if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
                        return
                    unread.update(ids)
            # @author Kai @date 2026/10/23 16:52：覆盖始终停留在会话的情况，完成后留 15 秒等待 Codex 写入未读状态。
            # 身份/主机数据尚未加载时不按空集合自动确认。
            if not state["unreadByIdentity"] or not any(state["unreadByIdentity"].values()):
                return
            for task in store.snapshot()["agents"]:
                if task.get("source") != "local-session-log" or task["state"] != "completed" or task.get("viewed"):
                    continue
                finished = datetime.fromisoformat(task["occurred_at"].replace("Z", "+00:00"))
                if finished.tzinfo is None:
                    continue
                age = (datetime.now(timezone.utc) - finished).total_seconds()
                thread = task["task_id"].split(":", 1)[0]
                if age >= 15 and thread not in unread:
                    store.review(task["event_id"])
            self.unread_threads = unread
        except (OSError, ValueError, TypeError):
            # 文件暂不可读时不清空已知未读状态，防止误收起任务。
            return

    def save_request(self, store, thread, turn, snippet):
        """保存本轮主动消息的短摘要。@param store 看板库 @param thread 会话 @param turn 轮次 @param snippet 最多百字摘要 @author Kai @date 2026/10/23 16:12"""
        key = f"Codex:{thread}:{turn}"
        with store.connect() as db:
            row = db.execute("SELECT payload FROM tasks WHERE id=?", (key,)).fetchone()
            if row:
                task = json.loads(row[0])
                task["request_excerpt"] = snippet
                db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task, ensure_ascii=False), key))

    def refresh_titles(self, store):
        """只读同步真实会话标题。@param store 看板独立库 @author Kai @date 2026/10/23 16:09"""
        path = self.root.parent / "state_5.sqlite"
        if not path.exists():
            return
        try:
            # mode=ro 禁止修改 Codex 数据；只查询标题，不读取正文、预览或凭据。
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as source:
                with store.connect() as target:
                    for key, encoded in target.execute("SELECT id,payload FROM tasks").fetchall():
                        task = json.loads(encoded)
                        if task.get("source") != "local-session-log":
                            continue
                        thread = task["task_id"].split(":", 1)[0]
                        row = source.execute("SELECT title FROM threads WHERE id=?", (thread,)).fetchone()
                        if row and row[0] and row[0] != task["title"]:
                            task["title"] = row[0][:160]
                            target.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task, ensure_ascii=False), key))
        except (OSError, sqlite3.Error, ValueError):
            # 标题库暂不可读时保留已知标题，不能影响生命周期采集。
            return

    def capture_usage(self, payload, timestamp):
        """提取最新会话的累计用量及服务报告的限额。

        @param payload token_count 事件数据
        @param timestamp 日志记录发生时间，ISO 格式
        @author Kai
        @date 2026/11/02 09:54
        """
        if not timestamp or timestamp < self.usage.get("updated_at", ""):
            return
        info = payload.get("info")
        # 独立额度事件可能没有 info，仍须采集限额；保留已知 Token 计数。
        # @author Kai @date 2026/11/02 14:48
        has_info = isinstance(info, dict)
        if not has_info and not isinstance(payload.get("rate_limits"), dict):
            return
        info = info if has_info else {}
        # 只提取明确的非负计数，缺失数据保留为空而不是伪造零用量。
        total = info.get("total_token_usage") or {}
        last = info.get("last_token_usage") or {}
        if not isinstance(total, dict) or not isinstance(last, dict):
            return
        counts = {}
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
            value = total.get(key)
            counts[key] = (value if type(value) is int and value >= 0 else None) if has_info else self.usage.get(key)
        recent = last.get("total_tokens")
        limits = payload.get("rate_limits") or {}
        windows = []
        if isinstance(limits, dict):
            for key in ("primary", "secondary"):
                item = limits.get(key)
                if not isinstance(item, dict):
                    continue
                used = item.get("used_percent")
                minutes = item.get("window_minutes")
                reset = item.get("resets_at")
                if type(used) not in (int, float) or not 0 <= used <= 100:
                    continue
                if type(minutes) not in (int, float) or minutes <= 0:
                    continue
                windows.append({"minutes": minutes, "remaining_percent": round(100 - used, 1),
                                "resets_at": reset if type(reset) in (int, float) else None})
        # 没有额度信息的用量事件不清空已知额度，也不刷新其采集时间。
        self.usage = {"updated_at": timestamp, "scope": "latest_session", **counts,
                      "last_tokens": (recent if type(recent) is int and recent >= 0 else None) if has_info else self.usage.get("last_tokens"),
                      "limits": windows or self.usage.get("limits", []),
                      "limits_updated_at": timestamp if windows else self.usage.get("limits_updated_at")}
        if self.usage_cache:
            try:
                temporary = self.usage_cache.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.usage, ensure_ascii=False), encoding="utf-8")
                temporary.replace(self.usage_cache)
            except OSError:
                # 磁盘暂不可写时继续使用内存快照，不能中止任务观察。
                pass


    def restore_recent_usage(self):
        """冷启动从最近八个日志恢复数值，不将历史任务重新上报。

        @author Kai
        @date 2026/11/03 08:48
        """
        if self.usage.get("limits"):
            return
        try:
            paths = sorted(self.root.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)[:8]
            for path in reversed(paths):
                try:
                    with path.open(encoding="utf-8") as stream:
                        for line in stream:
                            try:
                                record = json.loads(line)
                                payload = record.get("payload", {})
                                if record.get("type") == "event_msg" and payload.get("type") == "token_count":
                                    self.capture_usage(payload, record.get("timestamp", ""))
                            except (ValueError, AttributeError, TypeError):
                                continue
                except (OSError, UnicodeError):
                    continue
        except OSError:
            pass
