"""Read-only company directory API. No external company integrations yet."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .directory import Directory


class Employee(BaseModel):
    id: str
    name: str
    email: str
    team_id: str
    title: str
    status: Literal["active", "inactive"]
    is_team_lead: bool


class Team(BaseModel):
    id: str
    name: str
    description: str
    employee_count: int
    lead_employee_id: str | None


class TeamDetail(Team):
    employees: list[Employee]


def create_app(db_path: Path | None = None) -> FastAPI:
    directory = Directory(db_path or Path(os.getenv("HELIO_DB_PATH", "data/helio.sqlite3")))

    @asynccontextmanager
    async def lifespan(app):
        directory.initialize()
        yield

    app = FastAPI(
        title="Helio Company Simulator",
        version="0.1.0",
        description="Fictional company directory: five teams and twenty employees. "
                    "Local development only; all endpoints are read-only.",
        lifespan=lifespan,
    )

    def find_team(team_id):
        team = next((t for t in directory.teams() if t["id"] == team_id), None)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")
        return team

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @app.get("/health", tags=["System"])
    def health():
        with directory.connection() as db:
            db.execute("SELECT COUNT(*) FROM teams").fetchone()
        return {"status": "ok"}

    @app.get("/snapshot", tags=["Directory"])
    def snapshot():
        return directory.snapshot()

    @app.get("/company", tags=["Directory"])
    def company():
        teams = directory.teams()
        return {"id": "helio", "name": "Helio", "synthetic": True,
                "team_count": len(teams),
                "employee_count": sum(t["employee_count"] for t in teams)}

    @app.get("/teams", response_model=list[Team], tags=["Directory"])
    def teams():
        return directory.teams()

    @app.get("/teams/{team_id}", response_model=TeamDetail, tags=["Directory"])
    def team(team_id: str):
        return {**find_team(team_id), "employees": directory.employees(team_id=team_id)}

    @app.get("/employees", response_model=list[Employee], tags=["Directory"])
    def employees(team_id: str | None = None,
                  status: Literal["active", "inactive"] | None = None):
        if team_id is not None:
            find_team(team_id)
        return directory.employees(team_id=team_id, status=status)

    @app.get("/employees/{employee_id}", response_model=Employee, tags=["Directory"])
    def employee(employee_id: str):
        person = next((e for e in directory.employees() if e["id"] == employee_id), None)
        if person is None:
            raise HTTPException(status_code=404, detail="Employee not found")
        return person

    return app


app = create_app()
