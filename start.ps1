# Copyright (c) iGroup.
# All rights reserved.
<#
独立看板启动入口。
@author Kai
@date 2026/10/23 16:01
@description 使用独立虚拟环境，不启动或修改原助手。
#>
param(
    # 助手导出目录；源码运行时改为 windows 目录。
    [string]$Source = (Join-Path $PSScriptRoot '../windows/dist'),
    # 服务端口，手机需使用同一端口访问。
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
# 使用脚本位置定位运行目录，避免从资源管理器打开时找不到模块。
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:DESK_HUB_SOURCE = (Resolve-Path -LiteralPath $Source).Path
$deskPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $deskPython)) {
    throw '请先按 README 安装 Desk Hub 独立环境。'
}
Write-Host "Desk Hub: http://127.0.0.1:$Port"
Write-Host "访问令牌文件：$PSScriptRoot/data/access-token.txt"
& $deskPython -m uvicorn desk_hub.server:app --host 0.0.0.0 --port $Port
