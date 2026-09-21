"""Integration tests for the default polling settings; run after building xtail."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


BINARY = Path(os.environ.get('XTAIL_BINARY', './xtail')).resolve()


class WatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xtail-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.output = self.root / 'stdout'
        self.debug = self.root / 'stderr'

    def start(self):
        with self.output.open('wb') as out, self.debug.open('wb') as err:
            self.process = subprocess.Popen(
                [str(BINARY), '-D', str(self.logs)], stdout=out, stderr=err)
        self.addCleanup(self.stop)
        self.wait_for(self.debug, '>>> checking directory list')

    def stop(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=3)

    def wait_for(self, path, text, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            contents = path.read_text()
            if text in contents:
                return contents
            self.assertIsNone(self.process.poll(), self.debug.read_text())
            time.sleep(0.05)
        self.fail('Timed out waiting for %r in %s:\n%s\nDebug:\n%s' %
                  (text, path.name, path.read_text(), self.debug.read_text()))

    def pause(self):
        self.process.send_signal(signal.SIGSTOP)
        _, status = os.waitpid(self.process.pid, os.WUNTRACED)
        self.assertTrue(os.WIFSTOPPED(status))

    def test_new_file_with_unchanged_directory_timestamp(self):
        # Restoring mtime reproduces the same-second discovery race without
        # depending on scheduler speed or filesystem timestamp resolution.
        self.start()
        self.pause()
        previous = self.logs.stat()
        (self.logs / 'new.log').write_text('NEW CONTENT\n')
        os.utime(self.logs, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.process.send_signal(signal.SIGCONT)
        self.wait_for(self.output, 'NEW CONTENT\n')
        with (self.logs / 'new.log').open('a') as stream:
            stream.write('LATER APPEND\n')
        self.wait_for(self.output, 'LATER APPEND\n')

    def test_replacement_of_closed_file_with_same_size_and_mtime(self):
        self.check_closed_replacement('NEW CONTENT\n')

    def test_replacement_of_closed_file_larger_than_old_offset(self):
        self.check_closed_replacement('NEW CONTENT LONGER THAN BEFORE\n')

    def check_closed_replacement(self, replacement):
        cold = self.logs / 'cold.log'
        cold.write_text('OLD CONTENT\n')
        os.utime(cold, (1000000000, 1000000000))
        # Default MAX_OPEN is eight: keep cold.log outside the open set.
        for index in range(8):
            (self.logs / ('hot%d.log' % index)).write_text('HOT\n')
        self.start()
        self.pause()
        self.assertNotIn("opening entry '%s'" % cold, self.debug.read_text())
        old = cold.stat()
        cold.rename(self.root / 'old.log')  # Retain old inode, prevent reuse.
        cold.write_text(replacement)
        os.utime(cold, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertNotEqual(old.st_ino, cold.stat().st_ino)
        self.process.send_signal(signal.SIGCONT)
        self.wait_for(self.output, replacement)

    def test_existing_file_starts_at_end_and_follows_appends(self):
        log = self.logs / 'existing.log'
        log.write_text('HISTORY\n')
        self.start()
        with log.open('a') as stream:
            stream.write('APPENDED\n')
        contents = self.wait_for(self.output, 'APPENDED\n')
        self.assertNotIn('HISTORY\n', contents)

    def test_open_file_rotation(self):
        log = self.logs / 'existing.log'
        log.write_text('HISTORY\n')
        self.start()
        self.pause()
        log.rename(self.root / 'rotated.log')
        log.write_text('REPLACEMENT\n')
        self.process.send_signal(signal.SIGCONT)
        self.wait_for(self.output, 'REPLACEMENT\n')

    def test_truncation_rewinds(self):
        log = self.logs / 'existing.log'
        log.write_text('LONG HISTORICAL CONTENT\n')
        self.start()
        log.write_text('SHORT\n')
        self.wait_for(self.output, 'SHORT\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
