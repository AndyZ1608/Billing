const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

class Node {
  constructor(){this.children=[];this.value='month';this.hidden=false;this.style={};}
  append(...items){this.children.push(...items);}
  prepend(...items){this.children.unshift(...items);}
  replaceChildren(...items){this.children=items;}
}
async function dashboard(path){
  const nodes=new Map(),requests=[];
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
  const cost={cpu:'2020858.40310001',ram:'2020858.60310001',ssd:'0',total:'4041717.00620002'};
  const current={instance_count:100,active_vm_count:50,vcpu_count:'100',ram_gb:'200',total_billable_ssd_gib:'50'};
  const projects=['A','B'].map(project_id=>({project_id,project_name:project_id,current,...current,cost}));
  const all=projects.flatMap(p=>Array.from({length:100},(_,i)=>({instance_id:`${p.project_id}-${i}`,instance_name:`web-${i}`,project_id:p.project_id,current:{...current,status:i%2?'SHUTOFF':'ACTIVE',vcpus:2,ram_gib:'4'},cost})));
  const summary={current,cost,rated_cost:cost,estimated_cost:cost,pricing:{cpu_per_vcpu_hour:'10000',ram_per_gib_hour:'11000',ssd_per_gib_hour:'500'},actual_unit_prices:{cpu:['10000']},data_quality_status:'HEALTHY',diagnostics:{billing_projects:2},unrated_segments:0};
  const context=vm.createContext({Node,URLSearchParams,Date,console,MoneyDisplay:null,
    location:{pathname:path,search:''},document:{querySelector:get,querySelectorAll:()=>[],createElement:()=>new Node(),createTextNode:text=>text},
    setInterval:()=>{},setTimeout,clearTimeout,
    fetch:async raw=>{requests.push(raw);const u=new URL(raw,'http://test');let body;
      if(u.pathname.endsWith('/metering/meters'))body={default_end_date:'2026-09-18'};
      else if(u.pathname.endsWith('/calendar-range'))body={start:'2026-09-01T00:00:00Z',end:'2026-10-01T00:00:00Z'};
      else if(u.pathname.endsWith('/health'))body={openstack:'OK'};
      else if(u.pathname.endsWith('/billing/summary')||u.pathname.endsWith('/billing/projects/A'))body=summary;
      else if(u.pathname.endsWith('/billing/projects'))body={items:projects,total:2,limit:20,offset:0};
      else if(u.pathname.endsWith('/billing/instances')){assert.equal(u.searchParams.get('project_id'),'A');assert.equal(u.searchParams.get('current_only'),'true');const items=all.filter(r=>r.project_id==='A');body={items:items.slice(0,20),total:100,limit:20,offset:0};}
      else if(u.pathname.endsWith('/projects/A'))body=projects[0];
      else body={};
      return {ok:true,json:async()=>body};
    }});
  vm.runInContext(fs.readFileSync('app/static/money.js','utf8'),context);
  vm.runInContext(fs.readFileSync('app/static/app.js','utf8'),context);
  for(let i=0;i<20;i++)await new Promise(setImmediate);
  assert.equal(get('#error').textContent,'');
  return {get,requests};
}
test('200 VMs: overview renders two project rows and never fetches VM lists',async()=>{
  const {get,requests}=await dashboard('/');
  assert.equal(get('#projects tbody').children.length,2);
  assert.equal(get('#vm-billing').hidden,true);
  assert.equal(get('#internal-vms tbody').children.length,0);
  assert(!requests.some(u=>u.includes('/instances')||u.includes('/sync-runs')));
});
test('project detail lazily fetches only selected project and renders 20 VMs',async()=>{
  const {get,requests}=await dashboard('/projects/A');
  assert.equal(get('#overview').hidden,true);
  assert.equal(get('#internal-vms tbody').children.length,20);
  assert.equal(requests.filter(u=>u.includes('/billing/instances?')).length,1);
  assert(!requests.some(u=>u.includes('/projects/A/instances')||u.includes('/projects/A/volumes')));
  assert.equal(get('#internal-vms tbody').children[0].children[6].children[0],'4,041,717 ₫');
});
