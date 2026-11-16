import unittest
from types import SimpleNamespace
from skillrl.pastbench_persistence import PROTOCOL, restore_specifications, restore_environment_flags, persistence_runtime_enabled


class PersistenceRestorationTests(unittest.TestCase):
    def rule(self, **updates):
        rule = {'persistence_allowed': True, 'shared_cold_run': False, 'history_mode': 'continue',
                'history_load_anchor': '', 'history_save_anchor': '',
                'tool_flags': dict(memory_enabled=False, user_profile_enabled=False, skills_enabled=False, session_search_enabled=True)}
        rule.update(updates)
        return rule

    def apply(self, spec, rule):
        return restore_specifications([spec], {'protocol': PROTOCOL, 'upstream_commit': 'pinned', 'episodes': {'episode': rule}})[0]

    def test_absent_protocol_preserves_existing_contract_and_object(self):
        specs = [{'task': '/tasks/episode', 'prompt': 'unchanged'}]
        self.assertIs(restore_specifications(specs, None), specs)

    def test_only_persistence_fields_change_and_source_is_immutable(self):
        spec = {'task': '/tasks/episode', 'persistence_allowed': True, 'history_mode': 'from_anchor',
                'mechanism': 'session_search', 'private_grader': 'unchanged'}
        restored = self.apply(spec, self.rule(persistence_allowed=False, history_mode='fresh'))
        self.assertFalse(restored['persistence_allowed'])
        self.assertEqual(restored['history_mode'], 'fresh')
        self.assertEqual(restored['private_grader'], 'unchanged')
        self.assertEqual(restored['mechanism'], spec['mechanism'])
        self.assertTrue(spec['persistence_allowed'])

    def test_collection_cold_stays_enabled_even_with_shared_cold_metadata(self):
        flags = {k: False for k in self.rule()['tool_flags']}
        flags['memory_enabled'] = True
        restored = self.apply({'task': '/tasks/episode', 'shared_cold_run': True}, self.rule(shared_cold_run=True, tool_flags=flags))
        self.assertTrue(restored['persistence_allowed'])
        self.assertTrue(restored['shared_cold_run'])
        self.assertTrue(persistence_runtime_enabled(restored))

    def test_original_history_only_permission_overrides_pg_skill_extension(self):
        spec = self.apply({'task': '/tasks/episode'}, self.rule())
        env = SimpleNamespace(persistence_flags={k: True for k in spec['persistence_tool_flags']}, skill_write_capability={'keep': False})
        restore_environment_flags(env, spec)
        self.assertTrue(env.persistence_flags['session_search_enabled'])
        self.assertFalse(env.persistence_flags['skills_enabled'])
        self.assertFalse(hasattr(env, 'skill_write_capability'))

    def test_disabled_all_and_missing_inventory_fail_closed(self):
        flags = {k: False for k in self.rule()['tool_flags']}
        spec = self.apply({'task': '/tasks/episode'}, self.rule(persistence_allowed=False, history_mode='fresh', tool_flags=flags))
        env = SimpleNamespace(persistence_flags={}, skill_write_capability={})
        restore_environment_flags(env, spec)
        self.assertFalse(any(env.persistence_flags.values()))
        with self.assertRaisesRegex(ValueError, 'inventory'):
            restore_specifications([{'task': '/tasks/missing'}], {'protocol': PROTOCOL, 'upstream_commit': 'pin', 'episodes': {'episode': self.rule()}})


if __name__ == '__main__':
    unittest.main()
