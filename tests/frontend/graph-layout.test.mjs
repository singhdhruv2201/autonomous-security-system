import test from 'node:test';
import assert from 'node:assert/strict';
import * as model from '../../src/helio_dashboard/static/model.mjs';
const node=(id,kind,source='crm')=>({id,source_id:id,name:id,kind,source});
const edge=(start,end,kind)=>({start,end,kind});
test('related people, agents, identities and grants align with accounts kept off canvas',()=>{
 const nodes=[node('person-z','Employee','directory'),node('person-a','Employee','directory'),node('account-a','AppAccount'),node('account-z','AppAccount'),node('bot','Agent','workbench'),node('identity','Agent'),node('grant','AccessGrant')];
 const edges=[edge('account-a','person-z','IDENTITY_OF'),edge('account-z','person-a','IDENTITY_OF'),edge('person-z','bot','OWNS'),edge('bot','identity','ACTS_AS'),edge('identity','grant','HAS_GRANT')];
 const layout=model.graphLayout(nodes,edges),byId=Object.fromEntries(layout.map(n=>[n.id,n]));
 assert.equal(byId['account-a'],undefined);
 assert.equal(byId['account-z'],undefined);
 assert.equal(nodes.filter(n=>n.kind==='AppAccount').length,2);
 for(const id of ['bot','identity','grant']) assert.equal(byId[id].y,byId['person-z'].y);
 assert.ok(byId.identity.x>byId.bot.x);
 assert.deepEqual(model.graphLayout([...nodes].reverse(),[...edges].reverse()),layout);
});
test('dense lanes grow instead of overlapping cards and labels',()=>{
 const nodes=Array.from({length:20},(_,i)=>node(`person-${i}`,'Employee','directory')).concat(Array.from({length:12},(_,i)=>node(`identity-${i}`,i%2?'Agent':'ServiceAccount')));
 const layout=model.graphLayout(nodes,[]);
 for(const x of new Set(layout.map(n=>n.x))){
  const lane=layout.filter(n=>n.x===x).sort((a,b)=>a.y-b.y);
  assert.ok(lane.every((n,i)=>i===0||n.y-lane[i-1].y>=72));
 }
 assert.equal(typeof model.graphSize,'function');
 const size=model.graphSize(layout);
 assert.ok(layout.every(n=>n.y+36<size.height&&n.x+90<size.width));
});
test('shared destinations and missing endpoints never duplicate or lose entities',()=>{
 const nodes=[node('a','Agent','workbench'),node('b','Agent','workbench'),node('c','Connection','workbench'),node('out','Destination','workbench')];
 const layout=model.graphLayout(nodes,[edge('a','c','HAS_CONNECTION'),edge('b','c','HAS_CONNECTION'),edge('c','out','TO'),edge('missing','out','TO')]);
 assert.equal(layout.length,nodes.length);assert.equal(new Set(layout.map(n=>n.id)).size,nodes.length);
 assert.ok(layout.every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)));
});
test('labels distinguish workbench agents from CRM identities and show connection hosts',()=>{
 assert.equal(typeof model.graphNodeLabel,'function');
 const asset={...node('wb','Agent','workbench'),name:'Lead Enrichment Agent'};
 const principal={...node('crm','Agent'),name:asset.name};
 assert.equal(model.graphNodeLabel(asset).subtitle,'Workbench agent');
 assert.equal(model.graphNodeLabel(principal).subtitle,'CRM identity');
 assert.equal(model.graphNodeLabel({...node('["agent","partner-api"]','Connection','workbench'),host:'partner-api'}).title,'partner-api');
});

test('node cards mask edges and escape full titles while keeping labels inside the card',async()=>{
 const {renderGraphNode,graphEdgePath}=await import('../../src/helio_dashboard/static/graph.mjs');
 const html=renderGraphNode({...node('x','Agent','workbench'),name:'<script>A very long agent name</script>',x:100,y:100},{color:'#776699',icon:'',dim:true});
 assert.match(html,/class="node-surface"/);assert.match(html,/fill="#fff"/);
 assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>|fill-opacity/);
 assert.match(html,/Workbench agent/);
 const path=graphEdgePath({x:110,y:100},{x:330,y:100});
 assert.match(path,/M208 100/);assert.match(path,/232 100$/);
});

test('person account lookup supports multiple accounts and incomplete graph evidence without name matching',()=>{
 assert.equal(typeof model.personAccounts,'function');
 const person={...node('person','Employee','directory'),source_id:'owner'};
 const a={...node('a','AppAccount'),owner_employee_id:'owner'};
 const b=node('b','AppAccount');
 const other={...node('other','AppAccount'),name:person.name,owner_employee_id:'elsewhere'};
 const graph={nodes:[b],edges:[edge('b','person','IDENTITY_OF')],truncated:true};
 assert.deepEqual(model.personAccounts(person,[a,other],graph).map(n=>n.id),['a','b']);
 assert.deepEqual(model.personAccounts({...person,source_id:undefined},[other],{nodes:[],edges:[]}),[]);
});
