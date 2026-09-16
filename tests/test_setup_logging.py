from __future__ import annotations

import importlib.util
import logging
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import load_exo_pool_module

api = load_exo_pool_module("api")

_REPO_ROOT = pathlib.Path(__file__).parent.parent


def _load_real_exo_pool_init():
    """Load the real __init__.py as a proper package, relative imports intact.

    The other test files register custom_components.exo_pool as an empty
    stub in sys.modules so submodules can be exec'd standalone without a
    full HA install. __init__.py's own relative imports need a real
    package (with submodule_search_locations) instead, so it is loaded
    under a private alias that doesn't collide with that stub tree.
    """
    alias = "exo_pool_real_init_under_test"
    if alias in sys.modules:
        return sys.modules[alias]
    pkg_dir = _REPO_ROOT / "custom_components" / "exo_pool"
    spec = importlib.util.spec_from_file_location(
        alias, pkg_dir / "__init__.py", submodule_search_locations=[str(pkg_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


exo_init = _load_real_exo_pool_init()


@pytest.fixture
def entry(secret_entry):
    return secret_entry


async def test_setup_entry_does_not_log_any_secret_value(hass, entry, monkeypatch, caplog):
    monkeypatch.setattr(exo_init, "get_coordinator", AsyncMock(return_value=MagicMock(data=None)))
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())

    with caplog.at_level(logging.DEBUG):
        await exo_init.async_setup_entry(hass, entry)

    log_text = caplog.text
    for secret in (
        "hunter2",
        "auth-tok-abc123",
        "id-tok-abc123",
        "refresh-tok-abc123",
    ):
        assert secret not in log_text
