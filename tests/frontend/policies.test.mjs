import test from 'node:test';
import assert from 'node:assert/strict';
import {securityState, formatTimestamp} from '../../src/helio_dashboard/static/model.mjs';
import {renderActivity} from '../../src/helio_dashboard/static/activity.mjs';

test('finding evidence formats both activity ISO times and observation epoch times',()=>{
 const iso='2026-10-31T12:30:00Z';
 const formatted=formatTimestamp(iso);
 assert.equal(formatted,formatTimestamp(Date.parse(iso)/1000));
 assert.doesNotMatch(formatted,/Invalid Date|Not observed/);
 assert.equal(formatTimestamp(null),'Not observed');
 assert.equal(formatTimestamp('invalid'),'Not observed');
 assert.doesNotMatch(formatTimestamp(0),/Not observed/);
});

test('unknown policy coverage prevents a clear security verdict',()=>{
 const state={errors:{},overview:{coverage_complete:true},findings:{items:[]},policies:{items:[{enabled:true,coverage_status:'unknown'}]}};
 assert.equal(securityState(state).tone,'warn');
});

test('activity shows actual policy decisions and escapes evidence',()=>{
 const html=renderActivity({items:[{stages:[{event_type:'operation.completed',policy_decisions:[{rule:'sensitive_content',result:'deny',mode:'enforce',reason:'<script>'}]}]}]},null);
 assert.match(html,/sensitive_content/);
 assert.match(html,/&lt;script&gt;/);
 assert.doesNotMatch(html,/policies are not enabled/);
});

test('policy cards describe observation and graph coverage without CRM runtime',async()=>{
 let html=''; const element={addEventListener(){},contains(){return false;},get innerHTML(){return html;},set innerHTML(value){html=value;}};
 globalThis.document={hidden:false,activeElement:null,addEventListener(){},querySelector(s){return ['#main','#navigation','#company-toggle'].includes(s)?element:null;}};
 globalThis.location={hash:'#policies'}; globalThis.window={addEventListener(){}}; globalThis.setInterval=()=>{};
 globalThis.fetch=async()=>({ok:true,json:async()=>({errors:{},policies:{items:[
  {id:'a',name:'Inactive owner <test>',mode:'enforce',enabled:true,coverage_status:'current',conditions:['Owner is inactive'],sources:['directory','crm']},
  {id:'b',name:'Unknown policy',enabled:true,coverage_status:'unknown',conditions:[],sources:['directory','crm']},
 ]}})});
 await import('../../src/helio_dashboard/static/app.mjs'); await new Promise(r=>setImmediate(r));
 assert.match(html,/Observing/); assert.match(html,/Coverage unknown/);
 assert.match(html,/Inactive owner &lt;test&gt;/); assert.match(html,/Owner is inactive/);
 assert.doesNotMatch(html,/Enforcing|Observe or enforce|CRM-reported|Disabled|configuration/);
});


test('retired CRM worker runtime does not change graph observation coverage', () => {
 const state = {errors:{}, overview:{coverage_complete:true, policy_runtime:{identity_publisher:{value:{status:'unavailable'},updated_at:0}}}, findings:{items:[]}, policies:{items:[{enabled:true,coverage_status:'current'}]}};
 assert.equal(securityState(state).tone, 'good');
});
