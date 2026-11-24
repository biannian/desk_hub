# Copyright (c) iGroup.
# All rights reserved.
"""看板 EXE 入口。@author Kai @date 2026/10/23 16:57 @description 隐藏终端、日志落盘、端口已运行时退出。"""

import logging
import socket
import sys
from pathlib import Path


def main():
    """启动独立看板，不重复占用端口。@author Kai @date 2026/10/23 16:57"""
    # 合并目录只共享考勤导出文件，两个进程互不改变原业务逻辑。
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    with socket.socket() as probe:
        try:
            probe.bind(("0.0.0.0", 8765))
        except OSError:
            return
    logging.basicConfig(filename=root / "desk_hub.log", level=logging.INFO, encoding="utf-8")
    import uvicorn
    from desk_hub.server import app
    uvicorn.run(app, host="0.0.0.0", port=8765, log_config=None)


if __name__ == "__main__":
    main()
