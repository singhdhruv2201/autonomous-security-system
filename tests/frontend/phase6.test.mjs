import test from 'node:test';
import assert from 'node:assert/strict';
import * as model from '../../src/helio_dashboard/static/model.mjs';

export const scenario = (kind='public') => {
  const nodes=[['owner','Employee'],['app','App'],['principal','ServiceAccount'],['grant','AccessGrant'],['data','DataResource'],['public','Audience'],['agent','Agent'],['connection','Connection'],['outside','Destination'],['other','Destination']].map(([id,kind])=>({id,source_id:id,kind,name:id}));
  const edges=[['owns','owner','app','OWNS'],['acts','app','principal','ACTS_AS'],['grant-edge','principal','grant','HAS_GRANT'],['data-edge','grant','data','ON'],['exposed','app','public','EXPOSED_TO'],['agent-owner','owner','agent','OWNS'],['agent-principal','agent','principal','ACTS_AS'],['connect','agent','connection','HAS_CONNECTION'],['send','connection','outside','TO'],['unrelated','connection','other','TO']].map(([id,start,end,kind])=>({id,start,end,kind}));
  return {graph:{nodes,edges},finding:{id:'finding',rule:kind==='public'?'public_app_confidential_data':'agent_unapproved_destination',evidence:{employee_id:'owner',...(kind==='public'?{app_id:'app'}:{agent_id:'agent',connection_id:'connection',host:'outside'}),principal_id:'principal',grant_id:'grant',resource_id:'data'}}};
};

test('connections stay inside; destinations and public audience occupy distinct outside positions',()=>{
  const positions=model.graphLayout(scenario().graph.nodes);
  const byId=Object.fromEntries(positions.map(n=>[n.id,n]));
  assert.ok(byId.connection.x>byId.data.x);
  assert.ok(byId.outside.x>byId.connection.x);
  assert.equal(byId.public.x,byId.outside.x);
  assert.notEqual(byId.public.y,byId.outside.y);
  assert.ok(positions.every(n=>n.x<2200&&n.y<model.graphSize(positions).height));
});

for(const kind of ['public','external']) test(`${kind} highlights only the evidenced ownership, data and exposure path`,()=>{
  assert.equal(typeof model.riskPath,'function');
  const {graph,finding}=scenario(kind), path=model.riskPath(finding,graph);
  const expected=kind==='public'?['owns','acts','grant-edge','data-edge','exposed']:['agent-owner','agent-principal','grant-edge','data-edge','connect','send'];
  assert.deepEqual(path.edges.map(e=>e.id).sort(),expected.sort());
  assert.ok(!path.nodes.some(n=>n.id==='other'));
  assert.equal(path.incomplete,false);
  const partial=model.riskPath(finding,{...graph,edges:graph.edges.filter(e=>e.id!==expected[0]),truncated:true});
  assert.equal(partial.incomplete,true);
  assert.ok(!partial.edges.some(e=>e.id===expected[0]));
});

test('case timeline preserves chronology, verification, undo and escaping without actions',async()=>{
  const {renderCase}=await import('../../src/helio_dashboard/static/cases.mjs');
  const html=renderCase({id:'c',status:'needs_review',summary:'<script>bad</script>',
    events:[{id:2,type:'decision',text:'Second <img>',actor:'<owner>',occurred_at:20},{id:1,type:'info',text:'First event',actor:'awasi',occurred_at:10}],
    actions:[{id:'a',playbook:'workbench.restrict_app',target:'<app>',status:'undone',created_at:12,verified_at:13},
      {id:'b',playbook:'workbench.restrict_app',target:'app',status:'unknown',undo_of:'a',created_at:21,verified_at:null}],
    spans:[{id:'trace',kind:'model',name:'<tool>',started_at:11,ended_at:12,tokens_in:3,tokens_out:2,error:'<failure>'}]});
  assert.ok(html.indexOf('First event')<html.indexOf('Second &lt;img&gt;'));
  assert.match(html,/Previously verified/);assert.match(html,/Not verified/);assert.match(html,/Undo of/);
  assert.match(html,/<details[^>]*><summary>Trace/);assert.doesNotMatch(html,/<details[^>]*\bopen/);
  assert.doesNotMatch(html,/<script>|<img>|<tool>|<failure>|data-action="approve"|data-action="undo"/);
  assert.match(html,/&lt;script&gt;/);assert.match(html,/Decisions stay in Slack/);
});

test('case selection uses the newest matching case and never another finding',()=>{
  assert.equal(typeof model.findingCases,'function');
  const rows=[{id:'old',finding_id:'f',created_at:1},{id:'other',finding_id:'x',created_at:50},{id:'new',finding_id:'f',created_at:10}];
  assert.deepEqual(model.findingCases('f',{items:rows}).map(c=>c.id),['new','old']);
});

test('workbench apps and agents retain their actual findings and access state',()=>{
  const grant={kind:'AccessGrant',principal_id:'crm-agent',source_id:'grant',status:'active',scopes:['customers:read']};
  const agent={source_id:'workbench-agent',crm_principal_id:'crm-agent',status:'paused'};
  const access=model.agentAccess(agent,[grant]);
  assert.equal(access.grants.length,1);assert.equal(access.label,'Agent paused');
  assert.equal(model.agentAccess({source_id:'crm-agent',principal_status:'disabled'},[grant]).label,'Access paused');
  assert.deepEqual(model.appFindings({source_id:'app'},[],[{id:'f',status:'open',evidence:{app_id:'app'}},{id:'other',status:'open',evidence:{app_id:'different'}}]).map(f=>f.id),['f']);
});

test('principals do not overlap within their respective lanes',()=>{
 const nodes=Array.from({length:5},(_,i)=>({id:`a${i}`,kind:'Agent'})).concat([{id:'s1',kind:'ServiceAccount'},{id:'s2',kind:'ServiceAccount'}]);
 const positions=model.graphLayout(nodes);
 for(const x of new Set(positions.map(n=>n.x))) {
  const lane=positions.filter(n=>n.x===x).sort((a,b)=>a.y-b.y);
  assert.ok(lane.every((n,i)=>i===0 || n.y-lane[i-1].y>=72));
 }
});

test('events at the same timestamp retain their recorded sequence',async()=>{
 const {renderCase}=await import('../../src/helio_dashboard/static/cases.mjs');
 const html=renderCase({events:[{id:2,occurred_at:100,text:'Earlier event'},{id:10,occurred_at:100,text:'Later event'}]});
 assert.ok(html.indexOf('Earlier event')<html.indexOf('Later event'));
});
