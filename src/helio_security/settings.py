"""Local service configuration; secret values never appear in repr or errors."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, repr=False)
class Settings:
    operator_token: str
    viewer_token: str
    observer_token: str
    db_path: Path = Path("data/security.sqlite3")
    company_id: str = "helio"
    directory_url: str = "http://127.0.0.1:8000"
    crm_url: str = "http://127.0.0.1:8001"
    graph_uri: str = "bolt://127.0.0.1:7687"
    graph_user: str = "neo4j"
    graph_password: str = ""
    sync_interval: int = 60
    publisher_token: str = ""

    def __post_init__(self):
        tokens = (self.operator_token, self.viewer_token, self.observer_token)
        if self.publisher_token:
            tokens += (self.publisher_token,)
        if any(not isinstance(t, str) or len(t) < 32 for t in tokens) or len(set(tokens)) != len(tokens):
            raise ValueError("Distinct strong platform and observer credentials required; run make setup")
        if self.sync_interval != 0 and not 5 <= self.sync_interval <= 3600:
            raise ValueError("Sync interval must be zero or between 5 and 3600 seconds")

    @classmethod
    def from_environment(cls):
        try:
            return cls(operator_token=os.environ["SECURITY_OPERATOR_TOKEN"],
                viewer_token=os.environ["SECURITY_VIEWER_TOKEN"], observer_token=os.environ["CRM_OBSERVER_TOKEN"],
                db_path=Path(os.getenv("SECURITY_DB_PATH", "data/security.sqlite3")),
                directory_url=os.getenv("HELIO_DIRECTORY_URL", "http://127.0.0.1:8000"),
                crm_url=os.getenv("CRM_URL", "http://127.0.0.1:8001"),
                graph_uri=os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"),
                graph_user=os.getenv("NEO4J_USER", "neo4j"), graph_password=os.environ["NEO4J_PASSWORD"],
                sync_interval=int(os.getenv("SECURITY_SYNC_INTERVAL", "60")),
                publisher_token=os.getenv('CRM_IDENTITY_PUBLISHER_TOKEN',''))
        except (KeyError, ValueError):
            raise ValueError("Invalid security service configuration; check local settings and run make setup") from None
