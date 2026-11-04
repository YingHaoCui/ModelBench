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
  const summary = data?.summary ?? data?.progress;
  const s = summary, final = Boolean(data?.summary);
  const ttft = s?.observed_ttft_ms ?? s?.ttft_ms;
  const values = [
    ['已完成 / 失败请求',s ? `${s.requests} / ${s.failed ?? 0}` : null],
    ['在途 / 峰值',s ? `${s.active ?? 0} / ${s.peak_in_flight}` : null],
    ['等待首 token / 生成中',s ? `${s.waiting_first_token ?? 0} / ${s.streaming ?? 0}` : null],
    ['已观测 TTFT 均值 / ms',ttft?.mean],['已观测 TTFT P95 / ms',ttft?.p95],
    ['TPOT P95 / ms',s?.tpot_ms?.p95],['生成速度 / tok/s',s?.weighted_decode_tokens_per_s],
    ['集群输出吞吐 / tok/s',s?.reported_output_tokens_per_s],
    ['KV 命中率',s?.weighted_cache_hit_ratio == null ? null : fmt(s.weighted_cache_hit_ratio*100)+'%']
  ];
  $('metrics').replaceChildren(...values.map(([name,value]) => {const box=element('div','','metric');box.append(element('span',name),element('strong',typeof value==='string'?value:fmt(value)));return box;}));
  if(s) $('metrics').append(element('small',`${final?'最终结果':'实时快照'} · 已观测 TTFT ${ttft?.count ?? 0} 个请求（含在途/失败请求） · ${s.updated_at ? new Date(s.updated_at).toLocaleTimeString() : ''}`));
}

function table(title, headers, rows) {
  const wrapper=element('div','','table-wrap');wrapper.append(element('h3',title));const t=document.createElement('table');
  const head=document.createElement('thead'), tr=document.createElement('tr');headers.forEach(h=>{const th=element('th',h);th.scope='col';tr.append(th);});head.append(tr);t.append(head);
  const body=document.createElement('tbody');rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(cell=>tr.append(element('td',cell)));body.append(tr);});t.append(body);wrapper.append(t);return wrapper;
}
function renderReport(s) {
  $('report').replaceChildren();if(!s)return;
  const entries=[['TTFT',s.ttft_ms],['首个正文',s.first_text_ms],['首个推理',s.first_reasoning_ms],['响应头',s.headers_ms],['TPOT',s.tpot_ms],['生成耗时',s.generation_ms],['E2E',s.e2e_ms],['SSE 块间隔',s.chunk_gap_mean_ms],['事件循环延迟',s.event_loop_lag_ms]];
  $('report').append(table('延迟分布 / ms（成功请求；循环延迟为客户端采样）',['指标','样本','均值','P50','P95','P99'],entries.map(([name,d])=>[name,d?.count??0,fmt(d?.mean),fmt(d?.p50),fmt(d?.p95),fmt(d?.p99)])));
  const tokenLabels = {prompt_tokens:'输入总量（包含缓存命中）',uncached_tokens:'未缓存输入',cached_tokens:'缓存读取',completion_tokens:'输出（包含推理）',reasoning_tokens:'其中推理 token',total_tokens:'输入 + 输出'};
  if(s.token_totals) $('report').append(table('Token 用量（服务端报告，成功请求）',['类别','累计 tokens','有数据的请求数'],Object.entries(tokenLabels).map(([key,label])=>[label,fmt(s.token_totals[key]?.value),s.token_totals[key]?.requests ?? 0])));
  $('report').append(element('p',`会话 ${fmt(s.sessions)} · 工具调用 ${fmt(s.tool_calls)} · 请求累计耗时 ${fmt(s.llm_time_ms == null ? null : s.llm_time_ms/1000)} s · 生成累计耗时 ${fmt(s.generation_time_ms == null ? null : s.generation_time_ms/1000)} s`,'muted'));
  $('report').append(element('p',`成功吞吐 ${fmt(s.successful_requests_per_s)} req/s · 运行时长 ${fmt(s.elapsed_s)} s`,'muted'));
  $('report').append(element('p',`缓存 usage 覆盖 ${fmt(s.cache_usage_coverage*100)}% · 输出 usage 覆盖 ${fmt(s.output_usage_coverage*100)}% · 后端同节点比例 ${s.backend_same_ratio == null?'—':fmt(s.backend_same_ratio*100)+'%'}（${s.backend_adjacent_pairs} 对）`,'footnote'));
  $('report').append(element('p','生成速度 = 有 usage 请求的输出 token 总量 / 对应生成耗时总量；集群吞吐按运行墙钟时间计算。TPOT 需有 token usage 和至少两个输出块。工具在本工具内不实际执行，故不统计真实工具耗时。','footnote'));
  if(!s.requests) $('report').append(element('p',s.streaming ? '正在生成：TTFT 已显示，等待请求完成后统计用量与生成速度。' : '等待首个有效输出；收到响应头或心跳不会计作首 token。','muted'));
  if(s.requests && !s.output_usage_coverage) $('report').append(element('p','尚无成功请求返回 completion token 用量：TTFT 不依赖 usage，但 TPOT 和生成速度将显示 —。','muted'));
  for(const [title,group] of [['按工具画像',s.by_profile],['按会话轮次（从 0 开始）',s.by_turn]]) {
    if(group) $('report').append(table(title,['分组','成功 / 请求','TTFT P95','TPOT P95'],Object.entries(group).map(([name,d])=>[name,`${d.success} / ${d.requests}`,fmt(d.ttft_ms?.p95),fmt(d.tpot_ms?.p95)])));
  }
  if(s.recent_requests?.length) $('report').append(table('最近 20 个已结束请求',['会话/轮次','状态','TTFT ms','TPOT ms','输出 tok','缓存 tok'],s.recent_requests.map(r=>[`${r.session_index}/${r.turn}`,r.ok?'成功':`${r.status ?? '—'} ${r.error ?? '失败'}`,fmt(r.ttft_ms),fmt(r.tpot_ms),fmt(r.completion_tokens),fmt(r.cached_tokens)])));
  if(s.failed) $('report').append(element('p',`失败 ${s.failed}：${JSON.stringify(s.errors)}`));
  if(Object.keys(s.metric_unavailable ?? {}).length) $('report').append(table('指标缺失诊断',['原因','请求数'],Object.entries(s.metric_unavailable).map(([reason,count])=>[reason,count])));
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
  metrics(data);renderReport(data.summary ?? data.progress);
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
