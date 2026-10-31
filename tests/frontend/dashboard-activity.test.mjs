import test from 'node:test';
import assert from 'node:assert/strict';

test('overview points to request evidence without claiming activity is unimplemented', async () => {
  let html = '';
  const element = {addEventListener() {}, contains() { return false; },
    get innerHTML() { return html; }, set innerHTML(value) { html = value; }};
  globalThis.document = {hidden:false, activeElement:null, addEventListener() {},
    querySelector(selector) { return ['#main','#navigation','#company-toggle'].includes(selector) ? element : null; }};
  globalThis.location = {hash:'#dashboard'};
  globalThis.window = {addEventListener() {}};
  globalThis.setInterval = () => {};
  globalThis.fetch = async () => ({ok:true,json:async () => ({errors:{},overview:{observation:{activity_ingestion:true}}})});
  await import('../../src/helio_dashboard/static/app.mjs');
  await new Promise(resolve => setImmediate(resolve));
  assert.doesNotMatch(html, /Request-level events not collected yet/);
  assert.match(html, /Request evidence and delivery health/);
  assert.match(html, /data-page="activity"/);
});
