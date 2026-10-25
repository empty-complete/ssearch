"""Desktop entry point; smoke mode is suitable for a frozen offline package."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import sys
import re

from atom_search.content import message


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=message("app.title"))
    parser.add_argument("--root", help=message("settings.root"))
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--no-auto-scan", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--self-test", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--service-test", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--service-test-scenario", choices=('two-clients', 'multi-source', 'health-offline', 'project-settings', 'project-context', 'project-retrieval', 'project-golden'),
                        default='two-clients', help=argparse.SUPPRESS)
    parser.add_argument("--service-attach-test", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.service_test:
        if args.data_dir is None:
            return 1
        from atom_search.diagnostics import (run_service_self_test, run_multi_source_service_test,
                                            run_health_offline_service_test, run_project_settings_service_test,
                                            run_project_context_service_test, run_project_retrieval_service_test,
                                            run_project_search_service_test)
        operation = {'two-clients': run_service_self_test, 'multi-source': run_multi_source_service_test,
                     'health-offline': run_health_offline_service_test,
                     'project-settings': run_project_settings_service_test,
                     'project-context': run_project_context_service_test,
                     'project-retrieval': run_project_retrieval_service_test,
                     'project-golden': run_project_search_service_test}[args.service_test_scenario]
        return operation(args.service_test, data_dir=args.data_dir, model_dir=args.model_dir)
    if args.service_attach_test:
        if args.data_dir is None:
            return 1
        from atom_search.diagnostics import run_service_attach_test
        return run_service_attach_test(args.service_attach_test, data_dir=args.data_dir, model_dir=args.model_dir)
    if args.self_test:
        from atom_search.diagnostics import run_self_test
        return run_self_test(args.self_test, data_dir=args.data_dir, model_dir=args.model_dir)
    if args.smoke_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont, QFontDatabase, QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from atom_search.service.owner import open_service_owner
    from atom_search.paths import model_path, resource_path
    from atom_search.service.contracts import ServiceError
    from atom_search.runtime.storage import StorageBudgetError
    from atom_search.runtime.postgres import PostgresRuntimeError
    from atom_search.runtime._windows import WindowsError as RuntimeWindowsError
    from .data import DataMigrationError, initialize_data_directory
    from .window import SearchWindow

    app = QApplication.instance() or QApplication([])
    app.setApplicationName(message("app.name"))
    app.setOrganizationName(message("app.organization"))
    app.setWindowIcon(QIcon(str(resource_path("branding", "atom-search.ico"))))
    # Qt's Windows offscreen backend may omit system-font discovery. Use the
    # same installed Segoe family explicitly so frozen QA captures real glyphs.
    if os.name == "nt" and not QFontDatabase.families():
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        for filename in ("segoeui.ttf", "segoeuib.ttf"):
            QFontDatabase.addApplicationFont(str(fonts / filename))
    app.setFont(QFont("Segoe UI", 10))
    model = args.model_dir or model_path()
    try:
        folder = initialize_data_directory(args.data_dir, model_dir=model)
    except DataMigrationError as error:
        if args.smoke_test:
            print(str(error), file=sys.stderr)
        else:
            QMessageBox.critical(None, message("migration.title"), str(error))
        return 1
    try:
        owner = open_service_owner(folder, model, discover_legacy=args.data_dir is None,
                                   automatic_catalog=not (args.no_auto_scan or args.smoke_test))
        window = SearchWindow(data_dir=folder,
                              service_factory=owner.desktop_view, service_owner=owner,
                              root=args.root, auto_scan=not (args.no_auto_scan or args.smoke_test),
                              model_dir=model)
    except ServiceError as error:
        if args.smoke_test:
            print(error.message, file=sys.stderr)
        else:
            QMessageBox.critical(None, message('settings.title'), error.message)
        return 1
    except (StorageBudgetError, PostgresRuntimeError, RuntimeWindowsError, OSError) as error:
        # Keep fixed public machine codes for diagnostics, never raw OS text
        # (which can contain private paths or other caller-controlled values).
        code = 'filesystem_error' if isinstance(error, OSError) else str(error)
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,127}', code):
            code = 'settings_preparation_failed'
        logging.getLogger(__name__).warning('settings_preparation_failed', extra={
            'error_code': code, 'error_type': type(error).__name__})
        text = message('settings.preparation_failed')
        if args.smoke_test:
            print(text, file=sys.stderr)
        else:
            QMessageBox.critical(None, message('settings.title'), text)
        return 1
    window.show()
    if args.smoke_test:
        def finish():
            if args.screenshot:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                window.grab().save(str(args.screenshot))
            window.close()
            app.quit()
        QTimer.singleShot(900, finish)
    result = app.exec()
    window.workers.shutdown()
    clean = window.workers.wait()
    return result if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
