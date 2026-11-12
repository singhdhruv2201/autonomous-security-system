import test from "node:test";
import assert from "node:assert/strict";
import {
  securityState,
  monitoringRows,
  agentAccess,
  appFindings,
  graphLayout,
  escapeHTML,
} from "../../src/helio_dashboard/static/model.mjs";
const base = () => ({
  errors: {},
  overview: {
    coverage_complete: true,
    finding_counts: { open: 0 },
    latest_sync_run: { status: "succeeded" },
  },
  findings: { items: [] },
  policies: { items: [{ enabled: true }] },
  inventory: { items: [] },
  runs: { items: [] },
});
test("clear status requires complete evidence and policy coverage", () => {
  assert.equal(securityState(base()).tone, "good");
  for (const state of [
    { ...base(), errors: { findings: "unavailable" } },
    { ...base(), overview: { coverage_complete: false } },
    { ...base(), policies: { items: [] } },
    { ...base(), findings: null },
  ])
    assert.equal(securityState(state).tone, "warn");
  assert.equal(
    securityState({
      ...base(),
      overview: { ...base().overview, latest_sync_run: { status: "running" } },
    }).tone,
    "warn",
  );
});
test("open risks stay visible even with stale evidence", () => {
  const state = base();
  state.overview.finding_counts.open = 2;
  state.overview.coverage_complete = false;
  assert.equal(securityState(state).tone, "danger");
});
test("agent access is derived from its grant, never guessed from inventory presence", () => {
  const agent = { source_id: "agent:research" };
  assert.equal(agentAccess(agent, []).label, "No observed grants");
  assert.equal(
    agentAccess(agent, [
      {
        kind: "AccessGrant",
        principal_id: "agent:research",
        status: "revoked",
      },
    ]).label,
    "Access revoked",
  );
  assert.equal(
    agentAccess(agent, [
      {
        kind: "AccessGrant",
        principal_id: "agent:research",
        status: "active",
        scopes: ["customers:read"],
      },
    ]).label,
    "Access granted",
  );
});
test("app flags are linked through grants rather than applied to every app", () => {
  const app = { source_id: "crm" };
  const inventory = [
    { kind: "AccessGrant", source_id: "grant-1", app_id: "crm" },
  ];
  const findings = [
    { id: "f", status: "open", evidence: { grant_id: "grant-1" } },
    { id: "resolved", status: "resolved", evidence: { grant_id: "grant-1" } },
    { id: "elsewhere", status: "open", evidence: { grant_id: "grant-2" } },
  ];
  assert.deepEqual(
    appFindings(app, inventory, findings).map((x) => x.id),
    ["f"],
  );
});
test("monitoring labels snapshots as observations and uses actual timestamps", () => {
  const state = base();
  state.inventory.items = [
    {
      id: "e",
      source_id: "maya",
      kind: "Employee",
      name: "Maya",
      status: "inactive",
      last_observed_at: 100,
    },
  ];
  state.runs.items = [
    {
      id: "r",
      status: "succeeded",
      finished_at: 110,
      source_results: { directory: "ok", crm: "ok" },
    },
  ];
  const rows = monitoringRows(state);
  assert.equal(rows.find((x) => x.category === "People").at, 100);
  assert.equal(
    rows.find((x) => x.category === "People").mode,
    "Observed state",
  );
  assert.equal(rows.find((x) => x.category === "Sync").at, 110);
  assert.equal(rows.length, 2);
});
test("graph layout is deterministic, bounded, and retains real IDs", () => {
  const nodes = Array.from({ length: 20 }, (_, i) => ({
    id: "e" + i,
    kind: "Employee",
    name: "Person " + i,
  })).concat([{ id: "app", kind: "App" }]);
  const first = graphLayout(nodes);
  assert.deepEqual(first, graphLayout(nodes));
  assert.equal(new Set(first.map((x) => x.id)).size, 21);
  assert.ok(first.every((x) => x.x > 0 && x.x < 2200 && x.y > 0 && Number.isFinite(x.y)));
});
test("untrusted labels are escaped before HTML or SVG insertion", () => {
  assert.equal(
    escapeHTML('<img src=x onerror="bad()">'),
    "&lt;img src=x onerror=&quot;bad()&quot;&gt;",
  );
});
test("a visible open finding survives an unavailable or inconsistent overview", () => {
  const state = base();
  state.overview = null;
  state.findings.items = [{ status: "open" }];
  assert.equal(securityState(state).tone, "danger");
  state.overview = base().overview;
  assert.equal(securityState(state).tone, "danger");
});
test("failed coverage never labels an app connection as current", () => {
  const state = base();
  state.inventory.items = [
    {
      id: "crm",
      source_id: "crm",
      kind: "App",
      name: "Helio CRM",
      source: "crm",
    },
  ];
  state.overview.sources = { crm: { fresh: false, status: "error" } };
  assert.equal(
    monitoringRows(state)[0].title,
    "Helio CRM · connection needs attention",
  );
});

test("zero scoped findings is only verified with fresh, complete evidence", async () => {
  const { findingsVerified } = await import(
    "../../src/helio_dashboard/static/model.mjs"
  );
  assert.equal(findingsVerified(base()), true);
  for (const state of [
    { ...base(), findings: null },
    { ...base(), errors: { connection: "unavailable" } },
    { ...base(), overview: { coverage_complete: false } },
    { ...base(), findings: { items: [], total: 201 } },
    { ...base(), inventory: { items: [], total: 201 } },
  ])
    assert.equal(findingsVerified(state), false);
});
