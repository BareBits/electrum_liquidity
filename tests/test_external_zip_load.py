"""Regression test for the zip-install crash.

When Electrum installs the plugin as an external ZIP, it imports the package
under the module name ``electrum_external_plugins.inbound_liquidity`` (not the
internal ``electrum.plugins.inbound_liquidity`` the rest of the suite uses).
Electrum's ``ConfigVar(plugin=...)`` strips the ``electrum.plugins.`` prefix but
NOT the ``electrum_external_plugins.`` one, then asserts the remainder has no
dots -- so passing the raw ``__name__`` used to blow up at import with
``AssertionError`` and crash the app on install. We now pass the bare plugin
name (:data:`inbound_liquidity._PLUGIN_NAME`) instead.

This test imports the plugin under the external name in a SUBPROCESS (a clean
interpreter, so it does not collide with the internal copy the session already
imported) and asserts it loads without raising. Skipped outside the Electrum
venv.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("electrum.simple_config")

_PKG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "inbound_liquidity")

# Loads the plugin package from disk under the EXTERNAL module name, exactly the
# way Electrum's zip loader names it, and imports the qt submodule too (that is
# what triggers the __init__ ConfigVar registrations). Prints a sentinel on
# success; any AssertionError from ConfigVar surfaces as a non-zero exit.
_CHILD = textwrap.dedent(
    """
    import importlib.util, os, sys, types
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pkg_dir = sys.argv[1]
    ext_name = "electrum_external_plugins.inbound_liquidity"
    parent = types.ModuleType("electrum_external_plugins")
    parent.__path__ = [os.path.dirname(pkg_dir)]
    sys.modules["electrum_external_plugins"] = parent
    spec = importlib.util.spec_from_file_location(
        ext_name, os.path.join(pkg_dir, "__init__.py"),
        submodule_search_locations=[pkg_dir])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[ext_name] = mod
    spec.loader.exec_module(mod)
    assert mod._PLUGIN_NAME == "inbound_liquidity", mod._PLUGIN_NAME
    importlib.import_module(ext_name + ".qt")   # exercises the qt entry point too
    print("EXTERNAL_LOAD_OK")
    """
)


def test_plugin_loads_under_external_zip_namespace() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, _PKG_DIR],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"external-namespace load failed (this is the zip-install crash):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    assert "EXTERNAL_LOAD_OK" in proc.stdout, proc.stdout
