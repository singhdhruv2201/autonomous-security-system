export const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export const entities = (state) => state?.inventory?.items ?? [];
export const findings = (state) => state?.findings?.items ?? [];
export const initials = (name) =>
  String(name ?? "?")
    .split(/[\s:-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((x) => x[0])
    .join("")
    .toUpperCase();
export function securityState(state) {
  const open = Math.max(
    state?.overview?.finding_counts?.open ?? 0,
    findings(state).filter((f) => f.status === "open").length,
  );
  if (open)
    return {
      tone: "danger",
      label: `${open} finding${open === 1 ? "" : "s"} need${open === 1 ? "s" : ""} attention`,
      description:
        "Review the evidence and affected access before taking action.",
    };
  const pending = ["queued", "running"].includes(
    state?.overview?.latest_sync_run?.status,
  );
  if (
    !state?.overview ||
    !state?.findings ||
    Object.keys(state.errors ?? {}).length ||
    !state.overview.coverage_complete ||
    !state?.policies?.items?.some((p) => p.enabled) ||
    state?.policies?.items?.some((p) => p.coverage_status === 'unknown') ||
    pending
  )
    return {
      tone: "warn",
      label: pending ? "Checking your company" : "Visibility needs attention",
      description: pending
        ? "An observation is in progress. The last known evidence remains available."
        : "Fresh evidence is needed before we can verify the configured checks.",
    };
  return {
    tone: "good",
    label: "All observed checks clear",
    description:
      "No open findings across your connected systems and configured policies.",
  };
}
export function agentAccess(agent, inventory) {
  const grants = inventory.filter(
    (n) => n.kind === "AccessGrant" && n.principal_id === agent.source_id,
  );
  if (!grants.length)
    return { label: "No observed grants", tone: "neutral", grants };
  if (grants.some((g) => g.status === "active" && g.scopes?.length))
    return { label: "Access granted", tone: "good", grants };
  if (grants.every((g) => g.status === "revoked"))
    return { label: "Access revoked", tone: "neutral", grants };
  return { label: "No permitted scopes", tone: "neutral", grants };
}
export function appFindings(app, inventory, items) {
  const ids = new Set(
    inventory
      .filter((n) => n.kind === "AccessGrant" && n.app_id === app.source_id)
      .map((n) => n.source_id),
  );
  return items.filter(
    (f) => f.status === "open" && ids.has(f.evidence?.grant_id),
  );
}
export function monitoringRows(state) {
  const inventory = entities(state),
    rows = [];
  for (const node of inventory) {
    if (node.kind === "Employee")
      rows.push({
        id: node.id,
        category: "People",
        title: `${node.name} is ${node.status}`,
        detail: `${node.title ?? "Employee"} · ${node.team_id ?? "Company directory"}`,
        at: node.last_observed_at,
        mode: "Observed state",
        tone: node.status === "inactive" ? "warn" : "neutral",
        priority: node.status === "inactive" ? 3 : 0,
        entity: node,
      });
    if (node.kind === "Agent") {
      const access = agentAccess(node, inventory);
      const owner = inventory.find(
        (n) => n.kind === "Employee" && n.source_id === node.owner_employee_id,
      );
      rows.push({
        id: node.id,
        category: "Agents",
        title: `${node.name} · ${access.label.toLowerCase()}`,
        detail: `Owned by ${owner?.name ?? node.owner_employee_id} · CRM permissions`,
        at: node.last_observed_at,
        mode: "Observed state",
        tone: "neutral",
        priority: 2,
        entity: node,
      });
    }
    if (node.kind === "App")
      rows.push({
        id: node.id,
        category: "Apps",
        title: state.overview?.sources?.[node.source]?.fresh
          ? `${node.name} is connected`
          : `${node.name} · connection needs attention`,
        detail: `${inventory.filter((n) => ["AppAccount", "Agent", "ServiceAccount"].includes(n.kind) && state.graph?.edges?.some((e) => e.start === n.id && e.end === node.id && e.kind === "USES")).length} observed accounts · ${node.source}`,
        at: node.last_observed_at,
        mode: "Observed state",
        tone: "neutral",
        priority: 1,
        entity: node,
      });
  }
  for (const run of state?.runs?.items ?? [])
    rows.push({
      id: run.id,
      category: "Sync",
      title:
        run.status === "succeeded"
          ? "Company observation completed"
          : run.status === "partial"
            ? "Observation needs attention"
            : run.status === "running"
              ? "Observing connected systems"
              : run.status === "queued"
                ? "Observation queued"
                : `Observation ${run.status}`,
      detail:
        Object.entries(run.source_results ?? {})
          .map(([k, v]) => `${k}: ${v}`)
          .join(" · ") || "Directory and CRM",
      at: run.finished_at ?? run.started_at ?? run.created_at,
      mode: "Sync event",
      tone: ["failed", "partial", "interrupted"].includes(run.status)
        ? "warn"
        : "neutral",
      priority: 0.5,
    });
  return rows.sort(
    (a, b) => b.priority - a.priority || (b.at ?? 0) - (a.at ?? 0),
  );
}
export function graphLayout(nodes) {
  const lanes = {
    Team: 80,
    Employee: 255,
    AppAccount: 430,
    App: 610,
    Agent: 770,
    ServiceAccount: 770,
    AccessGrant: 930,
    DataResource: 1095,
  };
  const groups = Object.groupBy
    ? Object.groupBy(nodes, (n) => n.kind)
    : nodes.reduce((a, n) => ((a[n.kind] ??= []).push(n), a), {});
  return Object.entries(groups).flatMap(([kind, items]) =>
    items
      .slice()
      .sort((a, b) => a.id.localeCompare(b.id))
      .map((n, i) => {
        let y = 60 + ((i + 0.5) * 520) / items.length;
        if (kind === "Agent") y = 95 + ((i + 0.5) * 150) / items.length;
        if (kind === "ServiceAccount")
          y = 310 + ((i + 0.5) * 210) / items.length;
        return { ...n, x: lanes[kind] ?? 610, y };
      }),
  );
}

export function formatTimestamp(at) {
  if (at == null || at === '') return 'Not observed';
  const date = new Date(typeof at === 'number' ? at * 1000 : at);
  return Number.isFinite(date.getTime())
    ? date.toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      })
    : 'Not observed';
}

export function findingsVerified(state) {
  return Boolean(
    state?.findings &&
      state?.inventory &&
      state?.overview?.coverage_complete &&
      !Object.keys(state.errors ?? {}).length &&
      (state.findings.total ?? 0) <= findings(state).length &&
      (state.inventory.total ?? 0) <= entities(state).length,
  );
}
