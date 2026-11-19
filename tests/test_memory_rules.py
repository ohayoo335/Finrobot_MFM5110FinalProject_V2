"""Complete JSON rules must survive long replies, restart and bounded recall."""
import json
from contextlib import closing
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finrobot.agents.memory import MAX_CONTEXT_CHARS, ResearchMemory
from finrobot.agents.memory_tools import recall_context


def rule(name="volume_confirmed_momentum"):
    return {"name": name, "signal": {"all": [
        {"indicator": "momentum", "window": 20, "operator": ">", "value": 0},
        {"indicator": "average_volume_ratio", "short_window": 5, "long_window": 20,
         "operator": ">", "value": 1}]}, "execution": "next_open"}


def tool_result(value):
    return [{"role": "A_Share_Factor_Ideator", "history": [{"role": "tool",
        "name": "backtest_a_share_strategy", "content": json.dumps(
            {"symbol": "000651", "rule": value, "audit_ok": True})}]}]


class MemoryRuleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite3"
        self.memory = ResearchMemory(self.path)

    def save(self, answer, chats=(), status="completed"):
        return self.memory.save_turn("研究000651", answer, ["A_Share_Factor_Ideator"], chats, status=status)

    def test_rule_after_long_answer_survives_restart_and_model_recall(self):
        answer = "经济逻辑说明。" * 300 + "\n```json\n" + json.dumps({"rule_json": rule()}) + "\n```"
        self.save(answer)
        restored = ResearchMemory(self.path, session_id=self.memory.session_id)
        self.assertEqual(restored.recent_turns()[0]["answer"], answer)
        context = restored.context("继续同一个规则")
        self.assertEqual(context["recent_turns"][0]["rules"][0]["rule"], rule())
        self.assertEqual(context["recent_turns"][0]["rules"][0]["source"], "assistant_answer")
        recalled = recall_context(restored, "000651", 1)
        self.assertEqual(recalled["turns"][0]["rules"][0]["rule"], rule())
        script = (
            "import json,sys; from finrobot.agents.memory import ResearchMemory; "
            "m=ResearchMemory(sys.argv[1],session_id=sys.argv[2]); "
            "print(json.dumps(m.context('continue')))"
        )
        child = subprocess.run([sys.executable, "-c", script, str(self.path), self.memory.session_id],
                               capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
        self.assertEqual(json.loads(child.stdout)["recent_turns"][0]["rules"][0]["rule"], rule())

    def test_tool_rule_is_saved_without_relying_on_answer_text(self):
        self.save("策略已完成，详细逻辑另见报告。", tool_result(rule()))
        saved = self.memory.recent_turns()[0]["rules"][0]
        self.assertEqual(saved["rule"], rule())
        self.assertEqual(saved["source"], "tool_result")

    def test_encoded_rule_argument_is_preserved(self):
        chats = [{"role": "A_Share_Factor_Ideator", "history": [{"role": "assistant", "tool_calls": [
            {"function": {"name": "backtest_a_share_strategy", "arguments": json.dumps({"rule_json": json.dumps(rule())})}}
        ]}]}]
        self.save("候选规则待进一步核查。", chats)
        self.assertEqual(self.memory.recent_turns()[0]["rules"][0]["rule"], rule())
        self.assertEqual(self.memory.recent_turns()[0]["rules"][0]["source"], "tool_argument")

    def test_nested_rule_is_not_flattened_or_truncated(self):
        nested = rule()
        nested["signal"] = {"any": [{"all": nested["signal"]["all"]},
                                     {"indicator": "momentum", "window": 60, "operator": ">", "value": 0}]}
        self.save("多条件规则。", tool_result(nested))
        self.assertEqual(recall_context(self.memory, "000651", 1)["turns"][0]["rules"][0]["rule"], nested)

    def test_oversized_rule_is_explicitly_omitted_without_a_partial_object(self):
        large = rule("large_rule_" + "x" * 14000)
        self.save("完整规则已保留。", tool_result(large))
        self.assertEqual(self.memory.recent_turns()[0]["rules"][0]["rule"], large)
        for context in (self.memory.context("继续"), recall_context(self.memory, "000651", 1)):
            rows = context.get("recent_turns", context.get("turns"))
            self.assertEqual(rows[0]["rules"], [])
            self.assertEqual(rows[0]["rules_omitted"], 1)
            self.assertTrue(rows[0]["truncated"])
            self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), MAX_CONTEXT_CHARS)

    def test_legacy_database_is_upgraded_and_old_answer_rule_is_recovered(self):
        legacy = self.path.parent / "legacy.sqlite3"
        answer = "历史说明。" * 400 + json.dumps({"rule_json": rule()})
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY,mode TEXT,role TEXT,created_at TEXT,updated_at TEXT,title TEXT)")
            db.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY,session_id TEXT,question TEXT,answer TEXT,roles TEXT,status TEXT,created_at TEXT,evidence TEXT)")
            db.execute("INSERT INTO sessions VALUES ('legacy','auto',NULL,'2026-11-19','2026-11-19','old')")
            db.execute("INSERT INTO turns VALUES (1,'legacy','研究000651',?,?,'completed','2026-11-19','[]')",
                       (answer, json.dumps(["A_Share_Factor_Ideator"])))
        restored = ResearchMemory(legacy, session_id="legacy")
        self.assertEqual(restored.recent_turns()[0]["answer"], answer)
        self.assertEqual(restored.context("继续")["recent_turns"][0]["rules"][0]["rule"], rule())
        with closing(sqlite3.connect(legacy)) as db, db:
            self.assertIn("rules", {row[1] for row in db.execute("PRAGMA table_info(turns)")})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)

    def test_oversized_json_answer_preview_does_not_leak_a_partial_rule(self):
        large = rule("large_rule_" + "x" * 14000)
        self.save(json.dumps({"rule_json": large}))
        row = recall_context(self.memory, "000651", 1)["turns"][0]
        self.assertEqual(row["rules"], [])
        self.assertEqual(row["rules_omitted"], 1)
        self.assertNotIn("large_rule_", row["answer"])
        self.assertNotIn('"signal"', row["answer"])

    def test_invalid_rule_is_not_promoted_to_executable_memory(self):
        invalid = {"signal": {"all": [{"indicator": "python", "code": "arbitrary()"}]}}
        self.save(json.dumps({"rule_json": invalid}))
        self.assertEqual(self.memory.context("继续")["recent_turns"][0]["rules"], [])

    def test_partial_turn_rule_does_not_become_completed_research(self):
        self.save("部分失败。", tool_result(rule()), status="partial")
        self.assertEqual(self.memory.context("继续")["recent_turns"], [])
        self.assertEqual(recall_context(self.memory, "000651", 1)["turns"], [])

    def test_restarted_auto_session_passes_the_complete_rule_to_the_model_boundary(self):
        from finrobot.agents.research_router import AutoResearch
        answer = "规则说明。" * 400 + json.dumps({"rule_json": rule()})
        self.save(answer)
        restored = ResearchMemory(self.path, session_id=self.memory.session_id)
        research = AutoResearch({}, memory=restored)
        question = "回测刚才这个因子，换成600031"
        roles = research.plan(question)
        self.assertEqual(roles, ("A_Share_Factor_Ideator",))
        with patch.object(research, "_reply", return_value={"answer": "待执行规则已完整恢复。"}) as reply:
            research.answer(question, roles)
        prompt = reply.call_args.args[1]
        context, _ = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])
        recovered = context["memory_context"]["recent_turns"][0]["rules"][0]["rule"]
        self.assertEqual(recovered, rule())


if __name__ == "__main__":
    unittest.main()
