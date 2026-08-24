---
name: watchy-vps-deployment
description: Watchy VPS 部署已完成。正式上线日 = 2026-06-09 美东交易日。记录最终环境配置和工作流。
metadata: 
  node_type: memory
  type: project
  originSessionId: b1456a24-9c49-4d0a-9ae3-94abfb219ebf
---

## 🚀 正式上线（Official launch）

**Watchy 于 2026-06-09(美东时间这个交易日)正式上线。** 这一天把完整功能集
推上 VPS 并实地验证(消息截断修复 #3、Tier 2 门控 + 自动入场目标价 #15/#16、
现金/集中度 #19)。当天 ~14:46 UTC `git pull` + `systemctl restart`,守护进程
active、18 个任务、DB 迁移到位,Gemini 实测 #16 入场目标提取通过。6/8 是初次
部署验证,**6/9 是功能完整的正式上线**。详见 [[watchy-issue-plan]]。

> 注意:下面的环境记录较旧(2026-06-03),Tier 2 实际是 **11:30 UTC**(非 22:00),
> "待解决"项均已修复。真相以 repo CLAUDE.md + docs/IMPLEMENTATION_PLAN.md 为准。

## VPS 环境

- 服务商: BandwagonHost（2026-08 从 Hetzner 迁移过来，旧的 ubuntu-4gb-hil-2 记录已作废）
- 主机: `qcvps` → 65.49.218.116（ssh 配置在 WSL `~/.ssh/config`，密钥 `~/.ssh/qcvps`）
  另有 `qcvps-cf` → fps.cong.fyi，走 cloudflared ProxyCommand
- OS: Ubuntu 24.04 LTS
- 用户: watchy
- Python: 3.11.9 via pyenv (`/home/watchy/.pyenv/versions/3.11.9/`)
- 虚拟环境: `trading` (pyenv virtualenv 3.11.9 trading)

## 部署架构

```
~/watchy/                          # Git clone of github.com/quentincong/watchy
    ├── watchy/                    # Python package (pip install -e .)
    ├── config.yaml                # 非敏感配置（可安全提交，通过 GitHub 编辑 watchlist）
    ├── secrets.example.yaml       # 敏感配置模板
    ├── watchy.service             # Watchy 守护进程 systemd unit
    ├── watchy-update.service      # 自动更新 oneshot
    ├── watchy-update.timer        # 每 5 分钟触发更新检查
    └── auto-update.sh             # git fetch → 有更新则 pull + restart

~/watchy_config/
    ├── config.yaml                # 实际使用的非敏感配置
    └── secrets.yaml               # 敏感配置（API key、Telegram token）— git-ignored

~/TradingAgents/                   # TradingAgents 源码 (pip install -e .)
```

## 已部署的 systemd 单元

| 单元 | 说明 |
|------|------|
| `watchy.service` | Watchy 守护进程（Tier 1 每小时 + Tier 2 每天 22:00 UTC） |
| `watchy-update.timer` | 每 5 分钟触发 git pull 检查 |
| `watchy-update.service` | 执行 auto-update.sh（有更新才重启 watchy） |

## 工作流

1. **改自选股/watchlist**: 本地编辑 `config.yaml` → `git push` → 5 分钟内 VPS 自动生效
2. **改代码**: 同上
3. **改 secrets**: SSH 到 VPS → `nano ~/watchy_config/secrets.yaml` → `sudo systemctl restart watchy`

## 监控

```bash
sudo systemctl status watchy              # 主服务状态
sudo systemctl status watchy-update.timer  # 自动更新状态
journalctl -u watchy -f                   # 实时日志
journalctl -u watchy-update               # 自动更新日志
```

## 待解决 (2026-06-03)

### 1. Telegram HTTP 400
Telegram 文本消息里 summary/risk/rec_text 含 `<` `>` 等 HTML 特殊字符，parse_mode=HTML 解析失败。
- **已修**: `notify.py` 加了 `_escape_html()` (commit 2a6ece5)
- **VPS 待做**: `git pull` + `sudo systemctl restart watchy`

### 2. VPS config 未更新
VPS 读的是 `~/watchy_config/config.yaml`（只有 NVDA/TSLA/AAPL，1h 间隔），不是仓库里新的 config.yaml（16 只票，30min，11:30 UTC）。
- **待做**: 
  ```bash
  cp ~/watchy/config.yaml ~/watchy_config/config.yaml
  sudo systemctl restart watchy
  ```

### 3. yfinance 429 Rate Limit
本地触发，VPS 暂时 OK。详见 [[yfinance-429-rate-limit]]。
