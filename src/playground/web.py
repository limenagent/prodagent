"""playground 单页前端：原生 HTML/CSS/JS，无任何外部依赖，由 server 直接返回。"""

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>src playground</title>
<style>
  :root{--bd:#e5e7eb;--mut:#6b7280;--bg:#f7f8fa;--ink:#1f2937;--brand:#2563eb;}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
       color:var(--ink);background:var(--bg);padding-bottom:64px}
  header{padding:14px 20px;background:#fff;border-bottom:1px solid var(--bd);
         display:flex;align-items:baseline;gap:12px}
  header h1{font-size:16px;margin:0}
  header span{color:var(--mut);font-size:12px}
  .wrap{display:grid;grid-template-columns:260px 1fr 320px;gap:16px;padding:16px;max-width:1400px;margin:0 auto}
  .card{background:#fff;border:1px solid var(--bd);border-radius:10px;padding:14px}
  .scene{display:block;width:100%;text-align:left;margin:6px 0;padding:9px 10px;border:1px solid var(--bd);
         border-radius:8px;background:#fff;cursor:pointer}
  .scene:hover{border-color:var(--brand)}
  .scene.active{border-color:var(--brand);background:#eff6ff}
  .scene b{display:block;font-size:13px}
  .scene small{color:var(--mut)}
  .bar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid var(--bd);z-index:10}
  .bar .in{display:grid;grid-template-columns:260px 1fr;gap:16px;
           max-width:1100px;margin:0 auto;padding:10px 16px}
  input[type=text]{flex:1;padding:9px 11px;border:1px solid var(--bd);border-radius:8px;font-size:14px}
  button{padding:9px 16px;border:0;border-radius:8px;background:var(--brand);color:#fff;cursor:pointer;font-size:14px}
  button.ghost{background:#fff;color:var(--ink);border:1px solid var(--bd)}
  button:disabled{opacity:.5;cursor:not-allowed}
  h3{font-size:13px;color:var(--mut);margin:16px 0 8px;font-weight:600}
  .ev{display:flex;gap:10px;padding:7px 10px;border-left:3px solid var(--bd);margin:6px 0;background:#fafafa;border-radius:0 6px 6px 0}
  .ev .ic{width:18px;text-align:center}
  .ev.node_started{border-color:#93c5fd}.ev.node_completed{border-color:#86efac}
  .ev.state_delta{border-color:#d8b4fe}.ev.interrupted,.ev.suspended{border-color:#fbbf24;background:#fffbeb}
  .ev.run_completed{border-color:#22c55e;background:#f0fdf4}.ev.run_failed{border-color:#ef4444;background:#fef2f2}
  .ev small{color:var(--mut)}
  .ev{cursor:pointer}
  .ev:hover{background:#eff6ff}
  .detail{white-space:pre-wrap;font:12px/1.5 ui-monospace,Menlo,monospace;
          max-height:75vh;overflow:auto}
  .approve{margin:12px 0;padding:12px;border:1px solid #fbbf24;background:#fffbeb;border-radius:8px;display:none}
  .approve .q{font-weight:600;margin-bottom:8px}
  .chunk{margin:8px 0;border:1px solid var(--bd);border-radius:8px;overflow:hidden}
  .chunk .ck{padding:3px 10px;background:#f3f4f6;color:var(--mut);font-size:12px}
  .chunk .ct{padding:8px 10px;white-space:pre-wrap;font-size:13px}
  .chunk .rs{color:var(--mut);font-style:italic;border-left:2px solid var(--bd);
             padding-left:8px;margin-bottom:6px;max-height:140px;overflow:auto}
  #stream.live .chunk:last-child .ct::after{content:"▍";color:var(--brand);animation:blink 1s steps(1) infinite}
  @keyframes blink{50%{opacity:0}}
  .result{margin-top:12px;padding:12px;background:#f0fdf4;border:1px solid #86efac;border-radius:8px;white-space:pre-wrap;display:none}
  .muted{color:var(--mut)}
  code{background:#f3f4f6;padding:1px 5px;border-radius:4px}
</style>
</head>
<body>
<header><h1>src playground</h1>
  <span>选一个场景运行，看节点/状态事件流，遇到人工节点就在这里审批</span></header>
<div class="wrap">
  <div class="card">
    <h3 style="margin-top:0">示例场景</h3>
    <div id="scenes"></div>
  </div>
  <div class="card">
    <div id="desc" class="muted" style="margin-bottom:8px"></div>

    <div class="approve" id="approve">
      <div class="q" id="approveQ"></div>
      <button id="yes">批准 / 继续</button>
      <button id="no" class="ghost">拒绝</button>
    </div>

    <h3>事件流</h3>
    <div id="timeline"><div class="muted">还没有运行。</div></div>

    <h3 id="streamH" style="display:none">模型吐字</h3>
    <div id="stream"></div>

    <h3>最终结果</h3>
    <div class="result" id="result"></div>
  </div>

  <div class="card">
    <h3 style="margin-top:0">事件详情</h3>
    <pre class="detail muted" id="detail">点击事件流里的条目，这里显示它的完整数据。</pre>
  </div>
</div>

<div class="bar"><div class="in"><div></div><div style="display:flex;gap:8px">
  <input id="msg" type="text" placeholder="给 Agent 的输入"/>
  <button id="run">运行</button>
</div></div></div>

<script>
let current = null, sid = null, since = 0, timer = null, streams = {}, chat = false;
const esc = s => String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const ICON = {run_started:"▶", node_started:"▸", node_completed:"✔", state_delta:"∆",
              interrupted:"⏸", resumed:"↺", run_completed:"🏁", run_failed:"✖",
              user_turn:"💬"};
const NAME = {run_started:"运行开始", node_started:"节点开始", node_completed:"节点完成",
              state_delta:"状态更新", interrupted:"等待人工", resumed:"已恢复",
              run_completed:"运行完成", run_failed:"运行失败", user_turn:"用户输入"};

async function api(url, opts){ const r = await fetch(url, opts); return r.json(); }

async function loadScenes(){
  const list = await api("/api/scenarios");
  const box = document.getElementById("scenes"); box.innerHTML="";
  list.forEach(s=>{
    const b=document.createElement("button"); b.className="scene";
    b.innerHTML=`<b>${s.title}</b><small>${s.desc}</small>`;
    b.onclick=(e)=>select(s, e.currentTarget); box.appendChild(b);
  });
  if(list.length) select(list[0], box.firstChild);
}
function select(s, el){
  current=s; since=0; sid=null; clearInterval(timer); streams={}; chat=false;
  document.getElementById("run").textContent="运行";
  document.querySelectorAll(".scene").forEach(x=>x.classList.remove("active"));
  if(el) el.classList.add("active");
  document.getElementById("msg").value=s.default;
  document.getElementById("desc").textContent=s.desc;
  document.getElementById("timeline").innerHTML='<div class="muted">点「运行」开始。</div>';
  document.getElementById("streamH").style.display="none";
  document.getElementById("stream").innerHTML="";
  document.getElementById("result").style.display="none";
  document.getElementById("approve").style.display="none";
}

function render(ev){
  const box=document.getElementById("timeline");
  if(box.firstChild && box.firstChild.classList && box.firstChild.classList.contains("muted")) box.innerHTML="";
  const d=document.createElement("div"); d.className="ev "+ev.kind;
  let detail="";
  if(ev.data && ev.data.node) detail=`节点 <code>${ev.data.node}</code>`;
  if(ev.kind==="user_turn" && ev.data.text) detail=esc(ev.data.text);
  if(ev.kind==="interrupted" && ev.data.question) detail=esc(ev.data.question);
  if(ev.kind==="run_failed" && ev.data.reason) detail=esc(ev.data.reason);
  d.innerHTML=`<span class="ic">${ICON[ev.kind]||"•"}</span>
    <div><div>${NAME[ev.kind]||esc(ev.kind)} ${detail}</div><small>#${ev.seq}</small></div>`;
  d.onclick=()=>{                                    // 点谁看谁的完整数据
    const p=document.getElementById("detail");
    p.classList.remove("muted");
    p.textContent=JSON.stringify(ev,null,2);
  };
  box.appendChild(d); box.scrollTop=box.scrollHeight;
}

function onDelta(ev){
  // 吐字流：按「run · 节点」聚成一块；思考通道(reasoning)灰色斜体，正文正常。
  const key=`${(ev.data.run_id||"?").slice(0,8)} · ${ev.data.node_id||"?"}`;
  const s=streams[key]||(streams[key]={r:"",c:""});
  if(ev.data.kind==="reasoning") s.r+=ev.data.text||""; else s.c+=ev.data.text||"";
  document.getElementById("streamH").style.display="";
  document.getElementById("stream").innerHTML=Object.entries(streams).map(([k,s])=>
    `<div class="chunk"><div class="ck">${k}</div><div class="ct">`+
    (s.r?`<div class="rs">${esc(s.r)}</div>`:"")+esc(s.c)+`</div></div>`).join("");
}

async function poll(){
  const d = await api(`/api/events?sid=${sid}&since=${since}`);
  d.events.forEach(ev=>{since++; ev.kind==="llm_delta" ? onDelta(ev) : render(ev);});
  document.getElementById("stream").classList.toggle("live", d.status==="running");
  if(d.chat){ chat=true; document.getElementById("run").textContent="发送"; }
  const ap=document.getElementById("approve");
  if(d.status==="suspended"){ ap.style.display="block";
    document.getElementById("approveQ").textContent=d.question||"该流程需要你确认后继续。";
    clearInterval(timer);
  } else if(d.status==="completed" || d.status==="failed"){
    clearInterval(timer);
    const r=document.getElementById("result");
    r.style.display="block"; r.textContent=d.output || d.error || "";
  }
}

document.getElementById("run").onclick=async()=>{
  if(!current) return;
  const box=document.getElementById("msg"), text=box.value.trim();
  if(!text) return;
  if(sid && chat){                                 // 已在对话里：带历史续一轮
    await api("/api/turn",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({sid,input:text})});
    box.value="";
  } else {                                         // 新的一次运行
    streams={}; chat=false;
    document.getElementById("run").textContent="运行";
    document.getElementById("timeline").innerHTML="";
    document.getElementById("streamH").style.display="none";
    document.getElementById("stream").innerHTML="";
    document.getElementById("result").style.display="none";
    document.getElementById("approve").style.display="none";
    const d=await api("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({scenario:current.key,input:text})});
    sid=d.sid; since=0;
  }
  clearInterval(timer); timer=setInterval(poll,400); poll();
};
async function decide(approved){
  document.getElementById("approve").style.display="none";
  await api("/api/resume",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({sid,approved})});
  since = since; timer=setInterval(poll,400); poll();
}
document.getElementById("yes").onclick=()=>decide(true);
document.getElementById("no").onclick=()=>decide(false);
document.getElementById("msg").addEventListener("keydown",e=>{
  if(e.key==="Enter") document.getElementById("run").click();
});
loadScenes();
</script>
</body>
</html>
"""
