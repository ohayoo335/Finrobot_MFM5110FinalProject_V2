"""Offline checks for automatic role routing and saved CLI output."""
import contextlib
import io
import json
import logging
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from finrobot.agents import research_router as router

ROOT = Path(__file__).resolve().parents[1]
API = runpy.run_path(str(ROOT / "finrobot_autogen/run_agent.py"))


class RoutingTests(unittest.TestCase):
    def test_readme_custom_rule_uses_rule_tool_role(self):
        question = "帮我用20日动量大于0且5日均量高于20日均量的规则回测000001，2021-06-01到2022-12-31"
        self.assertEqual(router.route_research(question), (router.FACTOR_IDEA_ROLE,))

    def test_fixed_momentum_uses_factor_researcher(self):
        self.assertEqual(router.route_research("回测000001的20日动量"), ("A_Share_Factor_Researcher",))

    def test_parameterized_sma_uses_quant_role(self):
        question = "回测000651在2023-2024年的10/30均线，本金10万，开仓预算60%"
        self.assertEqual(router.route_research(question), ("Quant_Analyst",))

    def test_broad_stock_analysis_excludes_unrequested_fixed_experiment(self):
        roles = router.route_research("综合分析600519")
        self.assertNotIn("Quant_Analyst", roles)
        self.assertEqual(len(roles), 3)

    def test_financial_and_news_intents_can_combine(self):
        self.assertEqual(router.route_research("看600519的基本面和近期新闻"),
                         ("A_Share_Analyst", "Market_Analyst"))

    def test_explicit_experiment_and_report_intents(self):
        self.assertEqual(router.route_research("复现V2实验"), ("Quant_Analyst",))
        self.assertEqual(router.route_research("生成600519研究报告"), ("Expert_Investor",))

    def test_factor_idea_followup_keeps_its_rule(self):
        self.assertEqual(router.route_research("回测刚才那个因子", (router.FACTOR_IDEA_ROLE,)),
                         (router.FACTOR_IDEA_ROLE,))

    def test_switch_symbol_keeps_rule_despite_numeric_format_keywords(self):
        question = "换成600031，2025年继续用刚才同一个规则重新计算，收益率和波动率保留原始小数"
        self.assertEqual(router.route_research(question, (router.FACTOR_IDEA_ROLE,)),
                         (router.FACTOR_IDEA_ROLE,))


class AutomaticReplyTests(unittest.TestCase):
    def test_factor_and_fixed_snapshot_tools_are_scoped_without_mutating_roles(self):
        question = "计算000651截至2024年的价量因子，并读取原V2固定实验的人工SMA已保存指标"
        original = list(router.library["A_Share_Factor_Researcher"]["toolkits"])
        factor = router.scoped_specialist("A_Share_Factor_Researcher", question)
        quant = router.scoped_specialist("Quant_Analyst", question)
        self.assertEqual([tool.__name__ for tool in factor["toolkits"]], ["get_a_share_factors"])
        self.assertEqual({tool.__name__ for tool in quant["toolkits"]}, {"get_data_summary", "read_backtest_result"})
        self.assertEqual(router.library["A_Share_Factor_Researcher"]["toolkits"], original)
        rerun = router.scoped_specialist("Quant_Analyst", "重跑固定V2实验")
        self.assertIn("backtest_reference", {tool.__name__ for tool in rerun["toolkits"]})

    def test_one_failed_role_does_not_hide_other_evidence(self):
        research = router.AutoResearch({})
        with patch.object(research, "_reply", side_effect=[{"answer": "财务证据"},
                {"error": "ConnectionError"}, {"answer": "汇总财务证据"}]):
            answer = research.answer("基本面和新闻", ("A_Share_Analyst", "Market_Analyst"))
        self.assertIn("财务证据", answer)
        self.assertIn("Market_Analyst", answer)
        self.assertFalse(research.last_run["ok"])

    def test_named_assistant_final_reply_is_accepted(self):
        history = [{"role": "user", "name": "User_Proxy", "content": "question"},
                   {"role": "assistant", "name": "Market_Analyst", "content": "本轮结论\nTERMINATE"}]
        proxy = SimpleNamespace(initiate_chat=lambda *a, **k: SimpleNamespace(chat_history=history))
        agent = SimpleNamespace(assistant=SimpleNamespace(name="Market_Analyst"), user_proxy=proxy)
        with patch.object(router, "SingleAssistant", return_value=agent):
            reply = router.AutoResearch({})._reply("Market_Analyst", "question")
        self.assertEqual(reply["answer"], "本轮结论")

    def test_incomplete_tool_reply_is_not_an_answer(self):
        history = [{"role": "user", "content": "question"},
                   {"role": "tool", "content": "tool output"}]
        proxy = SimpleNamespace(initiate_chat=lambda *a, **k: SimpleNamespace(chat_history=history))
        agent = SimpleNamespace(assistant=SimpleNamespace(name="Market_Analyst"), user_proxy=proxy)
        with patch.object(router, "SingleAssistant", return_value=agent):
            self.assertIn("error", router.AutoResearch({})._reply("Market_Analyst", "question"))


class AutomaticConsoleTests(unittest.TestCase):
    def test_default_auto_output_is_quiet_and_records_the_turn(self):
        class Research:
            last_run = None
            def plan(self, question):
                return ("A_Share_Factor_Ideator",)
            def answer(self, question, roles):
                print("RAW_TOOL_OUTPUT")
                logging.getLogger("auto_test").warning("framework warning")
                self.last_run = {"ok": True, "question": question, "roles": list(roles),
                                 "answer": "本轮分析", "chats": [{"history": [{"content": "工具记录"}]}]}
                return "本轮分析"

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "finrobot_autogen"
            base.mkdir()
            console = io.StringIO()
            with patch.dict(API["run_auto"].__globals__, {"BASE_DIR": base, "AutoResearch": lambda cfg: Research()}), \
                    contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
                API["run_auto"]({}, "回测规则")
            output = console.getvalue()
            self.assertIn("本轮分析", output)
            self.assertNotIn("RAW_TOOL_OUTPUT", output)
            self.assertNotIn("framework warning", output)
            folder = base.parent / "artifacts/agent_chats"
            self.assertEqual(len(list(folder.glob("*.md"))), 1)
            self.assertEqual(len(list(folder.glob("*.json"))), 1)
            log = next(folder.glob("*.log")).read_text(encoding="utf-8")
            self.assertIn("RAW_TOOL_OUTPUT", log)
            self.assertIn("framework warning", log)
            record = json.loads(next(folder.glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(record["roles"], ["A_Share_Factor_Ideator"])


if __name__ == "__main__":
    unittest.main()
