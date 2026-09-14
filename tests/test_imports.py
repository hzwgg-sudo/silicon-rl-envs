"""M0 smoke tests: packages must be importable with no EDA tools, Docker,
network access, or API keys required."""

import importlib


def test_top_level_package_importable():
    assert importlib.import_module("silicon_env") is not None


def test_environments_package_importable():
    assert importlib.import_module("silicon_env.environments") is not None


def test_agents_package_importable():
    assert importlib.import_module("silicon_env.agents") is not None
