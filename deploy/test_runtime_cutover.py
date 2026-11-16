import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('cutover', Path(__file__).with_name('runtime_cutover.py'))
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)


class CutoverTests(unittest.TestCase):
    def test_dynamic_state_move_and_rollback_preserve_current_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public, private = root / 'state', root / 'private/state'
            private.mkdir(parents=True)
            (private / 'bot.sqlite3').write_bytes(b'original')
            public.symlink_to(private)
            moved = cutover.normalize_state(public, private)
            self.assertTrue(moved)
            self.assertFalse(public.is_symlink())
            (public / 'bot.sqlite3').write_bytes(b'new current state')
            cutover.restore_layout(moved, public, private)
            self.assertTrue(public.is_symlink())
            self.assertEqual((public / 'bot.sqlite3').read_bytes(), b'new current state')

    def test_refuse_unrelated_symlink_without_changing_any_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public, other, private = root / 'state', root / 'other', root / 'private'
            other.mkdir(); private.mkdir(); public.symlink_to(other)
            with self.assertRaises(RuntimeError):
                cutover.normalize_state(public, private)
            self.assertEqual(public.resolve(), other.resolve())

    def test_consistent_backup_and_protected_hashes_are_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'state.sqlite3', Path(folder) / 'backup.sqlite3'
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT)')
                db.executemany('INSERT INTO kv VALUES (?,?)',
                               [(key, '{"preserved":true}') for key in cutover.KEYS])
            before = source.read_bytes()
            hashes = cutover.state_hashes(source)
            cutover.backup_db(source, target)
            self.assertEqual(hashes, cutover.state_hashes(target))
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
