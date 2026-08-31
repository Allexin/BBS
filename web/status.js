"use strict";
const byId = id => document.getElementById(id);
const text = (tag, value, className) => { const node=document.createElement(tag); if(value!=null)node.textContent=value; if(className) node.className=className; return node; };
const formatTime = value => value ? new Date(value).toLocaleString() : "—";
const formatDuration = seconds => seconds == null ? "—" : `${Math.floor(seconds/3600)}h ${Math.floor(seconds%3600/60)}m`;
const formatBytes = value => { if(value==null) return "—"; const units=["B","KiB","MiB","GiB","TiB"]; let size=value,index=0; while(size>=1024&&index<units.length-1){size/=1024;index++;} return `${size.toFixed(index?1:0)} ${units[index]}`; };
const addFact = (list,label,value) => { list.append(text("dt",label),text("dd",value)); };
const stageNames={backing_up:"Creating snapshot",verifying:"Verifying",restoring:"Restoring","smart-test-completed":"SMART test completed"};
const age = value => { if(!value)return null; const seconds=Math.max(0,Math.floor((Date.now()-new Date(value).getTime())/1000)); return seconds<60?`${seconds}s ago`:`${Math.floor(seconds/60)}m ago`; };
function operationStatus(item){
  const parts=[stageNames[item.stage]??item.stage??item.state];
  const progress=item.progress;
  if(progress?.files_done!=null)parts.push(progress.files_total!=null?`${progress.files_done.toLocaleString()} / ${progress.files_total.toLocaleString()} files`:`${progress.files_done.toLocaleString()} files`);
  if(progress?.bytes_done!=null)parts.push(progress.bytes_total!=null?`${formatBytes(progress.bytes_done)} / ${formatBytes(progress.bytes_total)}`:formatBytes(progress.bytes_done));
  if(progress?.updated_at)parts.push(`heartbeat ${age(progress.updated_at)}`);
  return parts.join(" · ");
}

function renderHealth(data, health) {
  const card=byId("health"); card.className=`health-card ${data.overall_health}`;
  byId("health-title").textContent=data.overall_health;
  byId("health-meta").textContent=`Manager ${health.manager_state} · backup disk ${data.backup_disk_state} · ${health.version} · started ${formatTime(health.manager_started_at)}`;
  const list=byId("health-issues"); list.replaceChildren();
  data.health_issues.forEach(issue=>list.append(text("li",`${issue.subject}: ${issue.summary}`,issue.severity)));
}
function renderOperations(items) {
  const root=byId("operations"); root.replaceChildren();
  if(!items.length){root.append(text("p","Nothing is running right now","notice"));return;}
  items.forEach(item=>{const row=text("article",null,"operation");row.append(text("div",item.state==="running"?"RUN":`#${item.position}`,"position"));const body=document.createElement("div");body.append(text("strong",`${item.job_display_name} · ${item.kind}`),text("p",`${operationStatus(item)} · elapsed ${formatDuration(item.elapsed_seconds)}`,"muted"));if(item.blocked_reason)body.append(text("p",item.blocked_reason,"critical"));if(item.progress?.bytes_total){const bar=document.createElement("progress");bar.max=item.progress.bytes_total;bar.value=item.progress.bytes_done??0;body.append(bar);}row.append(body,text("code",item.operation_id));root.append(row);});
}
function runLabel(run){return run?`${run.state}${run.result?` / ${run.result}`:""}${run.stage?` / ${run.stage}`:""} · ${formatDuration(run.duration_seconds)}`:"not recorded";}
function renderJobs(items,operations){const active=new Map(operations.map(item=>[item.job_id,item]));const root=byId("jobs");root.replaceChildren();items.forEach(job=>{const card=text("article",null,"card");const head=document.createElement("header");const title=document.createElement("div");title.append(text("h3",job.display_name),text("p",`${job.kind} · ${job.job_id}`,"muted"));head.append(title,text("span",job.health,"badge "+job.health));const facts=text("dl",null,"facts");const current=active.get(job.job_id);if(current)addFact(facts,"Current status",operationStatus(current));addFact(facts,"Reason",job.health_reason);if(job.protection_info)addFact(facts,"Info",job.protection_info);addFact(facts,"Last run",runLabel(job.last_run));addFact(facts,"Previous",runLabel(job.previous_run));addFact(facts,"Last success",formatTime(job.last_success_at));addFact(facts,"Next",`${job.next_operation??"unknown"} · ${formatTime(job.next_fire_at)}`);addFact(facts,"Deadline",job.deadline??"not configured");addFact(facts,"Protected",formatBytes(job.backup_metrics?.protected_logical_bytes));addFact(facts,"Added",formatBytes(job.backup_metrics?.repository_added_bytes));card.append(head,facts);root.append(card);});}
const metricNames={overall_passed:"SMART overall",nvme_critical_warning:"NVMe critical warning",temperature_celsius:"Temperature, °C",power_on_hours:"Power-on hours",reallocated_sectors:"Reallocated sectors",pending_sectors:"Pending sectors",offline_uncorrectable:"Offline uncorrectable",reported_uncorrectable:"Reported uncorrectable",interface_crc_errors:"Interface CRC errors",nvme_percentage_used:"NVMe wear used, %",nvme_media_errors:"NVMe media errors"};
const dash=value=>value==null?"—":String(value);
function metricState(name,value){if(value==null)return["Unknown","unknown"];if(name==="overall_passed")return value?["Normal","healthy"]:["Critical","critical"];if(name==="nvme_critical_warning")return value?["Critical","critical"]:["Normal","healthy"];if(["pending_sectors","offline_uncorrectable","reported_uncorrectable","nvme_media_errors"].includes(name)&&value>0)return["Critical","critical"];if(["reallocated_sectors","interface_crc_errors"].includes(name)&&value>0)return["Warning","warning"];return["Normal","healthy"];}
function summaryCell(label,value,className){const cell=text("div",null,"summary-cell");cell.append(text("span",label),text("strong",value,className));return cell;}
function renderDisks(items){
  const root=byId("disks");root.replaceChildren();
  items.forEach(disk=>{
    const card=text("article",null,"card disk-card");
    const head=document.createElement("header"),title=text("div",null,"disk-title");
    const identity=`Manufacturer: ${dash(disk.manufacturer)} · Type: ${dash(disk.media_type)} · Bus: ${dash(disk.bus_type)} · Capacity: ${formatBytes(disk.capacity_bytes)}`;
    title.append(text("h3",disk.model??disk.disk_id),text("p",identity,"muted"),text("p",`Mount points: ${disk.mount_points.length?disk.mount_points.join(", "):"none"}`,"muted"));
    head.append(title,text("span",disk.affects_system_health?disk.smart_health:`${disk.smart_health} · excluded`,"badge "+disk.smart_health));
    const test=disk.last_self_test;
    const summary=text("div",null,"disk-summary");
    summary.append(summaryCell("Passive SMART",disk.passive_smart_health,disk.passive_smart_health),summaryCell("Last self-test",test?`${test.test_type} / ${test.result}`:"not recorded",test?.result==="success"?"healthy":test?"warning":"unknown"),summaryCell("Observed",formatTime(disk.observed_at)));
    card.append(head,summary);
    if(!disk.affects_system_health)card.append(text("p",`Excluded from system health · ${disk.health_policy_reason??"accepted risk"}`,"policy-note"));
    if(disk.health_reasons.length)card.append(text("p",disk.health_reasons.join(" · "),disk.smart_health==="critical"?"health-reasons critical-box":"health-reasons"));
    if(test){const detail=`${test.reason} · ${formatDuration(test.duration_seconds)} · ${formatTime(test.finished_at)}${test.remaining_percent==null?"":` · ${test.remaining_percent}% remaining`}`;card.append(text("p",detail,test.result==="success"?"muted":"test-message"));}
    const rows=Object.entries(disk.metrics).filter(([,metric])=>[metric.current,metric.previous,metric.delta,metric.change_24h,metric.change_30d].some(value=>value!=null));
    if(rows.length){const wrap=text("div",null,"metric-wrap"),table=text("table",null,"metric-table"),thead=document.createElement("thead"),header=document.createElement("tr"),tbody=document.createElement("tbody");["Indicator","Current","Delta","24h","30d","State"].forEach(value=>header.append(text("th",value)));thead.append(header);rows.forEach(([name,metric])=>{const row=document.createElement("tr"),[state,stateClass]=metricState(name,metric.current);row.append(text("td",metricNames[name]??name),text("td",dash(metric.current)),text("td",dash(metric.delta)),text("td",dash(metric.change_24h)),text("td",dash(metric.change_30d)),text("td",state,"metric-state "+stateClass));tbody.append(row);});table.append(thead,tbody);wrap.append(table);card.append(wrap);}
    else card.append(text("p","No normalized SMART metrics were reported.","notice"));
    root.append(card);
  });
}
function renderVolumes(items){const root=byId("volumes");root.replaceChildren();items.forEach(volume=>{const card=text("article",null,"card");card.append(text("h3",volume.display_name));const facts=text("dl",null,"facts");addFact(facts,"State",volume.stale?"stale / unknown":volume.online?"online":"offline");addFact(facts,"Disk",volume.disk_id);addFact(facts,"Filesystem",volume.filesystem);addFact(facts,"Used",formatBytes(volume.used_bytes));addFact(facts,"Free",`${formatBytes(volume.free_bytes)}${volume.free_percent==null?"":` (${volume.free_percent.toFixed(1)}%)`}`);addFact(facts,"Observed",formatTime(volume.observed_at));card.append(facts);root.append(card);});}
async function loadStatus(){try{const health=await fetch("/backup-status/health.json",{cache:"no-store"}).then(r=>{if(!r.ok)throw new Error(`health HTTP ${r.status}`);return r.json();});const data=await fetch("/backup-status/status.json",{cache:"no-store"}).then(r=>{if(!r.ok)throw new Error(`status HTTP ${r.status}`);return r.json();});if(data.generation_id!==health.generation_id)throw new Error("projection generation is changing; retrying");renderHealth(data,health);renderOperations(data.operations);renderJobs(data.jobs,data.operations);renderDisks(data.disks);renderVolumes(data.volumes);byId("updated").textContent=`updated ${formatTime(data.generated_at)}`;byId("connection").className="notice hidden";}catch(error){byId("connection").textContent=String(error);byId("connection").className="notice error";}}
loadStatus(); window.setInterval(loadStatus,10000);
