# Desk Hub

独立 FastAPI 页面，只读现有助手的 `status.json` 和 `cache.json`，不修改助手、不访问考勤接口。前端每秒本地倒计时；服务每两秒检查本地文件并推送变化/心跳。

## 启动

在项目根目录 PowerShell 执行：

```powershell
uv venv desk_hub/.venv --python windows/.venv/Scripts/python.exe
uv pip install --python desk_hub/.venv/Scripts/python.exe -r desk_hub/requirements.txt
$env:DESK_HUB_SOURCE = 'D:\Code\AttendanceHelper\windows\dist'
desk_hub/.venv/Scripts/python.exe -m uvicorn desk_hub.server:app --host 0.0.0.0 --port 8765
```

若使用源码版助手，将 `DESK_HUB_SOURCE` 改为 `windows` 的绝对目录。服务首次启动生成 `desk_hub/data/access-token.txt`。手机打开 `http://电脑当前局域网IP:8765`，输入此令牌。用 `ipconfig` 查看无线/以太网地址；先在电脑打开 `http://127.0.0.1:8765` 验证，再测试手机。不要使用 VPN、虚拟网卡地址。

防火墙仅在需要时手动允许该 Python 的 TCP 8765 入站，限定 Private 域和手机 IP，勿开放 Public 域或路由器端口映射。公司访客 Wi-Fi 的设备隔离可能阻止访问，需要实际测试；本次未更改防火墙。HTTP 令牌只能在信任的局域网使用，需要更强传输保护时配置 HTTPS。令牌授权持有者，防火墙 IP 限制设备。

## 数据边界

- 原助手真实查询后更新 `status.json`，服务观察文件变化推送，完全不改原助手写入行为。助手没有运行/没有获取当天记录时页面显示不可用。
- `status.json` 只有分钟精度，无独立接口成功时间；页面“最近核验”是其导出更新时间。查询失败会显示不可用；服务离线后保留快照并标过期，停止倒计时。
- 目标时间直接复用助手导出的 `target_time`，含现有请假及有效工时规则，不另算固定九小时。
- 休息日隐藏详情。复用助手年份节假日缓存中的 holiday/调休条目；缓存缺失或超过 72 小时标未知，不调用外网也不假装已知。工作日未知时保留标识及真实考勤，不错误隐藏。
- 按后续确认的电脑端展示规则：当日倒计时始终按当前时间推进，下班卡仅作详情，不展示“工时不足”。到点显示“可以下班了”，不将其描述为最终考勤正常；此调整覆盖原需求中的下班卡冻结展示规则。
- 不上传姓名、代码、提示词或工具调用内容。任务摘要由已授权 Adapter 主动上报。

## Agent 接入

`POST /api/agents/events` 需要 `Authorization: Bearer <令牌>`。可通过 `DESK_HUB_ADAPTER_TOKEN` 配置独立写入令牌，浏览器继续使用只读令牌。

事件示例（时间必须带时区，每轮执行使用不同 task_id）：

```json
{"event_id":"unique-event-1","task_id":"run-1","tool":"custom","title":"构建任务","state":"waiting_user","occurred_at":"2026-10-23T16:00:00+08:00","reason":"等待批准","summary":"需要在原工具中处理","source":"webhook","confidence":"observed"}
```

支持 queued/running/waiting_user/completed/failed/interrupted/unknown。SQLite 保存任务、事件、通知是否处理；重复 event_id 丢弃，相同任务相同重点状态只提醒一次；延迟旧事件不会覆盖新状态。无事件不推断失败，不显示虚假百分比。

`python -m desk_hub.adapter cursor` 从 stdin 接收一条 Cursor Hooks JSON，配置 `sessionStart`、`beforeSubmitPrompt`、`stop` 指向此命令。必须设置运行目录为本项目根目录，并设置 `DESK_HUB_ADAPTER_TOKEN`。Windows 命令应使用 Python 的绝对路径；不要覆盖已有 hooks.json，请手动合并。未自动修改用户 Hooks。Cursor 的等待审批目前没有可靠事件映射，保留未知，不猜测。

`python -m desk_hub.adapter codex` 可转换已经由自建 App Server 客户端转发的单条生命周期/审批事件。**不会自动订阅桌面 Codex 已打开的所有会话**。需在实际客户端获得事件后转发到 stdin；新开 app-server 进程也不等于能观察其他进程。ChatGPT Work 没有已验证自动回调，页面明确标记未接入，可用手动统一事件。

协议参考：[Cursor Hooks](https://cursor.com/docs/hooks)、[Codex App Server](https://developers.openai.com/codex/app-server)。这些适配器仅通过模拟协议事件验证，尚未做真实工具端联调。

## 验证与限制

```powershell
desk_hub/.venv/Scripts/python.exe -m pytest desk_hub/tests -q
```

手机局域网连通、全屏、常亮、断网恢复需实际设备验收。浏览器后台/熄屏可能暂停 JS，回到前台后按当前时间重算；服务不会让助手增加轮询。页面重载后仍保留 SQLite 任务，浏览器只缓存最后快照。第一版看板内提醒，不发送系统通知。

## 本机 Codex 自动观察

新增只读监听当天 `.codex/sessions` 日志，仅识别 task_started/task_complete/turn_aborted。只存任务标识、事件时间及状态，不存对话、代码和工具内容；来源标记 local-session-log。此格式为本机观察得到的非稳定内部格式，升级后可能需要适配；未观测到明确事件时不推断失败或成功。不等于自动接入 ChatGPT 网页版；Cursor 仍需配置 Hooks。屏幕每个会话只展示最近一轮，离线保留同步时刻的画面并用底部小字标记。

## 2026-10-26 单 EXE 整合

Windows 正式入口现为 AttendanceHelper.exe，Qt 助手在同一进程的后台线程启动看板，退出助手会停止看板。无需另开 DeskHub.exe 或 VBS。命令行 --check 不启动看板；--desk-hub-only 为构建烟测保留。页面资源打包内置，用户配置及 desk_hub-data 仍保存在 EXE 旁。更新时保留这些数据目录，先退出旧助手和旧独立看板再启动新版本。
