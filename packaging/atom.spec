# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 6.16 onedir release; the model is not extracted at every start."""

import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files, collect_delvewheel_libs_directory
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
    VarStruct, VSVersionInfo,
)


ROOT = Path(SPECPATH).parent
helper_spec = importlib.util.spec_from_file_location("atom_build_support", ROOT / "packaging/build_support.py")
support = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(support)
from atom_search.content import PRODUCT, message
version = support.project_version(ROOT)
identity_module = support.generate_build_identity(ROOT, Path(workpath))
driver_datas, driver_binaries = collect_delvewheel_libs_directory('psycopg_binary')
version_tuple = (*map(int, version.split(".")), 0)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple,
                      mask=0x3F, flags=0, OS=0x40004, fileType=0x1, subtype=0,
                      date=(0, 0)),
    kids=[StringFileInfo([StringTable("041904B0", [
        StringStruct("CompanyName", message("app.publisher")),
        StringStruct("FileDescription", message("app.file_description")),
        StringStruct("FileVersion", version),
        StringStruct("InternalName", "ssearch"),
        StringStruct("OriginalFilename", "ssearch.exe"),
        StringStruct("ProductName", message("app.name")),
        StringStruct("ProductVersion", version),
        StringStruct("LegalCopyright", message("app.copyright")),
        StringStruct("Comments", PRODUCT["support_email"]),
    ])]), VarFileInfo([VarStruct("Translation", [1049, 1200])])],
)

a = Analysis(
    [str(ROOT / "packaging/launcher.py")],
    pathex=[str(identity_module.parent), str(ROOT / "src"), str(ROOT / "packages/atom-search-core/src")],
    binaries=collect_dynamic_libs("onnxruntime") + driver_binaries,
    datas=support.collect_datas(ROOT) + driver_datas + collect_data_files('tzdata'),
    hiddenimports=["numpy", "onnxruntime", "onnxruntime.capi.onnxruntime_pybind11_state",
                   "tokenizers", "tokenizers.tokenizers", "PySide6.QtCore",
                   "PySide6.QtGui", "PySide6.QtWidgets", "psycopg", "psycopg_binary",
                   "psycopg_binary.pq", "psycopg_binary._psycopg", "tzdata",
                   "atom_search.runtime.manifest", "atom_search.runtime.postgres",
                   "atom_search.runtime.credentials", "atom_search.runtime.storage",
                   "_atom_build_identity"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest", "tkinter", "torch", "tensorflow", "transformers", "huggingface_hub"],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="ssearch", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=True,
    version=version_info, icon=str(ROOT / "resources/branding/atom-search.ico"),
    uac_admin=False, contents_directory="_internal",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ssearch")
