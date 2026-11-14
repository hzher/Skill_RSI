"""VIP source ambiguity, original-validator blind spots and local mail contract."""
from copy import deepcopy
from decimal import Decimal
import unittest

from adapters import vip_email_validation as vip


def customers():
    names = ("Ann", "Bob", "Cara", "Dan", "Eve", "Fred", "Grace", "Hugh", "Ivy", "Jack", "Kate", "Leon")
    return [{"id": i+1, "first_name": name, "last_name": "Example", "email": name.lower() + "@example.com",
        "orders_count": 3, "total_spent": str((12-i)*100)} for i,name in enumerate(names)]


def outputs(source):
    truth = vip.expected(source, source_binding=vip.source_identity())
    cloud = {"title": vip.TITLE, "sheets": [
        {"title": "Top Customers", "headers": list(vip.HEADERS["Top Customers"]), "rows": deepcopy(truth["rows"])},
        {"title": "Store Summary", "headers": ["Metric", "Value"], "rows": [{"Metric": k, "Value": v} for k,v in truth["metrics"].items()]}]}
    mail = [{"id": i+1, "from_addr": vip.SENDER, "to_addr": [address], "subject": vip.SUBJECT,
        "body_text": "Dear " + first + ", thank you for your loyalty and patronage."}
        for i,(address,first) in enumerate(truth["recipients"].items())]
    logs = [{"id": m["id"], "message_id": m["id"]} for m in mail]
    return cloud, mail, logs


class VipEmailTests(unittest.TestCase):
    def setUp(self):
        self.source = customers(); self.cloud, self.mail, self.logs = outputs(self.source)

    def measure(self, **kw):
        return vip.measure(kw.get("source", self.source), kw.get("cloud", self.cloud), kw.get("mail", self.mail),
            kw.get("logs", self.logs), source_binding=kw.get("binding", vip.source_identity()))

    def failed(self):
        result = self.measure(); self.assertIs(result["supplemental_passed"], False); self.assertEqual(result["evaluation_status"], "completed")

    def test_complete_pure_contract_has_no_runtime_or_reward_claim(self):
        result = self.measure(); self.assertTrue(result["supplemental_passed"])
        self.assertIsNone(result["task_passed"]); self.assertIsNone(result["reward"])
        for key in ("training_eligible", "nemo_task_executed", "original_evaluator_executed", "source_completeness_verified", "resource_ownership_verified"):
            self.assertIs(result[key], False)

    def test_one_cent_error_is_rejected_even_with_original_half_dollar_tolerance(self):
        self.cloud["sheets"][0]["rows"][0]["Total_Spent"] += Decimal("0.01"); self.failed()

    def test_repeated_valid_customer_cannot_displace_another_top_ten(self):
        rows = self.cloud["sheets"][0]["rows"]; rows[-1] = deepcopy(rows[-2]); self.failed()

    def test_top_customer_order_must_be_descending(self):
        self.cloud["sheets"][0]["rows"].reverse(); self.failed()

    def test_duplicate_or_extra_sheet_and_wrong_title_fail(self):
        original = deepcopy(self.cloud)
        for update in (lambda: self.cloud.update(title="My VIP"),
            lambda: self.cloud["sheets"].append(deepcopy(self.cloud["sheets"][0])),
            lambda: self.cloud["sheets"][1].update(title="Top Customers")):
            self.cloud = deepcopy(original); update(); self.failed()

    def test_summary_duplicates_and_wrong_revenue_share_fail(self):
        summary = self.cloud["sheets"][1]["rows"]; original = deepcopy(summary)
        summary[-1] = deepcopy(summary[0]); self.failed()
        self.cloud["sheets"][1]["rows"] = original
        self.cloud["sheets"][1]["rows"][2]["Value"] += Decimal("0.1"); self.failed()

    def test_wrong_or_repeated_recipient_fails(self):
        self.mail[0]["to_addr"] = [self.mail[1]["to_addr"][0]]; self.failed()
        self.mail[0]["to_addr"] = ["outsider@example.com"]; self.failed()

    def test_extra_mail_and_missing_log_are_business_failures(self):
        self.mail.append(deepcopy(self.mail[0])); self.failed(); self.mail.pop()
        self.logs.pop(); self.failed()

    def test_duplicate_log_link_does_not_prove_five_sends(self):
        self.logs[-1]["message_id"] = self.logs[0]["message_id"]; self.failed()

    def test_sender_subject_and_hidden_recipients_are_checked(self):
        original = deepcopy(self.mail[0])
        for key,value in (("from_addr", "other@example.com"), ("subject", "Thanks"), ("bcc_addr", ["hidden@example.com"]), ("body_html", "hidden")):
            self.mail[0] = {**original, key: value}; self.failed()

    def test_first_name_substring_is_not_a_personalized_address(self):
        self.mail[0]["body_text"] = "Dear Joanne, thank you for your loyalty."; self.failed()

    def test_thanks_and_loyalty_words_are_only_lexical_checks(self):
        self.mail[0]["body_text"] = "Ann, here is an unrelated update."; self.failed()
        self.assertEqual(self.measure()["mail_body_semantics"], "first_name_and_english_lexical_checks_only")

    def test_wrong_source_binding_or_missing_field_is_unknown(self):
        self.assertIsNone(self.measure(binding={})["supplemental_passed"])
        del self.source[0]["total_spent"]
        self.assertIsNone(self.measure()["supplemental_passed"])

    def test_ambiguous_fifth_and_tenth_rank_boundaries_are_unknown(self):
        for boundary in (5, 10):
            source = customers(); source[boundary]["total_spent"] = source[boundary-1]["total_spent"]
            result = self.measure(source=source)
            self.assertIsNone(result["supplemental_passed"]); self.assertIn("boundary_tie", result["errors"][0])

    def test_internal_tie_can_permute_without_changing_selection(self):
        self.source[1]["total_spent"] = self.source[0]["total_spent"]
        self.cloud, self.mail, self.logs = outputs(self.source)
        rows = self.cloud["sheets"][0]["rows"]; rows[0],rows[1] = rows[1],rows[0]
        self.assertTrue(self.measure()["supplemental_passed"])

    def test_insufficient_customers_duplicate_email_and_nonfinite_are_unknown(self):
        nonfinite = customers(); nonfinite[0]["total_spent"] = "NaN"
        negative = customers(); negative[0]["total_spent"] = "-1"
        for source in (customers()[:9], nonfinite, negative):
            self.assertIsNone(self.measure(source=source)["supplemental_passed"])
        source = customers(); source[-1]["email"] = source[0]["email"]
        self.assertIsNone(self.measure(source=source)["supplemental_passed"])

    def test_decimal_rounding_and_zero_order_aov_match_documented_policy(self):
        self.source[0].update(total_spent="1200.005", orders_count=0)
        truth = vip.expected(self.source, source_binding=vip.source_identity())
        self.assertEqual(truth["rows"][0]["Total_Spent"], Decimal("1200.01"))
        self.assertEqual(truth["rows"][0]["Avg_Order_Value"], Decimal("0.00"))
        self.cloud, self.mail, self.logs = outputs(self.source)
        self.assertTrue(self.measure()["supplemental_passed"])

    def test_missing_output_observation_is_unknown_not_a_measured_failure(self):
        result = self.measure(mail=None)
        self.assertIsNone(result["supplemental_passed"]); self.assertEqual(result["evaluation_status"], "error")
