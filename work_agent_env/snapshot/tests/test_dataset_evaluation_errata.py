"""Reviewed rules preserve numeric meaning and reject incorrect values."""
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
path=ROOT/'integrations/benchmarks/toolathon-gym/adapters/evaluation_errata.py'
spec=importlib.util.spec_from_file_location('dataset_evaluation_errata',path)
errata=importlib.util.module_from_spec(spec)
spec.loader.exec_module(errata)


class EvaluationErrataTests(unittest.TestCase):
    def test_thousands_separators_do_not_change_values_or_accept_different_numbers(self):
        self.assertTrue(errata.has_number('Total 12,000 citations; average 3,000.',12000))
        self.assertTrue(errata.has_number('Fare 349.00 CNY',349))
        self.assertFalse(errata.has_number('Total 120,000 citations',12000))
        self.assertFalse(errata.has_number('Cost 3490 CNY',349))
        self.assertFalse(errata.has_number('Cost 349.9 CNY',349))

    def test_only_declared_sources_are_changed_and_source_drift_is_rejected(self):
        package=ROOT/'integrations/benchmarks/toolathon-gym'
        for task in ('arxiv-fetch-terminal-pipeline','12306-beijing-shanghai-trip-notion-gcal-word','12306-canvas-fieldtrip-gcal-word-email'):
            source=(package/'tasks'/task/'evaluation/main.py').read_text()
            reviewed,notes=errata.reviewed_source(task,source)
            self.assertTrue(notes)
            self.assertNotEqual(source,reviewed)
            compile(reviewed,str(path),'exec')
        self.assertEqual(errata.reviewed_source('unrelated','print(1)'),(None,[]))
        with self.assertRaises(RuntimeError):errata.reviewed_source('arxiv-fetch-terminal-pipeline','print(1)')

    def test_arxiv_id_syntax_remains_exact_and_does_not_reinterpret_topic_queries(self):
        query_path=path.with_name('arxiv_query.py')
        query_spec=importlib.util.spec_from_file_location('dataset_arxiv_query',query_path)
        query=importlib.util.module_from_spec(query_spec);query_spec.loader.exec_module(query)
        self.assertEqual(query.paper_ids('id:2301.13379'),['2301.13379'])
        self.assertEqual(query.paper_ids('id:2301.13379v2 OR id:2302.01560'),['2301.13379','2302.01560'])
        self.assertEqual(query.paper_ids('2301.13379'),['2301.13379'])
        self.assertIsNone(query.paper_ids('prompt engineering'))
        self.assertIsNone(query.paper_ids("id:2301.13379' OR 1=1"))
