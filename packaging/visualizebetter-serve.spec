# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — bundle `visualizebetter serve` as a standalone sidecar ([8-F]/TASK 12).

Run from the repo root, after `npm --prefix frontend run build`:

    pyinstaller packaging/visualizebetter-serve.spec

Produces `dist/visualizebetter[.exe]`. CI renames it to the Tauri externalBin convention
`src-tauri/binaries/visualizebetter-<target-triple>[.exe]`.

★ Bundling caveats the plan flags:
  - google-re2 ([6] `matches`) is a C extension — its .pyd/.so + any bundled
    libre2/abseil dynamic libs must be collected, or `import re2` fails at runtime.
  - lark ([6] parser) ships its grammar as package data.
  - fastmcp / uvicorn / starlette / aiosqlite pull submodules dynamically, so they
    need explicit hidden imports.
  - the built SPA (frontend/dist) is bundled so serve's StaticFiles can host it
    ([9-A]); server.frontend_dist() resolves it under sys._MEIPASS when frozen.
"""

import importlib.util
import os

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


def _re2_vendored_libs():
    """delvewheel-vendored google-re2 runtime DLLs, bundled under ``google_re2.libs/``.

    ★ Ship-blocker fix ([8-F]/[11]): the google-re2 wheel is delvewheel-repaired, so
    ``_re2*.pyd`` PE-imports ``msvcp140-<hash>.dll`` which lives *only* in the sibling
    ``google_re2.libs/`` directory (never inside the importable ``re2/`` package).
    ``collect_dynamic_libs("re2")`` therefore returns ``[]`` and the frozen exe fails
    ``import re2`` with "DLL load failed" on a clean machine — the sidecar crashes at
    start, serve.json is never written, and the desktop shell loads forever.

    We collect that directory under the same relative name the delvewheel patch (and
    rthook_re2.py) look for, so the vendored DLLs load from the bundle alone. Located
    via importlib so the path is correct on any machine / CI runner, not hard-coded.
    """
    spec = importlib.util.find_spec("re2")
    if spec is None or not spec.origin:
        return []
    libs_dir = os.path.join(os.path.dirname(os.path.dirname(spec.origin)), "google_re2.libs")
    if not os.path.isdir(libs_dir):
        return []
    return [
        (os.path.join(libs_dir, name), "google_re2.libs")
        for name in os.listdir(libs_dir)
        if name.lower().endswith(".dll")
    ]

# SPECPATH is packaging/ ; the repo root is its parent. Anchor every path there so
# the spec works regardless of the working directory PyInstaller is invoked from.
_ROOT = os.path.dirname(SPECPATH)  # noqa: F821 (SPECPATH is injected by PyInstaller)
_ENTRY = os.path.join(SPECPATH, "_sidecar_entry.py")  # noqa: F821

hidden_imports = (
    collect_submodules("fastmcp")
    + collect_submodules("mcp")
    + collect_submodules("uvicorn")
    + collect_submodules("starlette")
    + collect_submodules("aiosqlite")
    + collect_submodules("lark")
    + collect_submodules("re2")
    + ["visualizebetter.cli", "visualizebetter.server", "visualizebetter.stdio_proxy"]
)

datas = (
    [
        (os.path.join(_ROOT, "frontend", "dist"), "frontend/dist"),
        # The licences of everything bundled below travel with the binary — MIT
        # and BSD want the copyright line, MPL-2.0 (certifi) wants a source
        # pointer. Shipping the exe without this file cannot satisfy them.
        (os.path.join(_ROOT, "THIRD_PARTY_NOTICES.txt"), "."),
        (os.path.join(_ROOT, "LICENSE"), "."),
    ]
    + collect_data_files("lark")
    + collect_data_files("fastmcp")
    + collect_data_files("mcp")
    # fastmcp reads its own version via importlib.metadata at import time, so its
    # dist-info metadata must ship too (both the full and -slim distributions).
    + copy_metadata("fastmcp", recursive=True)
    + copy_metadata("fastmcp-slim")
    + copy_metadata("mcp", recursive=True)
)

# google-re2's compiled extension + its delvewheel-vendored runtime DLLs. The
# extension itself is pulled in by collect_submodules("re2") + Analysis; the vendored
# msvcp140-<hash>.dll comes from google_re2.libs/ (see _re2_vendored_libs).
binaries = collect_dynamic_libs("re2") + _re2_vendored_libs()

a = Analysis(
    [_ENTRY],
    pathex=[_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    # Runs before any import: adds _MEIPASS/google_re2.libs to the DLL search path
    # so `import re2` finds its vendored DLLs in the frozen exe ([8-F]/[11]).
    runtime_hooks=[os.path.join(SPECPATH, "rthook_re2.py")],  # noqa: F821
    excludes=["tkinter", "matplotlib", "PIL", "pytest", "hypothesis"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="visualizebetter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # serve logs to stdout; the shell spawns it detached
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
