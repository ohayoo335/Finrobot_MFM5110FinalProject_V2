"""Offline checks that the real-run auditor does not accept wrong numbers/rules."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("auto_validation", ROOT / "scripts/validate_auto_research.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class AutoValidationTests(unittest.TestCase):
    def test_json_final_answer_with_code_fence(self):
        self.assertEqual(audit.answer_json('最终结果：\n```json\n{"symbol":"000651"}\n```'), {"symbol": "000651"})

    def test_wrong_scale_nan_and_missing_value_fail(self):
        audit.close_number(.123456789, .12345678901)
        for value in (12.3456789, float("nan"), None, True):
            with self.subTest(value=value), self.assertRaises((AssertionError, TypeError)):
                audit.close_number(value, .123456789)
        with self.assertRaises(AssertionError):
            audit.close_number(0, None)

    def test_rule_order_and_numeric_threshold_type_do_not_change_logic(self):
        a = {"indicator": "momentum", "window": 20, "operator": ">", "value": 0}
        b = {"indicator": "average_volume_ratio", "short_window": 5, "long_window": 20, "operator": ">", "value": 1}
        first = {"signal": {"all": [a, b]}}
        second = {"signal": {"all": [{**b, "value": 1.0}, {**a, "value": 0.0}]}}
        self.assertEqual(audit.rule_signal(first), audit.rule_signal(second))
        changed = {"signal": {"all": [{**a, "window": 60}, b]}}
        self.assertNotEqual(audit.rule_signal(first), audit.rule_signal(changed))

    def test_tool_calls_accept_empty_final_message_fields(self):
        calls = [{"function": {"name": "get_a_share_factors", "arguments": "{}"}}]
        record = {"chats": [{"history": [{"tool_calls": calls}, {"tool_calls": None}]}]}
        self.assertEqual(audit.tool_calls(record), [calls[0]["function"]])


if __name__ == "__main__":
    unittest.main()
