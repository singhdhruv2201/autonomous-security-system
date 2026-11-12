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
    (n) => n.kind === "AccessGrant" && n.principal_id === (agent.crm_principal_id ?? agent.source_id),
  );
  if (agent.status === 'paused') return {label:'Agent paused',tone:'neutral',grants};
  const principal = inventory.find(n=>n.source_id===agent.crm_principal_id);
  if (agent.principal_status === 'disabled' || principal?.principal_status === 'disabled')
    return {label:'Access paused',tone:'neutral',grants};
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
    (f) => f.status === "open" && (f.evidence?.app_id === app.source_id || ids.has(f.evidence?.grant_id)),
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
export const GRAPH_LANES = [
  ['Team', 'TEAMS'], ['Employee', 'PEOPLE'],
  ['App', 'APPS'], ['Agent', 'WORKBENCH AGENTS'], ['Principal', 'CRM IDENTITIES'],
  ['AccessGrant', 'GRANTS'], ['DataResource', 'DATA'], ['Connection', 'CONNECTIONS'], ['Outside', 'OUTSIDE'],
];
const graphLane = n => n.kind === 'ServiceAccount' || (n.kind === 'Agent' && n.source === 'crm') ? 'Principal'
  : ['Destination','Audience'].includes(n.kind) ? 'Outside' : n.kind;
export function graphNodeLabel(n) {
  const title = String(n.kind === 'Connection' ? n.host ?? n.name ?? n.source_id : n.name ?? n.source_id ?? n.id).replace(/^grant-/, '');
  const subtitle = n.kind === 'Agent' ? n.source === 'crm' ? 'CRM identity' : 'Workbench agent'
    : n.kind === 'ServiceAccount' ? 'Service identity' : n.kind === 'App' ? n.source === 'workbench' ? 'Workbench app' : 'CRM application'
    : n.kind === 'Employee' ? n.status === 'inactive' ? 'Inactive employee' : 'Employee'
    : n.kind === 'AppAccount' ? 'CRM account' : n.kind === 'Connection' ? 'Outbound connection'
    : n.kind === 'Destination' ? 'External destination' : n.kind === 'Audience' ? 'Public audience'
    : n.kind === 'DataResource' ? 'Data resource' : n.kind === 'AccessGrant' ? 'Access grant' : n.kind;
  return {title, subtitle};
}
export function personAccounts(person, inventory, graph) {
  if (person.kind !== 'Employee') return [];
  const linked = new Set((graph?.edges ?? []).filter(e=>e.kind==='IDENTITY_OF' && e.end===person.id).map(e=>e.start));
  const all = new Map([...(graph?.nodes ?? []),...inventory].map(n=>[n.id,n]));
  return [...all.values()].filter(n=>n.kind==='AppAccount' && n.source==='crm' &&
    (linked.has(n.id) || (person.source_id && n.owner_employee_id===person.source_id)))
    .sort((a,b)=>a.id.localeCompare(b.id));
}
export function graphSize(positions) {
  return {width:GRAPH_LANES.length*220, height:Math.max(640, ...positions.map(n=>n.y+80))};
}
export function graphLayout(nodes, edges = []) {
  // People anchor the rows; subsequent lanes follow their strongest access relationship.
  // Many-to-one relationships use the median, with collision resolution within each lane.
  const sorted = nodes.filter(n=>n.kind!=='AppAccount').sort((a,b)=>String(a.id).localeCompare(String(b.id)));
  const byId = new Map(sorted.map(n=>[n.id,n]));
  const neighbors = new Map(sorted.map(n=>[n.id,[]]));
  for (const e of edges) if (byId.has(e.start) && byId.has(e.end)) {
    neighbors.get(e.start).push({node:byId.get(e.end),kind:e.kind});
    neighbors.get(e.end).push({node:byId.get(e.start),kind:e.kind});
  }
  const groups = new Map(GRAPH_LANES.map(([kind])=>[kind, sorted.filter(n=>graphLane(n)===kind)]));
  const unknown = sorted.filter(n=>!groups.has(graphLane(n)));
  groups.get('App').push(...unknown);
  const count = Math.max(1,...[...groups.values()].map(g=>g.length));
  const bottom = Math.max(560, 100+(count-1)*72);
  const positions = new Map();
  const median = values => {values.sort((a,b)=>a-b); const mid=Math.floor(values.length/2);return values.length%2?values[mid]:(values[mid-1]+values[mid])/2;};
  const preferred = {Team:['MEMBER_OF'], AppAccount:['IDENTITY_OF'], App:['OWNS'], Agent:['OWNS'],
    Principal:['ACTS_AS','OWNS'], AccessGrant:['HAS_GRANT'],DataResource:['ON','STORES'],Connection:['HAS_CONNECTION'],Outside:['TO','EXPOSED_TO']};
  const teamName = n => neighbors.get(n.id).find(r=>r.kind==='MEMBER_OF')?.node.name ?? '';
  for (const lane of ['Employee','Team',...GRAPH_LANES.slice(2).map(([kind])=>kind)]) {
    const items=groups.get(lane);
    if(lane==='Employee') items.sort((a,b)=>teamName(a).localeCompare(teamName(b)) || String(a.name??a.id).localeCompare(String(b.name??b.id)) || a.id.localeCompare(b.id));
    const targets=items.map((n,i)=>{
      let values=[];
      for(const kind of preferred[lane]??[]) {
        values=neighbors.get(n.id).filter(r=>r.kind===kind&&positions.has(r.node.id)).map(r=>positions.get(r.node.id).y);
        if(values.length) break;
      }
      return {n, desired:values.length?median(values):items.length===1?(100+bottom)/2:100+i*(bottom-100)/(items.length-1)};
    }).sort((a,b)=>a.desired-b.desired||a.n.id.localeCompare(b.n.id));
    // Isotonic blocks keep shared-parent groups centered, with at least one card-row gap.
    const blocks=[];
    targets.forEach((t,i)=>{
      blocks.push({from:i,to:i,mean:t.desired-i*72,count:1});
      while(blocks.length>1 && blocks.at(-2).mean>blocks.at(-1).mean){
        const b=blocks.pop(),a=blocks.pop(),count=a.count+b.count;
        blocks.push({from:a.from,to:b.to,mean:(a.mean*a.count+b.mean*b.count)/count,count});
      }
    });
    const x=110+220*GRAPH_LANES.findIndex(([kind])=>kind===lane);
    for(const block of blocks) for(let i=block.from;i<=block.to;i++) {
      const base=Math.max(100,Math.min(bottom-(targets.length-1)*72,block.mean));
      positions.set(targets[i].n.id,{...targets[i].n,x,y:base+i*72});
    }
  }
  return sorted.map(n=>positions.get(n.id));
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


export function findingCases(findingId, page) {
  return (page?.items ?? []).filter(c => c.finding_id === findingId)
    .slice().sort((a,b) => b.created_at - a.created_at || String(b.id).localeCompare(String(a.id)));
}

export function riskPath(finding, graph) {
  const e = finding?.evidence ?? {}, all = graph?.nodes ?? [];
  const publicApp = finding?.rule === 'public_app_confidential_data';
  const external = finding?.rule === 'agent_unapproved_destination';
  const specs = [
    ['owner', ['Employee'], e.employee_id ?? e.owner_employee_id],
    ['asset', publicApp ? ['App'] : ['Agent'], publicApp ? e.app_id : e.agent_id],
    ['principal', ['Agent','ServiceAccount'], e.principal_id],
    ['grant', ['AccessGrant'], e.grant_id], ['resource', ['DataResource'], e.resource_id],
    ['connection', ['Connection'], external ? e.connection_id : null],
    ['outside', publicApp ? ['Audience'] : ['Destination'], publicApp ? 'public' : external ? e.host : null],
  ];
  const selected = {}, missing = [];
  for (const [role,kinds,id] of specs) {
    if (!id) continue;
    selected[role] = all.find(n => kinds.includes(n.kind) && n.source_id === id);
    if (!selected[role]) missing.push(role);
  }
  const pairs = [['owner','asset','OWNS'],
    ...(publicApp || external ? [['asset','principal','ACTS_AS'],['principal','grant','HAS_GRANT']] : [['asset','grant','HAS_GRANT']]),
    ['grant','resource','ON'], ...(publicApp ? [['asset','outside','EXPOSED_TO']] : []),
    ...(external ? [['asset','connection','HAS_CONNECTION'],['connection','outside','TO']] : [])];
  const edges = [];
  for (const [start,end,kind] of pairs) {
    const match = (graph?.edges ?? []).find(edge => edge.start === selected[start]?.id && edge.end === selected[end]?.id && edge.kind === kind);
    if (match) edges.push(match); else missing.push(`${start} → ${end}`);
  }
  return {nodes:[...new Map(Object.values(selected).filter(Boolean).map(n=>[n.id,n])).values()], edges,
    incomplete:Boolean(graph?.truncated || missing.length), missing};
}
