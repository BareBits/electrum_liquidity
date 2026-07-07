"""electrum_liquidity regtest test rig.

A self-contained harness that brings up a full local Lightning + submarine-swap
environment on Bitcoin regtest:

  * bitcoind (regtest) mining one block every N seconds,
  * a Fulcrum ElectrumX server,
  * a local-only nostr relay (nostr-rs-relay in Docker),
  * two Electrum wallets -- a client (``electrum_liqtest``) and a swap partner
    (``electrum_liqtest_swap_partner``) advertising LN->onchain swaps at 0.5%
    over the nostr swap extension,
  * two balanced Lightning channels between the two wallets.

See ``run.py`` for the orchestrator and ``README.md`` for usage.
"""
