"""The playground single-page frontend: plain HTML/CSS/JS, zero external
dependencies, served directly by the server. UI strings live in an I18N table
below; the header button toggles EN/中文 and the choice persists in
localStorage. The chosen language also travels with /api/start, so the
scripted dialogs and approval questions follow it."""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>src playground</title>
<style>
  :root{--bd:#e5e7eb;--mut:#6b7280;--bg:#f7f8fa;--ink:#1f2937;--brand:#2563eb;}
  *{box-sizing:border-box}
  html,body{height:100%}
  /* The page itself doesn't scroll: each column scrolls internally, so the
     right-hand detail pane always stays in the viewport */
  body{margin:0;font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
       color:var(--ink);background:var(--bg);display:flex;flex-direction:column;overflow:hidden}
  header{padding:14px 20px;background:#fff;border-bottom:1px solid var(--bd);
         display:flex;align-items:baseline;gap:12px;flex-shrink:0}
  header h1{font-size:16px;margin:0}
  header span{color:var(--mut);font-size:12px}
  .langbtn{margin-left:auto;align-self:center;font-size:12px;padding:4px 12px;
           background:#fff;color:var(--ink);border:1px solid var(--bd)}
  .wrap{flex:1;min-height:0;width:100%;display:grid;grid-template-columns:260px 1fr 320px;
        gap:16px;padding:16px;max-width:1400px;margin:0 auto}
  .card{background:#fff;border:1px solid var(--bd);border-radius:10px;padding:14px;min-height:0}
  .card.flow{display:flex;flex-direction:column;overflow:hidden}
  .card.flow>*{flex-shrink:0}
  #scenes{flex:1;min-height:0;overflow-y:auto}
  #timeline{flex:1;min-height:0;overflow-y:auto}
  #stream{max-height:26vh;overflow-y:auto}
  .result{max-height:22vh;overflow-y:auto}
  .scene{display:block;width:100%;text-align:left;margin:6px 0;padding:9px 10px;border:1px solid var(--bd);
         border-radius:8px;background:#fff;cursor:pointer;color:var(--ink)}
  .scene:hover{border-color:var(--brand)}
  .scene.active{border-color:var(--brand);background:#eff6ff}
  .scene b{display:block;font-size:13px}
  .scene small{color:var(--mut)}
  .bar{flex-shrink:0;background:#fff;border-top:1px solid var(--bd)}
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
          flex:1;min-height:0;margin:0;overflow:auto}
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
  <span id="tagline"></span>
  <button id="langbtn" class="langbtn"></button></header>
<div class="wrap">
  <div class="card flow">
    <h3 id="scenesH" style="margin-top:0"></h3>
    <div id="scenes"></div>
  </div>
  <div class="card flow">
    <div id="desc" class="muted" style="margin-bottom:8px"></div>

    <div class="approve" id="approve">
      <div class="q" id="approveQ"></div>
      <button id="yes"></button>
      <button id="no" class="ghost"></button>
    </div>

    <h3 id="timelineH"></h3>
    <div id="timeline"></div>

    <h3 id="streamH" style="display:none"></h3>
    <div id="stream"></div>

    <h3 id="resultH"></h3>
    <div class="result" id="result"></div>
  </div>

  <div class="card flow">
    <h3 id="detailH" style="margin-top:0"></h3>
    <pre class="detail muted" id="detail"></pre>
  </div>
</div>

<div class="bar"><div class="in"><div></div><div style="display:flex;gap:8px">
  <input id="msg" type="text"/>
  <button id="run"></button>
</div></div></div>

<script>
let current = null, sid = null, since = 0, timer = null, streams = {}, chat = false;
const esc = s => String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const ICON = {run_started:"▶", node_started:"▸", node_completed:"✔", state_delta:"∆",
              interrupted:"⏸", resumed:"↺", run_completed:"🏁", run_failed:"✖",
              user_turn:"💬"};

const I18N = {
  en:{tag:"Pick a scenario, watch the node/state event stream, approve human-in-the-loop nodes right here",
      scenes:"Scenarios", timeline:"Event timeline", stream:"Model streaming",
      result:"Final result", detail:"Event detail",
      approve:"Approve / continue", reject:"Reject", run:"Run", send:"Send",
      ph:"Input for the agent", nothing:"Nothing has run yet.", press:'Press "Run" to start.',
      clickHint:"Click an event in the timeline to see its full data here.",
      confirm:"This flow needs your confirmation to continue.",
      names:{run_started:"run started", node_started:"node started", node_completed:"node completed",
             state_delta:"state delta", interrupted:"waiting for human", resumed:"resumed",
             run_completed:"run completed", run_failed:"run failed", user_turn:"user input"}},
  zh:{tag:"选一个场景运行，看节点/状态事件流，遇到人工节点就在这里审批",
      scenes:"示例场景", timeline:"事件流", stream:"模型吐字",
      result:"最终结果", detail:"事件详情",
      approve:"批准 / 继续", reject:"拒绝", run:"运行", send:"发送",
      ph:"给 Agent 的输入", nothing:"还没有运行。", press:"点「运行」开始。",
      clickHint:"点击事件流里的条目，这里显示它的完整数据。",
      confirm:"该流程需要你确认后继续。",
      names:{run_started:"运行开始", node_started:"节点开始", node_completed:"节点完成",
             state_delta:"状态更新", interrupted:"等待人工", resumed:"已恢复",
             run_completed:"运行完成", run_failed:"运行失败", user_turn:"用户输入"}}
};
let LANG = localStorage.getItem("pg-lang") || "en";
const t = k => I18N[LANG][k];
// Pick a scenario field for the current language: title/desc/default (+ _zh).
const L = (s,k) => LANG==="zh" ? s[k+"_zh"] : s[k];

async function api(url, opts){ const r = await fetch(url, opts); return r.json(); }

function applyLang(){
  document.documentElement.lang = LANG==="zh" ? "zh-CN" : "en";
  document.getElementById("tagline").textContent=t("tag");
  document.getElementById("scenesH").textContent=t("scenes");
  document.getElementById("timelineH").textContent=t("timeline");
  document.getElementById("streamH").textContent=t("stream");
  document.getElementById("resultH").textContent=t("result");
  document.getElementById("detailH").textContent=t("detail");
  document.getElementById("yes").textContent=t("approve");
  document.getElementById("no").textContent=t("reject");
  document.getElementById("msg").placeholder=t("ph");
  document.getElementById("run").textContent=chat?t("send"):t("run");
  document.getElementById("langbtn").textContent=LANG==="zh"?"EN":"中文";
  const det=document.getElementById("detail");
  if(det.classList.contains("muted")) det.textContent=t("clickHint");  // untouched only
  const tl=document.getElementById("timeline");
  if(tl.firstChild && tl.firstChild.classList && tl.firstChild.classList.contains("muted"))
    tl.firstChild.textContent=t("nothing");
}

async function loadScenes(){
  const list = await api("/api/scenarios");
  const box = document.getElementById("scenes"); box.innerHTML="";
  let keep=null, keepEl=null;
  list.forEach(s=>{
    const b=document.createElement("button"); b.className="scene";
    b.innerHTML=`<b>${esc(L(s,"title"))}</b><small>${esc(L(s,"desc"))}</small>`;
    b.onclick=(e)=>select(s, e.currentTarget); box.appendChild(b);
    if(current && s.key===current.key){keep=s; keepEl=b;}  // keep selection across a language switch
  });
  if(keep) select(keep, keepEl);
  else if(list.length) select(list[0], box.firstChild);
}
function select(s, el){
  const prev=current;
  current=s; since=0; sid=null; clearInterval(timer); streams={}; chat=false;
  document.getElementById("run").textContent=t("run");
  document.querySelectorAll(".scene").forEach(x=>x.classList.remove("active"));
  if(el) el.classList.add("active");
  // Refill the input with this scenario's default unless the user typed
  // something custom. The defaults of the scenario being left count as stock
  // too, so switching scenarios swaps the prefilled text instead of leaving
  // the old one behind.
  const msg=document.getElementById("msg"), v=msg.value;
  const stock = prev ? [prev.default, prev.default_zh] : [];
  if(!v || v===s.default || v===s.default_zh || stock.includes(v)) msg.value=L(s,"default");
  document.getElementById("desc").textContent=L(s,"desc");
  document.getElementById("timeline").innerHTML=`<div class="muted">${esc(t("press"))}</div>`;
  document.getElementById("streamH").style.display="none";
  document.getElementById("stream").innerHTML="";
  document.getElementById("result").style.display="none";
  document.getElementById("approve").style.display="none";
}

function render(ev){
  const box=document.getElementById("timeline");
  const stick=box.scrollHeight-box.scrollTop-box.clientHeight<48;  // don't yank the view when the user scrolled up to read history
  if(box.firstChild && box.firstChild.classList && box.firstChild.classList.contains("muted")) box.innerHTML="";
  const d=document.createElement("div"); d.className="ev "+ev.kind;
  let detail="";
  if(ev.data && ev.data.node) detail=`node <code>${esc(ev.data.node)}</code>`;
  if(ev.kind==="user_turn" && ev.data.text) detail=esc(ev.data.text);
  if(ev.kind==="interrupted" && ev.data.question) detail=esc(ev.data.question);
  if(ev.kind==="run_failed" && ev.data.reason) detail=esc(ev.data.reason);
  d.innerHTML=`<span class="ic">${ICON[ev.kind]||"•"}</span>
    <div><div>${t("names")[ev.kind]||esc(ev.kind)} ${detail}</div><small>#${ev.seq}</small></div>`;
  d.onclick=()=>{                                    // click any event to inspect its full data
    const p=document.getElementById("detail");
    p.classList.remove("muted");
    p.textContent=JSON.stringify(ev,null,2);
  };
  box.appendChild(d); if(stick) box.scrollTop=box.scrollHeight;
}

function onDelta(ev){
  // Token stream: group into one chunk per "run · node"; the reasoning
  // channel renders gray-italic, the body text normal.
  const key=`${(ev.data.run_id||"?").slice(0,8)} · ${ev.data.node_id||"?"}`;
  const s=streams[key]||(streams[key]={r:"",c:""});
  if(ev.data.kind==="reasoning") s.r+=ev.data.text||""; else s.c+=ev.data.text||"";
  const st=document.getElementById("stream");
  const stick=st.scrollHeight-st.scrollTop-st.clientHeight<48;
  document.getElementById("streamH").style.display="";
  st.innerHTML=Object.entries(streams).map(([k,s])=>
    `<div class="chunk"><div class="ck">${esc(k)}</div><div class="ct">`+
    (s.r?`<div class="rs">${esc(s.r)}</div>`:"")+esc(s.c)+`</div></div>`).join("");
  if(stick) st.scrollTop=st.scrollHeight;
}

async function poll(){
  const d = await api(`/api/events?sid=${sid}&since=${since}`);
  d.events.forEach(ev=>{since++; ev.kind==="llm_delta" ? onDelta(ev) : render(ev);});
  document.getElementById("stream").classList.toggle("live", d.status==="running");
  if(d.chat){ chat=true; document.getElementById("run").textContent=t("send"); }
  const ap=document.getElementById("approve");
  if(d.status==="suspended"){ ap.style.display="block";
    document.getElementById("approveQ").textContent=d.question||t("confirm");
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
  if(sid && chat){                                 // already in a chat: continue one turn with history
    await api("/api/turn",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({sid,input:text})});
    box.value="";
  } else {                                         // a fresh run
    streams={}; chat=false;
    document.getElementById("run").textContent=t("run");
    document.getElementById("timeline").innerHTML="";
    document.getElementById("streamH").style.display="none";
    document.getElementById("stream").innerHTML="";
    document.getElementById("result").style.display="none";
    document.getElementById("approve").style.display="none";
    // LANG travels with the run: the scripted dialog and approval question
    // are built in the UI language.
    const d=await api("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({scenario:current.key,input:text,lang:LANG})});
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
document.getElementById("langbtn").onclick=()=>{
  LANG = LANG==="en" ? "zh" : "en";
  localStorage.setItem("pg-lang",LANG);
  applyLang(); loadScenes();
};
applyLang();
loadScenes();
</script>
</body>
</html>
"""
