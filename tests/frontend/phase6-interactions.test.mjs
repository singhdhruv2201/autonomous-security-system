import test from 'node:test';
import assert from 'node:assert/strict';

const tick = () => new Promise(r=>setImmediate(r));
async function ui(fetcher,hash='#findings') {
  const handlers={},intervals=[],elements={};
  const element = selector => elements[selector] ??= {innerHTML:'',hidden:false,isConnected:true,addEventListener(){},contains(){return false;},focus(){},setAttribute(){}};
  globalThis.document={hidden:false,activeElement:null,body:{classList:{add(){},remove(){}}},
    addEventListener(name,fn){(handlers[name]??=[]).push(fn);},querySelector:element};
  globalThis.window={addEventListener(name,fn){handlers[name]=[fn];},scrollTo(){}};
  globalThis.location={get hash(){return hash;},set hash(value){hash=value.startsWith("#")?value:"#"+value;}}; globalThis.setInterval=(fn,ms)=>intervals.push({fn,ms});globalThis.fetch=fetcher;
  await import(`../../src/helio_dashboard/static/app.mjs?phase6=${Math.random()}`);await tick();
  return {elements,intervals,async click(action,id,value){
    const target={dataset:{action,id,value},closest:s=>s==='[data-action]'?target:null};
    for(const fn of handlers.click??[]) fn({target});await tick();
  },async hashchange(){for(const fn of handlers.hashchange??[]) fn();await tick();}};
}
function fixture(){
 const nodes=[['o','Employee','owner'],['a','App','app'],['p','ServiceAccount','principal'],['g','AccessGrant','grant'],['d','DataResource','data'],['u','Audience','public']].map(([id,kind,source_id])=>({id,kind,source_id,name:source_id}));
 const edges=[['1','o','a','OWNS'],['2','a','p','ACTS_AS'],['3','p','g','HAS_GRANT'],['4','g','d','ON'],['5','a','u','EXPOSED_TO']].map(([id,start,end,kind])=>({id,start,end,kind}));
 const f={id:'f',title:'Public app',status:'open',evidence_fresh:true,rule:'public_app_confidential_data',evidence:{employee_id:'owner',app_id:'app',principal_id:'principal',grant_id:'grant',resource_id:'data'},events:[]};
 const state={errors:{},inventory:{items:nodes},graph:{nodes,edges},findings:{items:[f]},cases:{items:[{id:'old',finding_id:'f',created_at:1},{id:'new',finding_id:'f',created_at:2}],total:2}};
 return {f,state};
}

test('drawer fetches newest linked case, selects history and highlights the full path',async()=>{
 const {f,state}=fixture(),calls=[];
 const app=await ui(async url=>{calls.push(url);return {ok:true,json:async()=>url==='/api/state'?state:url==='/api/findings/f'?f:{id:url.split('/').at(-1),finding_id:'f',status:'waiting_owner',summary:url.includes('new')?'Latest case':'Older case',events:[],actions:[],spans:[]}};});
 await app.click('finding','f');await tick();
 assert.ok(calls.includes('/api/cases/new'));
 assert.match(app.elements['#drawer-root'].innerHTML,/Latest case/);
 await app.click('case-history','old');await tick();
 assert.match(app.elements['#drawer-root'].innerHTML,/Older case/);
 await app.click('show-risk','f');await app.hashchange();
 const html=app.elements['#main'].innerHTML;
 assert.match(html,/OUTSIDE/);assert.match(html,/viewBox="0 0 1980 640"/);
 assert.equal((html.match(/class="graph-edge highlighted/g)??[]).length,5);
 assert.match(html,/Risk path: Public app/);
 assert.ok(calls.every(url=>!url.includes('/decisions')));
});

test('case failure leaves finding evidence visible, and a late response cannot reopen a closed drawer',async()=>{
 const {f,state}=fixture(); let release;
 const app=await ui(async url=>url.startsWith('/api/cases/')?{ok:false,json:async()=>({})}:{ok:true,json:async()=>url==='/api/state'?state:f});
 await app.click('finding','f');await tick();
 assert.match(app.elements['#drawer-root'].innerHTML,/Case details unavailable/);
 assert.match(app.elements['#drawer-root'].innerHTML,/Supporting evidence/);
 globalThis.fetch=async url=>url==='/api/findings/f'?new Promise(r=>{release=()=>r({ok:true,json:async()=>f});}):{ok:false};
 await app.click('finding','f');await app.click('close-drawer');release();await tick();
 assert.equal(app.elements['#drawer-root'].innerHTML,'');
});

test('policies render server decision rules and playbook reversibility',async()=>{
 const state={errors:{},policies:{items:[],decision_rules:[{order:1,name:'Override <x>',description:'Ask a human'}],playbooks:[{id:'fix',description:'Restore <state>',reversible:true},{id:'revoke',description:'Permanent',reversible:false}]}};
 const app=await ui(async()=>({ok:true,json:async()=>state}),'#policies');
 assert.match(app.elements['#main'].innerHTML,/Override &lt;x&gt;/);
 assert.match(app.elements['#main'].innerHTML,/Reversible/);assert.match(app.elements['#main'].innerHTML,/Irreversible/);
 assert.doesNotMatch(app.elements['#main'].innerHTML,/No automatic remediation/);
});

test('an open case refreshes after polling and rejects case data for another finding',async()=>{
 const {f,state}=fixture(); let status='waiting_owner'; let foreign=false;
 const app=await ui(async url=>({ok:true,json:async()=>url==='/api/state'?state:url==='/api/findings/f'?f:{id:'new',finding_id:foreign?'wrong':'f',status,events:[],actions:[],spans:[]}}));
 await app.click('finding','f');await tick();
 assert.match(app.elements['#drawer-root'].innerHTML,/waiting owner/);
 status='resolved';
 app.intervals.find(i=>i.ms===15000).fn();await tick();await tick();
 assert.match(app.elements['#drawer-root'].innerHTML,/resolved/);
 foreign=true;
 await app.click('reload-case');await tick();
 assert.match(app.elements['#drawer-root'].innerHTML,/Case details unavailable/);
});

 test('polling preserves drawer scroll and expanded trace without replaying its entrance',async()=>{
 const {f,state}=fixture();
 const app=await ui(async url=>({ok:true,json:async()=>url==='/api/state'?state:url==='/api/findings/f'?f:{id:'new',finding_id:'f',status:'waiting_owner',events:[],actions:[],spans:[]}}));
 await app.click('finding','f');await tick();
 app.elements['.drawer']={scrollTop:420};
 app.elements['.case-traces']={open:true};
 const root=app.elements['#drawer-root'];let html=root.innerHTML;
 Object.defineProperty(root,'innerHTML',{get(){return html;},set(value){html=value;app.elements['.drawer']={scrollTop:0};app.elements['.case-traces']={open:false};}});
 app.intervals.find(i=>i.ms===15000).fn();await tick();await tick();
 assert.equal(app.elements['.drawer'].scrollTop,420);
 assert.equal(app.elements['.case-traces'].open,true);
 assert.match(html,/drawer refreshed/);
 });

test('polling keeps keyboard focus on the expanded trace control',async()=>{
 const {f,state}=fixture();
 const app=await ui(async url=>({ok:true,json:async()=>url==='/api/state'?state:url==='/api/findings/f'?f:{id:'new',finding_id:'f',status:'waiting_owner',events:[],actions:[],spans:[]}}));
 await app.click('finding','f');await tick();
 const oldSummary={tagName:'SUMMARY',closest(){return {};}};document.activeElement=oldSummary;
 let focused=false;document.querySelectorAll=()=>[{tagName:'SUMMARY',focus(){focused=true;}}];
 app.intervals.find(i=>i.ms===15000).fn();await tick();await tick();
 assert.equal(focused,true);
});

test('graph polling preserves the readable canvas scroll position',async()=>{
 const {state}=fixture();
 const app=await ui(async()=>({ok:true,json:async()=>state}),'#graph');
 app.elements['.graph-stage']={scrollTop:320,scrollLeft:480};
 const main=app.elements['#main'];let html=main.innerHTML;
 Object.defineProperty(main,'innerHTML',{get(){return html;},set(value){html=value;app.elements['.graph-stage']={scrollTop:0,scrollLeft:0};}});
 app.intervals.find(i=>i.ms===15000).fn();await tick();await tick();
 assert.equal(app.elements['.graph-stage'].scrollTop,320);
 assert.equal(app.elements['.graph-stage'].scrollLeft,480);
 await app.click('zoom-fit');
 assert.equal(app.elements['.graph-stage'].scrollTop,0);
 assert.equal(app.elements['.graph-stage'].scrollLeft,0);
});

test('CRM accounts live in the person drawer instead of duplicate graph cards',async()=>{
 const {state}=fixture();
 const account={id:'account',source_id:'employee:owner',name:'owner',kind:'AppAccount',source:'crm',role:'<user>',principal_status:'active',owner_employee_id:'owner'};
 const foreign={...account,id:'foreign',source_id:'employee:other',owner_employee_id:'someone-else'};
 state.inventory.items.push(account,foreign);
 state.graph.edges.push({id:'identity',start:'account',end:'o',kind:'IDENTITY_OF'},{id:'uses',start:'account',end:'a',kind:'USES'});
 const app=await ui(async()=>({ok:true,json:async()=>state}),'#graph');
 const html=app.elements['#main'].innerHTML;
 assert.doesNotMatch(html,/data-action="graph-node" data-id="account"|>ACCOUNTS</);
 assert.match(html,/CRM accounts in person details/);
 await app.click('graph-node','o');
 const drawer=app.elements['#drawer-root'].innerHTML;
 assert.match(drawer,/<h3>CRM accounts/);
 assert.match(drawer,/data-action="entity" data-id="account"/);
 assert.match(drawer,/&lt;user&gt;/);assert.match(drawer,/active/);
 assert.doesNotMatch(drawer,/data-id="foreign"/);
 await app.click('entity','account');
 assert.match(app.elements['#drawer-root'].innerHTML,/employee:owner/);
});
