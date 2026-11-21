#!/usr/bin/env bash
# 检测每个代理节点是否真的能用 opencode（不是测 ping，是测真实请求）
#
# 用法：
#   bash clash_check_nodes.sh              # 精确版（目标接口较大，但只读响应头）
#   LIGHT=1 bash clash_check_nodes.sh      # 轻量版（测 opencode.ai 上的小文件）
#   GROUP=你的组名 bash clash_check_nodes.sh
#
# 依赖 ~/.config/clash_auto.json 里的 group，或 GROUP 环境变量
set -uo pipefail

CLASH_CFG="${CLASH_CONFIG:-$HOME/.local/share/com.follow.clash/config.yaml}"
AUTO_CFG="${CLASH_AUTO_CONF:-$HOME/.config/clash_auto.json}"

[ -f "$CLASH_CFG" ] || { echo "找不到 $CLASH_CFG"; exit 1; }

SECRET=$(grep -m1 '^secret:' "$CLASH_CFG" | sed 's/.*"\(.*\)".*/\1/')
PORT=$(grep -m1 '^external-controller:' "$CLASH_CFG" | sed 's/.*"\(.*\)".*/\1/')
API="http://$PORT"

if [ -z "${GROUP:-}" ] && [ -f "$AUTO_CFG" ]; then
  GROUP=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('group') or '')" "$AUTO_CFG" 2>/dev/null)
fi
[ -n "${GROUP:-}" ] || { echo '未配置代理组名：设置 ~/.config/clash_auto.json 的 "group" 或用 GROUP=xxx'; exit 1; }

if [ "${LIGHT:-0}" = "1" ]; then
  TARGET="https://opencode.ai/robots.txt"
else
  TARGET="https://models.opencode.ai/experimental/v2.0/api.json"
fi
PING="http://www.gstatic.com/generate_204"

python3 - "$API" "$SECRET" "$TARGET" "$PING" "$GROUP" <<'PY'
import json, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

api, secret, target, ping, GROUP = sys.argv[1:6]
SKIP_TYPE = ("Selector", "URLTest", "Fallback", "LoadBalance")


def get(path, timeout=25):
    req = urllib.request.Request(api + path, headers={"Authorization": "Bearer " + secret})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def delay(name, url, timeout):
    q = urllib.parse.quote(name, safe="")
    u = urllib.parse.quote(url, safe="")
    try:
        return get(f"/proxies/{q}/delay?url={u}&timeout={timeout}").get("delay")
    except Exception:
        return None


d = get("/proxies")["proxies"]
nodes = [n for n in d[GROUP]["all"] if d.get(n, {}).get("type") not in SKIP_TYPE]


def job(n):
    return n, delay(n, ping, 5000), delay(n, target, 8000)


with ThreadPoolExecutor(max_workers=6) as ex:
    rows = list(ex.map(job, nodes))

print(f"探测目标: {target}\n")
print(f"{'节点':<30}{'ping':>7}{'opencode':>10}   判定")
ok = []
for n, p, o in rows:
    verdict = ("可用" if p and o else "ping通/opencode不通" if p
               else "ping超时/opencode可用" if o else "不可用")
    if p and o:
        ok.append((o, n))
    print(f"{n[:28]:<30}{str(p or '超时'):>7}{str(o or '超时'):>10}   {verdict}")

print("\n按 opencode 延迟排序（可用的）：")
for ms, n in sorted(ok):
    print(f"  {ms:>5}ms  {n}")
print(f"\n可用 {len(ok)} / {len(nodes)}")
PY
