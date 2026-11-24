# Copyright (c) iGroup.
# All rights reserved.
"""本机 Cursor 会话生命周期只读观察。

@author Kai
@date 2026/11/20 09:25
@description 只提取轮次开始和结束，不保存助手回复或工具输入。
新会话写在状态库，旧会话仍可能留在 agent-transcripts。
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from .core import TZ


def cursor_state_database():
    """定位 Cursor 全局状态库。

    @return 状态库路径；未安装时返回空
    @author Kai
    @date 2026/11/20 10:12
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    path = Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return path if path.exists() else None


def read_cursor_read_state(path):
    """只读取会话是否未读，不读取标题和正文。

    @param path 状态库路径
    @return 已知会话和未读会话；结构不明时返回空
    @author Kai
    @date 2026/11/20 10:12
    """
    try:
        with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            rows = db.execute("SELECT composerId, value FROM composerHeaders").fetchall()
    except (OSError, sqlite3.Error):
        return None
    known, unread = set(), set()
    for composer_id, raw in rows:
        if not isinstance(composer_id, str) or not isinstance(raw, str):
            continue
        try:
            header = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(header, dict) or not isinstance(header.get("hasUnreadMessages"), bool):
            continue
        known.add(composer_id)
        if header["hasUnreadMessages"]:
            unread.add(composer_id)
    if not known:
        return None
    return known, unread


class LocalCursor:
    """增量读取当天仍在更新的 Cursor 会话日志。

    @author Kai
    @date 2026/11/20 09:25
    @description 日志不是稳定公共协议；没有 turn_ended 时不推断成功或失败。
    """

    def __init__(self, root=None, state_db=None):
        """初始化观察游标。

        @param root 可选的 .cursor/projects 目录
        @param state_db 可选的已读状态库
        @author Kai
        @date 2026/11/20 10:12
        """
        # 会话日志根目录，只读，不修改 Cursor 自己的记录。
        self.root = root or Path.home() / ".cursor" / "projects"
        # 已读状态来自 Cursor 会话表，测试可替换为临时库。
        self.state_db = state_db
        # 文件偏移和轮次只留在内存，重启后靠事件编号去重。
        self.positions = {}
        self.turns = {}
        # 已发现的今日会话，避免两秒轮询反复遍历全部项目。
        self.recent_files = set()
        self.last_discovery = 0.0
        # 状态库会话的上次签名；没变化时不再读气泡正文。
        self.composer_seen = {}

    def poll(self, store):
        """提取今天仍在写入的会话轮次。

        @param store 看板独立状态库
        @author Kai
        @date 2026/11/20 09:25
        """
        if time.monotonic() - self.last_discovery >= 30 or not self.last_discovery:
            self.recent_files = self._discover()
            self.last_discovery = time.monotonic()
        # 已写入的摘要如果还带着日志时间戳，回头重读这一会话并换成正文。
        self._rewind_wrapped(store)
        for path in list(self.recent_files):
            try:
                self._read(path, store)
            except (OSError, ValueError, TypeError, KeyError):
                # 单个会话暂不可读时保留其他会话，不伪造结果。
                continue
        # 新版 Cursor 不再写 jsonl，进行中的任务在状态库里。
        self._poll_composers(store)
        self.sync_read_state(store)

    def _poll_composers(self, store):
        """从状态库读取今天仍在更新、且没有日志文件的会话。

        @param store 看板独立状态库
        @author Kai
        @date 2026/11/20 13:34
        """
        path = self.state_db or cursor_state_database()
        if not path or not Path(path).exists():
            return
        midnight = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        since_ms = int(midnight.timestamp() * 1000)
        try:
            with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                rows = db.execute(
                    "SELECT composerId, value FROM composerHeaders "
                    "WHERE lastUpdatedAt >= ? AND IFNULL(isSubagent, 0) = 0",
                    (since_ms,),
                ).fetchall()
                for composer_id, raw in rows:
                    self._read_composer(db, store, composer_id, raw)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            # 状态库正被 Cursor 写入时跳过这一轮，不把任务标成失败。
            return

    def _read_composer(self, db, store, composer_id, raw):
        """把一个会话头和用户气泡收成轮次。

        @param db 只读状态库连接
        @param store 看板独立状态库
        @param composer_id 会话标识
        @param raw 会话头 JSON
        @author Kai
        @date 2026/11/20 13:34
        """
        if not isinstance(composer_id, str) or not isinstance(raw, str):
            return
        # 已经有日志文件的会话继续走文件增量，避免同一轮记两次。
        if self._transcript_exists(composer_id):
            return
        try:
            header = json.loads(raw)
        except ValueError:
            return
        if not isinstance(header, dict):
            return
        signature = (
            header.get("lastUpdatedAt"),
            header.get("conversationCheckpointLastUpdatedAt"),
            header.get("unfinishedRunAt"),
            header.get("hasUnreadMessages"),
        )
        if self.composer_seen.get(composer_id) == signature:
            return
        # 空草稿没有用户轮次；签名不变时下一轮不再查气泡。
        if header.get("isDraft") is True:
            self.composer_seen[composer_id] = signature
            return
        turns = self._user_turns(db, composer_id)
        if not turns:
            self.composer_seen[composer_id] = signature
            return
        running = isinstance(header.get("unfinishedRunAt"), (int, float)) and header["unfinishedRunAt"] > 0
        title = "Cursor · " + self._header_project(header)
        events = []
        for index, (stamp, excerpt) in enumerate(turns, start=1):
            state = "running" if running and index == len(turns) else "completed"
            if state == "completed":
                stamp = max(stamp, self._header_time(header))
            # 同一秒结束的多轮也要让最后一轮排在前面。
            stamp = stamp + timedelta(milliseconds=index)
            event = self._event(composer_id, index, state, title, excerpt)
            event["occurred_at"] = stamp.isoformat()
            events.append(event)
        for event in events:
            store.ingest(event)
            self._replace_wrapped_excerpt(store, event)
        self.composer_seen[composer_id] = signature

    def _transcript_exists(self, composer_id):
        """判断这个会话是否仍由日志文件负责。

        @param composer_id 会话标识
        @return 存在对应 jsonl 时为真
        @author Kai
        @date 2026/11/20 13:34
        """
        if not self.root.exists():
            return False
        return any(self.root.glob(f"*/agent-transcripts/{composer_id}/{composer_id}.jsonl"))

    def _header_project(self, header):
        """从会话头取出项目目录名。

        @param header 会话头
        @return 项目名称
        @author Kai
        @date 2026/11/20 13:34
        """
        location = header.get("agentLocation") if isinstance(header.get("agentLocation"), dict) else {}
        environment = location.get("environment") if isinstance(location.get("environment"), dict) else {}
        uri = environment.get("uri") if isinstance(environment.get("uri"), dict) else {}
        folder = uri.get("fsPath") if isinstance(uri.get("fsPath"), str) else ""
        name = Path(folder).name.strip() if folder else ""
        return (name or "Cursor")[:80]

    def _header_time(self, header):
        """取会话头上较新的更新时间，用来盖过进行中的时间戳。

        @param header 会话头
        @return 更新时间；没有时返回当前时间
        @author Kai
        @date 2026/11/20 13:34
        """
        for name in ("conversationCheckpointLastUpdatedAt", "lastUpdatedAt"):
            value = header.get(name)
            if isinstance(value, (int, float)) and value > 0:
                return datetime.fromtimestamp(value / 1000, TZ)
        return datetime.now(TZ)

    def _user_turns(self, db, composer_id):
        """只读取用户气泡，助手回复和工具结果留在库里。

        @param db 只读状态库连接
        @param composer_id 会话标识
        @return 按时间排列的轮次时间和请求摘要
        @author Kai
        @date 2026/11/20 13:34
        """
        prefix = f"bubbleId:{composer_id}:"
        end = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        keys = [
            key for (key,) in db.execute(
                "SELECT key FROM cursorDiskKV WHERE key >= ? AND key < ?",
                (prefix, end),
            ) if isinstance(key, str) and "/" not in key
        ]
        turns = []
        for key in keys:
            row = db.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()
            if not row or not isinstance(row[0], str):
                continue
            try:
                bubble = json.loads(row[0])
            except ValueError:
                continue
            if not isinstance(bubble, dict) or bubble.get("type") != 1:
                continue
            excerpt = self._visible_request(str(bubble.get("text") or ""))[:100]
            if not excerpt:
                continue
            turns.append((self._bubble_time(bubble), excerpt))
        turns.sort(key=lambda item: item[0])
        return turns

    def _bubble_time(self, bubble):
        """读取用户气泡的发生时间。

        @param bubble 用户气泡
        @return 带时区的时间
        @author Kai
        @date 2026/11/20 13:34
        """
        started = bubble.get("startedAtMs")
        if isinstance(started, (int, float)) and started > 0:
            return datetime.fromtimestamp(started / 1000, TZ)
        created = bubble.get("createdAt")
        if isinstance(created, str):
            try:
                parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=TZ)
                return parsed.astimezone(TZ)
        return datetime.now(TZ)

    def sync_read_state(self, store):
        """已读的完成任务在短暂等待后自动收起。

        @param store 看板独立状态库
        @author Kai
        @date 2026/11/20 10:12
        """
        path = self.state_db or cursor_state_database()
        if not path:
            return
        state = read_cursor_read_state(path)
        # 没有明确的未读字段时不把任务当成已读。
        if not state:
            return
        known, unread = state
        for task in store.snapshot()["agents"]:
            if task.get("source") != "local-cursor-log" or task.get("state") != "completed" or task.get("viewed"):
                continue
            try:
                finished = datetime.fromisoformat(str(task.get("occurred_at", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if finished.tzinfo is None:
                continue
            age = (datetime.now(TZ) - finished.astimezone(TZ)).total_seconds()
            thread = str(task.get("task_id", "")).split(":", 1)[0]
            if age >= 15 and thread in known and thread not in unread:
                store.review(task["event_id"])

    def _discover(self):
        """找出今天修改过的主会话文件。

        @return 会话日志路径集合
        @author Kai
        @date 2026/11/20 09:25
        """
        discovered = set()
        if not self.root.exists():
            return discovered
        midnight = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        for candidate in self.root.rglob("*.jsonl"):
            # 子代理是主会话内部步骤，不单独占一条任务。
            if "subagents" in candidate.parts or candidate.stem != candidate.parent.name:
                continue
            try:
                if candidate.stat().st_mtime >= midnight:
                    discovered.add(candidate)
            except OSError:
                continue
        return discovered

    def _read(self, path, store):
        """增量消费一个会话文件。

        @param path 会话日志
        @param store 看板独立状态库
        @author Kai
        @date 2026/11/20 09:25
        """
        offset, thread, index, opened = self.positions.get(str(path), (0, None, 0, False))
        if path.stat().st_size < offset:
            offset, thread, index, opened = 0, None, 0, False
        thread = thread or path.stem
        title = "Cursor · " + self._project_name(path)
        events = []
        excerpt = ""
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
                if not isinstance(record, dict):
                    continue
                user_text = self._user_excerpt(record)
                if user_text is not None:
                    index += 1
                    opened = True
                    excerpt = user_text or excerpt
                    events.append(self._event(thread, index, "running", title, excerpt))
                    continue
                if record.get("type") != "turn_ended" or not opened:
                    continue
                state = self._terminal_state(record)
                if not state:
                    continue
                opened = False
                events.append(self._event(thread, index, state, title, excerpt))
            self.positions[str(path)] = (stream.tell(), thread, index, opened)
        self._stamp(events)
        for event in events:
            store.ingest(event)
            self._replace_wrapped_excerpt(store, event)

    def _project_name(self, path):
        """取会话所属项目目录名。

        @param path 会话日志
        @return 项目名称
        @author Kai
        @date 2026/11/20 09:25
        """
        try:
            return path.parents[2].name[:80]
        except IndexError:
            return path.stem[:12]

    def _user_excerpt(self, record):
        """提取用户本轮短请求；助手和工具内容直接丢弃。

        @param record 一行日志
        @return 最多一百字的请求；不是用户消息时返回 None
        @author Kai
        @date 2026/11/20 09:25
        """
        if record.get("role") != "user":
            return None
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content")
        texts = []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    texts.append(item["text"])
        elif isinstance(content, str):
            texts.append(content)
        text = re.sub(r"\s+", " ", " ".join(texts)).strip()
        return self._visible_request(text)[:100]

    def _visible_request(self, text):
        """去掉日志包在请求外面的时间戳和查询标签。

        @param text 用户消息原文
        @return 可见请求
        @author Kai
        @date 2026/11/20 09:40
        """
        cleaned = re.sub(r"<timestamp\b[^>]*>.*?</timestamp>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        wrapped = re.search(r"<user_query\b[^>]*>\s*(.*?)\s*</user_query>", cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = wrapped.group(1) if wrapped else re.sub(r"</?user_query\b[^>]*>", " ", cleaned, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", cleaned).strip()

    def _rewind_wrapped(self, store):
        """让仍显示时间戳标签的会话从头再读一遍。

        @param store 看板独立状态库
        @author Kai
        @date 2026/11/20 09:40
        """
        threads = set()
        try:
            with store.connect() as db:
                for _, encoded in db.execute("SELECT id, payload FROM tasks"):
                    task = json.loads(encoded)
                    excerpt = str(task.get("request_excerpt") or "")
                    if task.get("tool") == "Cursor" and ("<timestamp" in excerpt.lower() or "<user_query" in excerpt.lower()):
                        threads.add(str(task.get("task_id", "")).split(":", 1)[0])
        except (OSError, ValueError, TypeError):
            return
        if not threads:
            return
        for path in self.recent_files:
            if path.stem in threads:
                self.positions.pop(str(path), None)

    def _replace_wrapped_excerpt(self, store, event):
        """用去掉标签后的请求替换已经保存的时间戳摘要。

        @param store 看板独立状态库
        @param event 本轮事件
        @author Kai
        @date 2026/11/20 09:40
        """
        excerpt = event.get("request_excerpt") or ""
        if not excerpt:
            return
        key = event["tool"] + ":" + event["task_id"]
        try:
            with store.connect() as db:
                row = db.execute("SELECT payload FROM tasks WHERE id=?", (key,)).fetchone()
                if not row:
                    return
                task = json.loads(row[0])
                current = str(task.get("request_excerpt") or "")
                if current == excerpt or ("<timestamp" not in current.lower() and "<user_query" not in current.lower()):
                    return
                task["request_excerpt"] = excerpt
                db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task, ensure_ascii=False), key))
        except (OSError, ValueError, TypeError):
            return

    def _terminal_state(self, record):
        """把明确的结束状态映射到看板状态。

        @param record turn_ended 记录
        @return 看板状态；无法判断时返回空
        @author Kai
        @date 2026/11/20 09:25
        """
        status = record.get("status")
        if status == "success":
            return "completed"
        if status != "error":
            return None
        error = str(record.get("error") or "").lower()
        if any(word in error for word in ("abort", "stopped", "cancel")):
            return "interrupted"
        return "failed"

    def _event(self, thread, turn, state, title, excerpt):
        """组装一条不含回复正文的生命周期事件。

        @param thread 会话标识
        @param turn 本文件内的轮次序号
        @param state 看板状态
        @param title 项目标题
        @param excerpt 用户请求短摘要
        @return 统一事件
        @author Kai
        @date 2026/11/20 09:25
        """
        summary = {"running": "正在处理任务", "completed": "本轮执行已结束", "failed": "本轮执行失败", "interrupted": "本轮执行已中断"}[state]
        event = {
            "event_id": f"cursor:{thread}:{turn}:{state}",
            "task_id": f"{thread}:{turn}",
            "tool": "Cursor",
            "title": title[:160],
            "state": state,
            "source": "local-cursor-log",
            "confidence": "observed-lifecycle",
            "summary": summary,
            "reason": "",
        }
        if excerpt:
            event["request_excerpt"] = excerpt
        return event

    def _stamp(self, events):
        """按读取顺序给同一批新事件分配递增时间。

        @param events 本轮新读到的事件
        @author Kai
        @date 2026/11/20 09:25
        """
        if not events:
            return
        # 日志行本身没有时间戳；用读取时刻保证后发生的轮次能覆盖先前状态。
        end = datetime.now(TZ)
        start = end - timedelta(seconds=len(events) - 1)
        for offset, event in enumerate(events):
            event["occurred_at"] = (start + timedelta(seconds=offset)).isoformat()
