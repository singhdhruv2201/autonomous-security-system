import {
  escapeHTML as h,
  entities,
  findings,
  findingsVerified,
  initials,
  securityState,
  agentAccess,
  appFindings,
  monitoringRows,
  graphLayout,
  graphSize,
  personAccounts,
  GRAPH_LANES,
  riskPath,
  findingCases,
  formatTimestamp as stamp,
} from "./model.mjs";
import {renderGraphNode, graphEdgePath} from "./graph.mjs";
import { renderActivity } from "./activity.mjs";
import { renderCase, renderRiskPath, renderWorkflowCatalog } from "./cases.mjs";

const paths = {
  shield: "M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3Z",
  check: "m6 12 4 4 8-8",
  arrow: "M5 12h14m-5-5 5 5-5 5",
  chevron: "m9 5 7 7-7 7",
  refresh: "M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5M4 16a8 8 0 0 0 14 3l3-3m0 5v-5h-5",
  people:
    "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.9M12 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Zm5-3.9a4 4 0 0 1 0 7.8",
  agent:
    "M12 3v3m-6 1h12a2 2 0 0 1 2 2v10H4V9a2 2 0 0 1 2-2ZM1 11v5m22-5v5M8 11v2m8-2v2m-8 3h8",
  apps: "M3 3h7v7H3V3Zm11 0h7v7h-7V3ZM3 14h7v7H3v-7Zm11 0h7v7h-7v-7Z",
  graph:
    "M12 9V4M9 14l-5 4m11-4 5 4M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM14 2a2 2 0 1 1-4 0m-5 19a2 2 0 1 1-4 0m22 0a2 2 0 1 1-4 0",
  activity: "M2 12h4l3-8 5 16 3-8h5",
  flag: "M5 21V3m0 1c4-4 10 4 14 0v10c-4 4-10-4-14 0",
  clock: "M12 8v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z",
  database:
    "M20 6c0 2-4 3-8 3S4 8 4 6s4-3 8-3 8 1 8 3Zm0 0v12c0 2-4 3-8 3s-8-1-8-3V6m0 6c0 2 4 3 8 3s8-1 8-3",
  close: "m6 6 12 12M6 18 18 6",
  search: "M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Zm-2 5 7 7",
  info: "M12 11v6m0-10v.1M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z",
  policy: "M5 3h14v18H5V3Zm4 5h6m-6 4h6m-6 4h4",
  link: "m9 15 6-6m-5-4 2-2a5 5 0 0 1 7 7l-2 2M7 12l-2 2a5 5 0 0 0 7 7l2-2",
  warning: "m12 3 10 18H2L12 3Zm0 6v5m0 3v.1",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Zm13 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z",
  plus: "M12 5v14M5 12h14",
  minus: "M5 12h14",
  fit: "M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5",
};
const icon = (name, size = 18) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] ?? paths.apps}"/></svg>`;
const kindIcon = {
  Employee: "people",
  Team: "people",
  AppAccount: "people",
  Agent: "agent",
  ServiceAccount: "shield",
  App: "apps",
  DataResource: "database",
  AccessGrant: "link",
  Connection: "link",
  Destination: "arrow",
  Audience: "people",
};
const tabs = [
  ["dashboard", "Dashboard"],
  ["graph", "Company graph"],
  ["agents", "Agents"],
  ["apps", "Apps"],
  ["policies", "Policies"],
  ["findings", "Findings"],
  ["activity", "Activity"],
];
let state = null,
  tab = "dashboard",
  monitorFilter = "All",
  monitorLimit = 7,
  findingFilter = "open",
  findingSearch = "",
  graphFilter = "All",
  focusedNode = null,
  focusedFinding = null,
  zoom = 1,
  pan = { x: 0, y: 0 },
  pending = false,
  loading = false,
  drawer = null,
  drawerReturn = null;
let activityData = null, activityHealth = null, activityError = false,
  activityLoading = false, activityFilter = '', activityActor = '', activityVersion = 0;

async function loadActivity(more = false) {
  const version = ++activityVersion;
  activityLoading = true;
  render();
  const query = new URLSearchParams({limit:'25'});
  if (activityFilter) query.set('outcome', activityFilter);
  if (activityActor) query.set('actor_id', activityActor);
  if (more && activityData?.next_before) {
    query.set('before', activityData.next_before);
    query.set('snapshot', activityData.snapshot);
  }
  try {
    const responses = await Promise.all([fetch('/api/activity?'+query,{cache:'no-store'}),fetch('/api/activity/health',{cache:'no-store'})]);
    if (responses.some(r => !r.ok)) throw Error('unavailable');
    const [page, health] = await Promise.all(responses.map(r => r.json()));
    if (!Array.isArray(page.items) || !Array.isArray(health.sources)) throw Error('invalid');
    if (version !== activityVersion) return;
    activityData = more ? {...page,items:[...(activityData?.items ?? []),...page.items]} : page;
    activityHealth = health;
    activityError = false;
  } catch {
    if (version === activityVersion) activityError = true;
  } finally {
    if (version === activityVersion) {
      activityLoading = false;
      render();
    }
  }
}
const main = document.querySelector("#main"),
  nav = document.querySelector("#navigation");
const num = (value) => (value == null ? "—" : Number(value).toLocaleString());
const relative = (at) => {
  if (!at) return "Not yet";
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - at));
  return sec < 60
    ? "Just now"
    : sec < 3600
      ? `${Math.floor(sec / 60)}m ago`
      : sec < 86400
        ? `${Math.floor(sec / 3600)}h ago`
        : `${Math.floor(sec / 86400)}d ago`;
};
const badge = (text, tone = "neutral", dot = false) =>
  `<span class="badge ${tone}">${dot ? "<i></i>" : ""}${h(text)}</span>`;
const avatar = (name, kind = "Employee", large = false) =>
  `<span class="avatar ${kind.toLowerCase()} ${large ? "large" : ""}">${["Employee", "Team", "AppAccount"].includes(kind) ? h(initials(name)) : icon(kindIcon[kind] ?? "shield", large ? 26 : 19)}</span>`;
const btn = (text, action, extra = "", cls = "button") =>
  `<button class="${cls}" data-action="${action}" ${extra}>${text}</button>`;
const nameOf = (id) =>
  entities(state).find((n) => n.source_id === id)?.name ?? id ?? "Unknown";
const actionLink = (text, page) =>
  btn(
    `${text} ${icon("arrow", 15)}`,
    "navigate",
    `data-page="${page}"`,
    "text-button",
  );
const empty = (symbol, title, description, action = "") =>
  `<div class="empty-state"><span class="empty-icon">${icon(symbol, 26)}</span><h3>${h(title)}</h3><p>${h(description)}</p>${action}</div>`;
function pageHeader(title, description, eyebrow = "SECURITY WORKSPACE") {
  return `<section class="page-heading"><div><div class="eyebrow">HELIO SYSTEMS <span>/</span> ${h(eyebrow)}</div><h1>${title}</h1><p>${h(description)}</p></div><div class="page-tools"><span class="updated">${icon("clock", 14)} ${relative(state?.overview?.latest_sync_run?.finished_at)}</span>${btn(`${pending ? '<span class="spinner small"></span>' : icon("refresh", 16)} ${pending ? "Observing…" : "Refresh observations"}`, "refresh", pending ? 'disabled aria-busy="true"' : "")}</div></section>`;
}
function connectorRows() {
  const sources = state?.connectors?.sources ?? state?.overview?.sources ?? {};
  return ["directory", "crm"]
    .map((key, i) => {
      const s = sources[key];
      return `<div class="source-row"><span class="source-icon ${i ? "crm-icon" : ""}">${icon(i ? "apps" : "people", 19)}</span><div><strong>${i ? "Helio CRM" : "Company directory"}</strong><small>${s?.last_success ? `Observed ${relative(s.last_success)}` : "Awaiting observation"}</small></div>${badge(s?.fresh ? "Connected" : s?.status === "error" ? "Needs attention" : "Not current", s?.fresh ? "good" : "warn", true)}</div>`;
    })
    .join("");
}
function findingRows(items) {
  return items
    .map(
      (f) =>
        `<button class="finding-row" data-action="finding" data-id="${h(f.id)}"><span class="flag-icon">${icon("flag", 19)}</span><span class="row-main"><strong>${h(f.title)}</strong><small>${h(nameOf(f.evidence?.employee_id ?? f.evidence?.owner_employee_id))} <span>→</span> ${h(nameOf(f.evidence?.app_id ?? f.evidence?.agent_id ?? f.evidence?.actor_id))}</small></span>${badge(f.assessment === "unknown" ? "Unverified" : f.severity, "warn")}${icon("chevron", 16)}</button>`,
    )
    .join("");
}
function dashboard() {
  const security = securityState(state),
    counts = state?.overview?.counts ?? {},
    policies = state?.policies?.items ?? [],
    open = findings(state).filter((f) => f.status === "open");
  return (
    pageHeader(
      "A clear view of your company.",
      "People, agents, and applications. Connected and observed.",
      "OVERVIEW",
    ) +
    `<div class="hero-grid"><section class="security-card ${security.tone}"><div class="hero-top"><span class="eyebrow">COMPANY SECURITY</span>${badge(security.tone === "good" ? "Monitoring" : security.tone === "danger" ? "Needs review" : "Checking coverage", security.tone, true)}</div><div class="security-message"><span class="security-seal">${icon(security.tone === "good" ? "check" : security.tone === "danger" ? "flag" : "eye", 37)}</span><div><h2>${h(security.label)}</h2><p>${h(security.description)}</p></div></div><div class="security-bottom"><span>${icon("shield", 15)} ${policies.filter((p) => p.enabled).length} active policies</span><span>${icon("link", 15)} ${Object.values(state?.overview?.sources ?? {}).filter((s) => s.fresh).length} sources current</span>${actionLink("View policies", "policies")}</div><div class="hero-orbit" aria-hidden="true"><i></i><i></i><i></i><b>${icon("shield", 23)}</b><em>${icon("people", 15)}</em><em>${icon("agent", 15)}</em><em>${icon("apps", 15)}</em></div></section>
 <section class="panel coverage-panel"><div class="panel-heading"><h2>Source coverage</h2>${icon("activity", 17)}</div>${connectorRows()}<div class="panel-foot"><span class="live-dot"></span>${state?.overview?.observation?.interval_seconds ? `Observing every ${h(state.overview.observation.interval_seconds)} seconds` : "Manual observation"}<span class="muted">· Metadata</span></div></section></div>
 <section class="metrics" aria-label="Company inventory"><button data-action="navigate" data-page="graph"><span>${icon("people")} People & teams</span><strong>${num(counts.Employee)}<small>people</small></strong><p>${num(counts.Team)} teams in your directory ${icon("arrow", 15)}</p></button><button data-action="navigate" data-page="agents"><span>${icon("agent")} Company agents</span><strong>${num(counts.Agent)}<small>observed</small></strong><p>Ownership & access tracked ${icon("arrow", 15)}</p></button><button data-action="navigate" data-page="apps"><span>${icon("apps")} Connected apps</span><strong>${num(counts.App)}<small>application${counts.App === 1 ? "" : "s"}</small></strong><p>${num(counts.DataResource)} data resources ${icon("arrow", 15)}</p></button><button data-action="navigate" data-page="findings"><span>${icon("flag")} Open findings</span><strong>${num(state?.overview?.finding_counts?.open)}<small>to review</small></strong><p>${num(state?.overview?.finding_counts?.resolved)} resolved findings ${icon("arrow", 15)}</p></button></section>
 <div class="content-grid"><section class="panel flags-panel"><div class="panel-heading"><h2>Needs your attention <span class="count">${num(state?.overview?.finding_counts?.open)}</span></h2>${actionLink("All findings", "findings")}</div>${state?.errors?.findings ? empty("warning", "Findings are unavailable", "We cannot confirm the current findings. Refresh when the connection recovers.") : open.length ? findingRows(open.slice(0, 4)) : empty("shield", findingsVerified(state) ? "Nothing to review right now" : "Awaiting verified findings", findingsVerified(state) ? "The configured access policy has no open findings. We’ll surface new flags here." : "Current, complete evidence is needed to confirm the configured check.", actionLink("Explore your company", "graph"))}<div class="subtle-note">${icon("info", 14)} Checks cover connected sources, not every possible security risk.</div></section>
 <section class="panel posture-panel"><div class="panel-heading"><h2>Observation scope</h2>${icon("eye", 18)}</div><div class="scope-row"><span class="scope-check">${icon("check", 15)}</span><div><strong>Identity & ownership</strong><small>People, teams, and their agents</small></div></div><div class="scope-row"><span class="scope-check">${icon("check", 15)}</span><div><strong>Application permissions</strong><small>Accounts, grants, and sensitive resources</small></div></div><div class="scope-row subdued"><span class="scope-check">${icon("clock", 15)}</span><div><strong>CRM activity</strong><small>Request evidence and delivery health</small>${actionLink("View activity", "activity")}</div></div><div class="panel-foot">${icon("shield", 14)} Findings link to verified actions and human decisions</div></section></div>
 ${monitoringPanel()}`
  );
}
function monitoringPanel() {
  let rows = monitoringRows(state).filter(
    (r) => monitorFilter === "All" || r.category === monitorFilter,
  );
  const categories = ["All", "People", "Agents", "Apps", "Sync"];
  return `<section class="panel monitoring-panel"><div class="panel-heading"><div><h2>Company monitoring</h2><p>Current company state and recent observation runs.</p></div><span class="live-label"><span class="live-dot"></span> ${state?.overview?.coverage_complete ? "Sources current" : "Checking sources"}</span></div><div class="filter-bar"><div class="segmented">${categories.map((c) => btn(h(c), "monitor-filter", `data-value="${c}" aria-pressed="${monitorFilter === c}"`, monitorFilter === c ? "selected" : "")).join("")}</div><span class="mono muted">${rows.length} observations</span></div><div class="monitor-table"><div class="table-labels"><span>OBSERVATION</span><span>SOURCE TYPE</span><span>LAST OBSERVED</span></div>${
    rows.length
      ? rows
          .slice(0, monitorLimit)
          .map(
            (r) =>
              `<button class="monitor-row" data-action="${r.entity ? "entity" : "run-info"}" data-id="${h(r.entity?.id ?? r.id)}">${avatar(r.entity?.name ?? r.category, r.entity?.kind ?? "ServiceAccount")}<span class="row-main"><strong>${h(r.title)}</strong><small>${h(r.detail)}</small></span><span class="row-category">${badge(r.category)}<small>${h(r.mode)}</small></span><span class="row-time" title="${h(stamp(r.at))}">${relative(r.at)} ${icon("chevron", 14)}</span></button>`,
          )
          .join("")
      : empty(
          "activity",
          "No observations in this view",
          "Observations will appear after the next successful sync.",
        )
  }</div><div class="panel-foot"><span>Metadata snapshots are not records of every action in the company.</span>${rows.length > monitorLimit ? btn("Show more " + icon("arrow", 14), "more-monitor", "", "text-button") : ""}</div></section>`;
}
function agentsPage() {
  const inventory = entities(state),
    agents = inventory.filter((n) => n.kind === "Agent");
  return (
    pageHeader(
      "Agents, with accountability.",
      "Understand who owns each agent, what it can access, and what needs review.",
      "AGENTS",
    ) +
    `<div class="section-intro"><h2>Company agents <span class="count">${agents.length}</span></h2><span class="muted">Agents observed in connected applications</span></div><div class="agent-grid">${
      agents
        .map((a) => {
          const access = agentAccess(a, inventory),
            owner = inventory.find(
              (n) =>
                n.kind === "Employee" && n.source_id === a.owner_employee_id,
            ),
            flags = findings(state).filter(
              (f) =>
                f.status === "open" && f.evidence?.agent_id === a.source_id,
            );
          return `<article class="panel agent-card"><div class="agent-card-top">${avatar(a.name, "Agent", true)}${badge(access.label, access.tone, true)}</div><h2>${h(a.name)}</h2><p class="muted">Company agent · ${h(a.source)}</p><div class="owner-line">${avatar(owner?.name ?? "?", "Employee")}<span><small>Owned by</small><strong>${h(owner?.name ?? a.owner_employee_id)}</strong></span>${badge(owner?.status ?? "Unknown", owner?.status === "inactive" ? "warn" : "neutral")}</div><dl class="detail-grid"><div><dt>Observed grants</dt><dd>${access.grants.length}</dd></div><div><dt>Open findings</dt><dd>${flags.length || (findingsVerified(state) ? "0" : "Unverified")}</dd></div><div><dt>Last observed</dt><dd>${relative(a.last_observed_at)}</dd></div><div><dt>Execution telemetry</dt><dd>Not collected</dd></div></dl><div class="agent-work"><span>${icon("shield", 17)} Fix activity</span><strong>See related case timelines</strong><p>Findings record what Awasi changed and whether verification succeeded.</p></div>${btn("Inspect agent " + icon("arrow", 16), "entity", `data-id="${h(a.id)}"`, "button full")}</article>`;
        })
        .join("") ||
      empty(
        "agent",
        "No agents observed",
        "Connect a supported application to discover its declared agents.",
      )
    }
 <article class="panel platform-agent"><div class="eyebrow">PLATFORM OBSERVATION</div><div class="platform-visual">${icon("activity", 36)}<span></span><span></span><span></span></div><h2>Your observation worker</h2><p>Collects evidence, updates the company graph, and evaluates the configured access policy.</p><div class="worker-status">${badge("Deterministic worker", "neutral")}${badge(state?.overview?.coverage_complete ? "Sources current" : "Needs attention", state?.overview?.coverage_complete ? "good" : "warn", true)}</div><dl class="detail-grid"><div><dt>Observation interval</dt><dd>${state?.overview?.observation?.interval_seconds ? `${h(state.overview.observation.interval_seconds)} seconds` : "Manual"}</dd></div><div><dt>Case worker</dt><dd>${state?.overview?.observation?.agentic ? "Running" : "Not reporting"}</dd></div></dl><div class="panel-foot">${icon("info", 15)} Investigation and approved fixes are recorded in finding case timelines.</div></article></div>`
  );
}
function appsPage() {
  const inventory = entities(state),
    apps = inventory.filter((n) => n.kind === "App");
  return (
    pageHeader(
      "Your connected applications.",
      "See access, ownership, and findings across the systems your company uses.",
      "APPLICATIONS",
    ) +
    `<section class="panel"><div class="panel-heading"><h2>Connected applications <span class="count">${apps.length}</span></h2><span class="mono muted">API-BASED OBSERVATION</span></div><div class="apps-table"><div class="apps-labels table-labels"><span>APPLICATION</span><span>OWNER</span><span>RESOURCES</span><span>FINDINGS</span><span>CONNECTION</span></div>${
      apps
        .map((a) => {
          const flags = appFindings(a, inventory, findings(state)),
            owner = inventory.find(
              (n) =>
                n.kind === "Employee" && n.source_id === a.owner_employee_id,
            ),
            resources = (state.graph?.edges ?? []).filter(
              (e) => e.start === a.id && e.kind === "STORES",
            ),
            source = state.overview?.sources?.[a.source];
          return `<button class="app-row" data-action="entity" data-id="${h(a.id)}"><span class="app-name">${avatar(a.name, "App", true)}<span><strong>${h(a.name)}</strong><small>${h(a.source_id)}</small></span></span><span class="owner-compact">${avatar(owner?.name ?? "?")} ${h(owner?.name ?? "Unknown")}</span><span>${resources.length} resources</span><span>${badge(flags.length ? `${flags.length} open` : findingsVerified(state) ? "No open findings" : "Unverified", flags.length ? "warn" : "neutral")}</span><span>${badge(source?.fresh ? "Connected" : "Not current", source?.fresh ? "good" : "warn", true)} ${icon("chevron", 14)}</span></button>`;
        })
        .join("") ||
      empty(
        "apps",
        "No applications observed",
        "Connected application metadata will appear here.",
      )
    }</div></section>
 <div class="content-grid spaced"><section class="panel"><div class="panel-heading"><h2>Data resources <span class="count">${inventory.filter((n) => n.kind === "DataResource").length}</span></h2></div>${inventory
   .filter((n) => n.kind === "DataResource")
   .map(
     (r) =>
       `<button class="resource-row" data-action="entity" data-id="${h(r.id)}">${avatar(r.name, "DataResource")}<span class="row-main"><strong>${h(r.name)}</strong><small>${h(r.source)} · Resource metadata only</small></span>${badge(r.classification, r.classification === "confidential" ? "warn" : "neutral")}${icon("chevron", 15)}</button>`,
   )
   .join(
     "",
   )}</section><section class="panel"><div class="panel-heading"><h2>Source connections</h2></div>${connectorRows()}<div class="panel-foot">${icon("eye", 15)} Read-only access to company metadata</div></section></div>`
  );
}
function policiesPage() {
  const policies = state?.policies?.items ?? [];
  return (
    pageHeader(
      "Clear policies. Explainable checks.",
      "Understand exactly what is being evaluated and which evidence supports it.",
      "POLICIES",
    ) +
    `<div class="policy-summary"><div>${icon("shield", 22)} <strong>${policies.filter((p) => p.enabled).length} active policies</strong></div><span>${icon("eye", 17)} Deterministic checks</span><span>${icon("info", 17)} Verified actions and human decisions</span></div>${policies.map((p) => `<section class="panel policy-card"><div class="policy-main"><div class="policy-icon">${icon("shield", 25)}</div><div><div class="eyebrow">ACCESS GOVERNANCE</div><h2>${h(p.name)}</h2><p>${h(p.description)}</p></div>${badge(p.coverage_status === "unknown" ? "Coverage unknown" : !p.enabled ? "Off" : "Observing", p.coverage_status === "unknown" ? "warn" : p.enabled ? "good" : "neutral", p.enabled)}</div><div class="policy-rule"><div class="eyebrow">POLICY CONDITIONS</div><div class="condition-chain">${(p.conditions?.length ? p.conditions : [p.description]).map((c, i) => `${i ? '<span class="condition-and">AND</span>' : ""}<div><span>${String(i + 1).padStart(2, "0")}</span>${h(c)}</div>`).join("")}</div></div><div class="policy-bottom"><span>${badge(p.severity, "warn")} Severity</span><span>Sources: ${p.sources.map(h).join(" + ")}</span>${actionLink("View findings", "findings")}</div><div class="policy-evidence">${icon("info", 16)} ${h(p.coverage)}</div></section>`).join("") || empty("policy", "Policies are unavailable", "Refresh to reconnect to the security API.")}${renderWorkflowCatalog(state?.policies)}<div class="page-note">This catalog describes supported behavior. Current case records show what actually ran.</div>`
  );
}
function findingsPage() {
  const all = findings(state),
    items = all.filter(
      (f) =>
        (findingFilter === "all" || f.status === findingFilter) &&
        JSON.stringify([
          f.title,
          f.evidence?.employee_name,
          f.evidence?.agent_id,
          f.evidence?.asset_name,
          f.evidence?.host,
        ])
          .toLowerCase()
          .includes(findingSearch.toLowerCase()),
    );
  return (
    pageHeader(
      "Findings, with the full picture.",
      "Trace every flag back to its source, evidence, and affected access.",
      "FINDINGS",
    ) +
    `<section class="panel findings-panel"><div class="filter-bar"><div class="segmented">${[
      ["open", "Open"],
      ["resolved", "Resolved"],
      ["all", "All findings"],
    ]
      .map(([v, label]) =>
        btn(
          `${label} <span>${v === "all" ? num(state?.findings?.total) : num(state?.overview?.finding_counts?.[v])}</span>`,
          "finding-filter",
          `data-value="${v}" aria-pressed="${findingFilter === v}"`,
          findingFilter === v ? "selected" : "",
        ),
      )
      .join(
        "",
      )}</div><label class="table-search">${icon("search", 16)}<input id="finding-search" placeholder="Search findings" aria-label="Search findings" value="${h(findingSearch)}"></label></div>${state?.errors?.findings ? empty("warning", "Findings are unavailable", "Previously observed findings will return when the connection recovers.") : items.length ? `<div class="findings-table">${items.map((f) => `<button class="finding-row detailed" data-action="finding" data-id="${h(f.id)}"><span class="flag-icon ${f.status === "resolved" ? "resolved" : ""}">${icon(f.status === "resolved" ? "check" : "flag", 20)}</span><span class="row-main"><strong>${h(f.title)}</strong><small>${h(nameOf(f.evidence?.employee_id ?? f.evidence?.owner_employee_id))} · ${h(nameOf(f.evidence?.app_id ?? f.evidence?.agent_id ?? f.evidence?.actor_id))} · ${h(nameOf(f.evidence?.resource_id))}</small></span><span>${badge(f.status, f.status === "resolved" ? "good" : "warn")} ${f.assessment === "unknown" ? badge("Unverified", "warn") : ""}</span><span class="row-time">${relative(f.last_evaluated)}</span>${icon("chevron", 16)}</button>`).join("")}</div>` : empty(findingFilter === "resolved" ? "clock" : "shield", findingSearch ? "No matching findings" : findingFilter === "resolved" ? "No resolved findings yet" : findingFilter === "all" ? "No findings recorded" : "No open findings", findingSearch ? "Try another name or clear the search." : "The configured access check has nothing to show in this view. Findings will appear when a risky relationship is observed.", actionLink("See the active policies", "policies"))}<div class="panel-foot"><span>${state?.findings?.total > all.length ? `Showing the latest ${all.length} of ${state.findings.total} findings. ` : ""}Missing or stale evidence never automatically resolves a finding.</span></div></section>`
  );
}
function graphPage() {
  const nodes = (state?.graph?.nodes ?? []).filter(n=>n.kind!=='AppAccount'),
    nodeIds = new Set(nodes.map(n=>n.id)),
    edges = (state?.graph?.edges ?? []).filter(e=>nodeIds.has(e.start)&&nodeIds.has(e.end)),
    filtered = nodes.filter(
      (n) => graphFilter === "All" || n.kind === graphFilter,
    ),
    positions = graphLayout(nodes, edges),
    size = graphSize(positions),
    byId = new Map(positions.map((n) => [n.id, n])),
    visible = new Set(filtered.map((n) => n.id));
  const path = focusedFinding ? riskPath(focusedFinding, state?.graph) : null;
  const pathNodes = new Set(path?.nodes.map(n=>n.id) ?? []);
  const pathEdges = new Set(path?.edges.map(e=>e.id) ?? []);
  const highlighting = Boolean(focusedFinding || focusedNode);
  const connections = new Set([focusedNode]);
  for (const e of edges)
    if (e.start === focusedNode || e.end === focusedNode) {
      connections.add(e.start);
      connections.add(e.end);
    }
  const colors = {
    Employee: "#74816b",
    Team: "#899661",
    AppAccount: "#b0b1a2",
    App: "#6461b6",
    Agent: "#8b79a9",
    ServiceAccount: "#7c9c9d",
    AccessGrant: "#b5a181",
    DataResource: "#c19c77",
    Connection: "#9d8a70",
    Destination: "#b36f5c",
    Audience: "#b36f5c",
  };
  return (
    pageHeader(
      "Your company, connected.",
      "Follow relationships from people and agents to applications and sensitive data.",
      "COMPANY GRAPH",
    ) +
    `${focusedFinding ? `<div class="risk-focus" role="status"><strong>Risk path: ${h(focusedFinding.title)}</strong><span>${path.incomplete ? "Some evidence is absent from the current graph." : "Highlighted relationships match this finding’s evidence."}</span>${btn("Clear path", "clear-risk", "", "text-button")}</div>` : ""}<section class="panel graph-panel"><div class="graph-toolbar"><div class="segmented">${["All", "Employee", "Agent", "App", "DataResource"].map((k) => btn(k === "All" ? "All entities" : { Employee: "People", Agent: "Agents", App: "Apps", DataResource: "Data" }[k], "graph-filter", `data-value="${k}" aria-pressed="${graphFilter === k}"`, graphFilter === k ? "selected" : "")).join("")}</div><div class="graph-tools">${btn(icon("minus", 16), "zoom-out", 'aria-label="Zoom out"', "icon-button")}${btn(icon("plus", 16), "zoom-in", 'aria-label="Zoom in"', "icon-button")}${btn(icon("fit", 16), "zoom-fit", 'aria-label="Reset graph view"', "icon-button")}</div></div><div class="graph-stage">${
      nodes.length
        ? `<svg id="company-graph" viewBox="0 0 ${size.width} ${size.height}" style="aspect-ratio:${size.width}/${size.height}" role="group" aria-label="Company graph. Select an entity to inspect its relationships."><defs><pattern id="dots" width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".7" fill="#d8dbd0"/></pattern></defs><rect width="${size.width}" height="${size.height}" fill="url(#dots)"/><g id="graph-world" transform="translate(${pan.x} ${pan.y}) scale(${zoom})"><rect class="outside-zone" x="${size.width-220}" y="0" width="220" height="${size.height}"/><path class="outside-boundary" d="M${size.width-220} 0V${size.height}"/><g class="graph-lanes">${GRAPH_LANES.map(([kind,title],i)=>`<text x="${110+i*220}" y="38">${title}</text>`).join('')}</g>${edges
            .filter((e) => visible.has(e.start) && visible.has(e.end))
            .map((e) => {
              const a = byId.get(e.start),
                b = byId.get(e.end),
                active =
                  path ? pathEdges.has(e.id) : focusedNode &&
                  (e.start === focusedNode || e.end === focusedNode);
              return `<path class="graph-edge ${active ? "highlighted" : ""} ${highlighting && !active ? "dim" : ""}" d="${graphEdgePath(a,b)}"><title>${h(e.kind.replaceAll("_", " "))} · ${h(e.source)}</title></path>`;
            })
            .join("")}${positions
            .filter((n) => visible.has(n.id))
            .map((n) => {
              const selected = path ? pathNodes.has(n.id) : n.id === focusedNode;
              return renderGraphNode(n, {color:colors[n.kind]??'#74816b', icon:icon(kindIcon[n.kind],22), selected,
                dim:highlighting && !(path ? pathNodes.has(n.id) : connections.has(n.id))});
            })
            .join("")}</g></svg>`
        : empty(
            "graph",
            state?.errors?.graph
              ? "Graph unavailable"
              : "Awaiting your company graph",
            "The graph will appear after a successful observation.",
          )
    }<div class="graph-hint">${icon("info", 14)} Scroll to explore · Select a card for details · Drag to pan</div></div><div class="graph-footer"><div class="graph-legend">${[
      ["Employee", "People"],
      ["App", "Apps"],
      ["Agent", "Agents"],
      ["AccessGrant", "Grants"],
      ["DataResource", "Data"],
      ["Connection", "Connections"],
      ["Destination", "Outside"],
    ]
      .map(
        ([k, l]) => `<span><i style="background:${colors[k]}"></i>${l}</span>`,
      )
      .join(
        "",
      )}</div><span>${nodes.length} visible entities <b>·</b> ${edges.length} visible relationships <b>·</b> CRM accounts in person details <b>·</b> Neo4j</span></div></section>${state?.graph?.truncated ? '<div class="page-note">This is a bounded graph view; more entities or relationships exist.</div>' : ""}<div class="graph-explainer">${icon("link", 18)}<span><strong>Every line has a source.</strong> Ownership, team membership, and permissions come from directory, CRM, and workbench observations.</span></div>`
  );
}
function render() {
  const stage = tab === 'graph' ? document.querySelector('.graph-stage') : null;
  const graphScroll = {top:stage?.scrollTop ?? 0, left:stage?.scrollLeft ?? 0};
  const active = document.activeElement,
    focusId = main.contains(active) ? active?.id : null,
    selection = active?.selectionStart;
  nav.innerHTML = tabs
    .map(
      ([id, title]) =>
        `<a href="#${id}" ${tab === id ? 'aria-current="page"' : ""}>${title}${id === "findings" && state?.overview?.finding_counts?.open ? `<span class="nav-count">${state.overview.finding_counts.open}</span>` : ""}</a>`,
    )
    .join("");
  const errors = Object.keys(state?.errors ?? {});
  main.innerHTML =
    (errors.length
      ? `<div class="connection-banner" role="status">${icon("warning", 17)} <span>Some security data is unavailable. Showing the last available evidence; checks cannot be confirmed.</span>${btn("Retry", "retry", "", "text-button")}</div>`
      : "") +
    {
      dashboard,
      graph: graphPage,
      agents: agentsPage,
      apps: appsPage,
      policies: policiesPage,
      findings: findingsPage,
      activity: () => renderActivity(activityData,activityHealth,{error:activityError,loading:activityLoading,filter:activityFilter,actor:activityActor}),
    }[tab]();
  if (focusId) {
    const target = document.getElementById(focusId);
    target?.focus({ preventScroll: true });
    if (typeof selection === "number")
      target?.setSelectionRange?.(selection, selection);
  }
  const nextStage = tab === 'graph' ? document.querySelector('.graph-stage') : null;
  if (nextStage) { nextStage.scrollTop=graphScroll.top; nextStage.scrollLeft=graphScroll.left; }
  setupGraphPan();
}
function navigate(page) {
  if (tabs.some(([id]) => id === page)) location.hash = page;
}
function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => (toast.hidden = true), 6000);
}
async function load() {
  if (loading) return;
  loading = true;
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    const payload = await response.json();
    if (!payload.errors) throw Error("Invalid response");
    state = payload;
  } catch {
    state = {
      ...(state ?? {}),
      errors: { ...(state?.errors ?? {}), connection: "unavailable" },
    };
  } finally {
    loading = false;
    // Activity has its own polling and must retain expanded evidence and draft filters.
    if (tab !== 'activity') render();
    if (drawer?.type === 'finding') findingDrawer(drawer.id, {caseId:drawer.caseId, refresh:true});
  }
}
async function refresh() {
  if (pending) return;
  pending = true;
  render();
  try {
    const response = await fetch("/api/sync-runs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Awasi-Request": "dashboard",
      },
      body: "{}",
    });
    const result = await response.json();
    if (response.status !== 202 && response.status !== 409)
      throw Error("Unable to start observation");
    const id = result.id ?? result.detail?.run_id;
    showToast(
      response.status === 409
        ? "An observation is already running. Following its progress."
        : "Observation started. Collecting fresh company evidence.",
    );
    for (let i = 0; i < 45; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      const res = await fetch(`/api/sync-runs/${encodeURIComponent(id)}`);
      if (!res.ok) throw Error("Unable to check observation");
      const run = await res.json();
      if (!["queued", "running"].includes(run.status)) {
        showToast(
          run.status === "succeeded"
            ? "Observation complete. Your company view is up to date."
            : `Observation ${run.status}. Review source coverage for details.`,
        );
        await load();
        return;
      }
    }
    showToast(
      "The observation is still running. This view will keep updating.",
    );
  } catch {
    showToast(
      "Could not complete the refresh. Check the security API connection.",
    );
  } finally {
    pending = false;
    await load();
  }
}
function closeDrawer() {
  drawer = null;
  document.querySelector("#drawer-root").innerHTML = "";
  document.body.classList.remove("drawer-open");
  if (drawerReturn?.isConnected) drawerReturn.focus({ preventScroll: true });
}
function showDrawer(title, subtitle, content, focus = true) {
  document.body.classList.add("drawer-open");
  document.querySelector("#drawer-root").innerHTML =
    `<div class="drawer-overlay"><button class="drawer-scrim" data-action="close-drawer" aria-label="Close details"></button><aside class="drawer${focus ? "" : " refreshed"}" role="dialog" aria-modal="true" aria-labelledby="drawer-title"><div class="drawer-heading"><span class="eyebrow">${h(subtitle)}</span>${btn(icon("close", 20), "close-drawer", 'aria-label="Close details"', "icon-button")}<h2 id="drawer-title">${h(title)}</h2></div><div class="drawer-body">${content}</div></aside></div>`;
  if (focus) document.querySelector(".drawer .icon-button").focus();
}
function entityDrawer(id) {
  const n =
    entities(state).find((n) => n.id === id) ??
    state?.graph?.nodes?.find((n) => n.id === id);
  if (!n) return;
  drawer = { type: "entity", id };
  drawerReturn = document.activeElement;
  const all = [...new Map([...(state?.graph?.nodes ?? []),...entities(state)].map(item=>[item.id,item])).values()],
    accounts = personAccounts(n, all, state?.graph),
    accountIds = new Set(accounts.map(a=>a.id)),
    edges = (state?.graph?.edges ?? []).filter(
      (e) => (e.start === id || e.end === id) && !(n.kind === "Employee" && e.kind === "IDENTITY_OF" && accountIds.has(e.start)),
    ),
    grants = n.kind === "Agent" ? agentAccess(n, all).grants : [];
  const fields = [
    "status",
    "role",
    "principal_status",
    "owner_employee_id",
    "team_id",
    "email",
    "title",
    "classification",
    "visibility",
    "host",
    "enabled",
    "crm_principal_id",
    "anonymous_views_1h",
    "records_sent_1h",
    "principal_id",
    "resource_id",
    "scopes",
    "version",
  ];
  showDrawer(
    n.name ?? n.source_id,
    n.kind.replace(/([a-z])([A-Z])/g, "$1 $2"),
    `<div class="drawer-identity">${avatar(n.name ?? n.source_id, n.kind, true)}<div><strong>${h(n.name ?? n.source_id)}</strong><small>${h(n.source_id)}</small></div></div><div class="drawer-section"><h3>Observed details</h3><dl class="property-list">${fields
      .filter((k) => n[k] != null)
      .map(
        (k) =>
          `<div><dt>${h(k.replaceAll("_", " "))}</dt><dd>${h(Array.isArray(n[k]) ? n[k].join(", ") : k.endsWith("employee_id") ? nameOf(n[k]) : n[k])}</dd></div>`,
      )
      .join(
        "",
      )}<div><dt>Source</dt><dd>${h(n.source)}</dd></div><div><dt>Last observed</dt><dd>${h(stamp(n.last_observed_at))}</dd></div></dl></div>${n.kind === 'Employee' ? `<div class="drawer-section"><h3>CRM accounts <span class="count">${accounts.length}</span></h3>${accounts.map(a=>{
        const apps=(state?.graph?.edges ?? []).filter(e=>e.kind==='USES'&&e.start===a.id).map(e=>all.find(item=>item.id===e.end)).filter(Boolean);
        return `<button class="drawer-relation" data-action="entity" data-id="${h(a.id)}"><span>${h(apps.map(app=>app.name??app.source_id).join(', ') || 'CRM account')}<small>${h(a.source_id)} · ${h(a.role??'Role not observed')}</small></span>${badge(a.principal_status??'Not observed',a.principal_status==='active'?'good':'neutral')}</button>`;
      }).join('') || '<p class="muted">No linked CRM accounts in the loaded evidence.</p>'}${state?.graph?.truncated || (state?.inventory?.total??0)>all.length ? '<p class="muted">This view is limited; additional accounts may not be loaded.</p>' : ''}</div>` : ''}${n.kind === "Agent" ? `<div class="drawer-callout">${icon("info", 18)}<div><strong>Observed agent</strong><p>Related findings and case records show any containment, approval, and verified outcome.</p></div></div><div class="drawer-section"><h3>Access grants</h3>${grants.map((g) => `<button class="drawer-relation" data-action="entity" data-id="${h(g.id)}"><span>${h(g.source_id)}<small>${h(g.scopes?.join(", "))}</small></span>${badge(g.status, g.status === "active" ? "good" : "neutral")}</button>`).join("") || '<p class="muted">No grants observed.</p>'}</div>` : ""}<div class="drawer-section"><h3>Relationships <span class="count">${edges.length}</span></h3>${
      edges
        .slice(0, 30)
        .map((e) => {
          const other = all.find(
            (x) => x.id === (e.start === id ? e.end : e.start),
          );
          return `<button class="drawer-relation" data-action="entity" data-id="${h(other?.id ?? "")}"><span><small>${h(e.kind.replaceAll("_", " "))}</small>${h(other?.name ?? other?.source_id ?? "Entity")}</span>${icon("chevron", 15)}</button>`;
        })
        .join("") ||
      '<p class="muted">No visible relationships in the current graph.</p>'
    }</div>`,
  );
}
async function findingDrawer(id, {caseId=null, refresh=false} = {}) {
  const request = {type:'finding', id, caseId};
  const traceOpen = refresh && document.querySelector('.case-traces')?.open;
  const scroll = refresh ? document.querySelector('.drawer')?.scrollTop ?? 0 : 0;
  drawer = request;
  if (!refresh) {
    drawerReturn = document.activeElement;
    showDrawer('Finding details', 'FINDING', '<div class="initial-loading"><span class="spinner"></span> Loading evidence…</div>');
  }
  try {
    const response = await fetch(`/api/findings/${encodeURIComponent(id)}`);
    if (!response.ok) throw Error();
    const f = await response.json();
    if (drawer !== request) return;
    request.finding = f;
    const cases = findingCases(id, state?.cases);
    const selected = cases.find(c=>c.id===caseId) ?? cases[0];
    let detail=null, failed=false;
    if (selected) {
      try {
        const res=await fetch(`/api/cases/${encodeURIComponent(selected.id)}`);
        if (!res.ok) throw Error();
        detail=await res.json();
        if (detail.id !== selected.id || detail.finding_id !== id) throw Error();
      } catch { detail=null; failed=true; }
    }
    if (drawer !== request) return;
    const truncated = (state?.cases?.total ?? 0) > (state?.cases?.items?.length ?? 0);
    const caseContent = selected ? `${cases.length>1 ? `<div class="case-selector" aria-label="Case history">${cases.map(c=>btn(`${h(stamp(c.created_at))} · ${h(c.status ?? 'Case')}`, 'case-history', `data-id="${h(c.id)}" aria-pressed="${c.id===selected.id}"`, 'text-button')).join('')}</div>` : ''}${renderCase(detail)}`
      : `<section class="drawer-section"><h3>Case timeline</h3><p>${state?.errors?.cases || !state?.cases ? 'Case history unavailable.' : truncated ? 'No linked case in the loaded history.' : 'No case has been recorded for this finding yet.'}</p></section>`;
    const focusedControl = refresh && document.activeElement?.closest?.('.drawer') ? document.activeElement : null;
    showDrawer(f.title, 'FINDING', `<div class="drawer-badges">${badge(f.status, f.status==='resolved'?'good':'warn')}${badge(f.severity,'warn')}${badge(f.evidence_fresh?'Evidence current':'Evidence unverified',f.evidence_fresh?'good':'warn')}</div>
      ${renderRiskPath(f,state?.graph)}${caseContent}
      ${truncated ? '<p class="muted">Only the latest 200 cases are loaded. Older cases may be omitted.</p>' : ''}
      ${failed || state?.errors?.cases ? btn('Retry case details','reload-case','','text-button') : ''}
      <div class="drawer-section"><h3>Suggested fix</h3><p>${h(f.suggested_fix)}</p></div>
      <div class="drawer-section"><h3>Supporting evidence</h3><dl class="property-list">${Object.entries(f.evidence??{}).map(([k,v])=>`<div><dt>${h(k.replaceAll('_',' '))}</dt><dd>${h(k.endsWith('_at') ? stamp(v) : typeof v==='object' && v!==null ? JSON.stringify(v) : v)}</dd></div>`).join('')}</dl></div>
      <div class="drawer-section"><h3>Finding history</h3><ol class="timeline">${(f.events??[]).slice().reverse().map(e=>`<li><strong>${h(e.type?.replaceAll('_',' '))}</strong><small>${h(stamp(e.occurred_at))}</small><p>${h(e.reason?.replaceAll('_',' '))}</p></li>`).join('')}</ol></div>`, !refresh);
    if (focusedControl) {
      const replacement = [...document.querySelectorAll('.drawer button, .drawer summary')].find(el =>
        el.tagName === focusedControl.tagName && el.dataset?.action === focusedControl.dataset?.action && el.dataset?.id === focusedControl.dataset?.id);
      replacement?.focus({preventScroll:true});
    }
    if (traceOpen && document.querySelector('.case-traces')) document.querySelector('.case-traces').open=true;
    if (refresh && document.querySelector('.drawer')) document.querySelector('.drawer').scrollTop=scroll;
  } catch {
    if (drawer === request) showDrawer('Evidence unavailable','FINDING',empty('warning','Could not load this finding','The security API may be temporarily unavailable.'));
  }
}

function runDrawer(id) {
  const run = state?.runs?.items?.find((r) => r.id === id);
  if (!run) return;
  drawer = { type: "run", id };
  drawerReturn = document.activeElement;
  showDrawer(
    "Company observation",
    "SYNC RUN",
    `<div class="drawer-badges">${badge(run.status, run.status === "succeeded" ? "good" : "warn")}</div><dl class="property-list"><div><dt>Requested by</dt><dd>${h(run.actor)}</dd></div><div><dt>Started</dt><dd>${h(stamp(run.started_at))}</dd></div><div><dt>Finished</dt><dd>${h(stamp(run.finished_at))}</dd></div>${Object.entries(
      run.source_results ?? {},
    )
      .map(([k, v]) => `<div><dt>${h(k)}</dt><dd>${h(v)}</dd></div>`)
      .join(
        "",
      )}</dl><div class="drawer-callout">${icon("info", 18)} This records a metadata observation, not actions performed by employees or agents.</div>`,
  );
}
function setupGraphPan() {
  const svg = document.querySelector("#company-graph");
  if (!svg) return;
  let start = null;
  svg.addEventListener("pointerdown", (event) => {
    if (event.target.closest("[data-action]")) return;
    start = { x: event.clientX, y: event.clientY, pan: { ...pan } };
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener("pointermove", (event) => {
    if (!start) return;
    const scale = svg.viewBox.baseVal.width / svg.getBoundingClientRect().width;
    pan = {
      x: start.pan.x + (event.clientX - start.x) * scale,
      y: start.pan.y + (event.clientY - start.y) * scale,
    };
    document
      .querySelector("#graph-world")
      ?.setAttribute(
        "transform",
        `translate(${pan.x} ${pan.y}) scale(${zoom})`,
      );
  });
  svg.addEventListener("pointerup", () => (start = null));
  svg.addEventListener("pointercancel", () => (start = null));
}
function search() {
  const input = document.querySelector("#global-search"),
    list = document.querySelector("#search-results"),
    term = input.value.trim().toLowerCase();
  const matches = term
    ? entities(state)
        .filter((n) =>
          [n.name, n.source_id, n.email, n.kind].some((v) =>
            String(v ?? "")
              .toLowerCase()
              .includes(term),
          ),
        )
        .slice(0, 8)
    : [];
  list.hidden = !term;
  input.setAttribute("aria-expanded", String(!!term));
  list.innerHTML = matches.length
    ? matches
        .map(
          (n) =>
            `<button role="option" data-action="search-result" data-id="${h(n.id)}">${avatar(n.name ?? n.source_id, n.kind)}<span><strong>${h(n.name ?? n.source_id)}</strong><small>${h(n.kind)} · ${h(n.source)}</small></span>${icon("arrow", 14)}</button>`,
        )
        .join("")
    : '<div class="search-empty">No matching entities</div>';
}
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const { action, id, value, page } = target.dataset;
  switch (action) {
    case "activity-reload":
      loadActivity();
      break;
    case "activity-more":
      if (!activityLoading) loadActivity(true);
      break;
    case "navigate":
      navigate(page);
      break;
    case "refresh":
      refresh();
      break;
    case "retry":
      load();
      break;
    case "monitor-filter":
      monitorFilter = value;
      monitorLimit = 7;
      render();
      break;
    case "more-monitor":
      monitorLimit += 10;
      render();
      break;
    case "finding-filter":
      findingFilter = value;
      render();
      break;
    case "clear-risk":
      focusedFinding = null;
      render();
      break;
    case "case-history":
      if (drawer?.type === "finding") findingDrawer(drawer.id, {caseId:id});
      break;
    case "reload-case":
      if (drawer?.type === "finding") findingDrawer(drawer.id, {caseId:drawer.caseId});
      break;
    case "show-risk": {
      const finding = drawer?.finding ?? findings(state).find(f=>f.id===id);
      if (!finding) break;
      focusedFinding=finding; focusedNode=null; graphFilter="All"; zoom=1; pan={x:0,y:0};
      closeDrawer();
      tab="graph"; navigate("graph"); render();
      break;
    }
    case "graph-filter":
      focusedFinding = null;
      graphFilter = value;
      focusedNode = null;
      render();
      break;
    case "zoom-in":
      zoom = Math.min(zoom + 0.2, 2.5);
      render();
      break;
    case "zoom-out":
      zoom = Math.max(zoom - 0.2, 0.5);
      render();
      break;
    case "zoom-fit":
      if (document.querySelector(".graph-stage")) {
        document.querySelector(".graph-stage").scrollTop=0;
        document.querySelector(".graph-stage").scrollLeft=0;
      }
      focusedFinding = null;
      zoom = 1;
      pan = { x: 0, y: 0 };
      focusedNode = null;
      graphFilter = "All";
      render();
      break;
    case "graph-node":
      focusedFinding = null;
      focusedNode = id;
      render();
      entityDrawer(id);
      break;
    case "entity":
      entityDrawer(id);
      break;
    case "finding":
      findingDrawer(id);
      break;
    case "run-info":
      runDrawer(id);
      break;
    case "close-drawer":
      closeDrawer();
      break;
    case "search-result":
      document.querySelector("#global-search").value = "";
      search();
      entityDrawer(id);
      break;
  }
});
document.addEventListener("input", (event) => {
  if (event.target.id === "global-search") search();
  if (event.target.id === "finding-search") {
    findingSearch = event.target.value;
    render();
  }
});
document.addEventListener('submit', event => {
  if (event.target.id !== 'activity-filter-form') return;
  event.preventDefault();
  activityFilter = document.querySelector('#activity-outcome').value;
  activityActor = document.querySelector('#activity-actor').value.trim();
  activityData = null;
  loadActivity();
});
document.querySelector("#company-toggle").addEventListener("click", () => {
  const menu = document.querySelector("#company-menu");
  menu.hidden = !menu.hidden;
  document
    .querySelector("#company-toggle")
    .setAttribute("aria-expanded", String(!menu.hidden));
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".search-wrap")) {
    document.querySelector("#search-results").hidden = true;
    document
      .querySelector("#global-search")
      .setAttribute("aria-expanded", "false");
  }
  if (!event.target.closest(".company-control")) {
    document.querySelector("#company-menu").hidden = true;
    document
      .querySelector("#company-toggle")
      .setAttribute("aria-expanded", "false");
  }
});
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "k") {
    event.preventDefault();
    document.querySelector("#global-search").focus();
  }
  if (event.key === "Escape") {
    closeDrawer();
    document.querySelector("#search-results").hidden = true;
    document.querySelector("#company-menu").hidden = true;
  }
  if (
    (event.key === "Enter" || event.key === " ") &&
    event.target.matches(".graph-node")
  ) {
    event.preventDefault();
    event.target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  }
  if (event.key === "Tab" && drawer) {
    const controls = [
      ...document.querySelectorAll(".drawer button,.drawer a,.drawer input,.drawer summary"),
    ];
    const first = controls[0],
      last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }
  if (event.key === "ArrowDown" && event.target.id === "global-search") {
    event.preventDefault();
    document.querySelector("#search-results button")?.focus();
  }
});
window.addEventListener("hashchange", () => {
  tab = tabs.find(([id]) => id === location.hash.slice(1))?.[0] ?? "dashboard";
  closeDrawer();
  render();
  if (tab === 'activity') loadActivity();
  window.scrollTo({ top: 0, behavior: "instant" });
});
tab = tabs.find(([id]) => id === location.hash.slice(1))?.[0] ?? "dashboard";
render();
load();
if (tab === 'activity') loadActivity();
setInterval(() => {
  if (!document.hidden && !pending) load();
}, 15000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) load();
});
setInterval(() => {
  if (!document.hidden && tab === 'activity' && !activityLoading && !document.querySelector('.activity-request[open]') &&
      !document.activeElement?.closest('#activity-filter-form') && (activityData?.items?.length ?? 0) <= 25) loadActivity();
}, 5000);
