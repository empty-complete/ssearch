"""Visible, bounded pytest diagnostics for ssearch source-only Windows CI.

The launcher validates selected targets and source identity before this entry.
The retained Windows job owns this process and all descendants, including when
the hard deadline exits without pytest teardown. No profiles or logs are exported.
"""
from __future__ import annotations

import argparse
import faulthandler
import os
import sys
import threading

import pytest


class Progress:
    def __init__(self, *, dump_after=120, timeout=900):
        self.dump_after = dump_after
        self.timeout = timeout
        # Duplicate before pytest starts FD capture. Keeping sys.__stderr__
        # alone is insufficient: capfd can redirect the underlying descriptor.
        self.output = os.fdopen(os.dup(sys.__stderr__.fileno()), 'w',
                                encoding='utf-8', buffering=1)
        self.lock = threading.Lock()
        self.generation = 0
        self.timers = []

    def arm(self, label):
        self.cancel()
        with self.lock:
            generation = self.generation
            print(f'CI pytest starting: {label}', file=self.output, flush=True)
            for seconds, hard in ((self.dump_after, False), (self.timeout, True)):
                timer = threading.Timer(seconds, self.report, (generation, hard))
                timer.daemon = True
                self.timers.append(timer)
                timer.start()

    def report(self, generation, hard):
        with self.lock:
            if generation != self.generation:
                return
            seconds = self.timeout if hard else self.dump_after
            print(f'CI pytest {"hard timeout" if hard else "stack after"}: {seconds}s',
                  file=self.output, flush=True)
            # faulthandler prints stack frames only: never locals or values.
            faulthandler.dump_traceback(file=self.output, all_threads=True)
            if hard:
                # Works on Windows, including when pytest teardown is stuck.
                # The parent observes 124 and terminates its retained job tree.
                os._exit(124)

    def cancel(self):
        with self.lock:
            self.generation += 1
            for timer in self.timers:
                timer.cancel()
            self.timers.clear()

    def close(self):
        self.cancel()
        self.output.close()

    def pytest_sessionstart(self, session):
        self.arm('collection')

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        # One deadline includes setup, call and teardown, including fixtures.
        self.arm(item.nodeid)
        try:
            yield
        finally:
            self.arm('between tests / session cleanup')


def main(argv=None, *, dump_after=120, timeout=900):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('targets', nargs='+')
    args = parser.parse_args(argv)
    progress = Progress(dump_after=dump_after, timeout=timeout)
    try:
        # Unbuffered Python plus explicit newline progress survives the parent's
        # line reader. Keep normal pytest capture and avoid dumping local values.
        return int(pytest.main(['-vv', '--tb=short', *args.targets], plugins=[progress]))
    finally:
        progress.close()


if __name__ == '__main__':
    raise SystemExit(main())
