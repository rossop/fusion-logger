"""Checks for behaviour that differs between operating systems. Each prints what it saw, for the CI log."""
import json, os, sys, unittest, unittest.mock

sys.path.insert(0, os.path.dirname(__file__))
from test_logger import LoggerBase
import fake_adsk


class PlatformCase(LoggerBase):
    def test_final_line_is_written_after_the_file_is_marked_read_only(self):
        self.consent()
        self.ui.commandStarting.fire(fake_adsk.Args(commandId='ExtrudeCommand', isCanceled=False))
        self.assertEqual(self.send('finish')['status'], 'finished')
        path = self.logs()[0]
        last = self.records()[-1]
        print(f'\n[{os.name}] last record: {last["record"]}, writable afterwards: {os.access(path, os.W_OK)}, '
              f'errors: {[r["where"] for r in self.records() if r["record"] == "error"]}')
        self.assertEqual(last['record'], 'session_end')
        self.assertFalse(os.access(path, os.W_OK))
        self.assertEqual([r for r in self.records() if r['record'] == 'error'], [])

    def test_healthy_session_passes_every_check(self):
        self.consent()
        live, on_disk = os.fstat(self.logger._log_file.fileno()), os.stat(self.logs()[0])
        print(f'\n[{os.name}] fstat ino {live.st_ino} dev {live.st_dev}; stat ino {on_disk.st_ino} dev {on_disk.st_dev}')
        self.assertEqual(self.logger.runHealthChecks(), [])

    def test_moving_the_live_log_is_caught_or_refused(self):
        self.consent()
        path = self.logs()[0]
        try:
            os.rename(path, path + '.moved')
        except OSError as refused:
            # Windows will not move a file another handle has open, so the log cannot be lost this way.
            print(f'\n[{os.name}] moving the live log was refused: {type(refused).__name__}')
            self.assertEqual(self.logger.runHealthChecks(), [])
            return
        print(f'\n[{os.name}] live log moved')
        self.assertIn('file_in_place', self.logger.runHealthChecks())

    def test_lines_end_as_the_platform_writes_them_and_still_parse(self):
        self.consent()
        self.send('finish')
        with open(self.logs()[0], 'rb') as f:
            raw = f.read()
        print(f'\n[{os.name}] CRLF line endings: {b"\r\n" in raw}')
        self.assertTrue(all(json.loads(line) for line in raw.splitlines() if line.strip()))

    def test_read_only_works_without_fchmod(self):
        # Python before 3.13 on Windows has no fchmod, so the path fallback must do the job.
        self.consent()
        with unittest.mock.patch.object(self.logger.os, 'fchmod', side_effect=AttributeError, create=True):
            self.send('finish')
        self.assertFalse(os.access(self.logs()[0], os.W_OK))
        self.assertEqual([r for r in self.records() if r['record'] == 'error'], [])

    def test_read_only_without_fchmod_when_the_add_in_is_stopped(self):
        # Stopping from Scripts and Add-Ins clears the saved path before the session ends.
        self.consent()
        with unittest.mock.patch.object(self.logger.os, 'fchmod', side_effect=AttributeError, create=True):
            self.logger.stop(None)
        self.assertEqual(self.records()[-1]['record'], 'session_end')
        self.assertFalse(os.access(self.logs()[0], os.W_OK))

    def test_crashed_file_is_locked_at_the_next_launch(self):
        self.consent()
        self.logger._log_file.close()
        self.logger._log_file = None
        self.logger.stopHealthChecks()
        self.assertEqual(self.send('status')['previous']['ended'], 'cut_short')
        print(f'\n[{os.name}] crashed file writable after relaunch: {os.access(self.logs()[0], os.W_OK)}')
        self.assertFalse(os.access(self.logs()[0], os.W_OK))


if __name__ == '__main__':
    unittest.main()
