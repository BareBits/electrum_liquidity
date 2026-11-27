"""Apply a batch of Electrum config keys in ONE process.

Run as a script under the rig's Electrum venv:

    .venv-electrum/bin/python -m rig.apply_config --dir <datadir> \
        --wallet <wallet_path> [--regtest] < pairs.json

``pairs.json`` on stdin is ``[[key, value], ...]`` with values as the STRINGS
the command line would have carried, so this is a drop-in for a loop of
``electrum -o setconfig <key> <value>``.

Why this exists
---------------
``setup_wallet`` used to shell out to the Electrum CLI once per key. Each of
those is a full interpreter start plus the whole ``electrum`` import graph --
about 0.8s -- and there are 13 of them for the client and 15 for the partner.
Measured on the rig that was 25s of a 58s bring-up, over 40% of it, spent
re-importing Electrum 28 times to write 28 JSON values. Every one of the ~56
rig launches in the e2e suite paid it.

Why a SUBPROCESS rather than doing this inside the orchestrator
---------------------------------------------------------------
Because selecting the chain is a process-wide mutation.
``chain.set_as_network()`` rebinds ``electrum.constants.net``, a module global
that decides address prefixes, bech32 HRPs and BOLT11 HRPs for everything that
imports it afterwards. The orchestrator is a long-lived process that goes on to
drive the whole rig, and quietly leaving it pinned to regtest -- or to whatever
the last wallet asked for -- is the kind of action-at-a-distance that is
miserable to debug later. One throwaway process per wallet keeps the mutation
where it belongs and still costs 1 interpreter start instead of 15.

Faithfulness
------------
The value handling is Electrum's own, lifted from ``Commands._setconfig``:
``json_decode`` then a best-effort ``ast.literal_eval`` (which is what makes
``"true"`` arrive as a bool rather than a string), and plugin-enabler keys go
through ``config.set_key`` while everything else goes through the ConfigVar.
``test_rig_unit`` pins that this produces a config file byte-identical to the
one the old CLI loop produced.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from typing import Any, List, Tuple


def _normalize(key: str, value: str) -> Any:
    """Electrum's own command-line value handling.

    Mirrors ``electrum.commands.Commands._setconfig_normalize_value``. The
    ``literal_eval`` pass is the one that matters for us: the rig passes
    ``"true"`` / ``"false"`` / ``"5000"`` as strings, and this is what turns
    them into the bools and ints the ConfigVars expect.

    The RPC credential keys are exempted there (a password must survive
    verbatim, even one that happens to look like JSON) and are exempted here
    for the same reason, read off SimpleConfig rather than hardcoded.
    """
    from electrum.simple_config import SimpleConfig
    from electrum.util import json_decode

    if key in (SimpleConfig.RPC_USERNAME.key(), SimpleConfig.RPC_PASSWORD.key()):
        return value
    decoded = json_decode(value)
    try:
        return ast.literal_eval(decoded)
    except Exception:
        return decoded


def apply_pairs(datadir: str, wallet: str, pairs: List[Tuple[str, str]], *,
                regtest: bool = True) -> None:
    """Write every ``(key, value)`` into ``datadir``'s config file."""
    from electrum.plugin import Plugins
    from electrum.simple_config import SimpleConfig

    # The cmdline options the CLI would have been given. They matter beyond
    # locating the file: `set_key` refuses to change a key that was supplied on
    # the command line (`is_modifiable`), so passing these makes a key the real
    # invocation could not have written unwritable here too.
    options = {"electrum_path": datadir, "wallet_path": wallet}
    if regtest:
        options["regtest"] = True
    config = SimpleConfig(options)
    # Same order as run_electrum: build the config, then select the chain.
    config.get_selected_chain().set_as_network()

    for key, raw in pairs:
        value = _normalize(key, raw)
        if Plugins.is_plugin_enabler_config_key(key):
            config.set_key(key, value)
        else:
            config.cv.from_key(key).set(value)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True, help="Electrum data directory")
    ap.add_argument("--wallet", required=True, help="wallet file path")
    ap.add_argument("--regtest", action="store_true", default=False)
    args = ap.parse_args(argv)

    pairs = [(str(k), str(v)) for k, v in json.load(sys.stdin)]
    apply_pairs(args.dir, args.wallet, pairs, regtest=args.regtest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
