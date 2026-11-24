# Copyright (c) iGroup.
# All rights reserved.
"""只读查询当前登录 Cursor 的套餐余量。

@author Kai
@date 2026/11/20 09:25
@description 令牌只在当次请求中使用，不写入看板缓存，也不记录到日志。
"""

import asyncio
import base64
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SUMMARY_URL = "https://cursor.com/api/usage-summary"
PERIOD_URL = "https://cursor.com/api/dashboard/get-current-period-usage"


def state_database():
    """定位 Cursor 本地登录状态库。

    @return 状态库路径；未安装时返回空
    @author Kai
    @date 2026/11/20 09:25
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    path = Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return path if path.exists() else None


def read_access_token(path=None):
    """以只读方式取出当前登录令牌。

    @param path 可选状态库路径
    @return 访问令牌；未登录时返回空字符串
    @author Kai
    @date 2026/11/20 09:25
    """
    path = path or state_database()
    if not path:
        return ""
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            row = db.execute("SELECT value FROM ItemTable WHERE key=?", ("cursorAuth/accessToken",)).fetchone()
    except (OSError, sqlite3.Error):
        return ""
    if not row or not isinstance(row[0], str):
        return ""
    return row[0].strip().strip('"')


def _jwt_sub(token):
    """读取令牌里的用户标识，不校验签名。

    @param token 访问令牌
    @return 用户标识；格式不符时返回空
    @author Kai
    @date 2026/11/20 09:25
    """
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return ""
    subject = data.get("sub") if isinstance(data, dict) else ""
    return subject if isinstance(subject, str) else ""


def _number(value):
    """接受接口里的数字或数字字符串。

    @param value 原始字段
    @return 有限数字；无法解析时返回空
    @author Kai
    @date 2026/11/20 09:25
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _epoch_seconds(value):
    """把账期时间统一成 Unix 秒。

    @param value ISO 时间或毫秒/秒时间戳
    @return Unix 秒；无法解析时返回空
    @author Kai
    @date 2026/11/20 09:25
    """
    number = _number(value)
    if number is not None:
        return number / 1000 if number > 10**11 else number
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def _cycle(payload):
    """读取账期结束时刻和窗口长度。

    @param payload 用量响应
    @return 重置时刻秒数和窗口分钟数
    @author Kai
    @date 2026/11/20 09:25
    """
    if not isinstance(payload, dict):
        return None, None
    end = _epoch_seconds(payload.get("billingCycleEnd"))
    start = _epoch_seconds(payload.get("billingCycleStart"))
    minutes = round((end - start) / 60) if start and end and end > start else None
    return (int(end) if end else None), (int(minutes) if minutes and minutes > 0 else None)


def _window(label, used_percent, resets_at, minutes):
    """把已用百分比转换成看板余量窗口。

    @param label 窗口名称
    @param used_percent 已用百分比
    @param resets_at 重置时刻
    @param minutes 窗口分钟数
    @return 余量窗口；百分比无效时返回空
    @author Kai
    @date 2026/11/20 09:25
    """
    if used_percent is None:
        return None
    used = min(100.0, max(0.0, used_percent))
    # 空名称表示不再重复「套餐额度」，只保留剩余比例。
    window = {"remaining_percent": round(100 - used, 1), "resets_at": resets_at}
    if label:
        window["label"] = label
    if minutes:
        window["minutes"] = minutes
    return window


def _plan_windows(plan, resets_at, minutes, label):
    """从套餐对象提取至多两个余量窗口。

    @param plan 套餐用量对象
    @param resets_at 重置时刻
    @param minutes 窗口分钟数
    @param label 主窗口名称
    @return 余量窗口列表
    @author Kai
    @date 2026/11/20 09:25
    """
    if not isinstance(plan, dict):
        return []
    windows = []
    total = _number(plan.get("totalPercentUsed"))
    auto = _number(plan.get("autoPercentUsed"))
    api = _number(plan.get("apiPercentUsed"))
    primary = _window(label, total if total is not None else auto, resets_at, minutes)
    if primary:
        windows.append(primary)
    if api is not None and not any(abs(api - (100 - item["remaining_percent"])) <= 0.5 for item in windows):
        extra = _window("API 额度", api, resets_at, minutes)
        if extra:
            windows.append(extra)
    if windows:
        return windows[:2]
    used = _number(plan.get("used"))
    limit = _number(plan.get("limit"))
    if used is None or not limit or limit <= 0:
        return []
    remaining = _number(plan.get("remaining"))
    percent = (limit - remaining) / limit * 100 if remaining is not None else used / limit * 100
    window = _window(label, percent, resets_at, minutes)
    return [window] if window else []


def _unused(windows):
    """判断一组窗口是否还没有实际消耗。

    @param windows 余量窗口
    @return 没有窗口或全部仍为百分之百
    @author Kai
    @date 2026/11/20 09:25
    """
    return not windows or all(item.get("remaining_percent") == 100 for item in windows)


def normalize_cursor_usage(summary, period=None):
    """选择和看板一致的 Cursor 余量窗口。

    @param summary usage-summary 响应
    @param period 当前周期响应，可空
    @return 余量窗口列表
    @author Kai
    @date 2026/11/20 09:25
    """
    summary = summary if isinstance(summary, dict) else {}
    resets_at, minutes = _cycle(summary)
    individual = summary.get("individualUsage") if isinstance(summary.get("individualUsage"), dict) else {}
    windows = _plan_windows(individual.get("plan"), resets_at, minutes, "")
    if not windows:
        windows = _plan_windows(individual.get("overall"), resets_at, minutes, "")
    if not windows:
        team = summary.get("teamUsage") if isinstance(summary.get("teamUsage"), dict) else {}
        windows = _plan_windows(team.get("pooled"), resets_at, minutes, "团队额度")
    period_windows = []
    if isinstance(period, dict):
        period_end, period_minutes = _cycle(period)
        period_windows = _plan_windows(period.get("planUsage"), period_end, period_minutes, "")
    membership = str(summary.get("membershipType") or "")
    period_days = (period_minutes or 0) / 1440 if isinstance(period, dict) else 0
    if period_windows and (membership.startswith("unified_") or _unused(windows) or 0 < period_days < 27):
        return period_windows
    if windows:
        return windows
    if summary.get("isUnlimited") is True:
        unlimited = _window("不限额度", 0, resets_at, minutes)
        return [unlimited] if unlimited else []
    return period_windows


def _request(url, token, method="GET", body=None):
    """携带登录态请求 Cursor 用量接口。

    @param url 接口地址
    @param token 访问令牌
    @param method 请求方法
    @param body 可选 JSON 正文
    @return 解析后的对象
    @author Kai
    @date 2026/11/20 09:25
    """
    headers = {"Accept": "application/json", "User-Agent": "desk-hub", "Origin": "https://cursor.com"}
    subject = _jwt_sub(token)
    if subject:
        headers["Cookie"] = "WorkosCursorSessionToken=" + urllib.parse.quote(subject + "::" + token, safe="")
    else:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError("signed_out") from None
        raise RuntimeError("quota_query_failed") from None
    if not isinstance(payload, dict):
        raise RuntimeError("quota_query_failed")
    return payload


def fetch_usage(token):
    """读取套餐摘要，周额度账号再补当前周期。

    @param token 访问令牌
    @return 摘要和当前周期
    @author Kai
    @date 2026/11/20 09:25
    """
    summary = _request(SUMMARY_URL, token)
    period = None
    membership = str(summary.get("membershipType") or "")
    summary_unused = summary.get("isUnlimited") is not True and _unused(normalize_cursor_usage(summary))
    if membership.startswith("unified_") or summary_unused:
        try:
            period = _request(PERIOD_URL, token, "POST", {})
        except (RuntimeError, OSError, ValueError, TimeoutError):
            period = None
    return summary, period


class CursorQuota:
    """独立于任务的一分钟 Cursor 额度轮询器。

    @author Kai
    @date 2026/11/20 09:25
    @description 查询失败时保留上次余量，不用会话活跃度代替账户额度。
    """

    def __init__(self, cache):
        """初始化持久化余量。

        @param cache 看板缓存路径
        @author Kai
        @date 2026/11/20 09:25
        """
        # 只保存余量数字和更新时间，不保存登录令牌。
        self.cache = cache
        self.snapshot = {}
        self.status = "pending"
        # 由看板按配置决定是否查询；默认允许，关闭后跳过本轮。
        self.allowed = lambda: True
        try:
            saved = json.loads(Path(cache).read_text(encoding="utf-8"))
            if isinstance(saved, dict) and isinstance(saved.get("limits"), list):
                self.snapshot = saved
        except (OSError, ValueError):
            pass

    def refresh(self):
        """刷新真实余量；失败保留历史读数。

        @author Kai
        @date 2026/11/20 09:25
        """
        try:
            token = read_access_token()
            if not token:
                self.status = "signed_out"
                return
            summary, period = fetch_usage(token)
            windows = normalize_cursor_usage(summary, period)
            if not windows:
                raise RuntimeError("quota_missing")
            self.snapshot = {
                "limits": windows,
                "limits_updated_at": datetime.now(timezone.utc).isoformat(),
                "quota_source": "account_query",
            }
            self.status = "ok"
            temporary = Path(self.cache).with_suffix(".tmp")
            temporary.write_text(json.dumps(self.snapshot), encoding="utf-8")
            temporary.replace(self.cache)
        except RuntimeError as exc:
            self.status = "signed_out" if str(exc) == "signed_out" else "unavailable"
        except (OSError, ValueError, TimeoutError, TypeError):
            self.status = "unavailable"

    async def run(self):
        """启动即查询，此后每分钟刷新。

        @author Kai
        @date 2026/11/20 09:25
        """
        if os.environ.get("DESK_HUB_CURSOR_QUOTA") == "off":
            while True:
                await asyncio.sleep(3600)
        while True:
            if self.allowed():
                await asyncio.to_thread(self.refresh)
            await asyncio.sleep(60)
