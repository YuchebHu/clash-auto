#!/usr/bin/env bash
# 订阅更新后，把两处改动重新打进订阅配置：
#   1. sniffer.enable = true   —— 客户端不嗅探时，自带 DoH 的程序拿不到域名，
#                                 域名规则全部失效，只能落到 GEOIP/MATCH
#   2. GEOSITE,cn,DIRECT       —— 插在 GEOIP,CN 之前，补上 .com 的国内站点
#
# 为什么需要单独一个脚本：订阅一更新，profiles/*.yaml 会被机场的新配置整个覆盖。
# 关掉自动更新可以避免静默覆盖，但手动更新后仍然要补。
#
# 用法：
#   bash clash_fix_profile.sh                    # 自动找 profiles 下最新的 .yaml
#   bash clash_fix_profile.sh /path/to.yaml      # 指定文件
#   SKIP_RESTART=1 bash clash_fix_profile.sh     # 只改文件，不重启客户端
#
# 可用环境变量覆盖默认位置：
#   CLASH_DIR   客户端数据目录   （默认 ~/.local/share/com.follow.clash）
#   FLCLASH_BIN 客户端可执行文件 （默认 /usr/bin/FlClash）
set -euo pipefail

CLASH_DIR="${CLASH_DIR:-$HOME/.local/share/com.follow.clash}"
FLCLASH_BIN="${FLCLASH_BIN:-/usr/bin/FlClash}"

PROFILE="${1:-}"
if [ -z "$PROFILE" ]; then
  PROFILE=$(ls -t "$CLASH_DIR"/profiles/*.yaml 2>/dev/null | head -1 || true)
fi
if [ -z "${PROFILE:-}" ] || [ ! -f "$PROFILE" ]; then
  echo "找不到订阅配置文件；用 bash $0 /path/to/profile.yaml 指定"
  exit 1
fi

if [ "${SKIP_RESTART:-0}" != "1" ]; then
  echo "==> 退出客户端"
  pkill -TERM -f "^${FLCLASH_BIN}$" 2>/dev/null || true
  for _ in $(seq 1 20); do
    pgrep -f "^${FLCLASH_BIN}$" >/dev/null || break
    sleep 0.5
  done
fi

echo "==> 备份"
cp -a "$PROFILE" "$PROFILE.bak-$(date +%Y%m%d-%H%M%S)"

echo "==> 打补丁"
python3 - "$PROFILE" <<'PY'
import sys

path = sys.argv[1]
s = open(path, encoding="utf-8").read()

sniffer = """sniffer:
    enable: true
    override-destination: true
    force-dns-mapping: true
    parse-pure-ip: true
    skip-domain: ['+.push.apple.com', '+.apple.com']
    sniff:
        TLS: { ports: [443, 8443] }
        HTTP: { ports: [80, 8080, 8081], override-destination: true }
        QUIC: { ports: [443, 8443] }
"""

if "\nsniffer:" not in s:
    s = s.replace("\nproxies:\n", "\n" + sniffer + "proxies:\n", 1)

if "GEOSITE,cn,DIRECT" not in s:
    s = s.replace("    - 'GEOIP,CN,DIRECT'",
                  "    - 'GEOSITE,cn,DIRECT'\n    - 'GEOIP,CN,DIRECT'", 1)

open(path, "w", encoding="utf-8").write(s)
print("   sniffer:", "\nsniffer:" in s, "| GEOSITE,cn:", "GEOSITE,cn,DIRECT" in s)
PY

echo "==> 校验"
python3 - "$PROFILE" <<'PY'
import sys
import yaml

d = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
rules = d.get("rules") or []
pos = {r: i for i, r in enumerate(rules)}
sniffer_on = (d.get("sniffer") or {}).get("enable") is True
geo = pos.get("GEOSITE,cn,DIRECT")
geoip = pos.get("GEOIP,CN,DIRECT")
print(f"   YAML 解析成功，规则 {len(rules)} 条")
print(f"   sniffer.enable = {sniffer_on}")
if geo is not None and geoip is not None:
    print(f"   GEOSITE,cn 在第 {geo} 条，GEOIP,CN 在第 {geoip} 条 "
          f"（{'顺序正确' if geo < geoip else '顺序有问题'}）")
else:
    print("   ⚠ 没找到 GEOSITE,cn / GEOIP,CN 规则，配置结构可能和预期不同")
PY

if [ "${SKIP_RESTART:-0}" != "1" ]; then
  echo "==> 启动客户端"
  setsid nohup "$FLCLASH_BIN" >/dev/null 2>&1 </dev/null &
  sleep 10
  if pgrep -f "^${FLCLASH_BIN}$" >/dev/null; then
    echo "   已启动"
  else
    echo "   启动失败，请手动打开客户端"
  fi
fi
