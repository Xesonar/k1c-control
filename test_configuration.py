"""Deployment identity and offline startup boundaries."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bot
from access import AccessController
from configuration import ConfigurationError, validate_config
from manager import Manager
from media_service import MediaClient
from storage import Store
from test_manager import Printer, Telegram


class ConfigurationTests(unittest.TestCase):
    def settings(self, **overrides):
        value = {'owner_id': 20001, 'bot_token': 'test:credential',
                 'allowed_user_ids': [], 'printer_url': 'http://printer.local:7126',
                 'snapshot_url': 'http://printer.local:1984/api/frame.jpeg?src=chassis'}
        value.update(overrides)
        return value

    def test_identity_is_explicit_and_missing_invalid_owner_never_migrates_access(self):
        for owner in (None, 0, -1, True, '20001', 2**63):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as folder:
                store = Store(Path(folder) / 'bot.sqlite3')
                try:
                    with self.assertRaisesRegex(ValueError, 'invalid_owner'):
                        Manager(self.settings(owner_id=owner, state_dir=folder), store, Printer(), Telegram())
                    self.assertIsNone(store.get('access_control'))
                finally:
                    store.close()

    def test_owner_roles_follow_configuration_not_an_example_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'bot.sqlite3')
            try:
                manager = Manager(self.settings(state_dir=folder, allowed_user_ids=[10001]), store, Printer(), Telegram())
                self.assertEqual(manager.owners, {20001})
                self.assertTrue(manager.access.is_owner(20001))
                self.assertTrue(manager.access.is_allowed(10001))
                self.assertFalse(manager.access.is_owner(10001))
            finally:
                store.close()

    def test_access_controller_has_no_default_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'state.sqlite3')
            try:
                with self.assertRaisesRegex(ValueError, 'invalid_owner'):
                    AccessController(store)
                self.assertIsNone(store.get('access_control'))
            finally:
                store.close()

    def test_offline_validation_does_not_create_state_or_contact_services(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'config.json'; config.write_text(json.dumps(self.settings()))
            with patch('sys.argv', ['bot.py', '--config', str(config), '--state-dir', str(root/'state'), '--check-config']), \
                    patch.object(bot, 'Store', side_effect=AssertionError('must not create state')) as store, \
                    patch.object(bot, 'Telegram', side_effect=AssertionError('must not contact Telegram')) as telegram, \
                    patch.object(bot.logging, 'basicConfig'), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                bot.main()
            self.assertTrue(json.loads(output.getvalue())['ok'])
            self.assertFalse((root/'state').exists())
            store.assert_not_called(); telegram.assert_not_called()

    def test_configuration_errors_never_echo_credentials(self):
        invalid = self.settings(printer_url='http://private-user:private-password@printer.local')
        with self.assertRaises(ConfigurationError) as caught:
            validate_config(invalid)
        self.assertNotIn('private-user', str(caught.exception))
        self.assertNotIn('private-password', str(caught.exception))
        for change in ({'owner_id': 0}, {'bot_token':'YOUR_TELEGRAM_BOT_TOKEN'},
                       {'allowed_user_ids':[True]}, {'web_enabled': True, 'web_origin':'http://dashboard.example.com'}):
            with self.subTest(change=change), self.assertRaises(ConfigurationError):
                validate_config(self.settings(**change))

    def test_media_deletion_uses_configured_owner_and_never_grants_a_default(self):
        with tempfile.TemporaryDirectory() as folder:
            media = MediaClient({'media_state_dir': folder, 'owner_id': 20001})
            try:
                media_id = 'a'*32
                file = Path(folder)/'archive'/f'{media_id}.mp4';file.write_bytes(b'video')
                media.store.reserve_archive(media_id, 'example-job', 'clip', media.store.relative(file), 5, 1)
                with self.assertRaisesRegex(Exception, 'owner_required'):
                    media.owner_delete(10001, media_id, is_owner=True)
                self.assertTrue(file.exists())
                self.assertEqual(media.owner_delete(20001, media_id, is_owner=True), {'deleted':media_id})
            finally:
                media.close()
