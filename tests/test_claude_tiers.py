"""Claude alias switching against scratch configs only; no installed config reads."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agents
import gateway
import i18n


class ClaudeTiersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        data = self.home / 'data'
        self.config = self.home / '.claude' / 'settings.json'
        self.config.parent.mkdir()
        for target, attrs in (
                (gateway, {'DATA_DIR': data}),
                (i18n, {'DATA_DIR': data, 'SETTINGS_FILE': data / 'ui-settings.json'}),
                (agents, {'STATE_FILE': data / 'agents.json', 'BACKUP_DIR': data / 'agent-backups'})):
            for name, value in attrs.items():
                p = patch.object(target, name, value)
                p.start()
                self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'HOME': str(self.home), 'CLAUDE_CONFIG_DIR': str(self.config.parent),
                                    'API_CONSOLE_HOME': str(data)})
        p.start()
        self.addCleanup(p.stop)
        self.agent = agents.ClaudeCode()

    def write(self, data):
        self.config.write_text(json.dumps(data), encoding='utf-8')

    def read(self):
        return json.loads(self.config.read_text(encoding='utf-8'))

    def enable(self, model='main'):
        self.agent.enable('http://127.0.0.1:9999', 'sk-local-test', model, [model])

    def tiers(self, on=True):
        i18n.save_routing_settings({'claudeTiersEnabled': on, 'claudeTierLarge': 'big',
                                  'claudeTierMedium': 'mid', 'claudeTierSmall': 'tiny'})

    def test_off_on_reenable_off_exact_bytes(self):
        original = b'{\r\n "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL":"original"}, "theme":"dark"\r\n}\r\n'
        self.config.write_bytes(original)
        self.enable()
        self.assertEqual(self.read()['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'], 'main')
        self.tiers()
        self.enable('next')
        env = self.read()['env']
        self.assertNotIn('CLAUDE_CODE_SUBAGENT_MODEL', env)
        for alias, value in [('OPUS', 'big'), ('SONNET', 'mid'), ('HAIKU', 'tiny')]:
            self.assertEqual(env['ANTHROPIC_DEFAULT_' + alias + '_MODEL'], value)
        self.tiers(False)
        self.enable()
        env = self.read()['env']
        self.assertEqual(env['ANTHROPIC_MODEL'], 'main')
        self.assertEqual(env['CLAUDE_CODE_SUBAGENT_MODEL'], 'main')
        for alias in ('OPUS', 'SONNET', 'HAIKU'):
            self.assertNotIn('ANTHROPIC_DEFAULT_' + alias + '_MODEL', env)
        self.assertEqual(self.agent.disable(), 'exact')
        self.assertEqual(self.config.read_bytes(), original)

    def test_user_edits_survive_reenable_and_disable(self):
        self.write({'env': {'ANTHROPIC_DEFAULT_OPUS_MODEL': 'original'}, 'theme': 'dark'})
        self.tiers()
        self.enable()
        data = self.read()
        data['theme'] = 'light'
        data['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'] = 'user-big'
        self.write(data)
        self.enable('next')
        self.assertEqual(self.read()['env']['ANTHROPIC_DEFAULT_OPUS_MODEL'], 'big')
        self.assertEqual(self.agent.disable(), 'values')
        self.assertEqual(self.read(), {'env': {'ANTHROPIC_DEFAULT_OPUS_MODEL': 'user-big'}, 'theme': 'light'})

    def test_user_edit_after_enable_is_kept(self):
        self.write({'env': {'ANTHROPIC_MODEL': 'original'}})
        self.tiers()
        self.enable()
        data = self.read()
        data['env']['ANTHROPIC_MODEL'] = 'user-main'
        data['new'] = True
        self.write(data)
        self.assertEqual(self.agent.disable(), 'values')
        self.assertEqual(self.read(), {'env': {'ANTHROPIC_MODEL': 'user-main'}, 'new': True})

    def test_missing_file_restored(self):
        self.enable()
        self.tiers()
        self.enable()
        self.assertEqual(self.agent.disable(), 'exact')
        self.assertFalse(self.config.exists())

    def test_new_dynamic_field_original_captured(self):
        self.write({'env': {'EXTRA': 'before'}})
        self.enable()
        changes = self.agent.changes
        def expanded(*args):
            return {**changes(*args), ('env', 'EXTRA'): 'gateway'}
        with patch.object(self.agent, 'changes', expanded):
            self.enable()
        data = self.read()
        data['theme'] = 'light'
        self.write(data)
        self.assertEqual(self.agent.disable(), 'values')
        self.assertEqual(self.read(), {'env': {'EXTRA': 'before'}, 'theme': 'light'})

    def test_empty_on_tiers_remove_overrides(self):
        self.write({'env': {'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'original'}})
        i18n.save_routing_settings({'claudeTiersEnabled': True})
        self.enable()
        self.assertNotIn('ANTHROPIC_DEFAULT_HAIKU_MODEL', self.read()['env'])
        self.assertEqual(self.agent.disable(), 'exact')


if __name__ == '__main__':
    unittest.main()
