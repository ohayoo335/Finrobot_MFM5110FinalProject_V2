"""Check the live memory auditor offline; no service requests are made."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("memory_online_audit", ROOT / "scripts/validate_memory_online.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class MemoryOnlineAuditTests(unittest.TestCase):
    def test_direct_and_wrapped_rule_answers_preserve_the_same_complete_object(self):
        rule = audit.expected_rule("unique_fixture")
        direct, shape = audit.returned_rule(json.dumps(rule))
        wrapped, other = audit.returned_rule(json.dumps({"rule_json": rule}))
        self.assertEqual(direct, wrapped)
        self.assertEqual(direct, rule)
        self.assertEqual(shape, "direct_rule_object")
        self.assertEqual(other, "rule_json_wrapper")

    def test_long_answer_rule_location_is_measured_from_real_text(self):
        rule = audit.expected_rule("unique_fixture")
        text = "分析" * 800 + "\n" + json.dumps({"rule_json": rule})
        found, offset = audit.find_rule(text)
        self.assertGreater(offset, 1000)
        self.assertEqual(found, rule)

    def test_incomplete_or_unsupported_rule_cannot_pass_by_shape_normalization(self):
        for rule in ({"name": "missing_signal"}, {"signal": {"all": [{"indicator": "unknown"}]}}):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                audit.returned_rule(json.dumps(rule))

    def test_changed_conditions_are_not_hidden_by_parsing(self):
        rule = audit.expected_rule("unique_fixture")
        changed = audit.expected_rule("unique_fixture")
        changed["signal"]["all"][0]["window"] = 18
        self.assertNotEqual(audit.returned_rule(json.dumps(rule))[0], audit.returned_rule(json.dumps(changed))[0])


if __name__ == "__main__":
    unittest.main()
