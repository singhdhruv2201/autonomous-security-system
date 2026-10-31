import { escapeHTML as h } from './model.mjs';

const count = value => value == null ? 'Unknown' : Number(value).toLocaleString();
const date = value => value ? new Date(value).toLocaleString() : 'Unknown';

export function activityStatus(health) {
  if (!health) return {label:'Coverage unknown',tone:'warn'};
  if (!health.enabled) return {label:'Activity is disabled',tone:'neutral'};
  if (!health.worker_running || !health.coverage_complete || health.source_resets || !health.sources?.length ||
      health.sources.some(s => s.stale || s.queue?.dead_letter > 0)) {
    return {label:'Coverage needs attention',tone:'warn'};
  }
  return {label:'Up to date',tone:'good'};
}

function sourceCard(source) {
  const queue = source.queue;
  const alerts = [source.stale && 'Source check is stale',source.history_expired && 'Source history has expired',
    source.error && `Recovery: ${source.error.replaceAll('_',' ')}`,
    source.processing_error && 'Processing failed',source.capture_enabled === 0 && 'CRM capture is disabled'].filter(Boolean);
  return `<section class="activity-source"><div class="activity-source-title"><strong>${h(source.source)} ${source.active ? '' : '(previous or unconfirmed source)'}</strong>
    <small>Last checked ${h(date(source.checked_at ? source.checked_at*1000 : null))}</small></div>
    <dl class="activity-metrics">
    <div><dt>Received through</dt><dd>${count(source.received_cursor)}</dd></div>
    <div><dt>Processed through</dt><dd>${count(source.processed_cursor)}</dd></div>
    <div><dt>Source watermark</dt><dd>${count(source.high_watermark)}</dd></div>
    <div><dt>Missing events</dt><dd>${count(source.missing_events)}</dd></div>
    <div><dt>Awaiting processing</dt><dd>${count(source.pending_processing)}</dd></div>
    <div><dt>Dead letters</dt><dd>${count(queue?.dead_letter)}</dd></div>
    </dl><p class="activity-source-note">Delivery queue: ${count(queue?.pending)} pending · ${count(queue?.inflight)} in flight · ${count(queue?.delivered)} delivered.
    Retained: ${count(source.retained_events)} / ${count(source.capacity)}. Processing lag: ${count(source.processing_lag_events)} events.
    ${source.oldest_pending_seconds != null ? `Oldest pending: ${Math.round(source.oldest_pending_seconds)}s.` : ''}</p>
    ${source.missing_ranges?.length ? `<p class="activity-warning">Missing sequences: ${source.missing_ranges.map(([a,b]) => `${count(a)}–${count(b)}`).join(', ')}. Checkpoints will not skip these gaps.</p>` : ''}
    ${alerts.length ? `<p class="activity-warning">${alerts.map(h).join(' · ')}</p>` : ''}
    <small class="activity-epoch">Source instance ${h(source.source_instance_id)}</small></section>`;
}

function requestRow(item) {
  const stages = item.stages ?? [];
  const scan = item.scan_status === 'not_run' || !item.scan_status ? 'Not run' : item.scan_status;
  return `<details class="activity-request"><summary>
    <span><strong>${h(item.action ?? 'Request')}</strong><small>${h(date(item.occurred_at))}</small></span>
    <span class="activity-actor">${h(item.actor_id ?? 'Unknown actor')}</span>
    <span>${h(item.outcome ?? 'unknown')}<small>HTTP ${count(item.status_code)}</small></span>
    <span>${h(scan)}<small>Response scan</small></span>
    <span>${stages.length} stages<small>${h(item.completion === 'unknown' || !item.completion ? 'Completion unknown' : item.completion)}</small></span>
    </summary><div class="activity-evidence"><p>Request <code>${h(item.request_id)}</code> · Source <code>${h(item.source_instance_id)}</code></p>
    <ol>${stages.map(stage => `<li><strong>${h(stage.event_type)}</strong><span>Sequence ${count(stage.sequence)} · ${h(date(stage.occurred_at))}</span>
    <dl>${['status_code','outcome','result_record_count','serialized_bytes','emitted_bytes','duration_ms','completion','scan_status','release_decision'].filter(key => stage[key] != null).map(key =>
      `<div><dt>${h(key.replaceAll('_',' '))}</dt><dd>${h(stage[key])}</dd></div>`).join('')}</dl>${(stage.policy_decisions ?? []).map(d => `<p><strong>${h(d.rule)}</strong> · ${h(d.mode)} · ${h(d.result)} · ${h(d.reason)}</p>`).join('')}</li>`).join('')}</ol>
    <p>Missing stages remain unknown. A completed send does not prove the caller consumed the response. An allowed operation is not a security verdict.</p></div></details>`;
}

export function renderActivity(page, health, {error=false,loading=false,filter='',actor=''}={}) {
  const status = activityStatus(health);
  return `<section class="page-heading"><div><div class="eyebrow">HELIO SYSTEMS / CRM ACTIVITY</div><h1>Activity</h1>
    <p>Requests, responses, and the evidence between them.</p></div><button class="button" data-action="activity-reload" ${loading ? 'disabled' : ''}>${loading ? 'Loading…' : 'Refresh activity'}</button></section>
    ${error ? '<div class="connection-banner" role="status">Activity unavailable. Displayed evidence may be stale; coverage cannot be confirmed.</div>' : ''}
    <section class="activity-health"><div class="activity-source-title"><h2>Delivery & processing</h2><span class="badge ${error ? 'warn' : status.tone}">${error ? 'Coverage unknown' : h(status.label)}</span></div>
    ${health?.source_resets ? '<p class="activity-warning">A source reset was detected. Earlier coverage remains separate and needs review.</p>' : ''}
    ${health?.recovery_error ? `<p class="activity-warning">Recovery: ${h(health.recovery_error.replaceAll('_',' '))}. Coverage cannot be confirmed.</p>` : ''}
    ${(health?.sources ?? []).map(sourceCard).join('') || `<p>${health?.enabled === false ? 'Enable the activity pipeline to collect CRM requests.' : 'Awaiting a verified CRM source check.'}</p>`}
    <p class="activity-source-note">Receipt and processing are separate checkpoints. Expand a request for its policy decisions and scan results. Current policy modes are shown in Policies.</p></section>
    <section class="activity-list"><div class="activity-list-heading"><h2>Requests <span class="count">${count(page?.total)}</span></h2>
    <form id="activity-filter-form"><label>Outcome <select id="activity-outcome" name="outcome">${[['','All outcomes'],['allowed','Allowed'],['denied','Denied'],['failed','Failed']].map(([value,label]) => `<option value="${value}" ${value===filter ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
    <label>Actor ID <input id="activity-actor" name="actor" maxlength="200" value="${h(actor)}" placeholder="All actors"></label><button class="button" type="submit">Apply</button></form></div>
    <div class="activity-labels"><span>Activity / time</span><span>Actor</span><span>Operation / response</span><span>Checks</span><span>Evidence</span></div>
    ${(page?.items ?? []).map(requestRow).join('') || `<div class="empty-state"><h3>${loading ? 'Loading activity…' : 'No matching requests'}</h3><p>${page?.enabled === false ? 'Activity is disabled.' : 'New requests appear automatically after durable processing.'}</p></div>`}
    ${page?.next_before ? `<div class="activity-more"><button class="button" data-action="activity-more" ${loading ? 'disabled' : ''}>Load older requests</button></div>` : ''}</section>`;
}
