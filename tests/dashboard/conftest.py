"""Fixtures for dashboard tests (helpers live in dash_helpers.py)."""

import pytest

from dash_helpers import Dash


@pytest.fixture
def dash(tmp_path):
    d = Dash(tmp_path)
    yield d
    d.store.close()


@pytest.fixture
def client(dash):
    return dash.authed_client()
