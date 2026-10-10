#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm-switch-mcp · 一键换站 MCP
在不破坏原有配置的前提下，把客户端切到另一个 OpenAI 兼容站点
所有写操作默认 dry-run，必须显式 confirm 才执行，且自动备份
零依赖 stdio MCP 服务器
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

__version__ = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"

HOME = Path.home()
APPDATA = Path(os.environ.get("APPDATA", HOME / "AppData/Roaming"))
BACKUP_DIR = HOME / ".llm-switch" / "backups"

# 可安全切换的客户端（纯文本配置）
SWITCHABLE = {
    "aider": {
        "name": "Aider",
        "path": HOME / ".aider.conf.yml",
        "kind": "yaml_kv",
        "note": "也可用 .env 里的 OPENAI_API_BASE",
    },
    "continue": {
        "name": "Continue (VSCode)",
        "path": HOME / ".continue" / "config.json",
        "kind": "json_continue",
        "note": "修改 models 数组里 provider=openai 的 apiBase/apiKey",
    },
    "cline": {
        "name": "VS Code (Cline)",
        "path": APPDATA / "Code" / "User" / "settings.json",
        "kind": "json_vscode",
        "note": "修改 cline.openAiBaseUrl / openAiApiKey / openAiModelId",
    },
    "codex": {
        "name": "Codex CLI",
        "path": HOME / ".codex" / "config.toml",
        "kind": "toml_codex",
        "note": "定点编辑，不动 projects / mcp_servers 等其他配置",
    },
}

# 需要手动切换的（配置存在数据库或应用私有存储里）
MANUAL_ONLY = {
    "cherry": "Cherry Studio（配置在 SQLite，请用图形界面改，或用 OneClickRelay 一键写入）",
    "nextchat": "NextChat（配置在应用存储里，请在设置 → 自定义接口里改）",
    "chatbox": "Chatbox（配置在应用存储里，请在设置 → 模型里改）",
}


# ================= MCP 内核 =================

class MCPServer:
    def __init__(self, name: str, version: str, tools: list):
        self.name, self.version, self.tools = name, version, tools

    def _send(self, msg: dict) -> None:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _result(self, id_, result) -> None:
        self._send({"jsonrpc": "2.0", "id": id_, "result": result})

    def _error(self, id_, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})

    def handle(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            return
        method, id_ = msg.get("method"), msg.get("id")
        if method == "initialize":
            self._result(id_, {"protocolVersion": PROTOCOL_VERSION,
                               "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": {"name": self.name, "version": self.version}})
        elif method in ("notifications/initialized", "initialized", "notifications/cancelled"):
            return
        elif method == "ping":
            self._result(id_, {})
        elif method == "tools/list":
            self._result(id_, {"tools": [
                {"name": t["name"], "description": t["description"],
                 "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}}}
                for t in self.tools]})
        elif method == "tools/call":
            params = msg.get("params") or {}
            tool = next((t for t in self.tools if t["name"] == params.get("name")), None)
            if tool is None:
                self._error(id_, -32602, f"未知工具: {params.get('name')}")
                return
            try:
                out = tool["handler"](params.get("arguments") or {})
                self._result(id_, {"content": [{"type": "text", "text": str(out)}], "isError": False})
            except Exception as exc:
                self._result(id_, {"content": [{"type": "text",
                                                "text": f"错误: {type(exc).__name__}: {exc}"}],
                                   "isError": True})
        elif id_ is not None:
            self._error(id_, -32601, f"不支持的方法: {method}")

    def serve(self) -> None:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            for item in (msg if isinstance(msg, list) else [msg]):
                self.handle(item)


# ================= 切换逻辑 =================

def normalize(base: str) -> tuple[str, str]:
    root = (base or "").strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    return root, root + "/v1"


def read_current(client_id: str) -> str:
    spec = SWITCHABLE.get(client_id)
    if not spec:
        raise ValueError(f"不支持的客户端：{client_id}")
    path = Path(spec["path"])
    if not path.is_file():
        return f"（配置文件不存在：{path}）"
    text = path.read_text(encoding="utf-8", errors="replace")
    urls = re.findall(r"https?://[^\s\"',}\]]+", text)
    api_urls = [u for u in urls if "/v1" in u or "api." in u or "chat" in u]
    lines = [f"配置文件：{path}"]
    if api_urls:
        lines.append(f"检测到地址：{', '.join(dict.fromkeys(api_urls))[:200]}")
    else:
        lines.append("未检测到明显的 API 地址")
    return "\n".join(lines)


def build_new_content(client_id: str, text: str, root: str, full: str,
                      key: str, model: str) -> tuple[str, list[str]]:
    """返回 (新内容, 变更说明)"""
    changes: list[str] = []
    if client_id == "json_vscode" or client_id == "cline":
        try:
            data = json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"settings.json 不是合法 JSON（{exc}）") from None
        old = data.get("cline.openAiBaseUrl", "(未设置)")
        data["cline.openAiBaseUrl"] = full
        if key:
            data["cline.openAiApiKey"] = key
        if model:
            data["cline.openAiModelId"] = model
        changes.append(f"cline.openAiBaseUrl: {old} → {full}")
        return json.dumps(data, ensure_ascii=False, indent=4), changes

    if client_id == "continue":
        try:
            data = json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"config.json 不是合法 JSON（{exc}）") from None
        models = data.setdefault("models", [])
        if not isinstance(models, list):
            raise ValueError("config.json 的 models 字段格式异常")
        target = next((m for m in models if isinstance(m, dict)
                       and m.get("provider") == "openai"), None)
        if target is None:
            target = {"title": "Relay", "provider": "openai", "model": model or "gpt-4o-mini"}
            models.append(target)
        old = target.get("apiBase", "(未设置)")
        target["apiBase"] = full
        if key:
            target["apiKey"] = key
        if model:
            target["model"] = model
        changes.append(f"models[].apiBase: {old} → {full}")
        return json.dumps(data, ensure_ascii=False, indent=2), changes

    if client_id == "aider":
        out: list[str] = []
        seen = {"base": False, "key": False, "model": False}
        for line in (text or "").splitlines():
            if re.match(r"\s*openai-api-base\s*:", line):
                old = line.split(":", 1)[1].strip()
                out.append(f"openai-api-base: {full}")
                changes.append(f"openai-api-base: {old} → {full}")
                seen["base"] = True
            elif re.match(r"\s*openai-api-key\s*:", line) and key:
                out.append(f"openai-api-key: {key}")
                changes.append("openai-api-key: **** → ****（已更新）")
                seen["key"] = True
            elif re.match(r"\s*model\s*:", line) and model:
                out.append(f"model: openai/{model}")
                changes.append(f"model → openai/{model}")
                seen["model"] = True
            else:
                out.append(line)
        additions = []
        if not seen["base"]:
            additions.append(f"openai-api-base: {full}")
        if key and not seen["key"]:
            additions.append(f"openai-api-key: {key}")
        if model and not seen["model"]:
            additions.append(f"model: openai/{model}")
        if additions:
            out += ["# 由 llm-switch 追加"] + additions
            changes.append(f"追加 {len(additions)} 行配置")
        return "\n".join(out) + "\n", changes

    if client_id == "codex":
        # 定点编辑：只改 [model_providers.relay] 里的 base_url，绝不整体重写
        if "[model_providers.relay]" in text:
            block_re = re.compile(
                r"(\[model_providers\.relay\][^\[]*?base_url\s*=\s*\")([^\"]*)(\")",
                re.DOTALL)
            match = block_re.search(text)
            if match:
                old = match.group(2)
                text = block_re.sub(lambda m: m.group(1) + full + m.group(3), text, count=1)
                changes.append(f"[model_providers.relay].base_url: {old} → {full}")
                return text, changes
        appended = (f"\n[model_providers.relay]\nname = \"Relay\"\n"
                    f"base_url = \"{full}\"\nwire_api = \"chat\"\n"
                    f"env_key = \"RELAY_API_KEY\"\n")
        changes.append("追加 [model_providers.relay] 段（未改动原有任何内容）")
        return text + appended, changes

    raise ValueError(f"不支持的切换类型：{client_id}")


def do_backup(path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, target)
    return target


# ================= 工具 =================

def tool_list_switchable(args: dict) -> str:
    lines = ["可以直接切换的客户端（纯文本配置，自动备份）：", ""]
    for client_id, spec in SWITCHABLE.items():
        exists = "✓" if Path(spec["path"]).is_file() else "✗"
        lines.append(f"  {exists} {client_id:<10} {spec['name']:<20} {spec['path']}")
    lines += ["", "需要手动切换的（配置在数据库/应用私有存储）：", ""]
    for client_id, note in MANUAL_ONLY.items():
        lines.append(f"  - {client_id:<10} {note}")
    return "\n".join(lines)


def tool_current_config(args: dict) -> str:
    client_id = (args.get("client") or "").strip().lower()
    if not client_id:
        return "请指定 client。可选：" + ", ".join(SWITCHABLE)
    if client_id in MANUAL_ONLY:
        return f"{client_id}：{MANUAL_ONLY[client_id]}"
    try:
        return read_current(client_id)
    except ValueError as exc:
        return str(exc)


def tool_switch_site(args: dict) -> str:
    client_id = (args.get("client") or "").strip().lower()
    base_url = (args.get("base_url") or "").strip()
    api_key = (args.get("api_key") or "").strip()
    model = (args.get("model") or "").strip()
    confirm = bool(args.get("confirm"))

    if client_id in MANUAL_ONLY:
        return f"{client_id} 不能自动切换。\n{MANUAL_ONLY[client_id]}"
    spec = SWITCHABLE.get(client_id)
    if not spec:
        return f"不支持的客户端「{client_id}」。可选：{', '.join(SWITCHABLE)}"
    if not base_url:
        return "需要 base_url。"
    path = Path(spec["path"])
    if not path.is_file():
        return f"配置文件不存在：{path}\n客户端可能还没装或还没跑过一次。"

    root, full = normalize(base_url)
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        new_text, changes = build_new_content(client_id, text, root, full, api_key, model)
    except ValueError as exc:
        return f"无法生成新配置：{exc}"

    lines = [f"【{spec['name']}】切换方案", f"目标地址：{full}", ""]
    lines.append("将要做的改动：")
    for change in changes:
        lines.append(f"  · {change}")
    lines += ["", f"配置文件：{path}", f"原文件会先备份到：{BACKUP_DIR}"]

    if not confirm:
        lines += ["", "⚠ 这是草案（dry-run）。确认无误后，再次调用并加上 confirm=true 才会真正写入。"]
        return "\n".join(lines)

    backup = do_backup(path)
    # 原子写：先写临时文件再替换，避免写一半损坏原文件
    tmp = path.with_suffix(path.suffix + ".llmswitch.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    lines += ["", f"✓ 已写入。备份：{backup}",
              "如果出问题，用 restore_backup 还原。"]
    return "\n".join(lines)


def tool_list_backups(args: dict) -> str:
    if not BACKUP_DIR.exists():
        return "还没有备份。"
    files = sorted(BACKUP_DIR.glob("*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return "还没有备份。"
    lines = [f"备份目录：{BACKUP_DIR}", ""]
    for idx, path in enumerate(files[:20], 1):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        lines.append(f"  {idx}. {path.name}  ({path.stat().st_size} 字节 · {when})")
    return "\n".join(lines)


def tool_restore_backup(args: dict) -> str:
    name = (args.get("backup") or "").strip()
    if not name:
        return "需要 backup（备份文件名，用 list_backups 查看）。"
    path = BACKUP_DIR / Path(name).name
    if not path.is_file():
        return f"备份不存在：{path}"
    original_name = name.split(".20")[0] if ".20" in name else name.replace(".bak", "")
    candidates = [Path(s["path"]) for s in SWITCHABLE.values() if Path(s["path"]).name == original_name]
    if not candidates:
        return (f"无法自动判断 {name} 对应哪个配置文件。\n"
                f"请手动复制：copy \"{path}\" \"你的配置文件路径\"")
    target = candidates[0]
    shutil.copy2(path, target)
    return f"✓ 已还原 {target}（来源 {path.name}）"


TOOLS = [
    {"name": "list_switchable",
     "description": "列出哪些客户端能自动切换站点、哪些只能手动改。"
                    "用户问「能帮我换站吗」「哪些客户端支持」时使用。",
     "inputSchema": {"type": "object", "properties": {}},
     "handler": tool_list_switchable},
    {"name": "current_config",
     "description": "查看某个客户端当前配的 API 地址。用户问「我现在用的是哪个站」时使用。",
     "inputSchema": {"type": "object", "properties": {
         "client": {"type": "string", "description": "客户端 id"}},
         "required": ["client"]},
     "handler": tool_current_config},
    {"name": "switch_site",
     "description": "把客户端切换到另一个 OpenAI 兼容站点。默认只生成方案（dry-run），"
                    "必须显式传 confirm=true 才真正写入，写入前自动备份。",
     "inputSchema": {"type": "object", "properties": {
         "client": {"type": "string"},
         "base_url": {"type": "string", "description": "新站点地址"},
         "api_key": {"type": "string", "description": "新密钥（可选，不填则保留原密钥）"},
         "model": {"type": "string", "description": "新默认模型（可选）"},
         "confirm": {"type": "boolean", "description": "true 才真正写入"}},
         "required": ["client", "base_url"]},
     "handler": tool_switch_site},
    {"name": "list_backups",
     "description": "列出所有自动备份。切换前后都可以看。",
     "inputSchema": {"type": "object", "properties": {}},
     "handler": tool_list_backups},
    {"name": "restore_backup",
     "description": "从备份还原配置文件。切换出问题时使用。",
     "inputSchema": {"type": "object", "properties": {
         "backup": {"type": "string", "description": "备份文件名"}},
         "required": ["backup"]},
     "handler": tool_restore_backup},
]


def main() -> int:
    if os.name == "nt":
        try:
            sys.stdin.reconfigure(encoding="utf-8")
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    MCPServer("llm-switch-mcp", __version__, TOOLS).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
