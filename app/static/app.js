"use strict";
const $ = (selector) => document.querySelector(selector);
const state = {q:"",sort:"project_name",direction:"asc",projectOffset:0,instanceOffset:0,volumeOffset:0,vmCostOffset:0,traceOffset:0};
const projectId = location.pathname.startsWith("/projects/") ? location.pathname.split("/")[2] : null;
const limit = 25;
let selectedRange=null;
const selectedVm=new URLSearchParams(location.search).get("instance_id");
let loading = false;
const vnd=v=>{if(v==null)return "Unrated";const [whole,fraction=""]=String(v).split(".");const decimals=fraction.replace(/0+$/,"");return whole.replace(/\B(?=(\d{3})+(?!\d))/g,",")+(decimals?"."+decimals:"")+" ₫";};
function rangeQuery(extra={}){return new URLSearchParams({...selectedRange,...extra});}
let syncPending = false;
const number = (v) => v == null ? "Unknown" : Number(v).toLocaleString(undefined,{maximumFractionDigits:3});
const date = (v) => v ? new Date(v).toLocaleString() : "—";
function element(tag, text, className) {
  const node = document.createElement(tag);
  if(text != null) node.textContent = text;
  if(className) node.className = className;
  return node;
}
async function api(path, options) {
  const response = await fetch(`/api/v1${path}`,options);
  if(!response.ok) {
    let message = `Request failed (${response.status})`;
    try {const body=await response.json();if(typeof body.detail === "string")message=body.detail;} catch {}
    throw new Error(message);
  }
  return response.json();
}
function error(message) {$("#error").textContent=message;$("#error").hidden=!message;}
function table(id, rows, columns) {
  const body=$(`${id} tbody`);body.replaceChildren();
  if(!rows.length) {const row=element("tr"),cell=element("td","No resources to display.","empty");cell.colSpan=columns.length;row.append(cell);body.append(row);return;}
  for(const item of rows) {const row=element("tr");for(const column of columns){const cell=element("td"),value=column(item);cell.append(value instanceof Node ? value : document.createTextNode(String(value ?? "—")));row.append(cell);}body.append(row);}
}
function named(name, id, href) {const block=element("div"),label=element(href?"a":"span",name);if(href)label.href=href;block.append(label,element("small",id));return block;}
function presence(resource) {return named(resource.status, resource.is_missing ? "Missing · excluded" : resource.missing_scans ? `Pending missing · ${resource.missing_scans} scans` : resource.quality_issues?.length ? "Quality warning" : "Observed");}
function pager(id, page, key, refresh) {
  const node=$(id);node.replaceChildren();
  const previous=element("button","← Previous"),next=element("button","Next →");
  previous.disabled=page.offset===0;next.disabled=page.offset+page.limit>=page.total;
  previous.onclick=()=>{state[key]=Math.max(0,state[key]-limit);refresh().catch(e=>error(e.message));};
  next.onclick=()=>{state[key]+=limit;refresh().catch(e=>error(e.message));};
  node.append(element("span",`${page.total ? page.offset+1 : 0}–${Math.min(page.offset+page.limit,page.total)} of ${page.total}`),previous,next);
}
function metrics(data) {
  const values=[["VMs",data.instance_count,""],["Allocated vCPU",data.vcpu_count,""],["Allocated RAM",data.ram_gb,"GiB"],["Nova local disks",data.nova_disk_gib,"GiB"],["Cinder capacity",data.cinder_volume_gib,"GiB"],["Billable SSD",data.total_billable_ssd_gib,"GiB"]];
  $("#metrics").replaceChildren(...values.map(([title,value,unit])=>{const box=element("article",null,"metric"),valueNode=element("strong",number(value));valueNode.append(element("small",unit));box.append(element("span",title,"label"),valueNode);return box;}));
  const incomplete=data.incomplete_instances+data.incomplete_volumes;
  const pending=data.pending_missing_instances+data.pending_missing_volumes;
  $("#quality").hidden=!incomplete&&!pending;
  $("#quality").textContent=`${incomplete} counted resources have unknown dimensions; totals include only known quantities. ${pending} counted resources are pending reconciliation and still contribute their last observed allocation.`;
}
async function projects() {
  const params=rangeQuery({q:state.q,sort:state.sort,direction:state.direction,limit,offset:state.projectOffset});
  const page=await api(`/billing/projects?${params}`);
  table("#projects",page.items,[r=>named(r.project_name+(r.is_placeholder?" (unresolved)":r.is_missing?" (missing)":""),r.project_id,`/projects/${r.project_id}`),...['instance_count','vcpu_count','ram_gb','total_billable_ssd_gib'].map(k=>r=>number(r[k])),...['cpu','ram','ssd','total'].map(k=>r=>vnd(r.cost[k]))]);
  pager("#project-pager",page,"projectOffset",projects);
}
async function instances() {
  const page=await api(`/projects/${projectId}/instances?limit=${limit}&offset=${state.instanceOffset}`);
  table("#instances",page.items,[r=>named(r.instance_name,r.instance_id,`/projects/${projectId}?instance_id=${r.instance_id}#vm-cost-detail`),presence,r=>number(r.vcpus),r=>number(r.ram_gb),r=>number(r.root_disk_gb),r=>number(r.ephemeral_disk_gb),r=>named(r.flavor_name||r.flavor_id||"Unknown",`Boot: ${r.boot_source}`),r=>date(r.created_at_openstack),r=>date(r.last_seen_at)]);
  pager("#instance-pager",page,"instanceOffset",instances);
}
async function volumes() {
  const page=await api(`/projects/${projectId}/volumes?limit=${limit}&offset=${state.volumeOffset}`);
  table("#volumes",page.items,[r=>named(r.volume_name,r.volume_id,`/resources/VOLUME/${r.volume_id}`),presence,r=>r.volume_type,r=>number(r.size_gb),r=>r.bootable==null?"Unknown":r.bootable?"Yes":"No",r=>r.attachments.map(a=>a.instance_id).join(", ")||"—",r=>date(r.created_at_openstack),r=>date(r.last_seen_at)]);
  pager("#volume-pager",page,"volumeOffset",volumes);
}
async function refresh() {
  if(loading||!selectedRange)return;loading=true;
  selectedRange.as_of=new Date().toISOString();
  try {
    const [health,runs,summary]=await Promise.all([api("/health"),api("/sync-runs?limit=5"),api((projectId?`/billing/projects/${projectId}`:"/billing/summary")+"?"+rangeQuery())]);
    $("#connection").textContent=health.openstack.replaceAll("_"," ");
    if(!projectId) $("#title").textContent=`${health.cloud_name||"Cloud"} · Overview`;
    $("#region").textContent=health.region||"—";
    $("#last-sync").textContent=date(health.last_successful_sync);
    $("#last-failure").textContent=date(health.last_failed_sync);
    $("#sync").disabled=health.sync_running||syncPending;
    $("#sync").textContent=health.sync_running||syncPending?"Syncing…":"Sync now";
    $("#freshness").textContent=Object.entries(health.service_status||{}).map(([service,value])=>`${service}: ${value.status} (last complete: ${date(value.last_success_at)})`).join(" · ")||"No completed synchronization. Configure OpenStack credentials to begin.";
    metrics(summary.current);
    renderCosts(summary);
    await Promise.all([vmCosts(),loadDiagnostics(summary.diagnostics)]);
    if(selectedVm)await vmDetail();
    table("#runs",runs.items,[r=>named(date(r.started_at),r.sync_run_id),r=>r.status,r=>r.projects_found,r=>r.instances_found,r=>r.volumes_found,r=>r.errors.map(e=>`${e.service}: ${e.code}${e.count?` (${e.count})`:""}`).join("; ")||"—"]);
    if(projectId){
      const project=await api(`/projects/${projectId}`);
      $("#title").textContent=project.project_name;
      $("#project-meta").textContent=`UUID: ${project.project_id} · ${project.enabled==null?"Status unknown":project.enabled?"Enabled":"Disabled"} · Domain: ${project.domain_id||"Unknown"}`;
      await Promise.all([instances(),volumes()]);
    }else await projects();
    error("");
  }catch(e){error(e.message);}finally{loading=false;}
}
$("#overview").hidden=!!projectId;$("#detail").hidden=!projectId;
if(projectId){for(const [label,href] of [["Current Inventory",`/projects/${projectId}`],["Historical Usage",`/history?project_id=${projectId}`],["Cost",`/costs?project_id=${projectId}`],["Instances","#instances"],["Volumes","#volumes"]]){const a=element("a",label);a.href=href;$("#project-tabs").append(a);}}
$("#sync").onclick=async()=>{
  syncPending=true;$("#sync").disabled=true;$("#sync").textContent="Syncing…";
  try{await api("/sync",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});error("");}
  catch(e){error(e.message);}
  finally{syncPending=false;await refresh();}
};
let debounce;
$("#search").oninput=e=>{clearTimeout(debounce);debounce=setTimeout(()=>{state.q=e.target.value;state.projectOffset=0;projects().catch(e=>error(e.message));},250);};
document.querySelectorAll("[data-sort]").forEach(button=>button.onclick=()=>{state.direction=state.sort===button.dataset.sort&&state.direction==="asc"?"desc":"asc";state.sort=button.dataset.sort;state.projectOffset=0;projects().catch(e=>error(e.message));});
api("/billing/policy").then(policy=>{$("#policy").textContent=JSON.stringify(policy,null,2);}).catch(e=>error(e.message));
initializePeriod().then(refresh).catch(e=>error(e.message));setInterval(refresh,10000);

function cards(target,cost){$(target).replaceChildren(...["cpu","ram","ssd","total"].map(k=>{const n=element("article",null,"metric");n.append(element("span",k.toUpperCase()+" · selected period"),element("strong",vnd(cost[k])));return n;}));}
function renderCosts(data){if(!projectId){const n=element("article",null,"metric");n.append(element("span","Discovered projects"),element("strong",String(data.diagnostics.billing_projects)));$("#metrics").prepend(n);}cards("#cost-metrics",data.cost);const p=data.pricing;$("#internal-rates").textContent=`Configured internal rates: CPU ${vnd(p.cpu_per_vcpu_hour)} / vCPU-hour · RAM ${vnd(p.ram_per_gib_hour)} / GiB-hour · SSD ${vnd(p.ssd_per_gib_hour)} / GiB-hour. Actual historical rates: ${JSON.stringify(data.actual_unit_prices)}. Rates take effect through the INTERNAL-VND price book.`;$("#internal-status").textContent=`${data.data_quality_status} ${data.data_quality_status!=="HEALTHY"?"— BILLING DATA INCOMPLETE":""} · ${!data.rated_segments&&!data.estimated_segments&&!data.unrated_segments?"No observed billable usage in this range; earlier history may be unavailable":data.estimated?"Includes estimated open usage":"Rated usage"} · ${data.unrated_segments} unrated segments · Rated ${vnd(data.rated_cost.total)} + estimated ${vnd(data.estimated_cost.total)} · As of ${date(data.as_of)}. ${data.projects_with_billing_usage??""} projects with usage.`;}
async function vmCosts(){const q=rangeQuery({limit,offset:state.vmCostOffset});if(projectId)q.set("project_id",projectId);const page=await api(`/billing/instances?${q}`);table("#internal-vms",page.items,[r=>named(r.instance_name,r.instance_id,`/projects/${r.project_id}?instance_id=${r.instance_id}#vm-cost-detail`),r=>r.project_id,r=>r.current.status,r=>number(r.current.vcpus),r=>number(r.current.ram_gib),r=>number(r.current.total_billable_ssd_gib),...['cpu','ram','ssd','total'].map(k=>r=>vnd(r.cost[k]))]);pager("#vm-cost-pager",page,"vmCostOffset",vmCosts);}
async function vmDetail(){const data=await api(`/billing/instances/${selectedVm}?${rangeQuery({limit,offset:state.traceOffset})}`);$("#vm-cost-detail").hidden=false;$("#vm-title").textContent=`${data.instance_name} · VM billing trace`;$("#vm-inventory").textContent=JSON.stringify({instance_id:data.instance_id,project_id:data.project_id,...data.current},null,2);cards("#vm-cost-cards",data.cost);$("#vm-trace-status").textContent=`${data.data_quality_status} · CPU ${data.usage.vcpu_hours} vCPU-h · RAM ${data.usage.ram_gib_hours} GiB-h · SSD ${data.usage.ssd_gib_hours} GiB-h. Open intervals are estimates, not persisted financial charges.`;table("#vm-trace",data.trace,[r=>`${date(r.start)} → ${date(r.end)}`,r=>`${r.state} / ${r.history_confidence}`,r=>r.meter,r=>r.allocated_quantity,r=>r.hours,r=>r.usage,r=>vnd(r.unit_price),r=>vnd(r.amount),r=>r.status+(r.reason?`: ${r.reason}`:""),r=>named("Lifecycle & usage",r.source_state_period_id,`/resources/${r.resource_type}/${r.resource_id}`)]);pager("#vm-trace-pager",{total:data.trace_total,offset:data.offset,limit:data.limit},"traceOffset",vmDetail);}
async function loadDiagnostics(data){data=data||await api("/diagnostics/openstack");$("#diagnostics").textContent=JSON.stringify(data,null,2);$("#reconciliation-status").textContent=`${data.data_quality_status} · Discovered projects ${data.projects_visible??"Unknown"} / stored ${data.billing_projects} · VMs ${data.instances_visible??"Unknown"} / stored ${data.billing_instances} · Volumes ${data.volumes_visible??"Unknown"} / stored ${data.billing_volumes} · Projects with VMs ${data.projects_with_vms}; without VMs ${data.projects_without_vms} · Pending deletion ${data.pending_delete_confirmation} · Unknown project resources ${data.unknown_project_resources}. Counts describe the last SDK response; compare with CLI to verify scope.`;table("#reconciliation",data.per_project,[r=>named(r.project_name,r.project_id,`/projects/${r.project_id}`),r=>r.openstack_vms??"Unknown",r=>r.billing_vms,r=>r.openstack_volumes??"Unknown",r=>r.billing_volumes]);}
let billingToday;
function isoDate(d){return d.toISOString().slice(0,10);}
function preset(){const today=new Date(billingToday+"T00:00:00Z"),mode=$("#period-preset").value;let start,end;if(mode==='custom')return;if(mode==='today'){start=today;end=new Date(today.getTime()+86400000);}else if(mode==='last'){start=new Date(Date.UTC(today.getUTCFullYear(),today.getUTCMonth()-1,1));end=new Date(Date.UTC(today.getUTCFullYear(),today.getUTCMonth(),1));}else{start=new Date(Date.UTC(today.getUTCFullYear(),today.getUTCMonth(),1));end=new Date(Date.UTC(today.getUTCFullYear(),today.getUTCMonth()+1,1));}$("#period-start").value=isoDate(start);$("#period-end").value=isoDate(end);}
async function applyPeriod(){selectedRange=await api(`/metering/calendar-range?${new URLSearchParams({start_date:$("#period-start").value,end_date:$("#period-end").value})}`);state.projectOffset=state.vmCostOffset=state.traceOffset=0;await refresh();}
async function initializePeriod(){const info=await api("/metering/meters");billingToday=isoDate(new Date(new Date(info.default_end_date+"T00:00:00Z").getTime()-86400000));preset();selectedRange=await api(`/metering/calendar-range?${new URLSearchParams({start_date:$("#period-start").value,end_date:$("#period-end").value})}`);}
$("#period-preset").onchange=()=>{preset();};
$("#internal-period").onsubmit=e=>{e.preventDefault();applyPeriod().catch(e=>error(e.message));};
$("#test-connection").onclick=()=>$("#sync").click();

for(const id of ["#period-start","#period-end"]) $(id).onchange=()=>{$("#period-preset").value="custom";};
