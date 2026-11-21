#!/usr/bin/env bash
# 登录后延迟启动检测面板（等 Clash 的 API 就绪）
sleep 10
exec python3 "$(dirname "$(readlink -f "$0")")/opencode_panel.py"
