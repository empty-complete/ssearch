"""Windowed executable entry point; assets stay in the onedir installation."""

import os
from pathlib import Path
import sys
import traceback

from atom_search.content import message


def launch() -> int:
    # Defense in depth if a future local inference dependency gains hub support.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    if "--postgres-self-test" in sys.argv:
        # An owned one-shot executable may sanitize before importing workers.
        # Direct library calls never modify the caller's environment.
        import argparse
        from atom_search.diagnostics import run_postgres_self_test
        from atom_search.runtime.postgres import sanitize_host_environment
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--postgres-self-test", type=Path, required=True)
        parser.add_argument("--data-dir", type=Path, required=True)
        parser.add_argument("--postgres-qa-lifecycle", choices=("seed", "verify"))
        parser.add_argument("--postgres-qa-run")
        try:
            args = parser.parse_args()
            sanitize_host_environment()
            if args.postgres_qa_lifecycle:
                return run_postgres_self_test(
                    args.postgres_self_test, data_dir=args.data_dir,
                    lifecycle=args.postgres_qa_lifecycle, run_token=args.postgres_qa_run)
            if args.postgres_qa_run:
                return 2
            return run_postgres_self_test(args.postgres_self_test, data_dir=args.data_dir)
        except SystemExit:
            return 2
        except Exception:
            # Never dump raw SQL/driver exceptions into the real user profile.
            return 1
    try:
        from atom_search.desktop.app import main
        return main()
    except Exception:
        diagnostic = ""
        try:
            folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AtomSearch"
            folder.mkdir(parents=True, exist_ok=True)
            log = folder / "startup-error.log"
            log.write_text(traceback.format_exc(), encoding="utf-8")
            diagnostic = message("startup.details", path=log)
        except OSError:
            pass
        explanation = message("startup.failure", diagnostic=diagnostic)
        headless = any(flag in sys.argv for flag in ('--smoke-test', '--self-test', '--postgres-self-test',
                                                     '--service-test', '--service-attach-test'))
        if not headless and os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, explanation, message("app.name"), 0x10)
        elif sys.stderr is not None:
            print(explanation, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(launch())
