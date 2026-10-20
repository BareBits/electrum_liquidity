"""The per-wallet settings layer: what a wallet reads, what a write touches, and
what stays process-wide.

Before this, every setting lived in Electrum's process-wide config, so two
wallets open in one Electrum shared a single liquidity policy. Now a wallet
reads exactly two things: its OWN stored value, or the setting's shipped
default. Nothing arrives from the process-wide config -- these tests pin that
too, because it is the whole point: a value a wallet was never given must not
turn up from elsewhere and start moving its funds.

Glue-level: the real plugin package on a LiquidityPlugin built without
BasePlugin.__init__, against dict-backed fake wallets.
"""
from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict, List, Optional

import pytest


pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.simple_config import SimpleConfig  # type: ignore  # noqa: E402

from electrum.plugins.inbound_liquidity.log_buffer import (  # type: ignore  # noqa: E402
    clamp_max_lines,
)

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    DEFAULT_LIQUIDITY_GOAL_SAT,
    SETTINGS_DB_KEY,
    SETTINGS_STORE_VERSION,
    LiquidityPlugin,
    WalletSettings,
    config_var_default,
)


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeWallet:
    def __init__(self, name: str = "w") -> None:
        self.db = _FakeDB()
        self.saves = 0
        self._name = name

    def basename(self) -> str:
        return self._name

    def save_db(self) -> None:
        self.saves += 1


class _Config:
    """A config stand-in carrying only what a test sets, the way the other glue
    tests do. The settings layer must cope with that: a setting the config does
    not carry reads as the setting's own declared default."""

    def __init__(self, **values: Any) -> None:
        for key, value in values.items():
            setattr(self, key, value)


def _plugin(**config_values: Any) -> LiquidityPlugin:
    """A plugin whose config carries ``config_values``.

    Deliberately does NOT use ``conftest.seed_settings``: that helper exists so
    the other modules can keep configuring a plugin the way they were written,
    by pushing their fake config's values into each wallet's store. This module
    is the one that tests what the production code does with a config, so
    seeding would hide exactly what it is here to pin.
    """
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.settings")
    p.config = _Config(**config_values)
    return p


# --- the two levels -------------------------------------------------------
def test_an_untouched_wallet_reads_the_shipped_default() -> None:
    p, w = _plugin(), _FakeWallet()
    assert p.settings(w).INBOUND_LIQUIDITY_GOAL_SAT == DEFAULT_LIQUIDITY_GOAL_SAT


def test_the_process_wide_config_never_reaches_a_wallet() -> None:
    """`electrum setconfig plugins.inbound_liquidity.<key>` configures nothing.

    The sharp end of the isolation: a wallet that was never given a value reads
    the SHIPPED default, not whatever the installation happens to say. Were it
    otherwise, changing one config key would move every wallet that had not
    overridden it -- including its fee ceiling and whether it is armed.
    """
    p, w = _plugin(INBOUND_LIQUIDITY_GOAL_SAT=750_000,
                   INBOUND_LIQUIDITY_AUTOMATION_ENABLED=True,
                   INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT=99.0), _FakeWallet()
    assert p.settings(w).INBOUND_LIQUIDITY_GOAL_SAT == DEFAULT_LIQUIDITY_GOAL_SAT
    assert p.read_config(w).automation_enabled is False
    assert p.read_config(w).max_swap_fee_pct == config_var_default(
        p.config, "INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT")


def test_a_write_goes_to_the_wallet_and_leaves_the_config_alone() -> None:
    p, w = _plugin(INBOUND_LIQUIDITY_GOAL_SAT=750_000), _FakeWallet()
    p.set_setting(w, "INBOUND_LIQUIDITY_GOAL_SAT", 25_000)
    assert p.settings(w).INBOUND_LIQUIDITY_GOAL_SAT == 25_000
    # The config is not where settings live, in either direction.
    assert p.config.INBOUND_LIQUIDITY_GOAL_SAT == 750_000


def test_a_write_on_one_wallet_does_not_move_another() -> None:
    """The whole point of the feature."""
    p = _plugin(INBOUND_LIQUIDITY_MAX_CHANNELS=2)
    a, b = _FakeWallet("a"), _FakeWallet("b")
    p.set_setting(a, "INBOUND_LIQUIDITY_MAX_CHANNELS", 9)
    assert p.settings(a).INBOUND_LIQUIDITY_MAX_CHANNELS == 9
    assert p.settings(b).INBOUND_LIQUIDITY_MAX_CHANNELS == 2


def test_automation_armed_on_one_wallet_leaves_the_other_disarmed() -> None:
    """The setting where leaking across wallets would actually move funds."""
    p = _plugin()
    a, b = _FakeWallet("a"), _FakeWallet("b")
    p.set_setting(a, "INBOUND_LIQUIDITY_AUTOMATION_ENABLED", True)
    assert p.read_config(a).automation_enabled is True
    assert p.read_config(b).automation_enabled is False


def test_each_wallet_gets_its_own_engine_config() -> None:
    p = _plugin()
    a, b = _FakeWallet("a"), _FakeWallet("b")
    p.set_setting(a, "INBOUND_LIQUIDITY_GOAL_SAT", 400_000)
    p.set_setting(a, "INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT", 2.5)
    p.set_setting(b, "INBOUND_LIQUIDITY_GOAL_SAT", 10_000)
    cfg_a, cfg_b = p.read_config(a), p.read_config(b)
    assert (cfg_a.liquidity_goal_sat, cfg_a.max_swap_fee_pct) == (400_000, 2.5)
    assert cfg_b.liquidity_goal_sat == 10_000
    assert cfg_b.max_swap_fee_pct == config_var_default(
        p.config, "INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT")


# --- persistence ----------------------------------------------------------
def test_a_write_is_persisted_in_the_wallet_file() -> None:
    p, w = _plugin(), _FakeWallet()
    p.set_setting(w, "INBOUND_LIQUIDITY_MAX_CHANNELS", 4)
    stored = w.db.get(SETTINGS_DB_KEY)
    assert stored["MAX_CHANNELS"] == 4
    assert stored["_v"] == SETTINGS_STORE_VERSION
    assert w.saves == 1, "the wallet file must actually be written"


def test_only_the_settings_actually_set_are_stored() -> None:
    """A store full of defaults would freeze this wallet's view of every setting
    at the moment it first touched one, so an untouched setting is left out."""
    p, w = _plugin(), _FakeWallet()
    p.set_setting(w, "INBOUND_LIQUIDITY_MAX_CHANNELS", 4)
    stored = dict(w.db.get(SETTINGS_DB_KEY))
    stored.pop("_v")
    assert list(stored) == ["MAX_CHANNELS"]


def test_stored_settings_survive_a_reload() -> None:
    w = _FakeWallet()
    p = _plugin()
    p.set_setting(w, "INBOUND_LIQUIDITY_GOAL_SAT", 123_000)
    # A fresh plugin over the same wallet file: a restart, as far as this layer
    # is concerned.
    p2 = _plugin()
    assert p2.settings(w).INBOUND_LIQUIDITY_GOAL_SAT == 123_000


def test_an_unknown_key_in_the_store_is_ignored() -> None:
    """A store written by a newer build (or hand-edited) must not be able to
    smuggle an unknown field into a read."""
    p, w = _plugin(), _FakeWallet()
    w.db.put(SETTINGS_DB_KEY, {"_v": 1, "GOAL_SAT": 42, "FROM_THE_FUTURE": 1})
    assert p.settings(w).INBOUND_LIQUIDITY_GOAL_SAT == 42
    assert "FROM_THE_FUTURE" not in p.settings_as_dict(w)


def test_a_corrupt_store_reads_as_no_stored_settings() -> None:
    p, w = _plugin(), _FakeWallet()
    w.db.put(SETTINGS_DB_KEY, "not a dict")
    assert p.settings(w).INBOUND_LIQUIDITY_GOAL_SAT == DEFAULT_LIQUIDITY_GOAL_SAT


def test_a_db_less_wallet_still_answers() -> None:
    """Reads happen on paths that must not fail (a repaint, every tick), so a
    wallet with no store to read is a default, not an exception."""
    p = _plugin()

    class _NoDB:
        pass

    assert p.settings(_NoDB()).INBOUND_LIQUIDITY_GOAL_SAT == DEFAULT_LIQUIDITY_GOAL_SAT


def test_an_unhashable_wallet_still_answers() -> None:
    """Same rule, for an object that cannot be a cache key."""
    from types import SimpleNamespace
    p = _plugin()
    unhashable = SimpleNamespace(db=None)
    with pytest.raises(TypeError):
        {unhashable: 1}                      # the premise of the test
    assert p.settings(unhashable).INBOUND_LIQUIDITY_GOAL_SAT == DEFAULT_LIQUIDITY_GOAL_SAT


# --- validation -----------------------------------------------------------
def test_an_unknown_setting_is_refused() -> None:
    p, w = _plugin(), _FakeWallet()
    with pytest.raises(AttributeError):
        p.set_setting(w, "INBOUND_LIQUIDITY_NO_SUCH_THING", 1)
    with pytest.raises(AttributeError):
        getattr(p.settings(w), "INBOUND_LIQUIDITY_NO_SUCH_THING")


def test_the_prefix_is_optional_when_writing() -> None:
    p, w = _plugin(), _FakeWallet()
    p.set_setting(w, "MAX_CHANNELS", 3)
    assert p.settings(w).INBOUND_LIQUIDITY_MAX_CHANNELS == 3


# --- naming a setting -----------------------------------------------------
# A setting has two names: the SimpleConfig attribute the code reads, and the
# last segment of its config key, which is what a user (and the rig) types.
# `set_setting` used to resolve a name by BUILDING the attribute from it, which
# silently assumed those two always agree. For one setting they do not.
def test_a_setting_is_reachable_by_the_tail_of_its_config_key() -> None:
    # `plugins.inbound_liquidity.liquidity_goal_sat` is declared as the
    # attribute INBOUND_LIQUIDITY_GOAL_SAT -- not ..._LIQUIDITY_GOAL_SAT. The
    # built name therefore did not exist, and setting the liquidity goal by the
    # name it is configured under raised AttributeError.
    p, w = _plugin(), _FakeWallet()
    assert p.set_setting(w, "liquidity_goal_sat", 1_500_000) == 1_500_000
    assert p.settings(w).INBOUND_LIQUIDITY_GOAL_SAT == 1_500_000


def test_both_names_of_a_setting_write_the_same_place() -> None:
    # Not two settings that happen to agree: one setting, one store key.
    p, w = _plugin(), _FakeWallet()
    p.set_setting(w, "liquidity_goal_sat", 1_500_000)
    p.set_setting(w, "goal_sat", 250_000)
    assert p.settings(w).INBOUND_LIQUIDITY_GOAL_SAT == 250_000
    assert [k for k in p.settings_store(w) if "GOAL_SAT" in k] == ["GOAL_SAT"]


@pytest.mark.parametrize("name", [
    "liquidity_goal_sat", "LIQUIDITY_GOAL_SAT",
    "INBOUND_LIQUIDITY_GOAL_SAT", "goal_sat", "  Goal_Sat  ",
])
def test_every_way_of_naming_the_goal_resolves(name) -> None:
    p, w = _plugin(), _FakeWallet()
    assert p.set_setting(w, name, 400_000) == 400_000
    assert p.settings(w).INBOUND_LIQUIDITY_GOAL_SAT == 400_000


def test_every_setting_is_reachable_by_both_of_its_names() -> None:
    """The guard. Walks all 57 settings and writes each one under BOTH names.

    This is the test that would have caught the goal, and that a future rename
    cannot slip past: a setting whose attribute stops matching its config key
    fails here rather than in the e2e suite nine modules later.
    """
    from electrum.plugins.inbound_liquidity import (  # type: ignore
        _plugin_config_vars, _settings_field,
    )
    cvars = _plugin_config_vars()
    assert len(cvars) > 50, f"only {len(cvars)} settings found -- wrong registry?"
    p = _plugin()
    unreachable: List[str] = []
    for attr, var in sorted(cvars.items()):
        tail = str(var.key()).rsplit(".", 1)[-1]
        default = config_var_default(p.config, attr)
        for name in (_settings_field(attr), tail, attr):
            w = _FakeWallet()
            try:
                p.set_setting(w, name, default)
            except AttributeError:
                unreachable.append(f"{attr} not reachable as {name!r}")
                continue
            if _settings_field(attr) not in p.settings_store(w):
                unreachable.append(f"{name!r} wrote something other than {attr}")
    assert unreachable == [], "\n".join(unreachable)


def test_an_unknown_name_is_still_refused_by_either_route() -> None:
    # The second lookup must not turn a typo into a silent write.
    p, w = _plugin(), _FakeWallet()
    for name in ("liquidity_goal", "goal", "liquidity_goal_sats", "no_such_thing"):
        with pytest.raises(AttributeError):
            p.set_setting(w, name, 1)


def test_a_value_is_fitted_to_the_declared_type() -> None:
    p, w = _plugin(), _FakeWallet()
    assert p.set_setting(w, "MAX_CHANNELS", "3") == 3
    assert p.set_setting(w, "MAX_SWAP_FEE_PCT", "1.5") == 1.5
    assert p.set_setting(w, "SINK_ADDRESS", "alice@example.com") == "alice@example.com"


@pytest.mark.parametrize("text,expected", [
    ("true", True), ("false", False), ("yes", True), ("no", False),
    ("on", True), ("off", False), ("1", True), ("0", False),
    ("TRUE", True), (" False ", False),
])
def test_booleans_are_parsed_from_words_not_truthiness(text, expected) -> None:
    """``bool("false")`` is True. Were that the rule, asking the CLI to disarm
    automation would arm it."""
    p, w = _plugin(), _FakeWallet()
    assert p.set_setting(w, "AUTOMATION_ENABLED", text) is expected


def test_an_unparseable_value_is_refused_rather_than_stored() -> None:
    p, w = _plugin(), _FakeWallet()
    with pytest.raises(ValueError):
        p.set_setting(w, "MAX_CHANNELS", "lots")
    with pytest.raises(ValueError):
        p.set_setting(w, "AUTOMATION_ENABLED", "maybe")
    assert w.db.get(SETTINGS_DB_KEY) is None


def test_a_bool_is_not_accepted_as_a_number() -> None:
    """`isinstance(True, int)` is True, which would store a bool in an int
    setting and write `true` where the file should hold a count."""
    p, w = _plugin(), _FakeWallet()
    assert p.set_setting(w, "MAX_CHANNELS", True) == 1
    assert isinstance(p.settings(w).INBOUND_LIQUIDITY_MAX_CHANNELS, int)
    assert not isinstance(p.settings(w).INBOUND_LIQUIDITY_MAX_CHANNELS, bool)


# --- reset ----------------------------------------------------------------
def test_reset_drops_the_wallets_own_values_only() -> None:
    p = _plugin()
    shipped = config_var_default(p.config, "INBOUND_LIQUIDITY_MAX_CHANNELS")
    a, b = _FakeWallet("a"), _FakeWallet("b")
    p.set_setting(a, "INBOUND_LIQUIDITY_MAX_CHANNELS", 1)
    p.set_setting(b, "INBOUND_LIQUIDITY_MAX_CHANNELS", 2)
    p.reset_settings(a)
    assert p.settings(a).INBOUND_LIQUIDITY_MAX_CHANNELS == shipped
    assert p.settings(b).INBOUND_LIQUIDITY_MAX_CHANNELS == 2   # untouched
    assert dict(a.db.get(SETTINGS_DB_KEY)) == {"_v": SETTINGS_STORE_VERSION}


# --- the whole-view accessor ---------------------------------------------
def test_settings_as_dict_reports_every_setting() -> None:
    p, w = _plugin(), _FakeWallet()
    p.set_setting(w, "INBOUND_LIQUIDITY_MAX_CHANNELS", 4)
    view = p.settings_as_dict(w)
    names = {n[len("INBOUND_LIQUIDITY_"):] for n in vars(SimpleConfig)
             if n.startswith("INBOUND_LIQUIDITY_")}
    assert set(view) == names, "every declared setting must be reported"
    assert view["MAX_CHANNELS"] == 4
    assert view["GOAL_SAT"] == DEFAULT_LIQUIDITY_GOAL_SAT


def test_non_plugin_attributes_pass_through_to_the_real_config() -> None:
    """The GUI reads Electrum's own settings (base unit, fees) off the same
    object the tab uses for plugin settings, so the view has to be a drop-in."""
    p, w = _plugin(), _FakeWallet()
    p.config.SOMETHING_OF_ELECTRUMS = "x"
    assert p.settings(w).SOMETHING_OF_ELECTRUMS == "x"
    p.settings(w).SOMETHING_OF_ELECTRUMS = "y"
    assert p.config.SOMETHING_OF_ELECTRUMS == "y", "a write must reach the config"


def test_the_view_is_cached_per_wallet() -> None:
    p, a, b = _plugin(), _FakeWallet("a"), _FakeWallet("b")
    assert p.settings(a) is p.settings(a)
    assert p.settings(a) is not p.settings(b)
    assert isinstance(p.settings(a), WalletSettings)


# --- the settings whose effect cannot be per wallet ----------------------
STOCK = 200_000


@pytest.fixture
def restore_floor():
    """Snapshot every patched MIN_FUNDING_SAT binding (and the captured stock)
    and restore them afterwards: the floor is a module constant per process, so
    lowering it here would otherwise leak into other tests. Mirrors the fixture
    in test_min_funding_floor.py, which pins the same constant."""
    import sys
    import electrum.lnutil as lnutil                     # type: ignore
    import electrum.plugins.inbound_liquidity as IL      # type: ignore
    names = list(IL._MIN_FUNDING_CORE_MODULES) + [
        "electrum.plugins.inbound_liquidity.liquidity_manager",
        "electrum.plugins.inbound_liquidity",
    ]
    saved = {n: getattr(sys.modules[n], "MIN_FUNDING_SAT")
             for n in names
             if sys.modules.get(n) is not None and hasattr(sys.modules[n], "MIN_FUNDING_SAT")}
    saved_stock = IL._stock_min_funding_sat
    IL._stock_min_funding_sat = None
    lnutil.MIN_FUNDING_SAT = STOCK
    yield
    for n, v in saved.items():
        setattr(sys.modules[n], "MIN_FUNDING_SAT", v)
    IL._stock_min_funding_sat = saved_stock


def test_the_funding_floor_takes_the_lowest_across_open_wallets(restore_floor) -> None:
    """``MIN_FUNDING_SAT`` is one constant per process. It is a permission, not a
    policy -- each wallet is still held to its own min-on-chain by the engine --
    so the lowest wins, and one conservative wallet cannot block another's
    opens."""
    p = _plugin()
    a, b = _FakeWallet("a"), _FakeWallet("b")
    p.set_setting(a, "INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT", STOCK - 1000)
    p.set_setting(b, "INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT", STOCK - 50)
    assert p._enforce_min_funding_floor() == STOCK - 1000


def test_the_funding_floor_never_rises_above_stock(restore_floor) -> None:
    p, w = _plugin(), _FakeWallet()
    p.set_setting(w, "INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT", STOCK * 10)
    assert p._enforce_min_funding_floor() == STOCK


def test_log_capture_is_on_if_any_open_wallet_asks_for_it() -> None:
    """One handler set and one ring buffer serve every wallet, so the effective
    capture is the most inclusive answer: otherwise opening a second wallet would
    silently switch off a capture the user turned on to diagnose the first."""
    p = _plugin()
    a, b = _FakeWallet("a"), _FakeWallet("b")
    p.settings(a), p.settings(b)        # both open
    assert p._any_wallet_wants("INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG") is False
    p.set_setting(b, "INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG", True)
    assert p._any_wallet_wants("INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG") is True


def test_the_log_buffer_takes_the_largest_across_open_wallets() -> None:
    p = _plugin()
    a, b = _FakeWallet("a"), _FakeWallet("b")
    p.settings(a), p.settings(b)
    p.set_setting(a, "INBOUND_LIQUIDITY_LOG_BUFFER_LINES", 500)
    p.set_setting(b, "INBOUND_LIQUIDITY_LOG_BUFFER_LINES", 5_000)
    assert p.log_buffer_lines() == 5_000


def test_the_process_wide_settings_fall_back_to_the_shipped_default() -> None:
    """With no wallet open there is nothing to ask, and the shipped default is
    both the only answer available and the right one -- it is what the next
    wallet to open will read unless it has a value of its own."""
    p = _plugin(INBOUND_LIQUIDITY_LOG_BUFFER_LINES=777,
                INBOUND_LIQUIDITY_LOG_CAPTURE_LN=True)
    assert p.log_buffer_lines() == clamp_max_lines(
        config_var_default(p.config, "INBOUND_LIQUIDITY_LOG_BUFFER_LINES"))
    assert p._any_wallet_wants("INBOUND_LIQUIDITY_LOG_CAPTURE_LN") is False


def test_reading_a_setting_does_not_change_a_process_wide_constant(restore_floor) -> None:
    """Deriving the funding floor from the open wallets must be driven by a
    wallet opening or a setting changing -- not by anyone reading a setting,
    which happens on repaints and every tick."""
    import electrum.lnutil as lnutil  # type: ignore
    p, w = _plugin(), _FakeWallet()
    p.set_setting(w, "INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT", STOCK - 1000)
    p._enforce_min_funding_floor()
    lnutil.MIN_FUNDING_SAT = STOCK           # as if something reset it
    p.settings(w).INBOUND_LIQUIDITY_GOAL_SAT       # a plain read
    assert lnutil.MIN_FUNDING_SAT == STOCK
    p.apply_process_wide_settings()                 # the explicit re-derive
    assert lnutil.MIN_FUNDING_SAT == STOCK - 1000


# --- the CLI commands -----------------------------------------------------
def _command(name: str):
    from electrum.commands import Commands  # type: ignore
    return getattr(Commands, f"inbound_liquidity_{name}")


class _Runner:
    """The bit of ``Commands`` that ``plugin_command``'s wrapper touches: it
    reaches the live plugin through the daemon, so driving the real wrapper
    (rather than the bare coroutine) also covers that lookup."""

    def __init__(self, plugin: LiquidityPlugin) -> None:
        from types import SimpleNamespace
        self.daemon = SimpleNamespace(
            _plugins=SimpleNamespace(get_plugin=lambda name: plugin),
            # The wrapper completes a --wallet path before it looks at the
            # wallet it was handed; these tests pass the wallet object, so the
            # path stays None and only has to survive being completed.
            config=SimpleNamespace(maybe_complete_wallet_path=lambda path: path))


def _run(name: str, plugin: LiquidityPlugin, *args, **kwargs):
    import asyncio
    return asyncio.run(_command(name)(_Runner(plugin), *args, **kwargs))


def test_the_cli_commands_are_registered() -> None:
    from electrum.commands import known_commands  # type: ignore
    for name in ("get_settings", "set_setting", "reset_settings"):
        assert f"inbound_liquidity_{name}" in known_commands


def test_registering_twice_is_a_no_op() -> None:
    """``plugin_command`` raises on a duplicate name, and this module is
    imported under two names by the test suite itself."""
    from electrum.plugins.inbound_liquidity import _register_commands  # type: ignore
    _register_commands()
    _register_commands()


def test_the_cli_setter_writes_the_wallet_not_the_config() -> None:
    p = _plugin(INBOUND_LIQUIDITY_MAX_CHANNELS=2)
    a, b = _FakeWallet("a"), _FakeWallet("b")
    _run("set_setting", p, "max_channels", "6", wallet=a)
    assert p.settings(a).INBOUND_LIQUIDITY_MAX_CHANNELS == 6
    assert p.settings(b).INBOUND_LIQUIDITY_MAX_CHANNELS == 2
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNELS == 2


def test_the_cli_getter_reports_the_wallets_effective_settings() -> None:
    p, w = _plugin(), _FakeWallet()
    p.set_setting(w, "GOAL_SAT", 11_000)
    out = _run("get_settings", p, wallet=w)
    assert out["GOAL_SAT"] == 11_000


def test_the_cli_reset_clears_the_wallet() -> None:
    p, w = _plugin(), _FakeWallet()
    p.set_setting(w, "GOAL_SAT", 11_000)
    out = _run("reset_settings", p, wallet=w)
    assert out["GOAL_SAT"] == DEFAULT_LIQUIDITY_GOAL_SAT


# --- the layering stays the layering -------------------------------------
def test_no_module_reads_a_plugin_setting_off_the_shared_config() -> None:
    """A guard, not a behaviour test.

    Every ``INBOUND_LIQUIDITY_*`` read has to go through a wallet's settings
    view. One left on ``self.config`` would compile, pass its own tests, and
    quietly make that single setting shared again -- the exact bug this layer
    exists to fix, reintroduced for one knob and invisible until two wallets
    disagree about it. So the source is scanned instead.

    The settings layer itself is exempt: it is what reads the installation-wide
    level, by design.

    Parsed rather than grepped. This was a line-level regex for
    ``self.config.INBOUND_LIQUIDITY_``, and it could not see the form all three
    of its escapees actually used:

        c = self.config                          # one line
        ...
        getattr(c, "INBOUND_LIQUIDITY_X", ...)   # another

    Three functions read nine settings that way -- including the four deciding
    when the watchdog force-closes a wedged or offline channel, and the sink's
    fee ceiling -- so those nine were pinned to their shipped defaults however
    the user set them, and the guard said nothing. Following the alias needs
    the syntax tree, so that is what it reads now.
    """
    import ast
    import electrum.plugins.inbound_liquidity as pkg  # type: ignore

    root = pathlib.Path(pkg.__file__).parent
    # The functions allowed to reach the shared config: the settings layer and
    # the process-wide settings that have no single wallet to ask.
    allowed = {"config_var_default", "__getattr__", "__setattr__"}
    offenders: List[str] = []

    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name in allowed:
                continue
            # Locals bound to self.config anywhere in this function.
            aliases = {
                target.id
                for node in ast.walk(fn) if isinstance(node, ast.Assign)
                for target in node.targets
                if isinstance(target, ast.Name)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "config"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "self"
            }

            def _is_shared_config(node) -> bool:
                if isinstance(node, ast.Name) and node.id in aliases:
                    return True
                return (isinstance(node, ast.Attribute) and node.attr == "config"
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "self")

            for node in ast.walk(fn):
                # getattr(<shared config>, "INBOUND_LIQUIDITY_X", default)
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                        and len(node.args) >= 2
                        and _is_shared_config(node.args[0])
                        and isinstance(node.args[1], ast.Constant)
                        and str(node.args[1].value).startswith("INBOUND_LIQUIDITY_")):
                    offenders.append(
                        f"{path.name}:{node.lineno}: {fn.name}() reads "
                        f"{node.args[1].value} off the shared config")
                # <shared config>.INBOUND_LIQUIDITY_X
                if (isinstance(node, ast.Attribute)
                        and str(node.attr).startswith("INBOUND_LIQUIDITY_")
                        and _is_shared_config(node.value)):
                    offenders.append(
                        f"{path.name}:{node.lineno}: {fn.name}() reads "
                        f"{node.attr} off the shared config")

    assert not offenders, (
        "these read a per-wallet setting off the process-wide config; route "
        "them through self.settings(wallet) instead:\n  "
        + "\n  ".join(sorted(set(offenders))))


def test_the_layering_guard_catches_the_alias_it_used_to_miss(tmp_path) -> None:
    """The guard's own regression test.

    The previous guard passed while three functions were doing exactly this,
    so the guard needs one of its own: give it a module that launders the
    shared config through a local and check it is not fooled.
    """
    import ast

    source = (
        "class P:\n"
        "    def _params(self, wallet):\n"
        "        c = self.config\n"
        "        return getattr(c, 'INBOUND_LIQUIDITY_MAX_CHANNELS', 2)\n"
    )
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    aliases = {
        t.id for node in ast.walk(fn) if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and isinstance(node.value, ast.Attribute)
        and node.value.attr == "config"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    }
    assert aliases == {"c"}, "the alias of self.config was not followed"


# --- batching -------------------------------------------------------------
def test_a_batch_writes_the_wallet_file_once() -> None:
    """A settings form is two dozen fields; one write per field would be two
    dozen full wallet-file writes for one click of Apply."""
    p, w = _plugin(), _FakeWallet()
    with p.settings_batch(w):
        p.set_setting(w, "MAX_CHANNELS", 3)
        p.set_setting(w, "GOAL_SAT", 50_000)
        p.set_setting(w, "MAX_SWAP_FEE_PCT", 1.25)
    assert w.saves == 1
    stored = dict(w.db.get(SETTINGS_DB_KEY))
    assert (stored["MAX_CHANNELS"], stored["GOAL_SAT"], stored["MAX_SWAP_FEE_PCT"]) \
        == (3, 50_000, 1.25)


def test_values_in_a_batch_are_readable_before_it_closes() -> None:
    """The validation in the Apply handlers reads back what it has just set."""
    p, w = _plugin(), _FakeWallet()
    with p.settings_batch(w):
        p.set_setting(w, "MAX_CHANNELS", 3)
        assert p.settings(w).INBOUND_LIQUIDITY_MAX_CHANNELS == 3
    assert p.settings(w).INBOUND_LIQUIDITY_MAX_CHANNELS == 3


def test_a_batch_that_raises_still_persists_what_it_accepted() -> None:
    """The cache has the values by then, so leaving them unwritten would show
    the user settings the wallet file does not have."""
    p, w = _plugin(), _FakeWallet()
    with pytest.raises(RuntimeError):
        with p.settings_batch(w):
            p.set_setting(w, "MAX_CHANNELS", 3)
            raise RuntimeError("the GUI blew up mid-form")
    assert w.saves == 1
    assert dict(w.db.get(SETTINGS_DB_KEY))["MAX_CHANNELS"] == 3


def test_batches_nest() -> None:
    p, w = _plugin(), _FakeWallet()
    with p.settings_batch(w):
        p.set_setting(w, "MAX_CHANNELS", 3)
        with p.settings_batch(w):
            p.set_setting(w, "GOAL_SAT", 7_000)
        assert w.saves == 0, "the inner block must not flush"
    assert w.saves == 1


def test_a_batch_re_derives_the_process_wide_settings_once(restore_floor) -> None:
    p, w = _plugin(), _FakeWallet()
    calls: List[int] = []
    original = type(p)._on_setting_changed
    try:
        type(p)._on_setting_changed = lambda self, wallet, name: calls.append(1)
        with p.settings_batch(w):
            p.set_setting(w, "MAX_CHANNELS", 3)
            p.set_setting(w, "GOAL_SAT", 50_000)
    finally:
        type(p)._on_setting_changed = original
    assert len(calls) == 1


# --- the settings a watchdog reads ---------------------------------------
# A source guard says no module READS a setting off the shared config. These
# say the right value ARRIVES -- which is the part a user notices. Both are
# needed: the three functions that had this wrong passed every behavioural
# test in the suite, because the test factories seed the wallet store AND set
# the same values on the fake config, so either route answered the same. Here
# the two deliberately disagree, so only the wallet can be the source.
def _plugin_disagreeing(**wallet_values):
    """A plugin whose config says one thing and whose wallet says another."""
    p = _plugin(**{k: v for k, v in _CONFIG_SAYS.items()})
    w = _FakeWallet()
    for name, value in wallet_values.items():
        p.set_setting(w, name, value)
    return p, w


# Values put on the installation-wide config, which must reach nothing.
_CONFIG_SAYS = {
    "INBOUND_LIQUIDITY_STUCK_OPEN_CLOSE_TIMEOUT_MIN": 999,
    "INBOUND_LIQUIDITY_WEDGE_REQUIRED_CHECKS": 99,
    "INBOUND_LIQUIDITY_WEDGE_CHECK_SPREAD_MIN": 999,
    "INBOUND_LIQUIDITY_COOP_BEFORE_FORCE_MIN": 999,
    "INBOUND_LIQUIDITY_OFFLINE_UPTIME_WINDOW_DAYS": 99.0,
    "INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS": 99.0,
    "INBOUND_LIQUIDITY_OFFLINE_AUTOCLOSE_ENABLED": False,
    "INBOUND_LIQUIDITY_OFFLINE_MIN_UPTIME_PCT": 99.0,
    "INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT": 99.0,
}


def test_the_wedge_watchdogs_timers_come_from_the_wallet() -> None:
    # These four decide when a wedged open is force-closed. Read off the
    # shared config -- which nothing writes any more -- they were pinned to
    # their shipped defaults however the user set them.
    p, w = _plugin_disagreeing(
        STUCK_OPEN_CLOSE_TIMEOUT_MIN=2,
        WEDGE_REQUIRED_CHECKS=3,
        WEDGE_CHECK_SPREAD_MIN=1,
        COOP_BEFORE_FORCE_MIN=4,
    )
    params = p._wedge_params(w)
    assert params["close_timeout_sec"] == 2 * 60.0
    assert params["required_strikes"] == 3
    assert params["min_spread_sec"] == 1 * 60.0
    assert params["coop_window_sec"] == 4 * 60.0


def test_the_offline_autoclose_timers_come_from_the_wallet() -> None:
    p, w = _plugin_disagreeing(
        OFFLINE_UPTIME_WINDOW_DAYS=0.5,
        OFFLINE_FORCE_CLOSE_DAYS=0.25,
        OFFLINE_AUTOCLOSE_ENABLED=True,
        OFFLINE_MIN_UPTIME_PCT=10.0,
    )
    params = p._offline_autoclose_params(w)
    assert params["enabled"] is True, \
        "the wallet switched auto-close ON; the config's OFF reached it"
    assert params["window_sec"] == 0.5 * 86400.0
    assert params["force_close_sec"] == 0.25 * 86400.0
    assert params["min_uptime_pct"] == 10.0


def test_the_sink_fee_ceiling_comes_from_the_wallet() -> None:
    # A wallet that lowered its fee ceiling was still paying sink routes up to
    # whatever the shared config said.
    p, w = _plugin_disagreeing(MAX_SWAP_FEE_PCT=0.5)
    budget = p._sink_fee_budget(w, 1_000_000)
    assert budget.fee_msat == int(1_000_000 * 1000 * 0.5 / 100.0)


def test_a_wallet_that_set_none_of_them_gets_the_shipped_defaults() -> None:
    # The other half: with nothing of its own, a wallet reads the SHIPPED
    # default -- still not the config's value.
    from electrum.plugins.inbound_liquidity import (  # type: ignore
        DEFAULT_STUCK_OPEN_CLOSE_TIMEOUT_MIN,
    )
    p = _plugin(**_CONFIG_SAYS)
    w = _FakeWallet()
    assert p._wedge_params(w)["close_timeout_sec"] == \
        DEFAULT_STUCK_OPEN_CLOSE_TIMEOUT_MIN * 60.0


def test_reset_works_as_the_first_call_on_a_plugin() -> None:
    """``reset_settings`` must not need another settings call to have run first.

    ``settings`` and ``set_setting`` both build the cache on their way in, so
    they always found it there; ``reset_settings`` reached for the attribute
    directly and raised AttributeError when it happened to be the first thing
    called -- which the CLI's reset command, and any caller clearing a wallet
    before touching it, can be.
    """
    p, w = _plugin(), _FakeWallet()
    p.reset_settings(w)                     # must not raise
    assert p.settings_store(w) == {}
    assert p.settings(w).INBOUND_LIQUIDITY_GOAL_SAT == DEFAULT_LIQUIDITY_GOAL_SAT


def test_reset_after_a_write_still_clears_the_wallet() -> None:
    p, w = _plugin(), _FakeWallet()
    p.set_setting(w, "INBOUND_LIQUIDITY_GOAL_SAT", 777_000)
    p.reset_settings(w)
    assert p.settings(w).INBOUND_LIQUIDITY_GOAL_SAT == DEFAULT_LIQUIDITY_GOAL_SAT
