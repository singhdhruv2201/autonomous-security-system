"use strict";

const $ = (id) => document.getElementById(id);
const ui = {
  config: null, session: null, selected: "offboarding", anonymous: false,
  metric: "open_findings", model: null, node: null, pending: null,
};

const cards = {
  offboarding: { icon: "↪", hint: "Identity & access" },
  public: { icon: "◉", hint: "Public exposure" },
  external: { icon: "⇢", hint: "Outbound data" },
};
const metrics = {
  open_findings: "Open findings", actions_verified: "Verified actions",
  anonymous_views: "Anonymous views (1h)", records_delivered: "Records delivered",
};
const choices = {
  retire: "Retire agent", keep: "Keep agent paused", undo: "Undo restriction",
  approve: "Approve", reject: "Reject", restrict: "Make company-only",
  intentional: "Record intentional sharing", keep_restricted: "Keep company-only",
  remove_connection: "Disable connection and resume", keep_paused: "Keep agent paused",
  allow_7_days: "Allow for 7 days",
};
const people = {
  "emp-hannah-reed": "Hannah Reed", "emp-luca-rossi": "Luca Rossi",
  "emp-casey-wong": "Casey Wong", "emp-zara-ali": "Zara Ali",
};
const questions = {
  lead_decision: "Does Sales still need the Sales Research Agent?",
  "approve:crm.revoke_grant": "Approve revoking the Sales Research Agent’s CRM grant?",
  workbench_public: "Was the public sharing intentional?",
  workbench_restricted: "Keep Renewal Tracker company-only?",
  workbench_destination: "How should Awasi handle the unapproved connection?",
  workbench_approval: "Approve the proposed containment?",
};
const statusText = {
  waiting_owner: "Waiting for owner", waiting_approver: "Waiting for approver",
  needs_review: "Needs review", resolved: "Resolved", dismissed: "Dismissed",
};
const svgNS = "http://www.w3.org/2000/svg";
const timeFormat = new Intl.DateTimeFormat(undefined, { timeStyle: "medium", hour12: false });

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function svg(tag, attributes = {}, value) {
  const node = document.createElementNS(svgNS, tag);
  for (const [name, data] of Object.entries(attributes)) node.setAttribute(name, data);
  if (value !== undefined) node.textContent = value;
  return node;
}

function empty(message) {
  return element("p", "empty", message);
}

function pretty(value) {
  return String(value ?? "").replaceAll("_", " ");
}

function chip(target, text, tone = "neutral") {
  target.textContent = text;
  target.className = "chip " + tone;
}

function publicVariant() {
  if (ui.selected !== "public") return ui.selected;
  return ui.session?.scenarios.public_view.triggered || ui.anonymous ? "public_view" : "public";
}

function focus() {
  const id = publicVariant();
  const scenario = (ui.session?.scenarios ?? ui.config?.scenarios)?.[id];
  const finding = ui.session?.findings.find((item) => item.rule === scenario?.rule);
  const currentCase = ui.session?.cases.find((item) => item.finding_id === finding?.id);
  return { id, scenario, finding, currentCase };
}

async function api(path, method = "GET", data) {
  const options = { method };
  if (method === "POST") {
    options.headers = { "X-Awasi-Showcase": "1", "Content-Type": "application/json" };
    if (data) options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  if (response.status === 404 && path === "/api/session" && method === "GET") return null;
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(typeof payload.detail === "string" ? payload.detail : "The request could not be completed.");
  }
  return payload;
}

function renderEnvironment() {
  const state = ui.session;
  const busy = ui.pending || state?.busy;
  $("environment-dot").classList.toggle("on", Boolean(state));
  $("environment-status").textContent = busy ? "Operation in progress" : state ? "Baseline environment active" : "Environment offline";
  $("slack-mode").hidden = !ui.config;
  $("slack-mode").textContent = ui.config?.slack_interactive ? "Slack alerts and actions" :
    ui.config?.slack_enabled ? "Slack alerts only" : "Slack alerts off";
  $("environment-id").textContent = state ? "SESSION " + state.id.slice(0, 8).toUpperCase() : "";
  $("environment").textContent = state ? "New baseline" : "Start baseline";
  $("hero-action").firstChild.nodeValue = state ? "Explore scenarios " : "Start baseline ";
  $("environment").disabled = Boolean(busy || !ui.config);
  $("hero-action").disabled = Boolean(busy || !ui.config);
}

function renderScenarios() {
  const { id, scenario } = focus();
  const options = $("scenario-options");
  const activeCard = options.contains(document.activeElement) ? document.activeElement.dataset.id : null;
  options.replaceChildren();
  for (const [key, { icon, hint }] of Object.entries(cards)) {
    if (!ui.config) continue;
    const button = element("button", "scenario-card");
    button.dataset.id = key;
    button.setAttribute("aria-pressed", key === ui.selected);
    const title = element("div");
    title.append(element("strong", "", ui.config.scenarios[key].title), element("small", "", hint));
    const ran = ui.session?.scenarios[key].triggered || (key === "public" && ui.session?.scenarios.public_view.triggered);
    const marker = element("span", "card-check", ran ? "✓" : "›");
    marker.setAttribute("aria-hidden", "true");
    button.append(element("span", "scenario-icon", icon), title, marker);
    button.addEventListener("click", () => {
      ui.selected = key;
      ui.node = ui.model = null;
      render();
    });
    options.append(button);
    if (key === activeCard) button.focus({ preventScroll: true });
  }
  if (!scenario) return;
  const available = ui.session && scenario.available;
  const ranCount = Object.values(ui.session?.scenarios ?? {}).filter((item) => item.triggered).length;
  $("scenario-count").textContent = ui.session ? ranCount + " of 3 run" : "3 policies";
  $("scenario-control").hidden = false;
  $("scenario-title").textContent = scenario.title;
  $("scenario-description").textContent = scenario.description;
  $("public-option").hidden = ui.selected !== "public";
  $("public-view").checked = id === "public_view";
  $("public-view").disabled = Boolean(ui.pending || ui.session?.busy || (ui.session && !available));
  const run = $("run-scenario");
  run.disabled = Boolean(ui.pending || ui.session?.busy || !available);
  run.firstChild.nodeValue = !ui.session ? "Start baseline first " :
    ui.pending === "scenario" ? "Running… " : available ? "Run scenario " : "Scenario complete ";
}

function graphStatus(node) {
  if (node.kind === "Audience") return ["Anonymous audience", "risk"];
  if (node.kind === "Destination") return ["Outside destination", "risk"];
  if (node.kind === "Connection") return [node.enabled ? "Enabled connection" : "Disabled connection", node.enabled ? "risk" : "safe"];
  if (node.kind === "Employee") return [node.status + " employee", node.status === "inactive" ? "risk" : ""];
  if (node.kind === "AccessGrant") return [node.status + " grant", node.status === "revoked" ? "safe" : ""];
  if (node.kind === "DataResource") return [node.classification + " data", ""];
  if (node.kind === "App") return [node.visibility + " app", node.visibility === "public" ? "risk" : ""];
  if (node.kind === "Agent" && node.source === "workbench") return [node.status + " agent", node.status === "paused" ? "safe" : ""];
  if (node.kind === "Agent") return [node.principal_status + " CRM agent", node.principal_status === "disabled" ? "safe" : ""];
  return ["CRM service identity", ""];
}

function graphTitle(node) {
  return node.name || (node.kind === "Connection" ? node.host : null) || node.source_id;
}

function renderGraph() {
  const { scenario, finding } = focus();
  const container = $("graph");
  $("graph-subtitle").textContent = scenario ? "Observed access path · " + scenario.title : "Observed relationships for the selected scenario.";
  const policy = ui.config?.policies[scenario?.rule];
  const complete = ui.session && policy?.sources.every((source) => ui.session.sources[source]?.status === "ok");
  const findingLabel = finding?.status === "open" ? "Open finding" : !ui.session ? "Awaiting baseline" :
    !complete ? "Coverage incomplete" : finding ? "Finding resolved" : "No finding";
  const findingTone = finding?.status === "open" ? "danger" : complete ? "success" : ui.session ? "attention" : "neutral";
  chip($("finding-status"), findingLabel, findingTone);
  const graph = scenario?.graph;
  if (!graph?.nodes.length) {
    container.replaceChildren(empty("Start a baseline to see the seeded Helio access path."));
    $("graph-inspector").replaceChildren();
  } else {
    const width = 720;
    const height = 210;
    const cardWidth = 105;
    const cardHeight = 56;
    const outsideKinds = new Set(["Connection", "Destination", "Audience"]);
    const main = graph.nodes.filter((node) => !outsideKinds.has(node.kind));
    const outside = graph.nodes.filter((node) => outsideKinds.has(node.kind));
    const positions = new Map();
    main.forEach((node, index) => {
      positions.set(node.id, { x: 13 + index * (width - cardWidth - 26) / Math.max(1, main.length - 1), y: 125 });
    });
    outside.forEach((node, index) => {
      positions.set(node.id, { x: 13 + (index + 2) * 132, y: 30 });
    });

    const drawing = svg("svg", { viewBox: "0 0 " + width + " " + height, role: "group", "aria-label": "Observed access relationships" });
    const definitions = svg("defs");
    for (const [key, fill] of [["observed", "#82968a"], ["outside", "#b07843"]]) {
      const marker = svg("marker", { id: "arrow-" + key, viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse" });
      marker.append(svg("path", { d: "M0 0 L10 5 L0 10 Z", fill }));
      definitions.append(marker);
    }
    drawing.append(definitions);
    if (outside.length) drawing.append(svg("text", { x: 15, y: 27, class: "edge-label" }, "OUTSIDE THE COMPANY"));
    for (const edge of graph.edges) {
      const from = positions.get(edge.from);
      const to = positions.get(edge.to);
      const branch = from.y !== to.y;
      const x1 = from.x + (branch ? cardWidth / 2 : cardWidth);
      const x2 = to.x + (branch ? cardWidth / 2 : 0);
      const y1 = from.y + (branch ? 0 : cardHeight / 2);
      const y2 = to.y + (branch ? cardHeight : cardHeight / 2);
      const middle = (y1 + y2) / 2;
      const external = to.y === 30;
      const path = branch ?
        "M" + x1 + " " + y1 + " V" + middle + " H" + x2 + " V" + y2 :
        "M" + x1 + " " + y1 + " H" + x2;
      drawing.append(svg("path", { d: path, class: "edge" + (external ? " outside" : ""), "marker-end": "url(#arrow-" + (external ? "outside" : "observed") + ")" }));
      drawing.append(svg("text", { x: (x1 + x2) / 2, y: middle - 9, "text-anchor": "middle", class: "edge-label" }, edge.kind));
    }
    for (const node of graph.nodes) {
      const position = positions.get(node.id);
      const foreign = svg("foreignObject", { ...position, width: cardWidth, height: cardHeight });
      const [label, tone] = graphStatus(node);
      const button = element("button", "node-button " + tone);
      button.setAttribute("aria-pressed", ui.node === node.id);
      button.title = graphTitle(node) + " · " + label;
      button.append(element("span", "", pretty(label)), element("strong", "", graphTitle(node)));
      button.addEventListener("click", () => {
        ui.node = ui.node === node.id ? null : node.id;
        for (const item of drawing.querySelectorAll("button")) item.setAttribute("aria-pressed", item === button && Boolean(ui.node));
        renderInspector(graph);
      });
      foreign.append(button);
      drawing.append(foreign);
    }
    container.replaceChildren(drawing);
    renderInspector(graph);
  }

  const evidence = $("policy-evidence");
  evidence.replaceChildren();
  if (policy) {
    evidence.append(element("p", "policy-copy", policy.name + " · " + policy.coverage));
    if (scenario.evidence && Object.keys(scenario.evidence).length) {
      evidence.append(element("pre", "", JSON.stringify({
        rule: policy.id, finding: finding?.reason, evidence: scenario.evidence,
      }, null, 2)));
    }
  }
}

function renderInspector(graph) {
  const node = graph.nodes.find((item) => item.id === ui.node);
  const inspector = $("graph-inspector");
  inspector.replaceChildren();
  if (!node) return;
  const [label] = graphStatus(node);
  inspector.append(element("strong", "", graphTitle(node)),
    element("span", "", pretty(label) + " · " + node.source + " · "),
    element("code", "", node.source_id));
}

function renderModel() {
  const { id, scenario, currentCase } = focus();
  const target = $("assessment");
  const recording = scenario?.triggered ? scenario.recording : null;
  const current = currentCase?.investigation;
  const selected = ui.model ?? (current?.source === "model" ? "current" : "recorded");
  const fromRecording = Boolean(recording && (selected === "recorded" || !current));
  const assessment = fromRecording ? recording.assessment : current;
  $("model-options").hidden = !(recording && current);
  $("recorded-option").setAttribute("aria-pressed", fromRecording);
  $("current-option").setAttribute("aria-pressed", !fromRecording);
  $("model-detail").hidden = !assessment;
  target.replaceChildren();

  if (!assessment) {
    chip($("model-source"), "Awaiting case");
    target.append(empty(scenario?.triggered ? "Waiting for the investigation to finish." : "Run a scenario to review its assessment and cited evidence."));
    return;
  }
  const live = !fromRecording && current.source === "model";
  chip($("model-source"), fromRecording ? "Recorded response" : live ? "Live Anthropic" : "Workflow fallback", live || fromRecording ? "success" : "attention");
  const provenance = fromRecording
    ? recording.model + " · " + recording.captured_on + (recording.scenario !== id ? " · Related recording from the public sharing scenario" : " · Previous Helio walkthrough")
    : live ? ui.session.model + " · Generated for this run"
    : current.fallback_reason === "live_model_disabled" ? "Deterministic fallback · Live Anthropic calls are disabled"
    : "Deterministic fallback · " + pretty(current.fallback_reason);
  target.append(element("p", "provenance", provenance));
  target.append(element("p", "summary", assessment.summary));
  if (assessment.impact) target.append(element("p", "impact", assessment.impact));
  const tags = element("div", "tags");
  tags.append(element("span", "chip neutral", pretty(assessment.likely_cause)), element("span", "chip neutral", assessment.confidence + " confidence"));
  target.append(tags);
  if (assessment.question_for_owner) target.append(element("p", "impact", assessment.question_for_owner));
  const fields = ["summary", "impact", "likely_cause", "confidence", "evidence_refs", "question_for_owner", "recommended_playbook", "slack_message"];
  $("model-json").textContent = JSON.stringify(Object.fromEntries(fields.map((key) => [key, assessment[key]])), null, 2);
}

function renderMetrics() {
  const state = ui.session;
  $("graph-counts").textContent = state ? state.inventory.entities + " entities · " + state.inventory.relationships + " relationships" : "";
  const options = $("metric-options");
  options.replaceChildren();
  for (const [key, name] of Object.entries(metrics)) {
    const button = element("button", "metric-button");
    button.setAttribute("aria-pressed", key === ui.metric);
    button.append(element("strong", "", state?.metrics[key] ?? "–"), element("span", "", name));
    button.addEventListener("click", () => {
      ui.metric = key;
      for (const item of options.children) item.setAttribute("aria-pressed", item === button);
      renderChart();
    });
    options.append(button);
  }
  renderChart();
  const sources = $("sources");
  sources.replaceChildren();
  for (const [source, status] of Object.entries(state?.sources ?? {})) {
    const label = { directory: "Directory", crm: "CRM", workbench: "Workbench" }[source];
    const badge = element("span", "chip " + (status.status === "ok" ? "success" : "attention"),
      label + " · " + (status.status === "ok" ? "observed" : "unavailable"));
    if (status.last_success) badge.title = "Last success " + timeFormat.format(status.last_success * 1000);
    sources.append(badge);
  }
}

function renderChart() {
  const surface = $("metric-chart");
  const samples = ui.session?.samples ?? [];
  if (!samples.length) {
    surface.replaceChildren(empty("Metrics appear after the first observation."));
    return;
  }
  const label = metrics[ui.metric];
  const width = 620;
  const left = 27;
  const right = width - 19;
  const base = 98;
  const top = 15;
  const points = samples.map((sample) => sample[ui.metric]);
  const max = Math.max(1, ...points);
  const x = (index) => points.length === 1 ? (left + right) / 2 : left + (right - left) * index / (points.length - 1);
  const y = (value) => base - (base - top) * value / max;
  const chart = svg("svg", { viewBox: "0 0 " + width + " 129", role: "img", "aria-label": label + " over time" });
  chart.append(svg("desc", {}, samples.map((sample, index) => sample.label + " " + points[index]).join(", ")));
  const grid = [...new Set([0, Math.ceil(max / 2), max])];
  for (const value of grid) {
    chart.append(svg("line", { x1: left, x2: right, y1: y(value), y2: y(value), class: "chart-grid" }));
    chart.append(svg("text", { x: left - 8, y: y(value) + 3, "text-anchor": "end", class: "chart-label" }, value));
  }
  if (points.length > 1) {
    const line = points.map((value, index) => (index ? " L" : "M") + x(index) + " " + y(value)).join("");
    chart.append(svg("path", { d: line + " V" + base + " H" + left + " Z", class: "chart-fill" }));
    chart.append(svg("path", { d: line, class: "chart-line" }));
  }
  for (const [index, sample] of samples.entries()) {
    const point = svg("circle", { cx: x(index), cy: y(points[index]), r: 4, class: "chart-point" });
    point.append(svg("title", {}, sample.label + (sample.detail ? " · " + sample.detail : "") + " · " + label + " " + points[index]));
    chart.append(point);
    if (index === 0 || index === samples.length - 1 || samples.length <= 6) {
      const anchor = samples.length === 1 ? "middle" : index === 0 ? "start" : index === samples.length - 1 ? "end" : "middle";
      chart.append(svg("text", { x: x(index), y: 117, "text-anchor": anchor, class: "chart-label" }, sample.label));
    }
  }
  surface.replaceChildren(chart);
}

function renderDecision() {
  const { currentCase } = focus();
  const panel = $("decision");
  const question = currentCase?.question;
  const pending = question && currentCase.status.startsWith("waiting_");
  chip($("case-status"), pending && question.role === "lead" ? "Waiting for team lead" :
    statusText[currentCase?.status] ?? (currentCase ? pretty(currentCase.status) : "Awaiting case"),
  pending ? "attention" : currentCase?.status === "resolved" ? "success" : "neutral");
  const delivery = {
    sent: ["Slack alert posted", "success"],
    pending: ["Slack alert queued", "attention"],
    unknown: ["Slack delivery unconfirmed", "danger"],
  }[currentCase?.slack_delivery];
  $("slack-delivery").hidden = !delivery;
  if (delivery) chip($("slack-delivery"), ...delivery);

  panel.hidden = !pending;
  panel.replaceChildren();
  if (!pending) return;

  const person = people[question.addressed_to] ?? question.addressed_to;
  panel.append(element("strong", "", questions[question.key] ?? "Review the proposed next step"));
  panel.append(element("p", "", "Act as " + person + " · " + (question.role === "lead" ? "team lead" : question.role)));
  const buttons = element("div", "decision-buttons");
  for (const [index, choice] of question.choices.entries()) {
    const label = choice === "undo" && question.role === "lead" ? "Restore agent access" : choices[choice] ?? pretty(choice);
    const button = element("button", "button " + (index ? "button-outline" : "button-dark"), label);
    button.disabled = Boolean(ui.pending || ui.session.busy);
    button.addEventListener("click", () => operate("decision", "/api/cases/" + currentCase.id + "/decision", { key: question.key, choice }));
    buttons.append(button);
  }
  panel.append(buttons);
}

function timestamp(value) {
  return typeof value === "number" ? value * 1000 : Date.parse(value);
}

function logEntry({ text, label, time, details }) {
  const item = element("li");
  const row = element("div", "timeline-entry");
  const date = element("time", "", Number.isFinite(time) ? timeFormat.format(time) : "–");
  if (Number.isFinite(time)) date.dateTime = new Date(time).toISOString();
  const message = element("div");
  message.append(element("p", "", text), element("small", "", label));
  if (details) {
    const disclosure = element("details");
    const summary = element("summary", "timeline-summary");
    summary.append(row);
    disclosure.append(summary, element("pre", "", JSON.stringify(details, null, 2)));
    item.append(disclosure);
  } else {
    item.append(row);
  }
  row.prepend(date);
  row.append(message);
  return item;
}

function renderTimeline() {
  const state = ui.session;
  const { currentCase } = focus();
  const list = $("timeline");
  const mode = $("log-filter").value;
  const entries = [];
  if (mode === "all" || mode === "sources") {
    for (const event of state?.events ?? []) {
      entries.push({ time: timestamp(event.at), label: event.source, text: event.message });
    }
    for (const run of state?.runs ?? []) {
      entries.push({
        time: timestamp(run.finished_at), label: "observer · " + run.status,
        text: "Observed " + Object.keys(run.source_results).join(", "),
        details: { run_id: run.id, status: run.status, sources: run.source_results },
      });
    }
    for (const event of state?.workbench_events ?? []) {
      entries.push({
        time: timestamp(event.occurred_at), label: "workbench · " + event.outcome,
        text: event.kind + " · " + event.subject_id + (event.record_count ? " · " + event.record_count + " records" : ""),
        details: event,
      });
    }
  }
  if (mode === "all" || mode === "case") {
    for (const event of currentCase?.events ?? []) {
      entries.push({
        time: timestamp(event.occurred_at), label: pretty(event.type) + " · " + event.actor,
        text: event.text, details: event.data && Object.keys(event.data).length ? event.data : undefined,
      });
    }
  }
  if (mode === "traces") {
    for (const span of currentCase?.spans ?? []) {
      const duration = (span.ended_at - span.started_at) * 1000;
      const usage = span.tokens_in != null && span.tokens_out != null ? " · " + span.tokens_in + " in / " + span.tokens_out + " out" : "";
      entries.push({
        time: timestamp(span.started_at), label: "trace · " + span.kind,
        text: span.name + (Number.isFinite(duration) ? " · " + Math.round(duration) + " ms" : "") + usage,
        details: span,
      });
    }
    for (const action of currentCase?.actions ?? []) {
      entries.push({
        time: timestamp(action.verified_at ?? action.dispatched_at ?? action.created_at), label: "playbook · " + action.status,
        text: action.playbook + (action.verified_at ? " · verified" : ""),
        details: action,
      });
    }
  }
  $("log-count").textContent = entries.length ? entries.length + " events" : "";
  entries.sort((a, b) => b.time - a.time);
  list.replaceChildren(...entries.map(logEntry));
  if (!entries.length) {
    const item = element("li");
    item.append(empty(state ? "No events in this view yet." : "Start a baseline to populate the timeline."));
    list.append(item);
  }
}

function render() {
  renderEnvironment();
  renderScenarios();
  renderGraph();
  renderModel();
  renderMetrics();
  renderDecision();
  renderTimeline();
}

async function operate(kind, path, data) {
  if (ui.pending) return;
  ui.pending = kind;
  $("error").hidden = true;
  render();
  const poll = kind === "baseline" ? null : setInterval(async () => {
    try {
      const state = await api("/api/session");
      if (ui.pending === kind && state?.busy && state.samples.length !== ui.session?.samples.length) {
        ui.session = state;
        render();
      }
    } catch { /* The mutation response reports operation errors. */ }
  }, 650);
  try {
    ui.session = await api(path, "POST", data);
    if (kind === "baseline") {
      ui.node = ui.model = null;
    }
    $("announcement").textContent = kind === "baseline" ? "Baseline environment ready." :
      kind === "scenario" ? "Scenario ran. Review the assessment and case activity." : "Decision recorded. Case activity updated.";
  } catch (error) {
    $("error").textContent = error.message;
    $("error").hidden = false;
    ui.session = await api("/api/session").catch(() => ui.session);
  } finally {
    clearInterval(poll);
    ui.pending = null;
    render();
  }
}

let checkingSlack = false;
setInterval(async () => {
  const current = ui.session;
  if (checkingSlack || ui.pending || !ui.config?.slack_interactive ||
      !current?.cases.some((item) => item.status.startsWith("waiting_"))) return;
  checkingSlack = true;
  try {
    const state = await api("/api/session");
    if (!ui.pending && ui.session === current && state?.id === current.id &&
        (state.samples.length !== current.samples.length || state.busy !== current.busy)) {
      ui.session = state;
      render();
    }
  } catch { /* A transient refresh failure leaves the current case visible. */ }
  finally { checkingSlack = false; }
}, 1500);

$("environment").addEventListener("click", () => operate("baseline", "/api/session"));
$("hero-action").addEventListener("click", () => {
  if (ui.session) {
    $("scenarios").scrollIntoView();
    $("scenario-options").querySelector('[aria-pressed="true"]')?.focus({ preventScroll: true });
  } else {
    operate("baseline", "/api/session");
  }
});
$("public-view").addEventListener("change", (event) => {
  ui.anonymous = event.target.checked;
  ui.node = ui.model = null;
  render();
});
$("run-scenario").addEventListener("click", () => {
  ui.node = ui.model = null;
  operate("scenario", "/api/scenarios/" + publicVariant());
});
$("log-filter").addEventListener("change", renderTimeline);
$("recorded-option").addEventListener("click", () => {
  ui.model = "recorded";
  renderModel();
});
$("current-option").addEventListener("click", () => {
  ui.model = "current";
  renderModel();
});

Promise.all([api("/api/config"), api("/api/session")]).then(([config, state]) => {
  ui.config = config;
  ui.session = state;
  render();
}).catch((error) => {
  $("error").textContent = error.message;
  $("error").hidden = false;
});
render();
