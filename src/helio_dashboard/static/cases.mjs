import {escapeHTML as h, formatTimestamp as stamp, riskPath} from './model.mjs';
const label = value => h(String(value ?? 'unknown').replaceAll('_',' '));

export function renderCase(value) {
  if (!value) return '<p class="muted">Case details unavailable. Retry to load the recorded timeline.</p>';
  const actions = value.actions ?? [], events = (value.events ?? []).slice().sort((a,b)=>a.occurred_at-b.occurred_at);
  const spans = value.spans ?? [];
  return `<section class="drawer-section"><h3>Case timeline</h3><p><strong>${label(value.status)}</strong> · ${h(stamp(value.created_at))}</p>${value.summary ? `<p>${h(value.summary)}</p>` : ''}<p class="muted">Decisions stay in Slack or the authorized API. This view is read-only.</p>
    ${value.question && value.status?.startsWith('waiting_') ? `<div class="drawer-callout">Awaiting ${h(value.question.addressed_to ?? value.question.role)} · deadline ${h(stamp(value.question.deadline))}</div>` : ''}
    <ol class="timeline">${events.map(e=>`<li><strong>${label(e.type)}</strong><small>${h(stamp(e.occurred_at))} · ${h(e.actor)}</small><p>${h(e.text)}</p></li>`).join('') || '<li>No case events recorded yet.</li>'}</ol></section>
    <section class="drawer-section"><h3>Actions</h3>${actions.map(a=>{
      const verified = a.verified_at != null;
      const undo = actions.find(u=>u.undo_of===a.id);
      const historical = a.status==='undone' || Boolean(undo);
      return `<article class="case-action"><strong>${h(a.playbook)}</strong><p>${h(a.target)}</p><span class="badge">${label(a.status)}</span> <span class="badge ${verified && !historical && a.status==='executed' ? 'good' : 'neutral'}">${verified ? historical ? 'Previously verified' : 'Verified' : 'Not verified'}</span><small>${h(stamp(a.created_at))}${verified ? ` · Verification recorded ${h(stamp(a.verified_at))}` : ''}</small>${a.undo_of ? `<p>Undo of ${h(a.undo_of)}</p>` : ''}${historical ? '<p>The original action is no longer a current-state guarantee. See the undo record.</p>' : ''}</article>`;
    }).join('') || '<p class="muted">No actions recorded.</p>'}</section>
    <details class="case-traces"><summary>Trace (${spans.length}${spans.length===500 ? '+' : ''})</summary>${spans.length===500 ? '<p>Showing the first 500 trace entries.</p>' : ''}<ol>${spans.map(s=>`<li><strong>${h(s.kind)} · ${h(s.name)}</strong><small>${h(stamp(s.started_at))} · ${s.ended_at != null ? `${h(Math.max(0,Math.round((s.ended_at-s.started_at)*1000)))} ms` : 'In progress'}</small>${s.tokens_in != null || s.tokens_out != null ? `<p>Tokens: ${h(s.tokens_in ?? '—')} in / ${h(s.tokens_out ?? '—')} out</p>` : ''}${s.error ? `<p>${h(s.error)}</p>` : ''}</li>`).join('') || '<li>No trace entries recorded.</li>'}</ol></details>`;
}

export function renderRiskPath(finding, graph) {
  const path = riskPath(finding,graph), byId = new Map(path.nodes.map(n=>[n.id,n]));
  const name = id => { const n=byId.get(id); return h(n?.name ?? n?.source_id ?? 'Unavailable entity'); };
  return `<section class="drawer-section"><h3>Risk path</h3>${path.incomplete ? '<p class="path-warning">The current graph does not contain the complete recorded path. Relationships may have changed, or this graph view may be truncated.</p>' : ''}<ol class="risk-path">${path.edges.map(e=>`<li><strong>${name(e.start)}</strong><span>${label(e.kind)} →</span><strong>${name(e.end)}</strong></li>`).join('') || '<li>No matching relationships in this graph view.</li>'}</ol><button class="text-button" data-action="show-risk" data-id="${h(finding.id)}">Show in graph</button></section>`;
}

export function renderWorkflowCatalog(catalog) {
  return `<section class="panel policy-card"><h2>How Awasi decides</h2><p>First matching rule wins. The model cannot authorize actions.</p><ol class="decision-rules">${(catalog?.decision_rules ?? []).map(r=>`<li><strong>${h(r.order)}. ${h(r.name)}</strong><p>${h(r.description)}</p></li>`).join('') || '<li>Decision rules unavailable.</li>'}</ol></section><section class="panel policy-card"><h2>Available playbooks</h2>${(catalog?.playbooks ?? []).map(p=>`<article class="case-action"><strong>${h(p.id)}</strong> <span class="badge">${p.reversible ? 'Reversible' : 'Irreversible'}</span><p>${h(p.description)}</p></article>`).join('') || '<p>Playbooks unavailable.</p>'}</section>`;
}
