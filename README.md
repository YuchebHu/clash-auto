# clash-auto

围绕 Clash / mihomo 的节点自动择优工具。

和常见的自动选节点脚本不同，它不只测 `generate_204` 的 ping，而是把**真实业务请求**接进决策：如果你主要用 opencode，就让 opencode 的接口参与打分——因为「延迟 97ms」和「能不能用」经常是两回事。

## 为什么需要

Clash 界面上那个毫秒数来自 `http://www.gstatic.com/generate_204` 的往返时间。它能开，不代表别的服务能用：

- 有的节点 ping 只有 97ms，全组最快，但请求 opencode 接口 100% 失败
- 有的节点 ping 超时，但 opencode 却跑得通（线路只对部分目标友好）
- 节点的可用性是波动的，几分钟前不通、几分钟后又好了

所以单看 ping 会选到「看起来很快但干不了活」的节点，于是出现「明明有网，opencode 一直转圈」。

## 包含什么

| 文件 | 作用 |
|---|---|
| `clash_auto.py` | 核心。按计划任务运行，**只在当前节点真的失效时才**全量测速并切换 |
| `opencode_panel.py` | 本地网页面板，`ping` / `opencode` 两栏并排，支持一键切换、自动刷新 |
| `clash_check_nodes.sh` | 命令行版一次性全量检测，出问题时快速排查 |

## 依赖

- 任意 mihomo 内核的客户端（FlClash / Mihomo Party / Clash Verge Rev / OpenClash …）
- 已开启 External Controller（默认 `127.0.0.1:9090`）
- Python 3.8+，**只用标准库**，不需要 pip install

## 安装

```bash
git clone <this-repo> ~/codes/clash-auto
cd ~/codes/clash-auto

mkdir -p .
cp clash_auto.py opencode_panel.py opencode_panel_start.sh clash_check_nodes.sh ./
chmod +x ./*.sh

mkdir -p ~/.config
cp examples/clash_auto.example.json ~/.config/clash_auto.json
$EDITOR ~/.config/clash_auto.json     # 至少填 group
```

### 关于 secret

不需要填。脚本会自己去客户端的 `config.yaml` 里读当前的 secret：

```
~/.local/share/com.follow.clash/config.yaml
```

原因很实际：FlClash 每次重启/升级都会重新生成 secret，写死在配置里必然在某次升级后开始 401。如果你的配置在别处，用环境变量指过去：

```bash
export CLASH_CONFIG=/path/to/config.yaml
```

## 配置说明

`~/.config/clash_auto.json`（完整示例见 `examples/`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `group` | — | **必填**，要择优的代理组名 |
| `test_url` | gstatic 204 | 通用延迟探测目标 |
| `max_delay_ms` | 500 | 当前节点延迟超过它即视为失效；`0` 关闭该判断 |
| `require_opencode` | true | 是否启用 opencode 门禁 |
| `opencode_url` | models.opencode.ai/…/api.json | opencode 探测目标 |
| `opencode_timeout_ms` | 8000 | 探测超时 |
| `opencode_fallback` | true | 所有节点都过不了 opencode 时，是否退回只按延迟选 |
| `prefer_multiplier` | 0.1 | 只在一折节点里挑（按节点名里的 `x倍率` 解析） |
| `prefer_speed` | true | 决策前对候选做真实下载测速 |
| `speed_top` | 5 | 只测速延迟最好的前 N 个 |

## 用法

```bash
# 看一眼当前状态（两个延迟都会打出来）
python3 ./clash_auto.py --status

# 只测速不切换
python3 ./clash_auto.py --force --dry-run

# 忽略「当前可用」，强制重新择优并切换
python3 ./clash_auto.py --force

# 全量检测（命令行报表）
bash ./clash_check_nodes.sh

# 网页面板 → http://127.0.0.1:8765
python3 ./opencode_panel.py
```

## 定时运行

```cron
*/3 * * * * python3 $HOME/scripts/clash_auto.py
```

日志写在 `~/.local/state/clash_auto.log`。

## 它怎么判断

```
当前节点
  ├─ 延迟超时 / > max_delay_ms      → 失效
  ├─ opencode 探测失败（重试 3 次） → 失效
  └─ 都通过                          → 保持不变，本轮结束
失效后
  ├─ 并发探测所有真实节点（两个目标都测）
  ├─ 淘汰延迟超时、过慢、opencode 不通的
  ├─ 按 opencode 延迟排序，优先低倍率
  ├─ 对前 N 个做真实下载测速
  └─ 切到最快的那个；一个合格的都没有就不动
```

重试机制是必要的：这类探测会假失败，同一个节点可能在几秒内两次结果相反，直接判死会导致无意义的频繁切换。

## 关于 `/proxies/{name}/delay`

所有探测都走 mihomo 的这个接口，它会**从指定节点真实发起一次 HTTP(S) 请求**，返回往返毫秒数。

一个实测细节：它只读响应头，不下载 body。所以哪怕拿 12MB 的 JSON 当探针，单次也只有 0.2s 左右，不会吃流量。

局限：它**不校验 HTTP 状态码**，服务器返回 403 也会被算作「通」。因此它衡量的是传输层可达性；要区分「被风控拦了」和「连不上」，只能真读响应内容。

## License

MIT
