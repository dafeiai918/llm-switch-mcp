# -*- coding: utf-8 -*-
"""llm-switch-mcp 测试：协议 + 安全切换逻辑（重点验证不破坏原配置）"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm_switch_mcp as ls  # noqa: E402


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.server = ls.MCPServer("t", "1.0", ls.TOOLS)
        self.server._send = lambda m: self.sent.append(m)

    def _call(self, method, **kw):
        msg = {"jsonrpc": "2.0", "id": 1, "method": method}
        if kw:
            msg["params"] = kw
        self.server.handle(msg)
        return self.sent[-1]

    def test_initialize(self):
        self.assertEqual(self._call("initialize")["result"]["serverInfo"]["name"], "t")

    def test_tools_list(self):
        names = [t["name"] for t in self._call("tools/list")["result"]["tools"]]
        self.assertEqual(sorted(names), ["current_config", "list_backups", "list_switchable",
                                         "restore_backup", "switch_site"])

    def test_unknown_tool(self):
        self.assertEqual(self._call("tools/call", name="x", arguments={})["error"]["code"], -32602)


class SwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._orig_switch = ls.SWITCHABLE
        self._orig_backup = ls.BACKUP_DIR
        ls.BACKUP_DIR = base / "backups"
        self.files = {
            "cline": base / "settings.json",
            "continue": base / "continue.json",
            "aider": base / "aider.yml",
            "codex": base / "config.toml",
        }
        ls.SWITCHABLE = {
            "cline": {"name": "VS Code", "path": self.files["cline"],
                      "kind": "json_vscode", "note": ""},
            "continue": {"name": "Continue", "path": self.files["continue"],
                         "kind": "json_continue", "note": ""},
            "aider": {"name": "Aider", "path": self.files["aider"],
                      "kind": "yaml_kv", "note": ""},
            "codex": {"name": "Codex", "path": self.files["codex"],
                      "kind": "toml_codex", "note": ""},
        }

    def tearDown(self):
        ls.SWITCHABLE = self._orig_switch
        ls.BACKUP_DIR = self._orig_backup
        self.tmp.cleanup()

    # ---------- 纯函数 ----------

    def test_normalize(self):
        self.assertEqual(ls.normalize("https://a.com/v1"), ("https://a.com", "https://a.com/v1"))
        self.assertEqual(ls.normalize("https://a.com"), ("https://a.com", "https://a.com/v1"))

    def test_cline_json_edit_preserves_other_keys(self):
        original = json.dumps({"editor.fontSize": 14, "cline.openAiApiKey": "old"})
        new_text, changes = ls.build_new_content(
            "cline", original, "https://new.com", "https://new.com/v1", "sk-new", "gpt-4o-mini")
        data = json.loads(new_text)
        self.assertEqual(data["editor.fontSize"], 14)
        self.assertEqual(data["cline.openAiBaseUrl"], "https://new.com/v1")
        self.assertEqual(data["cline.openAiModelId"], "gpt-4o-mini")
        self.assertTrue(changes)

    def test_continue_json_targets_openai_provider(self):
        original = json.dumps({"models": [{"provider": "anthropic", "apiBase": "keep"},
                                          {"provider": "openai", "apiBase": "old"}]})
        new_text, _ = ls.build_new_content(
            "continue", original, "https://new.com", "https://new.com/v1", "sk", "m")
        data = json.loads(new_text)
        self.assertEqual(data["models"][0]["apiBase"], "keep")
        self.assertEqual(data["models"][1]["apiBase"], "https://new.com/v1")

    def test_continue_adds_model_when_missing(self):
        new_text, _ = ls.build_new_content(
            "continue", "{}", "https://new.com", "https://new.com/v1", "sk", "m")
        self.assertEqual(json.loads(new_text)["models"][0]["apiBase"], "https://new.com/v1")

    def test_bad_json_raises(self):
        with self.assertRaises(ValueError):
            ls.build_new_content("cline", "不是JSON", "https://a.com", "https://a.com/v1", "k", "m")

    def test_aider_replaces_existing_key(self):
        original = "model: openai/gpt-4\nopenai-api-base: https://old.com/v1\n# 其他注释\n"
        new_text, _ = ls.build_new_content(
            "aider", original, "https://new.com", "https://new.com/v1", "sk", "gpt-4o")
        self.assertIn("openai-api-base: https://new.com/v1", new_text)
        self.assertNotIn("old.com", new_text)
        self.assertIn("# 其他注释", new_text)

    def test_aider_appends_missing_keys_without_duplicating(self):
        new_text, _ = ls.build_new_content(
            "aider", "model: gpt-4\n", "https://new.com", "https://new.com/v1", "sk", "m")
        self.assertIn("openai-api-base: https://new.com/v1", new_text)
        self.assertEqual(new_text.count("model:"), 1)
        self.assertEqual(new_text.count("openai-api-key:"), 1)

    def test_aider_replaces_existing_key_keeps_other(self):
        original = "openai-api-key: sk-old\nother-flag: true\n"
        new_text, _ = ls.build_new_content(
            "aider", original, "https://new.com", "https://new.com/v1", "sk-new", "m")
        self.assertEqual(new_text.count("openai-api-key:"), 1)
        self.assertIn("sk-new", new_text)
        self.assertNotIn("sk-old", new_text)
        self.assertIn("other-flag: true", new_text)

    def test_codex_targeted_edit_preserves_other_sections(self):
        original = ('model = "gpt-4o"\n\n'
                    '[model_providers.relay]\nname = "Relay"\n'
                    'base_url = "https://old.com/v1"\n\n'
                    '[mcp_servers.foo]\ncommand = "bar"\n\n'
                    '[projects."C:\\\\work"]\ntrust_level = "trusted"\n')
        new_text, changes = ls.build_new_content(
            "codex", original, "https://new.com", "https://new.com/v1", "sk", "m")
        self.assertIn('base_url = "https://new.com/v1"', new_text)
        self.assertIn("[mcp_servers.foo]", new_text)
        self.assertIn("command = \"bar\"", new_text)
        self.assertIn("[projects.", new_text)
        self.assertIn("trust_level", new_text)
        self.assertNotIn("old.com", new_text)
        self.assertTrue(changes)

    def test_codex_appends_when_no_relay_block(self):
        original = 'model = "gpt-4o"\n\n[projects."C:\\\\x"]\ntrust_level = "trusted"\n'
        new_text, changes = ls.build_new_content(
            "codex", original, "https://new.com", "https://new.com/v1", "sk", "m")
        self.assertIn("[model_providers.relay]", new_text)
        self.assertIn("[projects.", new_text)
        self.assertIn("trust_level", new_text)

    # ---------- 工具层 ----------

    def test_list_switchable(self):
        text = ls.tool_list_switchable({})
        self.assertIn("可以直接切换", text)
        self.assertIn("需要手动切换", text)
        self.assertIn("Cherry Studio", text)

    def test_current_config_missing_file(self):
        self.assertIn("配置文件不存在", ls.tool_current_config({"client": "cline"}))

    def test_current_config_reads_url(self):
        self.files["aider"].write_text("openai-api-base: https://old.com/v1\n", encoding="utf-8")
        self.assertIn("old.com", ls.tool_current_config({"client": "aider"}))

    def test_current_config_manual_only(self):
        self.assertIn("OneClickRelay" if False else "Cherry Studio",
                      ls.tool_current_config({"client": "cherry"}))

    def test_switch_dry_run_does_not_write(self):
        original = "openai-api-base: https://old.com/v1\n"
        self.files["aider"].write_text(original, encoding="utf-8")
        text = ls.tool_switch_site({"client": "aider", "base_url": "https://new.com/v1"})
        self.assertIn("草案", text)
        self.assertIn("confirm=true", text)
        self.assertEqual(self.files["aider"].read_text(encoding="utf-8"), original)

    def test_switch_confirm_writes_and_backs_up(self):
        self.files["aider"].write_text("openai-api-base: https://old.com/v1\n", encoding="utf-8")
        text = ls.tool_switch_site({"client": "aider", "base_url": "https://new.com/v1",
                                    "api_key": "sk-new", "confirm": True})
        self.assertIn("已写入", text)
        self.assertIn("new.com", self.files["aider"].read_text(encoding="utf-8"))
        backups = list(ls.BACKUP_DIR.glob("*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertIn("old.com", backups[0].read_text(encoding="utf-8"))

    def test_switch_manual_only_refused(self):
        self.assertIn("不能自动切换",
                      ls.tool_switch_site({"client": "cherry", "base_url": "https://x.com"}))

    def test_switch_unknown_client(self):
        self.assertIn("不支持的客户端",
                      ls.tool_switch_site({"client": "zzz", "base_url": "https://x.com"}))

    def test_switch_requires_base_url(self):
        self.assertIn("需要 base_url", ls.tool_switch_site({"client": "aider"}))

    def test_switch_missing_file(self):
        self.assertIn("配置文件不存在",
                      ls.tool_switch_site({"client": "cline", "base_url": "https://x.com"}))

    def test_list_and_restore_backup(self):
        self.assertEqual(ls.tool_list_backups({}), "还没有备份。")
        self.files["aider"].write_text("openai-api-base: https://old.com/v1\n", encoding="utf-8")
        ls.tool_switch_site({"client": "aider", "base_url": "https://new.com/v1", "confirm": True})
        listing = ls.tool_list_backups({})
        self.assertIn("aider.yml", listing)

        name = listing.split("1. ")[1].split("  ")[0]
        result = ls.tool_restore_backup({"backup": name})
        self.assertIn("已还原", result)
        self.assertIn("old.com", self.files["aider"].read_text(encoding="utf-8"))

    def test_restore_unknown_backup(self):
        self.assertIn("备份不存在", ls.tool_restore_backup({"backup": "nope.bak"}))
        self.assertIn("需要 backup", ls.tool_restore_backup({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
