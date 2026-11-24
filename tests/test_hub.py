# Copyright (c) iGroup.
# All rights reserved.
"""Desk Hub 核心验收。@author Kai @date 2026/10/23 15:51 @description 验证真实状态、去重、重启和授权边界。"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

# 测试使用独立临时数据，不污染用户实际任务。
os.environ["DESK_HUB_DATA"] = tempfile.mkdtemp()
os.environ["DESK_HUB_TOKEN"] = "test-token"
os.environ["DESK_HUB_CURSOR_ROOT"] = tempfile.mkdtemp()
os.environ["DESK_HUB_CURSOR_QUOTA"] = "off"
from fastapi.testclient import TestClient
from desk_hub.core import Store, TZ, attendance
from desk_hub.server import app
from desk_hub.adapter import convert
from desk_hub.cursor_quota import normalize_cursor_usage
from desk_hub.server import enabled_providers, visible_tasks
from desk_hub.local_codex import LocalCodex
from desk_hub.local_cursor import LocalCursor, read_cursor_read_state


def test_local_codex_incremental(tmp_path):
    """真实日志事件增量读取且不保存正文。@param tmp_path 测试目录 @author Kai @date 2026/10/23 16:07"""
    folder = tmp_path / datetime.now().strftime("%Y/%m/%d")
    folder.mkdir(parents=True)
    path = folder / "session.jsonl"
    records = [{"type": "session_meta", "payload": {"id": "thread"}}, {"type": "event_msg", "timestamp": "2026-10-23T08:00:00Z", "payload": {"type": "task_started", "turn_id": "turn", "message": "private-content"}}]
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    store = Store(tmp_path / "observer.db")
    observer = LocalCodex(tmp_path)
    observer.poll(store)
    observer.poll(store)
    assert len(store.snapshot()["events"]) == 1
    assert store.snapshot()["agents"][0]["state"] == "running"
    assert "private-content" not in json.dumps(store.snapshot())


def test_persistence_dedupe_and_order(tmp_path):
    """重复及延迟事件不重复提醒或覆盖新状态。@param tmp_path 临时目录 @author Kai @date 2026/10/23 15:51"""
    path = tmp_path / "hub.db"
    store = Store(path)
    event = dict(event_id="1", task_id="t", tool="custom", state="waiting_user", occurred_at="2026-10-23T10:00:00+08:00")
    assert store.ingest(event)
    assert not store.ingest(event)
    assert store.ingest({**event, "event_id": "2"})
    assert sum(e["notify"] for e in store.snapshot()["events"]) == 1
    store.ingest({**event, "event_id": "3", "state": "completed", "occurred_at": "2026-10-23T11:00:00+08:00"})
    store.ingest({**event, "event_id": "4", "state": "running"})
    assert Store(path).snapshot()["agents"][0]["state"] == "completed"
    assert sum(e["notify"] for e in store.snapshot()["events"]) == 2


def test_attendance_unknown_stale_and_out(tmp_path):
    """缺失、过期及早退真实记录均不会变成正常状态。@param tmp_path 临时目录 @author Kai @date 2026/10/23 15:51"""
    assert attendance(tmp_path)["workday"] is None
    payload = dict(ok=True, date="2000-01-01", in_time="08:30", target_time="17:30", latest_out="16:00", status="未满应出勤 9h")
    path = tmp_path / "status.json"
    path.write_text(json.dumps(payload))
    assert not attendance(tmp_path)["ok"]
    payload["date"] = datetime.now(TZ).date().isoformat()
    path.write_text(json.dumps(payload))
    result = attendance(tmp_path)
    assert result["ok"] and result["latest_out"] and result["verified_status"] == "未满应出勤 9h"


def test_api_and_websocket():
    """检查 API 认证和首帧授权推送。@author Kai @date 2026/10/23 15:51"""
    with TestClient(app) as client:
        assert client.get("/api/dashboard").status_code == 401
        assert client.get("/api/dashboard", headers={"Authorization": "Bearer test-token"}).status_code == 200
        event = dict(event_id="http-1", task_id="http-run", tool="custom", state="waiting_user", occurred_at="2026-10-23T10:00:00+08:00")
        headers = {"Authorization": "Bearer test-token"}
        assert client.post("/api/agents/events", json=event).status_code == 401
        assert client.post("/api/agents/events", json=event, headers=headers).json()["accepted"]
        assert not client.post("/api/agents/events", json=event, headers=headers).json()["accepted"]
        assert client.post("/api/agents/events", json={**event, "state": "stopped"}, headers=headers).status_code == 422
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"token": "test-token"})
            message = ws.receive_json()
            assert message["type"] == "snapshot"
            # @author Kai @date 2026/10/26 10:27：实时采集会加入其他任务，断言测试任务而非列表首项。
            assert next(task for task in message["data"]["agents"] if task["task_id"] == "http-run")["state"] == "waiting_user"


def test_adapter_no_false_success():
    """停止不等于成功，未知事件不伪造进度。@author Kai @date 2026/10/23 15:51"""
    assert convert("cursor", {"hook_event_name": "stop", "conversation_id": "c", "status": "aborted"})["state"] == "interrupted"
    assert convert("cursor", {"hook_event_name": "other", "conversation_id": "c"}) is None
    assert convert("codex", {"method": "turn/completed", "params": {"threadId": "t", "turn": {"status": "failed", "id": "r"}}})["state"] == "failed"


def test_question_keeps_waiting(tmp_path):
    """结构化提问在本轮结束后仍需用户回答。@param tmp_path 临时目录 @author Kai @date 2026/10/23 16:57"""
    folder = tmp_path / datetime.now().strftime("%Y/%m/%d")
    folder.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "payload": {"id": "question-thread"}},
        {"type": "event_msg", "timestamp": "2026-10-23T08:00:00Z", "payload": {"type": "task_started", "turn_id": "turn"}},
        {"type": "response_item", "timestamp": "2026-10-23T08:00:01Z", "payload": {"type": "function_call", "name": "functions.request_user_input_async", "call_id": "q1", "arguments": json.dumps({"questions": [{"title": "要打包哪个？"}]})}},
        {"type": "event_msg", "timestamp": "2026-10-23T08:00:02Z", "payload": {"type": "task_complete", "turn_id": "turn"}},
    ]
    (folder / "q.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    store = Store(tmp_path / "q.db")
    observer = LocalCodex(tmp_path)
    observer.poll(store)
    task = store.snapshot()["agents"][0]
    assert task["state"] == "waiting_user"
    assert task["reason"] == "要打包哪个？"
    # 回答后同一轮能够继续执行并最终完成，不能永久留在待确认状态。
    with (folder / "q.jsonl").open("a") as stream:
        for row in [
            {"type": "response_item", "timestamp": "2026-10-23T08:00:03Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<send_user_message_question_reply>两个都打包</send_user_message_question_reply>"}]}},
            {"type": "event_msg", "timestamp": "2026-10-23T08:00:04Z", "payload": {"type": "task_complete", "turn_id": "turn"}},
        ]:
            stream.write(json.dumps(row) + "\n")
    observer.poll(store)
    assert store.snapshot()["agents"][0]["state"] == "completed"


def test_old_session_updated_today(tmp_path):
    """旧日期会话今天继续执行也应被采集。@param tmp_path 临时目录 @author Kai @date 2026/10/26 11:32"""
    folder = tmp_path / "2020/01/01"
    folder.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "payload": {"id": "old-thread"}},
        {"type": "event_msg", "timestamp": datetime.now(TZ).isoformat(), "payload": {"type": "task_started", "turn_id": "today-turn"}},
    ]
    (folder / "old.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    store = Store(tmp_path / "cross-day.db")
    LocalCodex(tmp_path).poll(store)
    assert store.snapshot()["agents"][0]["task_id"] == "old-thread:today-turn"
    assert store.snapshot()["agents"][0]["state"] == "running"


def test_local_cursor_turn_and_abort(tmp_path):
    """Cursor 主会话按轮次采集，中断和助手正文都不记成成功。@param tmp_path 临时目录 @author Kai @date 2026/11/20 09:25"""
    folder = tmp_path / "demo-project" / "agent-transcripts" / "conversation"
    folder.mkdir(parents=True)
    path = folder / "conversation.jsonl"
    rows = [
        {"role": "user", "message": {"content": [{"type": "text", "text": "把看板接上"}]}},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "SECRET-ASSISTANT"}]}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    store = Store(tmp_path / "cursor.db")
    observer = LocalCursor(tmp_path, state_db=tmp_path / "absent.vscdb")
    observer.poll(store)
    observer.poll(store)
    task = store.snapshot()["agents"][0]
    assert task["state"] == "running"
    assert task["tool"] == "Cursor"
    assert task["request_excerpt"] == "把看板接上"
    assert len(store.snapshot()["events"]) == 1
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "turn_ended", "status": "error", "error": "user_stopped_generation"}) + "\n")
    observer.poll(store)
    assert store.snapshot()["agents"][0]["state"] == "interrupted"
    assert "SECRET-ASSISTANT" not in json.dumps(store.snapshot(), ensure_ascii=False)


def test_cursor_state_database_turns(tmp_path):
    """没有 jsonl 的新会话从状态库采集，助手正文不入库。@param tmp_path 临时目录 @author Kai @date 2026/11/20 13:34"""
    database = tmp_path / "state.vscdb"
    started = int((datetime.now(TZ) - timedelta(seconds=5)).timestamp() * 1000)
    checkpoint = int(datetime.now(TZ).timestamp() * 1000)
    live = {
        "unfinishedRunAt": started, "lastUpdatedAt": started, "conversationCheckpointLastUpdatedAt": checkpoint,
        "isDraft": False, "hasUnreadMessages": True,
        "agentLocation": {"environment": {"uri": {"fsPath": "D:\\Code\\Demo"}}},
    }
    done = {
        "lastUpdatedAt": checkpoint, "conversationCheckpointLastUpdatedAt": checkpoint,
        "isDraft": False, "hasUnreadMessages": True,
        "agentLocation": {"environment": {"uri": {"fsPath": "D:\\Code\\Demo"}}},
    }
    filed = {"lastUpdatedAt": checkpoint, "isDraft": False, "hasUnreadMessages": True}
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, lastUpdatedAt INTEGER, isSubagent INTEGER, value TEXT)")
        db.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO composerHeaders VALUES (?,?,?,?)", ("live-chat", started, 0, json.dumps(live)))
        db.execute("INSERT INTO composerHeaders VALUES (?,?,?,?)", ("done-chat", checkpoint, 0, json.dumps(done)))
        db.execute("INSERT INTO composerHeaders VALUES (?,?,?,?)", ("file-chat", checkpoint, 0, json.dumps(filed)))
        db.execute("INSERT INTO composerHeaders VALUES (?,?,?,?)", ("child-chat", checkpoint, 1, json.dumps(done)))
        db.execute("INSERT INTO cursorDiskKV VALUES (?,?)", ("bubbleId:live-chat:user", json.dumps({"type": 1, "text": "把状态库里的任务接上", "startedAtMs": started})))
        db.execute("INSERT INTO cursorDiskKV VALUES (?,?)", ("bubbleId:live-chat:user/s:1", json.dumps({"type": 2, "text": "SECRET-ASSISTANT"})))
        db.execute("INSERT INTO cursorDiskKV VALUES (?,?)", ("bubbleId:done-chat:first", json.dumps({"type": 1, "text": "第一轮", "startedAtMs": started - 2000})))
        db.execute("INSERT INTO cursorDiskKV VALUES (?,?)", ("bubbleId:done-chat:second", json.dumps({"type": 1, "text": "第二轮", "startedAtMs": started})))
        db.execute("INSERT INTO cursorDiskKV VALUES (?,?)", ("bubbleId:file-chat:user", json.dumps({"type": 1, "text": "只应来自文件", "startedAtMs": started})))
        db.execute("INSERT INTO cursorDiskKV VALUES (?,?)", ("bubbleId:child-chat:user", json.dumps({"type": 1, "text": "子代理不要单独占一条", "startedAtMs": started})))
    folder = tmp_path / "demo-project" / "agent-transcripts" / "file-chat"
    folder.mkdir(parents=True)
    (folder / "file-chat.jsonl").write_text("", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    observer = LocalCursor(tmp_path, state_db=database)
    observer.poll(store)
    tasks = {task["task_id"]: task for task in store.snapshot()["agents"]}
    assert tasks["live-chat:1"]["state"] == "running"
    assert tasks["live-chat:1"]["request_excerpt"] == "把状态库里的任务接上"
    assert tasks["live-chat:1"]["title"] == "Cursor · Demo"
    assert tasks["done-chat:1"]["state"] == "completed"
    assert tasks["done-chat:2"]["request_excerpt"] == "第二轮"
    assert tasks["done-chat:2"]["state"] == "completed"
    assert "file-chat:1" not in tasks and "child-chat:1" not in tasks
    assert "SECRET-ASSISTANT" not in json.dumps(store.snapshot(), ensure_ascii=False)
    live["conversationCheckpointLastUpdatedAt"] = checkpoint + 1000
    live.pop("unfinishedRunAt")
    with sqlite3.connect(database) as db:
        db.execute("UPDATE composerHeaders SET value=?, lastUpdatedAt=? WHERE composerId=?", (json.dumps(live), checkpoint + 1000, "live-chat"))
    observer.poll(store)
    assert next(task for task in store.snapshot()["agents"] if task["task_id"] == "live-chat:1")["state"] == "completed"


def test_cursor_excerpt_drops_timestamp(tmp_path):
    """任务标题只保留用户请求，不带日志时间戳标签。@param tmp_path 临时目录 @author Kai @date 2026/11/20 09:40"""
    folder = tmp_path / "demo-project" / "agent-transcripts" / "conversation"
    folder.mkdir(parents=True)
    wrapped = "<timestamp>Friday, Nov 20, 2026, 9:28 AM (UTC+8)</timestamp>\n<user_query>\n现在任务里不要带时间戳\n</user_query>"
    (folder / "conversation.jsonl").write_text(json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": wrapped}]}}) + "\n", encoding="utf-8")
    store = Store(tmp_path / "clean.db")
    observer = LocalCursor(tmp_path, state_db=tmp_path / "absent.vscdb")
    observer.poll(store)
    assert store.snapshot()["agents"][0]["request_excerpt"] == "现在任务里不要带时间戳"
    # 已经存进库的旧摘要也要在下一次读取时换成正文。
    with store.connect() as db:
        row = db.execute("SELECT payload FROM tasks").fetchone()
        task = json.loads(row[0])
        task["request_excerpt"] = wrapped[:100]
        db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task, ensure_ascii=False), "Cursor:conversation:1"))
    observer.poll(store)
    assert "<timestamp" not in store.snapshot()["agents"][0]["request_excerpt"]
    assert store.snapshot()["agents"][0]["request_excerpt"] == "现在任务里不要带时间戳"


def _cursor_header_db(path, rows):
    """写入只含已读标记的临时会话表。@param path 数据库 @param rows 会话与是否未读 @author Kai @date 2026/11/20 10:12"""
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, value TEXT)")
        for composer_id, unread in rows:
            db.execute("INSERT INTO composerHeaders VALUES (?, ?)", (composer_id, json.dumps({"hasUnreadMessages": unread})))


def test_cursor_read_dismisses_completed(tmp_path):
    """已读且完成超过等待时间的 Cursor 任务自动收起，未读的保留。@param tmp_path 临时目录 @author Kai @date 2026/11/20 10:12"""
    database = tmp_path / "state.vscdb"
    _cursor_header_db(database, [("read-chat", False), ("unread-chat", True)])
    known, unread = read_cursor_read_state(database)
    assert known == {"read-chat", "unread-chat"} and unread == {"unread-chat"}
    store = Store(tmp_path / "read.db")
    old = (datetime.now(TZ) - timedelta(seconds=20)).isoformat()
    fresh = datetime.now(TZ).isoformat()
    for event_id, task_id, when in (("old-read", "read-chat:1", old), ("fresh-read", "read-chat:2", fresh), ("old-unread", "unread-chat:1", old)):
        store.ingest({"event_id": event_id, "task_id": task_id, "tool": "Cursor", "state": "completed", "occurred_at": when, "source": "local-cursor-log"})
    observer = LocalCursor(tmp_path, state_db=database)
    observer.sync_read_state(store)
    viewed = {task["event_id"]: task.get("viewed") for task in store.snapshot()["agents"]}
    assert viewed["old-read"] is True
    assert not viewed["fresh-read"]
    assert not viewed["old-unread"]
    assert read_cursor_read_state(tmp_path / "missing.vscdb") is None


def test_cursor_quota_windows():
    """套餐百分比、团队额度和周周期补齐都能换成余量。@author Kai @date 2026/11/20 09:25"""
    summary = {"billingCycleStart": "2026-11-01T00:00:00.000Z", "billingCycleEnd": "2026-12-01T00:00:00.000Z",
               "individualUsage": {"plan": {"totalPercentUsed": 40, "apiPercentUsed": 10}}}
    windows = normalize_cursor_usage(summary)
    assert "label" not in windows[0] and windows[0]["remaining_percent"] == 60
    assert windows[1]["label"] == "API 额度" and windows[1]["remaining_percent"] == 90
    assert windows[0]["resets_at"]
    team = {"individualUsage": {}, "teamUsage": {"pooled": {"used": 100, "limit": 1000, "remaining": 900}}}
    assert normalize_cursor_usage(team)[0]["remaining_percent"] == 90
    weekly = {"membershipType": "premium", "isUnlimited": False, "individualUsage": {"plan": {"totalPercentUsed": 0}}}
    period = {"billingCycleStart": "1792405460651", "billingCycleEnd": "1793010260651", "planUsage": {"totalPercentUsed": 7}}
    assert normalize_cursor_usage(weekly, period)[0]["remaining_percent"] == 93


def test_provider_selection(tmp_path):
    """未配置时两个都监听，关掉 Codex 后不再展示它的任务。@param tmp_path 临时目录 @author Kai @date 2026/11/20 09:40"""
    assert enabled_providers(tmp_path) == ["codex", "cursor"]
    (tmp_path / "config.json").write_text(json.dumps({"desk_hub_providers": ["cursor", "other"]}), encoding="utf-8")
    assert enabled_providers(tmp_path) == ["cursor"]
    tasks = [{"tool": "Codex", "task_id": "a"}, {"tool": "Cursor", "task_id": "b"}, {"tool": "custom", "task_id": "c"}]
    kept = visible_tasks(tasks, ["cursor"])
    assert [item["tool"] for item in kept] == ["Cursor", "custom"]
