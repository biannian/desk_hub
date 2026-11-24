# Copyright (c) iGroup.
# All rights reserved.
"""Desk Hub 只读考勤桥与持久化事件状态机。

@author Kai
@date 2026/10/23 15:51
@description 独立读取现有文件，不调用考勤接口，不写入助手数据。
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 所有输出时间均带中国时区，避免浏览器按设备时区误读。
TZ = timezone(timedelta(hours=8))
STATES = {"queued", "running", "waiting_user", "completed", "failed", "interrupted", "unknown"}


def read_json(path):
    """读取助手 JSON 文件。@param path 文件路径 @return 字典 @author Kai @date 2026/10/23 15:51"""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("数据文件必须是对象")
    return data


def attendance(source):
    """转换只读考勤快照。@param source 助手目录 @return 考勤状态 @author Kai @date 2026/10/23 15:51"""
    now = datetime.now(TZ)
    result = {"ok": False, "workday": None, "source": "AttendanceHelper/status.json", "error": "数据暂不可用"}
    # @author Kai @date 2026/10/23 16:18：只读取桌面展示配置白名单，绝不将凭据配置发送给网页。
    result["display"] = {"display_mode": "near", "near_minutes": 20, "far_display": "clock_in", "desk_hub_oled": False}
    try:
        config = read_json(source / "config.json")
        result["display"].update({key: config[key] for key in result["display"] if key in config})
    except (OSError, ValueError):
        pass
    # 只使用有效年份节假日表，接口失败时不将普通周末推断成休息日。
    try:
        cache = read_json(source / "cache.json")
        year = cache.get(f"holiday-year-{now.year}", {})
        if year.get("holiday") and now.timestamp() - float(year.get("savedAt", 0)) < 72 * 3600:
            entry = year["holiday"].get(now.strftime("%m-%d"))
            result["workday"] = not entry["holiday"] if entry is not None else now.weekday() < 5
    except (OSError, ValueError, TypeError, KeyError):
        pass
    try:
        data = read_json(source / "status.json")
        result["updated_at"] = data.get("updated_at")
        if not data.get("ok") or data.get("date") != now.date().isoformat():
            return result
        # 现有导出只到分钟，最终状态沿用真实核验结果，不凭倒计时归零判断正常。
        for field in ("in_time", "latest_out", "target_time"):
            value = data.get(field)
            result[field] = datetime.fromisoformat(f"{data['date']}T{value}:00").replace(tzinfo=TZ).isoformat() if value and value != "--" else None
        result.update(ok=True, error="", verified_status=data.get("status", "未知"), required_hours=data.get("required_hours"), worked=data.get("worked"))
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return result


class Store:
    """SQLite 任务与事件存储。@author Kai @date 2026/10/23 15:51 @description 重启保留状态并按事件及状态去重。"""

    def __init__(self, path):
        """初始化独立库。@param path 数据库路径 @author Kai @date 2026/10/23 15:51"""
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, state TEXT NOT NULL, time TEXT NOT NULL, notify INTEGER NOT NULL, handled INTEGER DEFAULT 0, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notices (task_id TEXT, state TEXT, PRIMARY KEY(task_id,state));
            """)

    def connect(self):
        """打开事务连接。@return SQLite 连接 @author Kai @date 2026/10/23 15:51"""
        return sqlite3.connect(self.path)

    def ingest(self, event):
        """接收已校验事件。@param event 统一事件 @return 是否产生新事件 @author Kai @date 2026/10/23 15:51"""
        key = event["tool"] + ":" + event["task_id"]
        encoded = json.dumps(event, ensure_ascii=False)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM events WHERE id=?", (event["event_id"],)).fetchone():
                return False
            old = db.execute("SELECT payload FROM tasks WHERE id=?", (key,)).fetchone()
            # 生命周期变更保留用户已授权展示的本轮消息摘要。
            if old:
                previous = json.loads(old[0])
                if previous.get("request_excerpt"):
                    event = {**event, "request_excerpt": previous["request_excerpt"]}
                    encoded = json.dumps(event, ensure_ascii=False)
            # 延迟到达的旧事件保留历史，但不得覆盖较新任务状态。
            current = not old or event["occurred_at"] >= json.loads(old[0])["occurred_at"]
            notice = False
            if current and event["state"] in {"completed", "failed", "waiting_user"}:
                notice = db.execute("INSERT OR IGNORE INTO notices VALUES (?,?)", (key, event["state"])).rowcount > 0
            db.execute("INSERT INTO events(id,task_id,state,time,notify,payload) VALUES (?,?,?,?,?,?)", (event["event_id"], key, event["state"], event["occurred_at"], int(notice), encoded))
            if current:
                db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?)", (key, encoded))
        return True

    def snapshot(self):
        """读取任务与最近事件。@return 快照字段 @author Kai @date 2026/10/23 15:51"""
        with self.connect() as db:
            tasks = [json.loads(row[0]) for row in db.execute("SELECT payload FROM tasks")]
            events = [{**json.loads(row[0]), "notify": bool(row[1]), "handled": bool(row[2])} for row in db.execute("SELECT payload,notify,handled FROM events ORDER BY rowid DESC LIMIT 100")]
        return {"agents": sorted(tasks, key=lambda item: item["occurred_at"], reverse=True), "events": events}

    def review(self, event_id):
        """持久化已查看结果并跨设备同步。@param event_id 完成事件标识 @return 是否找到任务 @author Kai @date 2026/10/23 16:38"""
        with self.connect() as db:
            for key, encoded in db.execute("SELECT id,payload FROM tasks").fetchall():
                task = json.loads(encoded)
                if task.get("event_id") == event_id:
                    task["viewed"] = True
                    db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task, ensure_ascii=False), key))
                    db.execute("UPDATE events SET handled=1 WHERE id=?", (event_id,))
                    return True
        return False
