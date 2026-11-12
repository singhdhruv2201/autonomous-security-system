import pytest
from fastapi.testclient import TestClient

from helio_simulator.api import create_app
from helio_simulator.directory import Directory


@pytest.fixture
def directory(tmp_path):
    directory = Directory(tmp_path / "company.sqlite3")
    directory.initialize()
    return directory


@pytest.fixture
def client(directory):
    with TestClient(create_app(directory.path)) as client:
        yield client


def pytest_addoption(parser):
    parser.addoption(
        "--live", action="store_true", default=False, help="run live API tests"
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        skip_live = pytest.mark.skip(reason="need --live option to run live API tests")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)
