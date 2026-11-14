"""Original verifier summaries cannot disguise infrastructure failure as reward."""
import unittest

from adapters.meal_plan_evaluation import classify_original, combine
from adapters.meal_plan_validation import result


def output(overall, body=""):
    return "=== Checking Google Calendar Events ===\n" + body + "\n=== Checking Excel ===\n=== SUMMARY ===\n  Overall: " + overall + "\n"


class MealPlanEvaluationTests(unittest.TestCase):
    def test_complete_consistent_original_pass_or_fail_is_measurable(self):
        self.assertIs(classify_original(0, output("PASS"), ""), True)
        self.assertIs(classify_original(1, output("FAIL"), ""), False)

    def test_database_error_with_overall_fail_remains_unknown(self):
        self.assertIsNone(classify_original(1, output("FAIL", "[FAIL] GCal check error: connection refused"), ""))

    def test_truncated_conflicting_and_exception_results_are_unknown(self):
        for code, stdout, stderr in ((0, "Overall: PASS", ""), (1, output("PASS"), ""), (0, output("FAIL"), ""),
                                     (1, output("FAIL"), "Traceback (most recent call last)"),
                                     (0, output("PASS") + "Overall: FAIL\n", ""), (2, output("FAIL"), "")):
            self.assertIsNone(classify_original(code, stdout, stderr))

    def test_either_verifier_unknown_prevents_binary_task_outcome(self):
        for upstream, supplemental in ((None, result(False)), (False, result(None, unknown=True)), (True, result(None, unknown=True))):
            measured = combine(upstream, supplemental)
            self.assertEqual(measured["evaluation_status"], "error")
            self.assertIsNone(measured["task_passed"])
            self.assertIsNone(measured["reward"])

    def test_both_measurable_results_required_for_success(self):
        self.assertTrue(combine(True, result(True))["task_passed"])
        self.assertIs(combine(True, result(False))["task_passed"], False)
        self.assertIs(combine(False, result(True))["task_passed"], False)

    def test_numeric_or_string_truth_values_do_not_become_valid_measurements(self):
        for value in (1, 0, "true", "false"):
            self.assertIsNone(combine(value, result(True))["task_passed"])
        self.assertIsNone(classify_original(True, output("FAIL"), ""))
