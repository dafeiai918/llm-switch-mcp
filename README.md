# llm-switch-mcp · 一键换站 MCP

**让 AI 直接说：「我看了下要改这 1 行，是这里 → 这里。确认了我就改，改前先备份。」**

零依赖 · 单文件 · stdio MCP 服务器 · **默认 dry-run** · 自动备份 · 中文

---

## 安全设计（重要）

这个 MCP 会写文件，所以做了四层保护：

| 保护 | 说明 |
|---|---|
| **默认 dry-run** | 不传 `confirm=true` 只生成方案，绝不写入 |
| **写入前自动备份** | 备份到 `~/.llm-switch/backups/`，带时间戳 |
| **原子写** | 先写临时文件再替换，不会写一半损坏原文件 |
| **定点编辑** | 只改目标字段，不动其他配置（尤其 Codex 的 `projects` / `mcp_servers`） |
| **一键还原** | `restore_backup` 随时还原 |

---

## 它让 AI 能做什么

> 「我现在用的是哪个站？」
> → AI 调用 `current_config`，读出当前 API 地址

> 「帮我切到新站点 https://new.example.com/v1」
> → AI 调用 `switch_site`（**不加 confirm 时只给方案**）

> 「确认，改吧」
> → AI 再调 `switch_site` 加 `confirm=true`，写前自动备份

> 「改坏了，还原」
> → AI 调用 `restore_backup`

---

## 提供的工具

| 工具 | 作用 |
|---|---|
| `list_switchable` | 列出哪些客户端能自动切换 |
| `current_config` | 读当前配置的地址 |
| `switch_site` | 切换站点（默认 dry-run） |
| `list_backups` | 列出备份 |
| `restore_backup` | 从备份还原 |

---

## 支持自动切换的客户端

| 客户端 | 配置文件 | 编辑方式 |
|---|---|---|
| **Aider** | `~/.aider.conf.yml` | 保留其他行，只改/追加三项 |
| **Continue (VSCode)** | `~/.continue/config.json` | 只改 provider=openai 那一项 |
| **VS Code (Cline)** | `AppData/.../settings.json` | 只改 cline.* 三个键 |
| **Codex CLI** | `~/.codex/config.toml` | **定点编辑**，不动 projects/mcp_servers |

**需要手动改的**（配置在数据库或应用私有存储里）：
Cherry Studio、NextChat、Chatbox —— 这三个请在图形界面里改，或用 [OneClickRelay](https://github.com/dafeiai918/oneclickrelay) 一键写入。

---

## 安装

```json
{
  "mcpServers": {
    "llm-switch-mcp": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/dafeiai918/llm-switch-mcp", "llm-switch-mcp"]
    }
  }
}
```

零第三方依赖，Python 3.9+。

---

## 典型对话

```
你：我现在 Codex 用的是哪个站？
AI：（调用 current_config）检测到 base_url = https://old.example.com/v1

你：切到 https://new.example.com/v1
AI：（调用 switch_site，dry-run）方案如下：
    [model_providers.relay].base_url: https://old.example.com/v1 → https://new.example.com/v1
    原文件会先备份到 ~/.llm-switch/backups/
    确认请回复"确认"

你：确认
AI：（调用 switch_site, confirm=true）✓ 已写入。备份：config.toml.20261010-143022.bak
```

---

## 出问题怎么办

```bash
# 看备份
ls ~/.llm-switch/backups/

# 或者让 AI 还原
「把刚才那个备份还原回去」
```

备份是完整文件副本，即使 MCP 挂了也能手动复制回去。

---

## 相关项目

| 项目 | 用途 |
|---|---|
| [OneClickRelay](https://github.com/dafeiai918/oneclickrelay) | 一键写入 60+ 客户端（覆盖数据库型客户端） |
| [llm-config-mcp](https://github.com/dafeiai918/llm-config-mcp) | 生成配置（不写入） |
| [config-doctor-mcp](https://github.com/dafeiai918/config-doctor-mcp) | 诊断配置问题 |
| [relay-mcp](https://github.com/dafeiai918/relay-mcp) | 查站点余额 |

---

## License

MIT
