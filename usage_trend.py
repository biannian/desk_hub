# Copyright (c) iGroup.
# All rights reserved.
"""本机近七日 Token 增量趋势。

@author Kai
@date 2026/11/02 09:58
@description 只缓存数值事件，按会话去重，缺失记录不视为零用量。
"""

import json
import time
from datetime import datetime, timedelta, timezone



class UsageTrend:
    """增量缓存日志文件的用量事件。

    @author Kai
    @date 2026/11/02 09:58
    @description 初次基线与计数回退保守处理，不将累计值归入单日。
    """

    def __init__(self, root):
        """初始化数值缓存。@param root 会话日志目录 @author Kai @date 2026/11/02 09:58"""
        # 缓存文件签名与纯数值事件；不保存日志正文。
        self.root = root
        self.files = {}
        self.last_poll = 0
        self.snapshot = {}

    def poll(self):
        """每分钟刷新一次趋势。@return 七天统计快照 @author Kai @date 2026/11/02 09:58"""
        if self.snapshot and time.monotonic() - self.last_poll < 60:
            return self.snapshot
        now = datetime.now(timezone(timedelta(hours=8)))
        cutoff = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        events = []
        incomplete = False
        live = set()
        for path in self.root.rglob("*.jsonl"):
            try:
                stat = path.stat()
                if stat.st_mtime < cutoff.timestamp():
                    continue
                live.add(path)
                signature = (stat.st_size, stat.st_mtime_ns)
                cached = self.files.get(path)
                if not cached or cached[0] != signature:
                    values = []
                    session = str(path)
                    with path.open(encoding="utf-8") as stream:
                        for line in stream:
                            if not line.endswith("\n"):
                                continue
                            try:
                                record = json.loads(line)
                                payload = record.get("payload", {})
                                if record.get("type") == "session_meta":
                                    session = str(payload.get("id") or session)
                                if record.get("type") != "event_msg" or payload.get("type") != "token_count":
                                    continue
                                info = payload.get("info") or {}
                                total = (info.get("total_token_usage") or {}).get("total_tokens")
                                last = (info.get("last_token_usage") or {}).get("total_tokens")
                                stamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
                                if stamp.tzinfo and type(total) is int and total >= 0:
                                    values.append((session, stamp.timestamp(), total, last if type(last) is int else None))
                            except (ValueError, TypeError, KeyError, AttributeError):
                                incomplete = True
                    self.files[path] = (signature, values)
                events.extend(self.files[path][1])
            except (OSError, UnicodeError):
                incomplete = True
        self.files = {key: value for key, value in self.files.items() if key in live}
        self.snapshot = self.aggregate(events, now, incomplete)
        self.last_poll = time.monotonic()
        return self.snapshot

    @staticmethod
    def aggregate(events, now, incomplete=False):
        """按中国日期累计会话增量。

        @param events 会话、事件时间、累计值和最近一次用量
        @param now 当前带时区时间
        @param incomplete 是否存在不可读日志
        @return 缺失日期为空的七日趋势
        @author Kai
        @date 2026/11/02 09:58
        """
        zone = timezone(timedelta(hours=8))
        today = now.astimezone(zone).date()
        days = {str(today - timedelta(days=i)): {"date": str(today - timedelta(days=i)), "tokens": None, "partial": False} for i in range(6, -1, -1)}
        previous = {}
        # 同会话相同事件去重，累计值重复不会重复收费；回退时只建立新基线。
        for session, stamp, total, last in sorted(set(events), key=lambda item: (item[1], item[2])):
            baseline = previous.get(session)
            previous[session] = total
            day = days.get(str(datetime.fromtimestamp(stamp, zone).date()))
            if day is None:
                continue
            delta = total - baseline if baseline is not None and total >= baseline else None
            if delta is None:
                day["partial"] = True
                delta = last if baseline is None and type(last) is int and 0 <= last <= total else 0
            day["tokens"] = (day["tokens"] or 0) + delta
        observed = [day for day in days.values() if day["tokens"] is not None]
        total = sum(day["tokens"] for day in observed)
        return {"days": list(days.values()), "total": total if observed else None,
                "daily_average": round(total / len(observed)) if observed else None,
                "observed_days": len(observed), "partial": incomplete or any(day["partial"] for day in observed),
                "updated_at": now.isoformat()}
