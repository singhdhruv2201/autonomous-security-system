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
