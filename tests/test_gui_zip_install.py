"""GUI-driven end-to-end test of installing the plugin as an external ZIP.

This drives Electrum's REAL Qt install path -- ``PluginsDialog`` +
``PluginDialog`` -- exactly as a user does: it builds the release zip, opens the
plugin dialog, clicks the **Install** button (which runs ``do_authorize`` ->
``authorize_plugin`` -> ``enable`` -> ``load_plugin``), and asserts the plugin

  * loads (``plugins.get(name)`` is a live instance),
  * is enabled, and
  * appears in the plugins list as **Enabled**.

Two things are stubbed because they are OS/interaction concerns, NOT plugin
correctness:

  * Electrum's third-party-plugin authorization key (a root-owned
    ``/etc/electrum/plugins_key``) -- replaced with an in-memory keypair; and
  * the file-picker / password dialogs.

Everything else -- the real zip loader, the real dialog widgets, the real
authorize/enable/load/show_list code -- runs unmodified. The test runs in a
SUBPROCESS with a clean interpreter (the plugin is loaded only under its external
``electrum_external_plugins.inbound_liquidity`` name, never the internal one the
rest of the suite imports) and a ``sys.path`` that cannot import the on-disk
source, mimicking a real user whose plugin exists only inside the zip.

Uses plain PyQt6 (no pytest-qt), offscreen. Skipped outside the Electrum venv.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import zipfile

import pytest

pytest.importorskip("electrum.simple_config")
pytest.importorskip("PyQt6")

_PKG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "inbound_liquidity")


def _build_zip(dest_dir: str) -> str:
    zip_path = os.path.join(dest_dir, "inbound_liquidity.zip")
    root = os.path.dirname(_PKG_DIR)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirnames, filenames in os.walk(_PKG_DIR):
            if "__pycache__" in dirpath:
                continue
            for fn in filenames:
                if fn.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, fn)
                zf.write(full, os.path.relpath(full, root))
    return zip_path


_CHILD = textwrap.dedent(
    """
    import os, shutil, sys, tempfile
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    zip_path = sys.argv[1]
    # A real user's env: the on-disk plugin source is NOT importable.
    sys.path[:] = [p for p in sys.path
                   if not os.path.isdir(os.path.join(p or '.', 'inbound_liquidity'))]

    from PyQt6.QtWidgets import QApplication, QPushButton
    app = QApplication([])
    from electrum.simple_config import SimpleConfig
    from electrum.plugin import Plugins
    from electrum_ecc import ECPrivkey
    from electrum.gui.qt.plugins_dialog import PluginsDialog, PluginDialog, PluginStatusButton

    name = 'inbound_liquidity'
    dd = tempfile.mkdtemp(prefix='eltest-')
    cfg = SimpleConfig({'electrum_path': dd})
    os.makedirs(os.path.join(dd, 'plugins'), exist_ok=True)
    installed_zip = os.path.join(dd, 'plugins', 'inbound_liquidity.zip')
    shutil.copyfile(zip_path, installed_zip)

    plugins = Plugins(cfg, gui_name='qt')
    # Treat strictly as an external zip plugin (drop any internal/dev copy).
    plugins.internal_plugin_metadata.pop(name, None)
    manifest = plugins.read_manifest(installed_zip)
    plugins.external_plugin_metadata[name] = manifest

    # Stub ONLY Electrum's OS-level auth gate (root-owned keyfile) + password.
    pk = ECPrivkey(bytes(range(1, 33)))
    plugins.get_pubkey_bytes = lambda: (pk.get_public_key_bytes(), b'\\x00' * 32)

    dlg = PluginsDialog(cfg, plugins, gui_object=None)
    dlg.get_plugins_privkey = lambda: pk

    # Drive the REAL install dialog: click its "Install" button.
    pd = PluginDialog(name, manifest, None, dlg)
    install = [b for b in pd.findChildren(QPushButton) if b.text() == 'Install']
    assert len(install) == 1, "Install button not present in PluginDialog"
    install[0].click()   # -> do_authorize -> authorize_plugin -> enable -> load

    assert pd.result() == 1, "install dialog was not accepted (authorize/enable/load failed)"
    p = plugins.get(name)
    assert p is not None, "plugin did not load after install"
    assert p.is_enabled(), "plugin loaded but is not enabled"
    assert type(p).__module__ == 'electrum_external_plugins.inbound_liquidity.qt', type(p).__module__

    # It must appear in the plugins list, shown as Enabled.
    dlg.show_list()
    rows = [b for b in dlg.findChildren(PluginStatusButton) if getattr(b, 'name', None) == name]
    assert rows, "plugin does not appear in the plugins list"
    assert rows[0].text() == 'Enabled', "plugin shown but not Enabled: " + rows[0].text()

    print("GUI_ZIP_INSTALL_OK")
    """
)


def test_install_zip_via_gui_loads_and_shows(tmp_path) -> None:
    zip_path = _build_zip(str(tmp_path))
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, zip_path],
        capture_output=True, text=True, timeout=180, cwd=str(tmp_path))
    assert proc.returncode == 0, (
        "GUI zip-install failed (this is the user-facing 'installs but doesn't "
        f"show' / crash path):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    assert "GUI_ZIP_INSTALL_OK" in proc.stdout, proc.stdout
