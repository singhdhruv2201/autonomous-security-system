import test from 'node:test';
import assert from 'node:assert/strict';
import { activityStatus, renderActivity } from '../../src/helio_dashboard/static/activity.mjs';

test('activity status never treats missing, stale or disabled evidence as complete', () => {
  assert.equal(activityStatus(null).tone, 'warn');
  assert.equal(activityStatus({enabled:false}).label, 'Activity is disabled');
  assert.equal(activityStatus({enabled:true,coverage_complete:false}).tone, 'warn');
  assert.equal(activityStatus({enabled:true,coverage_complete:true,sources:[]}).tone, 'warn');
  const healthy = {enabled:true,coverage_complete:true,worker_running:true,sources:[{active:true,stale:false,queue:{dead_letter:0}}]};
  assert.equal(activityStatus(healthy).label, 'Up to date');
  assert.equal(activityStatus({...healthy,source_resets:1}).tone, 'warn');
  assert.equal(activityStatus({...healthy,sources:[{active:true,queue:{dead_letter:2}}]}).tone, 'warn');
});

test('one request has four stages, unrun scanners remain explicit, metadata is escaped', () => {
  const page = {enabled:true,total:1,next_before:null,items:[{
    request_id:'request-1',source_instance_id:'epoch',action:'customers:read',actor_id:'<img src=x onerror=alert(1)>',
    occurred_at:'2026-10-31T01:00:00Z',outcome:'allowed',completion:'completed',scan_status:'not_run',status_code:200,
    stages:['request.received','operation.completed','response.prepared','response.completed'].map(event_type => ({event_type,sequence:1,status_code:200,scan_status:'not_run'})),
  }]};
  const html = renderActivity(page,null);
  assert.equal((html.match(/class="activity-request"/g) ?? []).length,1);
  for (const stage of page.items[0].stages) assert.ok(html.includes(stage.event_type));
  assert.ok(html.includes('Not run'));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img'));
});

test('outages and incomplete requests remain visible and pagination is explicit', () => {
  assert.ok(renderActivity(null,null,{error:true}).includes('Activity unavailable'));
  const html = renderActivity({enabled:true,items:[{request_id:'r',source_instance_id:'s',stages:[],completion:'unknown',scan_status:'not_run'}],next_before:10,total:2},null);
  assert.ok(html.includes('Completion unknown'));
  assert.ok(html.includes('activity-more'));
});
