const $ = id => document.getElementById(id);
const form = $('config');
let defaults, selected = null, current = null, busy = false;
const states = {running:'运行中',stopping:'正在停止',completed:'已完成',failed:'失败',cancelled:'已停止',interrupted:'已中断'};
async function api(path, body) {
  const response = await fetch('/api/' + path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if (!response.ok) { const text = await response.text(); let message = text; try {message = JSON.parse(text).error || text;} catch {} throw new Error(message); }
  return response.json();
}
function notify(error) {$('notice').textContent = error?.message || error || '';}
function fill(config) {
  $('api-key').value = '';
  for (const [key,value] of Object.entries(config)) {
    const input = form.elements.namedItem(key);
    if (input) {if (input.type === 'checkbox') input.checked = value; else input.value = value;}
  }
  document.querySelectorAll('#profiles input').forEach(input => input.checked = config.profiles.includes(input.value));
}
function config() {
  const result = {...defaults};
  for (const [key,value] of Object.entries(defaults)) {
    const input = form.elements.namedItem(key);
    if (input) result[key] = typeof value === 'boolean' ? input.checked : typeof value === 'number' ? Number(input.value) : input.value.trim();
  }
  result.profiles = [...document.querySelectorAll('#profiles input:checked')].map(i => i.value);
  return result;
}
const fmt = value => value == null ? '—' : Number(value).toLocaleString('zh-CN',{maximumFractionDigits:2});
function element(tag, text, className) {const node = document.createElement(tag); node.textContent = text; if(className) node.className=className; return node;}
function metrics(data) {
  const summary = data?.summary, progress = data?.progress;
  const values = [['已完成请求',summary?.requests ?? progress?.requests],['成功请求',summary?.success ?? progress?.success],['在途 / 峰值',summary ? '0 / '+summary.peak_in_flight : progress ? progress.active+' / '+progress.peak_in_flight : null],['TTFT P95 / ms',summary?.ttft_ms?.p95],['TPOT P95 / ms',summary?.tpot_ms?.p95],['KV 命中率',summary?.weighted_cache_hit_ratio == null ? null : fmt(summary.weighted_cache_hit_ratio * 100)+'%']];
  $('metrics').replaceChildren(...values.map(([name,value]) => {const box=element('div','','metric');box.append(element('span',name),element('strong',typeof value==='string'?value:fmt(value)));return box;}));
}
function table(title, headers, rows) {
  const wrapper=element('div','','table-wrap');wrapper.append(element('h3',title));const t=document.createElement('table');
  const head=document.createElement('thead'), tr=document.createElement('tr');headers.forEach(h=>{const th=element('th',h);th.scope='col';tr.append(th);});head.append(tr);t.append(head);
  const body=document.createElement('tbody');rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(cell=>tr.append(element('td',cell)));body.append(tr);});t.append(body);wrapper.append(t);return wrapper;
}
function renderReport(s) {
  $('report').replaceChildren();if(!s)return;
  const entries=[['TTFT',s.ttft_ms],['TPOT',s.tpot_ms],['E2E',s.e2e_ms],['SSE 块间隔',s.chunk_gap_mean_ms],['事件循环延迟',s.event_loop_lag_ms]];
  $('report').append(table('延迟分布 / ms',['指标','样本','P50','P95','P99'],entries.map(([name,d])=>[name,d?.count??0,fmt(d?.p50),fmt(d?.p95),fmt(d?.p99)])));
  $('report').append(element('p',`成功吞吐 ${fmt(s.successful_requests_per_s)} req/s · 已报告输出 ${fmt(s.reported_output_tokens_per_s)} token/s · 耗时 ${fmt(s.elapsed_s)} s`,'muted'));
  $('report').append(element('p',`缓存 usage 覆盖 ${fmt(s.cache_usage_coverage*100)}% · 输出 usage 覆盖 ${fmt(s.output_usage_coverage*100)}% · 后端同节点比例 ${s.backend_same_ratio == null?'—':fmt(s.backend_same_ratio*100)+'%'}（${s.backend_adjacent_pairs} 对）`,'footnote'));
  for(const [title,group] of [['按工具画像',s.by_profile],['按会话轮次（从 0 开始）',s.by_turn]]) {
    if(group) $('report').append(table(title,['分组','成功 / 请求','TTFT P95','TPOT P95'],Object.entries(group).map(([name,d])=>[name,`${d.success} / ${d.requests}`,fmt(d.ttft_ms?.p95),fmt(d.tpot_ms?.p95)])));
  }
  if(s.failed) $('report').append(element('p',`失败 ${s.failed}：${JSON.stringify(s.errors)}`));
}
async function refresh() {
  const runs = await api('runs');
  busy = runs.some(r=>['running','stopping'].includes(r.status)); $('start').disabled = busy;
  $('history').replaceChildren();
  if(!runs.length) $('history').append(element('p','还没有实验记录。先预览负载，再启动测试。','muted'));
  for(const run of runs) {
    const button=element('button','');button.type='button';button.setAttribute('aria-current',String(selected===run.id));
    const title=element('div',new Date(run.created).toLocaleString());title.append(element('small',run.id.slice(0,8),'mono'));
    button.append(title,element('span',states[run.status]||run.status,'badge'));
    button.onclick=()=>{selected=run.id;refresh().catch(notify);};$('history').append(button);
  }
  if(!selected&&runs.length) selected=runs[0].id;
  if(!selected) return;
  const requested=selected;const data=await api('runs/'+requested);if(selected!==requested)return;current=data;
  $('status').textContent=states[data.status]||data.status;$('run-label').textContent=data.id+' · '+data.config.model;
  $('stop').disabled=data.status!=='running';$('reuse').disabled=false;
  for(const [id,file] of [['download','summary.json'],['raw','requests.jsonl']]) {$(id).hidden=!data.summary;$(id).href=`/api/runs/${data.id}/artifacts/${file}`;}
  metrics(data);renderReport(data.summary);
  if(data.error) $('report').prepend(element('p',data.error));
}
form.onsubmit=async event=>{event.preventDefault();$('start').disabled=true;notify('');try{const run=await api('runs',{...config(),api_key:$('api-key').value.trim()});$('api-key').value='';selected=run.id;await refresh();}catch(error){notify(error);$('start').disabled=busy;}};
$('preview').onclick=async()=>{if(!form.reportValidity())return;const button=$('preview');button.disabled=true;notify('');try{$('payload').textContent=JSON.stringify(await api('preview',config()),null,2);$('preview-dialog').showModal();}catch(error){notify(error);}finally{button.disabled=false;}};
$('close-preview').onclick=()=>$('preview-dialog').close();
$('stop').onclick=async()=>{$('stop').disabled=true;try{await api('runs/'+selected+'/stop',{});await refresh();}catch(error){notify(error);}};
$('reuse').onclick=()=>{if(current){fill(current.config);form.scrollIntoView({block:'start'});notify('已复用配置；尚未启动新任务。');}};
$('refresh').onclick=()=>refresh().catch(notify);
async function poll(){try{await refresh();}catch(error){notify('连接失败：'+error.message);}finally{setTimeout(poll,2000);}}
async function init(){defaults=await api('defaults');fill(defaults);metrics(null);await poll();}
init().catch(notify);
