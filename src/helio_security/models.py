"""Validate complete source snapshots, then allowlist graph metadata."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ID = Annotated[str, Field(min_length=1, max_length=200)]
Text = Annotated[str, Field(max_length=500)]


class Record(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class Named(Record):
    id: ID
    name: Text


class Team(Named):
    description: Text


class Employee(Named):
    email: Text
    team_id: ID
    title: Text
    status: Literal["active", "inactive"]


class Envelope(Record):
    schema_version: Literal[1]
    company_id: Literal["helio"]
    complete: Literal[True]


class DirectoryInventory(Envelope):
    teams: list[Team]
    employees: list[Employee]


class App(Named):
    owner_employee_id: ID


class Resource(Named):
    classification: Literal["internal", "confidential"]


class Account(Named):
    kind: Literal["employee", "agent", "service"]
    owner_employee_id: ID
    role: Literal["user", "admin"]


class Grant(Record):
    id: ID
    principal_id: ID
    owner_employee_id: ID
    resource_id: ID
    app_id: ID
    scopes: list[ID]
    status: Literal["active", "revoked"]
    version: int = Field(ge=1)


class CRMInventory(Envelope):
    app: App
    resources: list[Resource]
    accounts: list[Account]
    grants: list[Grant]


@dataclass
class Snapshot:
    source: str
    nodes: list[dict]
    edges: list[dict]


def indexed(items):
    result = {item.id: item for item in items}
    if len(result) != len(items):
        raise ValueError("Duplicate source ID")
    return result


def normalize(source, payload):
    nodes, edges = [], []

    def node(kind, record, exclude=()):
        nodes.append({"kind": kind, "id": record.id,
                      "properties": record.model_dump(exclude=set(exclude))})

    def edge(kind, start_kind, start_id, end_kind, end_id):
        edges.append({"kind": kind, "start": [start_kind, start_id], "end": [end_kind, end_id]})

    if source == "directory":
        data = DirectoryInventory.model_validate(payload)
        teams = indexed(data.teams)
        indexed(data.employees)
        for team in data.teams:
            node("Team", team)
        for employee in data.employees:
            if employee.team_id not in teams:
                raise ValueError("Unknown team")
            node("Employee", employee)
            edge("MEMBER_OF", "Employee", employee.id, "Team", employee.team_id)
    elif source == "crm":
        data = CRMInventory.model_validate(payload)
        resources, accounts = indexed(data.resources), indexed(data.accounts)
        indexed(data.grants)
        node("App", data.app)
        edge("OWNS", "Employee", data.app.owner_employee_id, "App", data.app.id)
        for resource in data.resources:
            node("DataResource", resource)
            edge("STORES", "App", data.app.id, "DataResource", resource.id)
        kinds = {"employee": "AppAccount", "agent": "Agent", "service": "ServiceAccount"}
        for account in data.accounts:
            kind = kinds[account.kind]
            node(kind, account)
            edge("USES", kind, account.id, "App", data.app.id)
            if account.kind == "employee":
                edge("IDENTITY_OF", kind, account.id, "Employee", account.owner_employee_id)
            else:
                edge("OWNS", "Employee", account.owner_employee_id, kind, account.id)
        for grant in data.grants:
            account = accounts.get(grant.principal_id)
            if (account is None or grant.resource_id not in resources or grant.app_id != data.app.id
                    or grant.owner_employee_id != account.owner_employee_id):
                raise ValueError("Invalid grant reference")
            node("AccessGrant", grant)
            edge("HAS_GRANT", kinds[account.kind], account.id, "AccessGrant", grant.id)
            edge("ON", "AccessGrant", grant.id, "DataResource", grant.resource_id)
    else:
        raise ValueError("Unknown source")
    return Snapshot(source, nodes, edges)
