"""PyInstaller runtime hook — make google-re2's vendored DLLs loadable when frozen.

The google-re2 wheel is delvewheel-repaired: its ``_re2*.pyd`` PE-imports
``msvcp140-<hash>.dll``, which ships only in the sibling ``google_re2.libs/``
directory. On an unfrozen install ``re2/__init__.py``'s delvewheel patch adds that
directory with ``os.add_dll_directory`` before importing ``_re2``. Frozen, that
patch computes the path from ``__file__`` and can miss, so ``import re2`` fails with
"DLL load failed" on a clean machine ([8-F]/[11] ship-blocker).

The spec bundles ``google_re2.libs/`` into the onefile; this hook runs before any
import and adds it to the DLL search path straight from ``sys._MEIPASS`` — no
``__file__`` arithmetic — so ``_re2.pyd`` finds ``msvcp140-<hash>.dll`` from the
bundle alone. A no-op off Windows / when the directory is absent.
"""

import os
import sys

_meipass = getattr(sys, "_MEIPASS", None)
if _meipass and hasattr(os, "add_dll_directory"):
    _libs = os.path.join(_meipass, "google_re2.libs")
    if os.path.isdir(_libs):
        os.add_dll_directory(_libs)
