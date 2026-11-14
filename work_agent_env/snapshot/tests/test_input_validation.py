"""Malformed and impossible initial resources must fail before a run is created."""
import copy
import json
import unittest
from workenv.taskpacks import pack
from workenv.input_validation import validate_inputs


class InputValidationTests(unittest.TestCase):
    def inputs(self,family):return pack(family,11)["inputs"]

    def test_supported_generated_worlds_and_variants_validate(self):
        for family in ("receivables","meeting","documents","tickets"):
            for variant in ("base","changed_constraint"):
                self.assertTrue(validate_inputs(family,pack(family,21,variant)["inputs"])["schema_valid"])

    def test_duplicate_csv_headers_are_rejected(self):
        inputs=self.inputs("tickets")
        inputs["directory.csv"]="person,person,active\na,b,true\n"
        with self.assertRaises(ValueError):validate_inputs("tickets",inputs)

    def test_nonfinite_money_is_rejected(self):
        inputs=self.inputs("receivables")
        data=inputs["invoices.csv"].splitlines()
        row=data[1].rsplit(",",1)
        data[1]=row[0]+",NaN"
        inputs["invoices.csv"]="\n".join(data)+"\n"
        with self.assertRaises(ValueError):validate_inputs("receivables",inputs)

    def test_orphan_payment_is_rejected(self):
        inputs=self.inputs("receivables")
        inputs["payments.csv"]+="UNKNOWN,2026-09-01,10.00\n"
        with self.assertRaises(ValueError):validate_inputs("receivables",inputs)

    def test_empty_payments_and_empty_busy_calendar_are_valid(self):
        invoices=self.inputs("receivables")
        invoices["payments.csv"]="invoice_id,payment_date,amount\n"
        self.assertTrue(validate_inputs("receivables",invoices)["schema_valid"])
        meeting=self.inputs("meeting")
        meeting["busy.csv"]="person,start,end\n"
        self.assertTrue(validate_inputs("meeting",meeting)["schema_valid"])

    def test_invalid_calendar_timezone_is_rejected(self):
        inputs=self.inputs("meeting")
        inputs["availability.csv"]=inputs["availability.csv"].replace("T09:00","T09:00+08:00")
        with self.assertRaises(ValueError):validate_inputs("meeting",inputs)

    def test_policy_without_effective_approved_version_is_rejected(self):
        inputs=self.inputs("documents")
        for key in inputs:
            if key.startswith("policies/"):inputs[key]=inputs[key].replace("Status: approved","Status: draft")
        with self.assertRaises(ValueError):validate_inputs("documents",inputs)

    def test_insufficient_active_team_capacity_is_rejected(self):
        inputs=self.inputs("tickets")
        inputs["directory.csv"]=inputs["directory.csv"].replace(",true",",false")
        with self.assertRaises(ValueError):validate_inputs("tickets",inputs)

    def test_unknown_ticket_priority_is_rejected(self):
        inputs=self.inputs("tickets")
        inputs["tickets.csv"]=inputs["tickets.csv"].replace(",high,",",unknown,")
        with self.assertRaises(ValueError):validate_inputs("tickets",inputs)


if __name__=="__main__":unittest.main()
