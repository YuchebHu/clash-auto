#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clash 节点自动选优。

规则：
  1. 当前节点可用（Clash 的 delay 测试能返回毫秒数）→ 保持不变
     开启 require_opencode 时，还要求它能真的请求通 opencode 接口
  2. 当前节点超时/失败 → 并发测试该组所有真实节点，按延迟升序取第一个
     （开启 require_opencode 时，opencode 不通的节点直接淘汰，按 opencode 延迟排序）
  3. 切换失败或全部超时 → 保持原状并记日志，不乱切

依赖 Clash 的 External Controller（HTTP API）。
配置文件：~/.config/clash_auto.json
  {
    "controller": "127.0.0.1:9090",
    "secret": "xxx",
    "group": "你的代理组名",
    "test_url": "https://www.gstatic.com/generate_204",
    "timeout_ms": 3000,
    "workers": 12
  }

用法：
  clash_auto.py            # 正常执行
  clash_auto.py --status   # 只打印当前节点和延迟
  clash_auto.py --dry-run  # 只测速不乱切
"""
import argparse
import json
import re
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CONF = os.path.expanduser("~/.config/clash_auto.json")
LOG = os.path.expanduser("~/.local/state/clash_auto.log")

DEFAULTS = {
    "controller": "127.0.0.1:9090",
    "secret": "",
    "group": "",
    "test_url": "https://www.gstatic.com/generate_204",
    "timeout_ms": 3000,
    "workers": 12,
    # 当前节点延迟超过该值即视为不可用，重新择优（0=关闭）
    "max_delay_ms": 500,
    # 择优时优先选倍率 <= 该值的节点（0.1 = 一折，0.01 = 更便宜）
    "prefer_multiplier": 0.1,
    # 通过代理实测下载速度选节点
    "prefer_speed": True,
    "proxy_addr": "http://127.0.0.1:7890",
    "speed_url": "https://speed.cloudflare.com/__down?bytes=262144",
    "speed_bytes": 262144,
    "speed_timeout_ms": 8000,
    "speed_top": 5,          # 只测延迟最好的前 N 个
    "speed_enough_bps": 0,   # >0 时，达到该速度就提前结束测速
    # —— opencode 可用性检测 ——
    # 延迟测试走的是 gstatic，跟 opencode 能不能用没关系：有的节点延迟很低
    # 但连不上 models.opencode.ai。开启后，不通的节点不参与择优。
    "require_opencode": True,
    "opencode_url": "https://models.opencode.ai/experimental/v2.0/api.json",
    "opencode_timeout_ms": 8000,
    # 万一所有节点都过不了 opencode 检测，是否退回「只按延迟」择优
    "opencode_fallback": True,
}

# 这些不是真实节点，不参与择优
SKIP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}


def log(msg):
    line = f"{time.strftime('%F %T')} {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _clash_secret():
    """FlClash 每次重启/升级都会重新生成 secret，优先从它的 config.yaml 读，
    避免像升级后那样一直 401。可用 CLASH_CONFIG 环境变量指向别处。"""
    path = os.environ.get("CLASH_CONFIG") or os.path.expanduser(
        "~/.local/share/com.follow.clash/config.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("secret:"):
                    return line.split(":", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def load_conf():
    c = dict(DEFAULTS)
    if os.path.exists(CONF):
        with open(CONF, encoding="utf-8") as f:
            c.update(json.load(f))
    s = _clash_secret()
    if s:
        c["secret"] = s          # 以 FlClash 当前实际使用的为准
    return c


class Api:
    def __init__(self, conf):
        self.base = f"http://{conf['controller']}"
        self.secret = conf.get("secret") or ""
        self.timeout = conf["timeout_ms"] / 1000 + 5

    def _req(self, method, path, body=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        if self.secret:
            req.add_header("Authorization", f"Bearer {self.secret}")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw.strip() else {}

    def proxies(self):
        return self._req("GET", "/proxies").get("proxies", {})

    def delay(self, name, test_url, timeout_ms):
        q = urllib.parse.urlencode({"url": test_url, "timeout": timeout_ms})
        p = f"/proxies/{urllib.parse.quote(name, safe='')}/delay?{q}"
        try:
            d = self._req("GET", p)
            return int(d.get("delay", -1))
        except urllib.error.HTTPError:
            return -1          # 超时/失败时 Clash 返回 504
        except Exception:
            return -1

    def select(self, group, name):
        self._req("PUT", f"/proxies/{urllib.parse.quote(group, safe='')}",
                  {"name": name})


def multiplier_of(name):
    """从节点名解析倍率，如 '🇯🇵日本专线-0.1倍率' -> 0.1；没有则视为 1.0"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*倍率", name)
    return float(m.group(1)) if m else 1.0


def probe(api, name, conf):
    """返回 (通用延迟, opencode 延迟)；没开 opencode 检测时第二项恒为 -1。
    两者都是真实请求的往返时延，失败返回 -1。"""
    d = api.delay(name, conf["test_url"], conf["timeout_ms"])
    oc = -1
    if conf.get("require_opencode"):
        oc = api.delay(name, conf["opencode_url"], conf["opencode_timeout_ms"])
    return d, oc


def measure_speed(proxy_addr, url, nbytes, timeout):
    """通过代理下载 nbytes 字节，返回 bytes/s（失败为 0）。"""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_addr, "https": proxy_addr}))
    start = time.time()
    got = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "clash-auto/1.0"})
        with opener.open(req, timeout=timeout) as r:
            while got < nbytes:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
    except Exception:
        return 0, got
    dt = max(time.time() - start, 0.001)
    return got / dt, got


def candidates(proxies, group):
    g = proxies.get(group)
    if not g:
        raise SystemExit(f"找不到代理组：{group}")
    out = []
    for name in g.get("all", []):
        p = proxies.get(name)
        if not p:
            continue
        if p.get("type") in SKIP_TYPES:
            continue           # 跳过 自动选择/故障转移 等嵌套组
        if "剩余" in name or "到期" in name or "套餐" in name:
            continue           # 跳过机场信息条目
        out.append(name)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="忽略「当前节点可用」直接重新择优")
    args = ap.parse_args()

    conf = load_conf()
    api = Api(conf)
    group = conf["group"]
    if not group:
        log('未配置代理组名：请在 ~/.config/clash_auto.json 里设置 "group"')
        return 2

    try:
        proxies = api.proxies()
    except Exception as e:
        log(f"无法连接 Clash API（{conf['controller']}）：{e}")
        return 2

    g = proxies.get(group)
    if not g:
        log(f"代理组不存在：{group}")
        return 2

    cur = g.get("now")
    require_oc = bool(conf.get("require_opencode"))
    # 延迟/opencode 测试偶尔会假失败（节点实际能传数据），最多试 3 次再判定
    cur_delay, cur_oc = -1, -1
    if cur:
        for attempt, pause in enumerate((0, 0.6, 1.5)):
            if attempt:
                time.sleep(pause)
            cur_delay, cur_oc = probe(api, cur, conf)
            if cur_delay > 0 and (not require_oc or cur_oc > 0):
                break

    if args.status:
        extra = f" opencode={cur_oc}ms" if require_oc else ""
        log(f"组={group} 当前={cur} 延迟={cur_delay}ms{extra}")
        return 0

    max_delay = int(conf.get("max_delay_ms") or 0)
    delay_ok = cur_delay > 0 and (max_delay <= 0 or cur_delay <= max_delay)
    oc_ok = (not require_oc) or cur_oc > 0
    if delay_ok and oc_ok and not args.force:
        extra = f"，opencode {cur_oc}ms" if require_oc else ""
        log(f"当前节点可用：{cur} ({cur_delay}ms{extra})，保持不变")
        return 0

    if cur_delay <= 0:
        reason = "超时"
    elif not delay_ok:
        reason = f"延迟过高({cur_delay}ms>{max_delay}ms)"
    else:
        reason = f"opencode 不通(延迟 {cur_delay}ms 正常)"
    log(f"当前节点{reason}：{cur}，开始测试全部节点")
    nodes = candidates(proxies, group)
    if not nodes:
        log("没有可用候选节点")
        return 1

    with ThreadPoolExecutor(max_workers=conf["workers"]) as ex:
        results = list(ex.map(lambda n: (n,) + probe(api, n, conf), nodes))

    limit = int(conf.get("max_delay_ms") or 0) or 10 ** 9
    prefer = float(conf.get("prefer_multiplier") or 0)

    def rank(d, oc):
        """择优依据：开了 opencode 检测就用 opencode 真实延迟，否则用通用延迟"""
        return oc if (require_oc and oc > 0) else d

    ok = sorted([(rank(d, oc), n) for n, d, oc in results
                 if 0 < d < limit and (not require_oc or oc > 0)])
    delay_only = sorted([(d, n) for n, d, oc in results if 0 < d < limit])
    slow = [n for n, d, oc in results if d >= limit]
    bad = [n for n, d, oc in results if d <= 0]
    oc_bad = [n for n, d, oc in results if d > 0 and require_oc and oc <= 0]
    if require_oc:
        log(f"测速完成：延迟<{limit}ms 且 opencode 可用 {len(ok)} / 共 {len(nodes)}"
            f"（超时 {len(bad)}，过慢 {len(slow)}，opencode 不通 {len(oc_bad)}）")
    else:
        log(f"测速完成：<{limit}ms 可用 {len(ok)} / 共 {len(nodes)}"
            f"（超时 {len(bad)}，过慢 {len(slow)}）")

    if not ok and require_oc and conf.get("opencode_fallback") and delay_only:
        log("警告：没有节点能同时通过延迟和 opencode 检测，退回只按延迟择优")
        ok = delay_only

    if not ok:
        log("没有合格节点，保持原节点不切换")
        return 1

    cheap = [(d, n) for d, n in ok if multiplier_of(n) <= prefer]
    pool = cheap if cheap else ok
    tag = "低倍率" if cheap else "全部"
    basis = "延迟" if ok is delay_only else ("opencode" if require_oc else "延迟")
    log(f"候选池={tag}（低倍率 {len(cheap)} 个，按{basis}排序）；前 5："
        + ", ".join(f"{n}={d}ms" for d, n in pool[:5]))

    best_delay, best = pool[0]
    if conf.get("prefer_speed") and len(pool) > 1 and not args.dry_run:
        top = pool[:int(conf.get("speed_top") or 5)]
        log(f"实测下载速度（每次 {conf.get('speed_bytes')} 字节，最多测 {len(top)} 个）")
        speeds = []
        for d, n in top:
            try:
                api.select(group, n)
            except Exception as e:
                log(f"  切换 {n} 失败：{e}")
                continue
            time.sleep(0.3)
            # 预热：先拉一小段，避免把 TLS 握手算进速度
            warm_url = re.sub(r"bytes=\d+", "bytes=16384", conf["speed_url"])
            measure_speed(conf["proxy_addr"], warm_url, 16384,
                          min(4.0, conf["speed_timeout_ms"] / 1000))
            bps, got = measure_speed(conf["proxy_addr"], conf["speed_url"],
                                     int(conf["speed_bytes"]),
                                     conf["speed_timeout_ms"] / 1000)
            speeds.append((bps, n, d, got))
            log(f"  {n}: {bps/1024:.0f} KB/s ({got} 字节, 握手 {d}ms)")
            enough = float(conf.get("speed_enough_bps") or 0)
            if enough and bps >= enough:
                log(f"  已达到阈值 {enough/1024:.0f} KB/s，提前结束测速")
                break
        good = [x for x in speeds if x[0] > 0]
        if good:
            good.sort(reverse=True)
            bps, best, best_delay, _ = good[0]
            log(f"测速最优：{best} ({bps/1024:.0f} KB/s)")

    if args.dry_run:
        log(f"[dry-run] 将切换到 {best} ({best_delay}ms)，未做速度实测")
        return 0

    try:
        api.select(group, best)
        log(f"已切换：{cur} → {best} ({best_delay}ms)")
    except Exception as e:
        log(f"切换失败：{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
