import pytest

from helio_crm.agent import build_summary
from helio_crm.credentials import generate_credentials


def test_credentials_are_random_private_and_not_overwritten(tmp_path):
    path = tmp_path / ".env"
    generate_credentials(path)
    first = path.read_text()
    entries = dict(line.split("=", 1) for line in first.splitlines())
    assert set(entries) == {"CRM_ADMIN_TOKEN", "CRM_MAYA_TOKEN", "CRM_AGENT_TOKEN", "CRM_OBSERVER_TOKEN", "CRM_RECOVERY_TOKEN"}
    assert len(set(entries.values())) == 5
    assert all(len(value) >= 32 for value in entries.values())
    assert path.stat().st_mode & 0o777 == 0o600
    generate_credentials(path)
    assert path.read_text() == first
    other = tmp_path / "other.env"
    generate_credentials(other)
    assert other.read_text() != first


def test_setup_refuses_to_overwrite_an_unrelated_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("EXISTING_SETTING=keep\n")
    with pytest.raises(ValueError, match="existing"):
        generate_credentials(path)
    assert path.read_text() == "EXISTING_SETTING=keep\n"


def test_agent_summary_is_grounded_in_returned_customer_data():
    summary = build_summary([
        {"name": "Northstar", "subscription_status": "active", "plan": "growth"},
        {"name": "Orchard", "subscription_status": "trial", "plan": "starter"},
    ])
    assert summary["customer_count"] == 2
    assert summary["by_status"] == {"active": 1, "trial": 1}
    assert summary["accounts"] == ["Northstar (growth, active)", "Orchard (starter, trial)"]
    assert build_summary([])["customer_count"] == 0


def test_directory_status_change_is_explicit_persistent_and_validated(directory):
    result = directory.set_employee_status("emp-maya-chen", "inactive")
    assert result["status"] == "inactive"
    directory.initialize()
    assert directory.set_employee_status("emp-maya-chen", "inactive") == result
    with pytest.raises(ValueError):
        directory.set_employee_status("emp-maya-chen", "invalid")
    with pytest.raises(KeyError):
        directory.set_employee_status("missing", "inactive")
    assert directory.set_employee_status("emp-maya-chen", "active")["status"] == "active"
