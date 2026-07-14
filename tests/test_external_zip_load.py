"""Regression test for the external-ZIP install crash.

Electrum installs this plugin as a ZIP and loads it with a loader that is subtly
different from a normal import (see ``Plugins.maybe_load_plugin_init_method``):

  * the __init__ module is exec'd from a spec whose ``name`` is the bare package
    directory (``"inbound_liquidity"``), so the module's ``__name__`` /
    ``__package__`` are the bare name -- NOT ``electrum_external_plugins.
    inbound_liquidity``;
  * yet the module is registered in ``sys.modules`` under that longer external
    key.

Because Python resolves relative imports against ``__package__``, the plugin's
``from .liquidity_manager import ...`` used to look for a top-level
``inbound_liquidity`` package that is not on ``sys.path`` in a real install,
raising ``ModuleNotFoundError`` and crashing Electrum on install. ``__init__``
now adopts its real ``sys.modules`` key as ``__package__`` to fix this.

This test reproduces Electrum's exact loader naming against a freshly-built ZIP,
in a SUBPROCESS whose ``sys.path`` cannot import the on-disk source (mimicking a
real user, where the plugin exists only inside the ZIP). It fails loudly if the
compatibility shim is ever removed. Skipped outside the Electrum venv.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import zipfile

import pytest

pytest.importorskip("electrum.simple_config")

_PKG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "inbound_liquidity")


def _build_zip(dest_dir: str) -> str:
    """Package inbound_liquidity/ into a zip the way the release workflow does."""
    zip_path = os.path.join(dest_dir, "inbound_liquidity.zip")
    root = os.path.dirname(_PKG_DIR)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(_PKG_DIR):
            if "__pycache__" in dirpath:
                continue
            for fn in filenames:
                if fn.endswith((".pyc",)):
                    continue
                full = os.path.join(dirpath, fn)
                zf.write(full, os.path.relpath(full, root))
    return zip_path


# Reproduces Electrum's zip loader EXACTLY: exec __init__ from a spec named after
# the bare package dir, but registered in sys.modules under the external key.
# Then loads the qt submodule the way load_plugin_by_name does. Run with a
# sys.path that cannot import the on-disk source, so a real user's crash surfaces.
_CHILD = textwrap.dedent(
    """
    import importlib, importlib.util, os, sys, types, zipimport
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    zip_path = sys.argv[1]
    # Make sure the bare 'inbound_liquidity' source cannot be imported from disk.
    sys.path[:] = [p for p in sys.path
                   if not os.path.isdir(os.path.join(p or '.', 'inbound_liquidity'))]
    assert not any(os.path.isdir(os.path.join(p or '.', 'inbound_liquidity')) for p in sys.path)

    imp = zipimport.zipimporter(zip_path)
    base = "electrum_external_plugins.inbound_liquidity"
    init_spec = imp.find_spec("inbound_liquidity")   # spec.name == bare dir, like Electrum
    module = importlib.util.module_from_spec(init_spec)
    sys.modules[base] = module                        # stored under the external key
    init_spec.loader.exec_module(module)              # <-- crashed here before the fix
    importlib.import_module(base + ".qt")             # qt entry point, loaded later by Electrum
    print("EXTERNAL_ZIP_LOAD_OK")
    """
)


def test_plugin_loads_the_way_electrum_loads_an_external_zip(tmp_path) -> None:
    zip_path = _build_zip(str(tmp_path))
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, zip_path],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    assert proc.returncode == 0, (
        "external-zip load failed the way it would crash Electrum on install:\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    assert "EXTERNAL_ZIP_LOAD_OK" in proc.stdout, proc.stdout
