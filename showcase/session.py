"""An isolated Helio environment and its guided scenario runs."""
import asyncio
from dataclasses import asdict, dataclass
from importlib.resources import files
import json
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver

from helio_security.agent_worker import AgentWorker
from helio_security.cases import CaseStore
from helio_security.investigator import ModelInvestigator, scripted_fallback
from helio_security.platform_store import PlatformStore
from helio_security.rules import RULE_ID
from helio_security.worker import ObservationWorker
from helio_security.workflow import WorkflowDeps

from .projection import EXTERNAL_RULE, PUBLIC_RULE, Projection
from .services import HelioServices

COMPANY = "helio"
RENEWAL = "app-renewal-tracker"
ENRICHMENT = "agent-lead-enrichment"


@dataclass(frozen=True)
class Scenario:
    title: str
    rule: str
    target: str
    group: str
    description: str
    event: str


SCENARIOS = {
    "offboarding": Scenario(
        "Orphaned agent", RULE_ID, "agent:sales-research", "offboarding",
        "Offboard Maya Chen and follow the CRM access her Sales Research Agent retains.",
        "Directory marked Maya Chen inactive",
    ),
    "public": Scenario(
        "Public internal app", PUBLIC_RULE, RENEWAL, "public",
        "Publish Renewal Tracker and review the exposure with its owner.",
        "Workbench published Renewal Tracker",
    ),
    "public_view": Scenario(
        "Anonymous app access", PUBLIC_RULE, RENEWAL, "public",
        "Publish Renewal Tracker and open it anonymously to trigger immediate containment.",
        "Workbench recorded an anonymous view of Renewal Tracker",
    ),
    "external": Scenario(
        "Unapproved destination", EXTERNAL_RULE, ENRICHMENT, "external",
        "Send synthetic CRM records to a local receiver representing an unapproved destination.",
        "Workbench ran Lead Enrichment against the local receiver",
    ),
}
RECORDINGS = json.loads(files("showcase").joinpath("recordings.json").read_text())


@dataclass(frozen=True)
class ModelConfig:
    name: str
    api_key: str
    ledger: object


class Session:
    def __init__(self, gateway, model=None, *, make_notifier=None):
        self.id = uuid4().hex
        self.created_at = time.time()
        self.directory = tempfile.TemporaryDirectory(prefix="awasi-showcase-")
        self.root = Path(self.directory.name)
        self.model = model
        self.idle = asyncio.Event()
        self.idle.set()
        self.started = set()
        self.samples = []
        self.events = []
        self.projection = Projection()
        self.services = HelioServices(gateway, self.root)
        self.platform = PlatformStore(self.root / "security.sqlite3")
        self.cases = CaseStore(self.platform.path)
        self.platform.initialize()
        self.cases.initialize()
        self.notifier = make_notifier(self.cases, recording=self.recording_for_case) if make_notifier else None
        urls, tokens = self.services.urls, self.services.tokens
        self.observer = ObservationWorker(
            self.platform, self.projection, self.projection, urls["directory"], urls["crm"], tokens["observer"],
            workbench_url=urls["workbench"], workbench_token=tokens["observer"], interval=0,
        )

    @property
    def busy(self):
        return not self.idle.is_set()

    @busy.setter
    def busy(self, value):
        if value:
            self.idle.clear()
        else:
            self.idle.set()

    def close(self):
        self.services.close()
        self.directory.cleanup()

    def initialize(self):
        self.log("sandbox", "Seeded a fresh Helio directory, CRM and Workbench")
        self.observe("Baseline")

    def log(self, source, message):
        self.events.append({"source": source, "message": message, "at": time.time()})

    def observe(self, label, detail=None):
        self.platform.enqueue(COMPANY, "showcase", "manual", time.time())
        self.observer.run_once()
        graph = self.projection.graph()
        outside = self.services.request("outside", "GET", "/received")
        self.samples.append({
            "label": label, "detail": detail, "at": time.time(),
            "open_findings": self.platform.findings(COMPANY, status="open")["total"],
            "actions_verified": sum(
                bool(action["verified_at"]) for row in self.cases.list_cases(COMPANY)["items"]
                for action in self.cases.get_actions(row["id"])
            ),
            "anonymous_views": sum(node.get("anonymous_views_1h", 0) for node in graph.nodes.values()),
            "records_delivered": outside["total_records"],
        })

    def mutate(self, scenario_id):
        services = self.services
        if scenario_id == "offboarding":
            services.directory.set_employee_status("emp-maya-chen", "inactive")
        elif scenario_id in {"public", "public_view"}:
            app = services.workbench.get_app_by_id(RENEWAL)
            services.request(
                "workbench", "PATCH", f"/apps/{RENEWAL}", token="actions",
                body={"expected_version": app["version"], "visibility": "public"},
            )
            if scenario_id == "public_view":
                services.request("workbench", "GET", "/public/renewals")
        else:
            services.request(
                "workbench", "POST", f"/agents/{ENRICHMENT}/destinations", token="maya",
                body={"host": "127.0.0.1", "url": services.urls["outside"] + "/collect"},
            )
            services.request("workbench", "POST", f"/agents/{ENRICHMENT}/run", token="maya")
        self.log("trigger", SCENARIOS[scenario_id].event)

    async def run_worker(self):
        from helio_security.playbooks import REGISTRY

        urls, tokens = self.services.urls, self.services.tokens
        with SqliteSaver.from_conn_string(str(self.root / "checkpoints.sqlite3")) as saver:
            deps = WorkflowDeps(
                store=self.cases, checkpointer=saver, notifier=self.notifier, investigator=self.investigate,
                playbooks=REGISTRY, platform_store=self.platform,
                crm_url=urls["crm"], action_token=tokens["actions"], observer_token=tokens["observer"],
                workbench_url=urls["workbench"], workbench_action_token=tokens["actions"],
                workbench_observer_token=tokens["observer"],
            )
            await AgentWorker(self.platform, self.cases, deps).run_once()

    async def investigate(self, finding_id, facts, case_id=None):
        if self.model is None:
            assessment = scripted_fallback(finding_id, facts)
            assessment["fallback_reason"] = "live_model_disabled"
            self.cases.save_investigation(case_id, assessment, source="fallback")
            return assessment

        @tool
        def get_finding(finding_id: str) -> dict:
            """Read the current finding, including observed source evidence."""
            return self.platform.finding(COMPANY, finding_id) or {"found": False}

        @tool
        def get_evidence(finding_id: str) -> dict:
            """Read the associated ownership and access path from observed Helio data."""
            finding = self.platform.finding(COMPANY, finding_id)
            if not finding:
                return {"found": False}
            evidence = finding["evidence"]
            _, graph = self.projection.graph().focus(finding["rule"], evidence.get("app_id") or evidence["agent_id"])
            return {
                "finding_id": finding_id,
                "entities": [{**node, "id": node["source_id"]} for node in graph["nodes"]],
                "relationships": graph["edges"],
            }

        investigator = ModelInvestigator(
            tools=[get_finding, get_evidence], store=self.cases,
            model_name=self.model.name, api_key=self.model.api_key,
            ledger=self.model.ledger, timeout_seconds=25,
        )
        return await investigator(finding_id, facts, case_id=case_id)

    def recording_for_case(self, case_id):
        case = self.cases.get_case(case_id)
        finding = self.platform.finding(COMPANY, case["finding_id"]) if case else None
        scenario = next((key for key in self.started if finding and SCENARIOS[key].rule == finding["rule"]), None)
        return RECORDINGS["scenarios"].get(scenario)

    def slack_delivery(self, case_id):
        if self.notifier is None:
            return "disabled"
        root = self.notifier.slack_store.root(case_id)
        if root is None:
            return "waiting"
        if root["ts"]:
            return "sent"
        return "unknown" if root["status"] == "unknown" else "pending"

    def view(self):
        graph = self.projection.graph()
        findings = self.platform.findings(COMPANY)["items"]
        cases = [self.cases.get_case_full(row["id"]) for row in self.cases.list_cases(COMPANY)["items"]]
        for case in cases:
            case["slack_delivery"] = self.slack_delivery(case["id"])
        runs = self.platform.runs(COMPANY)["items"]
        scenarios = {}
        for key, scenario in SCENARIOS.items():
            evidence, focus = graph.focus(scenario.rule, scenario.target)
            scenarios[key] = {
                **asdict(scenario), "evidence": evidence, "graph": focus,
                "triggered": key in self.started,
                "available": not any(SCENARIOS[item].group == scenario.group for item in self.started),
                "recording": RECORDINGS["scenarios"].get(key),
            }
        return {
            "id": self.id, "created_at": self.created_at, "busy": self.busy,
            "model": self.model.name if self.model else None,
            "inventory": {
                "employees": sum(node["kind"] == "Employee" for node in graph.nodes.values()),
                "entities": len(graph.nodes), "relationships": len(graph.edges),
            },
            "metrics": {key: value for key, value in self.samples[-1].items() if key not in {"label", "detail", "at"}},
            "samples": self.samples, "events": self.events, "runs": runs,
            "workbench_events": self.services.workbench.list_events()["items"],
            "sources": graph.sources, "scenarios": scenarios, "findings": findings, "cases": cases,
        }
