import test from 'node:test';
import assert from 'node:assert/strict';

test('snapshot refresh leaves an open activity view and draft filters intact', async () => {
  const intervals = [];
  let paints = 0;
  const element = {
    addEventListener() {}, contains() { return false; },
    get innerHTML() { return ''; }, set innerHTML(value) { paints++; },
  };
  globalThis.document = {
    hidden:false, activeElement:null, addEventListener() {},
    querySelector(selector) { return ['#main','#navigation','#company-toggle'].includes(selector) ? element : null; },
  };
  globalThis.location = {hash:'#activity'};
  globalThis.window = {addEventListener() {}};
  globalThis.setInterval = (callback, ms) => { intervals.push({callback,ms}); };
  globalThis.fetch = async url => ({ok:true,json:async () => url==='/api/state' ? {errors:{}} :
    url==='/api/activity/health' ? {enabled:true,sources:[]} : {enabled:true,items:[],total:0}});
  await import('../../src/helio_dashboard/static/app.mjs');
  await new Promise(resolve => setImmediate(resolve));
  const before = paints;
  intervals.find(i => i.ms===15000).callback();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(paints,before,'Graph polling must not replace activity DOM or discard user input');
});
