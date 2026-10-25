"""Source service entry with real P2 ownership and no GUI dependency.

The CLI has a dedicated process boundary. Deadline failure exits that process,
leaves durable ownership recovery to P2 and never kills a discovered process.
Packaging's frozen launcher and Electron ownership leases belong to Task 3.
"""
import argparse
from pathlib import Path
import queue
import signal
import sys
import threading


def serve(profile: Path, *, model_dir: Path | None = None, stop: threading.Event | None = None,
          startup_timeout: float = 120, shutdown_timeout: float = 45) -> int:
    """Run one owner (or P2 attach) until stop, with bounded lifecycle replies.

    A nonzero result is terminal for the dedicated service process. An embedded
    caller must likewise not restart after a deadline: the existing claim remains
    authoritative while cleanup is uncertain. No foreign process is terminated.
    """
    if startup_timeout <= 0 or shutdown_timeout <= 0:
        raise ValueError('positive_deadline_required')
    from atom_search.paths import model_path
    # Lazy import keeps cold service/broker imports independent of composition.
    from .owner import open_service_owner
    stop = stop if stop is not None else threading.Event()
    ready, done = threading.Event(), threading.Event()
    outcome = queue.Queue(maxsize=1)

    def lifecycle():
        owner = None
        code = 1
        try:
            owner = open_service_owner(Path(profile), model_dir if model_dir is not None else model_path())
            if not stop.is_set():
                owner.start()
            ready.set()
            stop.wait()
            code = 0
        except BaseException:
            ready.set()
        finally:
            stop.set()
            try:
                if owner is not None:
                    owner.close()  # Attached owner close is intentionally harmless.
            except BaseException:
                code = 1
            outcome.put(code)
            done.set()

    threading.Thread(target=lifecycle, name='atom-headless-owner', daemon=True).start()
    if not ready.wait(startup_timeout):
        stop.set()
        return 1
    stop.wait()
    if not done.wait(shutdown_timeout):
        return 1
    return outcome.get_nowait()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='ssearch headless source service')
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--control-stdin', action='store_true',
                        help='stop when the private inherited control pipe reaches EOF')
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = {}
    for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
    if args.control_stdin:
        def control():
            try:
                while sys.stdin.buffer.read(1):
                    pass  # No token, arbitrary command or second settings writer.
            finally:
                stop.set()
        threading.Thread(target=control, name='atom-headless-control', daemon=True).start()
    try:
        return serve(args.data_dir, model_dir=args.model_dir, stop=stop)
    except Exception:
        return 1  # No traceback/private paths on stdout or stderr.
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    code = main()
    if code:
        # Service workers may still be draining after an exceeded deadline.
        # Bound only this dedicated process; never taskkill a PID from discovery.
        import os
        os._exit(code)
    raise SystemExit(code)
