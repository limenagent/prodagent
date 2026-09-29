"""The playground single-page frontend: plain HTML/CSS/JS, zero external
dependencies, served directly by the server.

One ledger, four folds. Everything on the page is a projection of the same
event stream the kernel emits — the UI just folds it different ways:

    transcript  the run as a conversation (messages channel + lifecycle facts)
    trace       the Run tree as a time-axis waterfall (durations by ts subtraction)
    events      the ledger itself, grouped by Run, kind-filterable
    state       channels folded live (arrays append, scalars last-write)
    graph       the plan as it was *walked* — edges appear when control facts
                record them, so the picture grows out of the stream, honestly
    files       artifact pointers (bytes stay in the BlobStore)

Design language — an "editorly engineering console": warm paper, ink, a deep
ink-green accent; serif/sans/mono pairing; hairline rules; inline SVG glyphs;
every scrolling region actually scrolls. UI strings live in the I18N table;
the header button toggles EN/中文 (persisted, and it travels with /api/start so
scripted dialogs follow it too).
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>prodagent playground</title>
<style>
  :root{
    --bg:#f3f1ea; --surface:#fbfaf5; --raised:#ffffff; --sunken:#eeebe2;
    --ink:#1e2925; --ink-2:#4d574f; --ink-3:#8f968c; --ink-4:#b4b9ad;
    --line:#e3ded1; --line-2:#ece8dd;
    --brand:#2e5e4c; --brand-2:#264f3f; --brand-soft:#e8efea; --brand-line:#c8d9cb;
    --amber:#93651e; --amber-soft:#f5ecd8; --amber-line:#e2d0a4;
    --red:#a14739;   --red-soft:#f7e8e5;   --red-line:#e1c1b9;
    --blue:#3c6c8d;  --blue-soft:#e7eef3;  --blue-line:#c5d6e2;
    --serif:"Iowan Old Style","Palatino Linotype",Palat,Georgia,"Songti SC","STSong",serif;
    --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
    --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Code",Menlo,Consolas,"Liberation Mono",monospace;
    --r-lg:14px; --r:10px; --r-sm:8px;
    --sh-sm:0 1px 2px rgba(30,41,37,.05);
    --sh:0 1px 2px rgba(30,41,37,.04),0 8px 24px -12px rgba(30,41,37,.14);
    --ease:cubic-bezier(.22,.61,.36,1);
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;font:13px/1.6 var(--sans);color:var(--ink);background:var(--bg);
       display:flex;flex-direction:column;overflow:hidden;
       -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}

  /* ── top bar ─────────────────────────────────────────────── */
  header{height:56px;flex-shrink:0;padding:0 18px;background:var(--surface);
         border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px}
  .wordmark{display:flex;align-items:center;gap:9px;font-family:var(--serif);
            font-size:18px;font-weight:600;letter-spacing:-.2px;color:var(--ink);white-space:nowrap}
  .wordmark svg{color:var(--brand)}
  .crumb{font-size:12.5px;color:var(--ink-3);display:flex;align-items:center;gap:8px;
         padding-left:14px;border-left:1px solid var(--line);min-width:0;overflow:hidden}
  .crumb b{color:var(--ink-2);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .crumb .rid{font:11px var(--mono);white-space:nowrap}
  .spacer{margin-left:auto}
  .pill{display:inline-flex;align-items:center;gap:7px;font:11px var(--mono);
        padding:4px 11px;border-radius:999px;border:1px solid var(--line);
        background:var(--surface);color:var(--ink-2);letter-spacing:.3px;white-space:nowrap}
  .pill .d{width:7px;height:7px;border-radius:50%;background:var(--ink-3)}
  .pill.running{color:var(--brand);background:var(--brand-soft);border-color:var(--brand-line)}
  .pill.running .d{background:var(--brand);animation:pulse 1.3s var(--ease) infinite}
  .pill.suspended{color:var(--amber);background:var(--amber-soft);border-color:var(--amber-line)}
  .pill.suspended .d{background:var(--amber)}
  .pill.completed{color:var(--brand);background:var(--brand-soft);border-color:var(--brand-line)}
  .pill.completed .d{background:var(--brand)}
  .pill.failed{color:var(--red);background:var(--red-soft);border-color:var(--red-line)}
  .pill.failed .d{background:var(--red)}
  @keyframes pulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.35);opacity:.55}}
  .iconbtn{display:inline-flex;align-items:center;gap:6px;font:12px var(--mono);
           padding:6px 12px;background:var(--surface);color:var(--ink-2);
           border:1px solid var(--line);border-radius:var(--r-sm);cursor:pointer;white-space:nowrap}
  .iconbtn:hover{color:var(--ink);border-color:var(--ink-4)}
  #inspToggle{display:none}

  /* ── workspace ───────────────────────────────────────────── */
  .wrap{flex:1;min-height:0;width:100%;display:grid;
        grid-template-columns:250px minmax(0,1fr) 392px;gap:14px;padding:14px;
        max-width:1560px;margin:0 auto}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);
        box-shadow:var(--sh-sm);min-height:0;display:flex;flex-direction:column;overflow:hidden}
  .card>.pad{padding:14px 16px 0}
  .lab{font:600 10.5px var(--mono);letter-spacing:.12em;text-transform:uppercase;
       color:var(--ink-3);display:flex;align-items:center;gap:7px;flex-shrink:0}
  .scroll{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;
          scrollbar-width:thin}
  .scroll::-webkit-scrollbar{width:9px;height:9px}
  .scroll::-webkit-scrollbar-thumb{background:#d8d2c4;border-radius:8px;border:3px solid transparent;background-clip:content-box}
  .scroll::-webkit-scrollbar-thumb:hover{background:#c3bca9;background-clip:content-box;border:3px solid transparent}

  /* scenarios */
  #side .scroll{padding:8px 8px 12px}
  .scn{width:100%;display:grid;grid-template-columns:24px 1fr;gap:2px 8px;
       text-align:left;margin:2px 0;padding:8px 10px 8px 8px;border:1px solid transparent;
       border-radius:var(--r-sm);background:none;cursor:pointer;color:var(--ink)}
  .scn .no{grid-row:1 / span 2;font:11px var(--mono);color:var(--ink-4);padding-top:2px}
  .scn b{font-size:12.5px;font-weight:600;line-height:1.35}
  .scn small{color:var(--ink-3);font-size:11px;line-height:1.45}
  .scn:hover{background:#f2efe6}
  .scn.active{background:var(--brand-soft);border-color:var(--brand-line)}
  .scn.active .no{color:var(--brand)}
  .scn.active b{color:var(--brand-2)}

  /* ── transcript (middle) ─────────────────────────────────── */
  .lead{font-size:12.5px;color:var(--ink-2);margin:0;padding:0 2px 10px;line-height:1.55;flex-shrink:0}
  #transcript{padding:2px 16px 14px;display:flex;flex-direction:column;gap:10px}
  .tc{border:1px solid var(--line);border-radius:var(--r);background:var(--raised);
      box-shadow:var(--sh-sm);animation:fade .2s var(--ease);max-width:100%}
  @keyframes fade{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
  .tc .h{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line-2);
         font:11px var(--mono);color:var(--ink-3)}
  .tc .h .who{font-weight:600;color:var(--brand-2)}
  .tc .h .aux{color:var(--ink-4)}
  .tc .bd{padding:10px 12px;white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.65}
  .tc.user{background:var(--brand-soft);border-color:var(--brand-line);
           border-left:3px solid var(--brand)}
  .tc.user .h{border-bottom:none;padding-bottom:0;color:var(--brand)}
  .tc.user .bd{font-family:var(--serif);font-size:14px;padding-top:2px}
  .tc.turn .bd{font-size:12.5px}
  .tc.turn .think{color:var(--ink-3);font-size:12px;border-left:2px solid var(--line);
                  padding:2px 0 2px 10px;margin:4px 0 8px;max-height:150px;overflow:auto}
  .tc.turn .think:empty{display:none}
  .tc.turn.live .bd::after{content:"▍";color:var(--brand);animation:blink 1s steps(1) infinite}
  @keyframes blink{50%{opacity:0}}
  .tc.child{margin-left:26px;border-style:dashed}
  .tc.tool .bd{font:12px/1.6 var(--mono);white-space:pre-wrap}
  .tc.tool .args{color:var(--ink-2)}
  .tc.tool .res{color:var(--brand-2);border-top:1px dashed var(--line);margin-top:8px;padding-top:8px}
  .tc.tool .res.err{color:var(--red)}
  .tc.turn .bd .tc.tool,.tc.turn .bd .tc.file{margin:8px 0 2px;background:var(--surface)}
  .tc.file{border:1px solid var(--line);border-radius:var(--r-sm);background:var(--raised)}
  .tc.file .bd{display:flex;align-items:center;gap:8px;font:12px var(--mono);padding:8px 12px;
               white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .tc.turn .bd .tc.file:hover{background:var(--brand-soft)}
  .tc .h .badge{margin-left:auto;font:10px var(--mono);color:var(--brand-2)}
  .tc.deleg{cursor:pointer}
  .tc.deleg:hover{border-color:var(--blue-line);background:var(--blue-soft)}
  .tc.deleg .bd{font:12px var(--mono);color:var(--blue)}
  .tc.file{cursor:pointer}
  .tc.file:hover{border-color:var(--brand-line);background:var(--brand-soft)}
  .tc.file .bd{font:12px var(--mono);display:flex;align-items:center;gap:8px;padding:8px 12px}
  .tc.file svg{color:var(--brand);flex-shrink:0}
  .tc.park{border-color:var(--amber-line);background:var(--amber-soft)}
  .tc.park .h{color:var(--amber);border-bottom-color:var(--amber-line)}
  .tc.park .q{font-family:var(--serif);font-size:14.5px;font-weight:600;color:#5c4318;padding:10px 12px 4px}
  .tc.park .pk{font:11px/1.7 var(--mono);color:var(--amber);background:rgba(255,255,255,.55);
               border:1px solid var(--amber-line);border-radius:var(--r-sm);
               margin:8px 12px;padding:8px 10px;white-space:pre-wrap;max-height:160px;overflow:auto}
  .tc.park .row{display:flex;gap:9px;padding:0 12px 12px}
  .tc.fin{background:var(--brand-soft);border-color:var(--brand-line)}
  .tc.fin.bad{background:var(--red-soft);border-color:var(--red-line)}
  .tc.fin .bd{font:12px var(--mono);color:var(--brand-2);white-space:pre-wrap;word-break:break-word}
  .tc.fin .bd .dur{margin-right:10px;color:var(--ink-3)}
  .tc.fin.bad .bd{color:var(--red)}
  .tc.sys{border:none;box-shadow:none;background:none;color:var(--ink-3);
          font:11px var(--mono);padding:0;display:flex;gap:8px;align-items:center}
  .btn{display:inline-flex;align-items:center;gap:7px;justify-content:center;
       font:12.5px var(--sans);font-weight:600;padding:8px 18px;border-radius:var(--r-sm);
       border:1px solid var(--brand);background:var(--brand);color:#fff;cursor:pointer;
       transition:all .14s var(--ease)}
  .btn:hover{background:var(--brand-2);border-color:var(--brand-2)}
  .btn.ghost{background:var(--surface);color:var(--ink-2);border-color:var(--line)}
  .btn.ghost:hover{color:var(--red);border-color:var(--red-line);background:var(--red-soft)}
  .btn:disabled{opacity:.5;cursor:not-allowed}
  .empty{margin:auto;max-width:250px;text-align:center;color:var(--ink-3);font-size:12px;
         line-height:1.8;padding:30px 0}
  .empty svg{color:var(--ink-4);margin-bottom:10px}

  /* input bar lives inside the middle card — it can never be painted over */
  .bar{flex-shrink:0;border-top:1px solid var(--line);padding:10px 14px;background:var(--surface)}
  .field{display:flex;gap:9px;align-items:center;background:var(--raised);
         border:1px solid var(--line);border-radius:999px;padding:5px 6px 5px 16px;
         box-shadow:var(--sh-sm);transition:border-color .14s var(--ease)}
  .field:focus-within{border-color:var(--brand)}
  #msg{flex:1;border:none;outline:none;background:transparent;font:13px var(--sans);color:var(--ink);min-width:0}
  #msg::placeholder{color:var(--ink-4)}
  .send{flex:0 0 auto;width:32px;height:32px;border-radius:50%;border:none;
        background:var(--brand);color:#fff;display:inline-flex;align-items:center;justify-content:center;
        cursor:pointer;transition:all .14s var(--ease)}
  .send:hover{background:var(--brand-2)}
  .send:disabled{opacity:.45;cursor:not-allowed}

  /* ── inspector (right) ───────────────────────────────────── */
  #insp{position:relative}
  .tabs{display:flex;gap:2px;padding:8px 8px 0;flex-shrink:0;border-bottom:1px solid var(--line)}
  .tabs button{flex:1;display:inline-flex;align-items:center;justify-content:center;gap:5px;
       font:11.5px var(--mono);padding:7px 4px;border:none;border-bottom:2px solid transparent;
       background:transparent;color:var(--ink-3);cursor:pointer;letter-spacing:.2px}
  .tabs button:hover{color:var(--ink)}
  .tabs button.active{color:var(--brand-2);border-bottom-color:var(--brand);font-weight:600}
  .tabs button .n{font-size:9.5px;color:var(--ink-4)}
  .panel{flex:1;min-height:0;display:none;flex-direction:column}
  .panel.active{display:flex}
  .pane-top{flex-shrink:0;padding:10px 14px 8px;display:flex;flex-direction:column;gap:8px}
  .hint{font:11px/1.6 var(--mono);color:var(--ink-4)}
  .chips{display:flex;flex-wrap:wrap;gap:5px}
  .chip{font:10.5px var(--mono);padding:3px 10px;border:1px solid var(--line);border-radius:999px;
        background:var(--surface);color:var(--ink-3);cursor:pointer;transition:all .12s var(--ease)}
  .chip:hover{color:var(--ink-2);border-color:var(--ink-4)}
  .chip.active{color:#fff;background:var(--brand);border-color:var(--brand)}
  .chip.run{color:var(--blue);border-color:var(--blue-line);background:var(--blue-soft)}
  .chip.run .x{margin-left:5px;cursor:pointer}

  /* waterfall */
  #wf{padding:4px 14px 16px}
  .wrow{position:relative;padding:4px 0;cursor:pointer;border-radius:6px}
  .wrow:hover{background:#f1eee5}
  .wrow .wl{display:grid;grid-template-columns:152px 1fr 56px;gap:8px;align-items:center}
  .wrow .nm{font:11px var(--mono);color:var(--ink-2);white-space:nowrap;overflow:hidden;
            text-overflow:ellipsis;display:flex;align-items:center;gap:6px}
  .wrow .nm .sd{width:7px;height:7px;border-radius:50%;flex-shrink:0}
  .wtrack{position:relative;height:12px;background:var(--sunken);border-radius:4px;overflow:hidden}
  .wbar{position:absolute;top:1px;bottom:1px;border-radius:3px;min-width:3px;
        background:var(--brand);opacity:.85}
  .wbar.error{background:var(--red)} .wbar.suspended{background:var(--amber)}
  .wrow .ms{font:10px var(--mono);color:var(--ink-4);text-align:right;white-space:nowrap}
  .wdet{margin:2px 0 6px 136px;color:var(--ink-2);
        background:var(--raised);border:1px solid var(--line);border-radius:var(--r-sm);
        padding:10px 12px;display:none;box-shadow:var(--sh-sm)}
  .wdet.show{display:block}
  .wdet .w-t{display:flex;align-items:baseline;gap:8px;font:600 12px var(--mono);color:var(--ink)}
  .wdet .w-st{font-size:10px;font-weight:600;padding:1px 8px;border-radius:999px}
  .wdet .w-st.ok{color:var(--brand-2);background:var(--brand-soft);border:1px solid var(--brand-line)}
  .wdet .w-st.error{color:var(--red);background:var(--red-soft);border:1px solid var(--red-line)}
  .wdet .w-st.suspended{color:var(--amber);background:var(--amber-soft);border:1px solid var(--amber-line)}
  .wdet .w-m{font:10.5px var(--mono);color:var(--ink-4);margin-top:3px}
  .wdet .w-m b{color:var(--ink-3);font-weight:600}
  .wdet .w-task{font:italic 12px/1.6 var(--serif);color:var(--ink-2);margin:7px 0 2px;
                border-left:2px solid var(--line);padding-left:9px;
                display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
  .wdet .kcounts{display:flex;flex-wrap:wrap;gap:4px;margin:6px 0}
  .wdet .kc{font:10px var(--mono);padding:1px 7px;border:1px solid var(--line);border-radius:999px;
            background:var(--sunken);color:var(--ink-3)}
  .wdet .lnk{color:var(--blue);cursor:pointer;text-decoration:underline;display:inline-block;margin-top:4px}

  /* events ledger */
  #ledger{padding:2px 14px 16px}
  .ev{display:grid;grid-template-columns:3ch 9px minmax(0,1fr);gap:0 8px;align-items:baseline;
      padding:2.5px 8px;border-radius:6px;cursor:pointer;
      font:11.5px/1.65 var(--mono);color:var(--ink-2);animation:fade .2s var(--ease)}
  .ev .seq{color:var(--ink-4);text-align:right;font-size:10.5px}
  .ev .dot{width:7px;height:7px;border-radius:50%;background:var(--ink-4);align-self:center;transform:translateY(-1px)}
  .ev .tx{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ev .k{color:var(--ink-3)}
  .ev .b{color:var(--ink)}
  .ev:hover{background:#f1eee5}
  .ev.sel{background:var(--blue-soft)}
  .ev.run-head{margin-top:9px;padding-top:8px;border-top:1px solid var(--line-2)}
  .ev.run-head .b{font-weight:600;color:var(--ink)}
  .ev.run-head .dot{background:var(--brand);width:8px;height:8px}
  .ev.g-start .dot{background:var(--brand)}
  .ev.g-ok .dot{background:var(--brand)}
  .ev.g-route .dot{background:var(--blue)}
  .ev.g-wait .dot{background:var(--amber)}
  .ev.g-fail .dot{background:var(--red)}
  .ev.g-state .dot{background:var(--ink-4)}
  .ev.g-file .dot{background:var(--brand)}
  .ev.interrupted,.ev.g-wait.interrupted{background:var(--amber-soft)}
  .ev.run_completed{background:var(--brand-soft)}
  .ev.run_failed{background:var(--red-soft)}
  .ev.run_failed .b,.ev.node_failed .b{color:var(--red)}
  .evjson{grid-column:1 / -1;margin:3px 0 5px;background:var(--sunken);border:1px solid var(--line);
          border-radius:var(--r-sm);padding:8px 10px;white-space:pre-wrap;word-break:break-word;
          font:10.5px/1.6 var(--mono);color:var(--ink-2);max-height:220px;overflow:auto;display:none}
  .evjson.show{display:block}

  /* state fold */
  #stateview{padding:2px 14px 16px}
  .st-delta{background:var(--sunken);border:1px solid var(--line);border-radius:var(--r-sm);
            padding:9px 11px;margin-bottom:10px;white-space:pre-wrap;word-break:break-word;
            font:11px/1.65 var(--mono);color:var(--ink-2);max-height:200px;overflow:auto}
  .st-delta::before{content:"\394 state_delta";display:block;font:600 9.5px var(--mono);
            letter-spacing:.12em;text-transform:uppercase;color:var(--ink-4);margin-bottom:5px}
  .strow[data-chat="1"] .sv{-webkit-line-clamp:unset;max-height:300px;overflow:auto}
  .sv .m-row{display:grid;grid-template-columns:56px minmax(0,1fr);gap:8px;align-items:baseline;margin:2px 0}
  .sv .m-row i{font:9.5px var(--mono);font-style:normal;color:var(--ink-4)}
  .sv .m-row.u i{color:var(--brand-2)}.sv .m-row.a i{color:var(--blue)}.sv .m-row.t i{color:var(--amber)}
  .sv .m-row span{font:11px var(--mono);color:var(--ink-2);white-space:nowrap;
                  overflow:hidden;text-overflow:ellipsis}
  .strow.open .m-row span{white-space:normal;word-break:break-word}
  .sv .m-more{font:10px var(--mono);color:var(--ink-4);padding:2px 0}
  .strow{display:grid;grid-template-columns:minmax(64px,96px) minmax(0,1fr) auto;gap:8px;
         align-items:baseline;padding:7px 4px;border-bottom:1px solid var(--line-2);cursor:pointer}
  .strow[data-chat="1"]{grid-template-columns:minmax(0,1fr) auto;display:grid}
  .strow[data-chat="1"] .sk{grid-column:1;grid-row:1}
  .strow[data-chat="1"] .sc{grid-column:2;grid-row:1}
  .strow[data-chat="1"] .sv{grid-column:1 / -1;grid-row:2;margin-top:4px}
  .strow:hover{background:#f1eee5}
  .strow.flash{animation:flash 1.6s var(--ease)}
  @keyframes flash{0%,45%{background:var(--amber-soft)}100%{background:transparent}}
  .strow .sk{font:11px var(--mono);font-weight:600;color:var(--brand-2);word-break:break-all}
  .strow .sv{font:11px/1.6 var(--mono);color:var(--ink-2);word-break:break-word;
             display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
  .strow.open .sv{display:block;-webkit-line-clamp:unset}
  .strow .sc{font:9.5px var(--mono);color:var(--ink-4);white-space:nowrap}

  /* files */
  #files{padding:4px 14px 16px}
  #files .file{display:grid;grid-template-columns:16px minmax(0,1fr) auto;gap:8px;align-items:center;
        padding:8px 10px;border:1px solid var(--line);border-radius:var(--r-sm);margin-bottom:7px;
        background:var(--raised);font:11.5px var(--mono);cursor:pointer}
  .file:hover{border-color:var(--brand-line);background:var(--brand-soft)}
  .file svg{color:var(--brand)}
  .file .nm{color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .file .nm i{font-style:normal;color:var(--ink-4);margin-left:6px}
  .file .mt{color:var(--ink-3);font-size:10.5px;white-space:nowrap}

  /* graph */
  #graphview{padding:2px 10px 16px}
  #graphview svg{width:100%;height:auto;display:block}
  .gnode rect{fill:var(--raised);stroke:var(--ink-4);stroke-width:1;rx:7}
  .gnode text{font:10.5px var(--mono);fill:var(--ink-2)}
  .gnode.done rect{stroke:var(--brand);fill:var(--brand-soft)}
  .gnode.done text{fill:var(--brand-2)}
  .gnode.active rect{stroke:var(--brand);stroke-width:2;fill:#fff;
                     animation:glow 1.4s var(--ease) infinite}
  @keyframes glow{0%,100%{stroke-opacity:1}50%{stroke-opacity:.35}}
  .gnode.failed rect{stroke:var(--red);fill:var(--red-soft)}
  .gnode.skipped rect,.gnode.template rect{stroke-dasharray:3 3;fill:var(--sunken)}
  .gnode.template text{fill:var(--ink-3)}
  .gedge{stroke:var(--ink-4);stroke-width:1.2;fill:none;marker-end:url(#arrow)}
  .gedge.hot{stroke:var(--brand);stroke-width:1.8}

  /* artifact modal */
  .overlay{position:fixed;inset:0;background:rgba(30,41,37,.4);backdrop-filter:blur(2px);
           display:none;align-items:center;justify-content:center;z-index:30;animation:ov .18s var(--ease)}
  @keyframes ov{from{opacity:0}}
  .modal{width:min(760px,92vw);max-height:86vh;display:flex;flex-direction:column;
         background:var(--raised);border:1px solid var(--line);border-radius:var(--r-lg);
         box-shadow:var(--sh);overflow:hidden;animation:md .2s var(--ease)}
  @keyframes md{from{opacity:0;transform:scale(.975) translateY(6px)}}
  .m-h{display:flex;align-items:center;gap:10px;padding:13px 18px;border-bottom:1px solid var(--line-2)}
  .m-h svg{color:var(--brand)}
  .m-h b{font:13px var(--mono);font-weight:600}
  .m-h .x{margin-left:auto;cursor:pointer;color:var(--ink-3);display:inline-flex;padding:3px;border-radius:6px}
  .m-h .x:hover{color:var(--ink);background:var(--line-2)}
  .m-c{overflow:auto;padding:16px 18px;flex:1;min-height:0}
  .ap-body{margin:0;white-space:pre-wrap;word-break:break-word;font:12.5px/1.75 var(--mono);color:var(--ink)}
  .md{font-size:13.5px;line-height:1.8;max-width:100%}
  .md h1,.md h2,.md h3,.md h4{font-family:var(--serif);margin:.9em 0 .35em;line-height:1.3}
  .md h1{font-size:1.3em}.md h2{font-size:1.15em}.md h3{font-size:1.04em}.md h4{font-size:1em}
  .md :first-child{margin-top:0}
  .md code{font:11.5px var(--mono);background:var(--sunken);border-radius:4px;padding:1px 5px}
  .md pre{background:var(--sunken);border:1px solid var(--line);border-radius:var(--r-sm);
          padding:10px 12px;overflow:auto;margin:.6em 0}
  .md pre code{background:none;padding:0}
  .md ul,.md ol{margin:.4em 0;padding-left:1.6em}
  .md li{margin:.15em 0}
  .md ul li{list-style:disc}.md ol li{list-style:decimal}
  .md li>ul,.md li>ol{margin:.15em 0}
  .md blockquote{border-left:3px solid var(--line);margin:.5em 0;padding:2px 0 2px 12px;
                 color:var(--ink-2)}
  .md a{color:var(--blue)}
  .tc .bd .md{font-size:12.5px}
  .ap-img{max-width:100%;border-radius:var(--r-sm)}
  .m-f{padding:11px 18px;border-top:1px solid var(--line-2);display:flex;justify-content:flex-end;gap:9px}
  .ap-dl{text-decoration:none}

  /* responsive: two columns, inspector becomes a drawer */
  @media (max-width:1230px){
    .wrap{grid-template-columns:224px minmax(0,1fr)}
    #insp{position:fixed;top:56px;right:0;bottom:0;width:min(420px,94vw);z-index:20;
          border-radius:var(--r-lg) 0 0 var(--r-lg);display:none;box-shadow:var(--sh)}
    #insp.open{display:flex}
    #inspToggle{display:inline-flex}
  }
  /* one column: scenarios become a horizontal strip */
  @media (max-width:880px){
    .wrap{grid-template-columns:minmax(0,1fr);padding:10px;gap:10px}
    #side{order:-1}
    #side .lab{display:none}
    #side .scroll{display:flex;overflow-x:auto;overflow-y:hidden;padding:8px;gap:6px}
    .scn{flex:0 0 auto;grid-template-columns:auto 1fr;width:auto}
    .scn .no{grid-row:auto}
    .scn small{display:none}
    header{padding:0 10px;gap:8px}
    .crumb .rid{display:none}
    .tc.child{margin-left:12px}
  }
  @media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<header>
  <span class="wordmark">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="M12 3l3.2 6.5 7.1 1-5.1 5 1.2 7L12 19.2 5.6 22.5l1.2-7-5.1-5 7.1-1z"/></svg>
    prodagent
  </span>
  <span class="crumb"><b id="crumb">—</b><span class="rid" id="runid"></span></span>
  <span class="spacer"></span>
  <button class="iconbtn" id="inspToggle"></button>
  <span class="pill" id="hstatus"><span class="d"></span><span id="hstatus-t">idle</span></span>
  <button class="iconbtn" id="langbtn"></button>
</header>

<div class="wrap">
  <div class="card" id="side">
    <div class="pad"><div class="lab" id="scenesH"></div></div>
    <div class="scroll" id="scenes"></div>
  </div>

  <div class="card" id="mid">
    <div class="pad"><p class="lead" id="desc"></p></div>
    <div class="scroll" id="transcript"></div>
    <div class="bar">
      <label class="field"><input id="msg" type="text"/>
        <button class="send" id="run" type="button" aria-label="Run"></button></label>
    </div>
  </div>

  <div class="card" id="insp">
    <div class="tabs" id="tabs">
      <button data-t="trace" id="tab-trace"></button>
      <button data-t="events" id="tab-events"></button>
      <button data-t="state" id="tab-state"></button>
      <button data-t="files" id="tab-files"></button>
      <button data-t="graph" id="tab-graph"></button>
    </div>

    <div class="panel" id="p-trace">
      <div class="pane-top"><div class="hint" id="wfHint"></div></div>
      <div class="scroll" id="wf"></div>
    </div>

    <div class="panel" id="p-events">
      <div class="pane-top">
        <div class="chips" id="chips"></div>
        <div class="chips" id="runchips"></div>
      </div>
      <div class="scroll" id="ledger"></div>
    </div>

    <div class="panel" id="p-state">
      <div class="pane-top"><div class="hint" id="stHint"></div></div>
      <div class="scroll" id="stateview"></div>
    </div>

    <div class="panel" id="p-files">
      <div class="pane-top"><div class="hint" id="fHint"></div></div>
      <div class="scroll" id="files"></div>
    </div>

    <div class="panel" id="p-graph">
      <div class="pane-top">
        <div class="chips" id="gchips"></div>
        <div class="hint" id="gHint"></div>
      </div>
      <div class="scroll" id="graphview"></div>
    </div>
  </div>
</div>

<div class="overlay" id="overlay">
  <div class="modal">
    <div class="m-h">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>
      <b class="ap-title"></b>
      <span class="x" id="m-close">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></span>
    </div>
    <div class="m-c ap-content"></div>
    <div class="m-f"><a class="ap-dl" download><button class="btn" id="m-dl" type="button"></button></a></div>
  </div>
</div>

<script>
/* ════════ state ════════ */
let current=null, sid=null, since=0, timer=null, chat=false, running=false;
let EV=[];                 /* every serialized event, in arrival order */

let runs={};               /* run_id -> {name, parent, depth, head:el, ...} */
let spans=[];              /* latest trace payload (server fold) */
let spanById={};
let liveText={};           /* "run:node:epoch" -> streaming block; a node re-armed for a new ReAct round opens a new block at the end, so text never jumps above the tool cards born between rounds */
let pendingTools={};       /* run_id -> [tool card elements awaiting a result] */
let gseq=0, kindFilter="all", runFilter=null, graphRun=null, stateRun=null, streamEpoch={}, spanFocus=null, tabFocus=null;
let lastQuestion="", lastDelta=null, artifacts=[], statusWas="";

const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const $=id=>document.getElementById(id);
const el=(tag,cls,html)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(html!==undefined)e.innerHTML=html;return e;};
const short=r=>String(r||"").slice(0,8);

/* inline SVG glyphs (feather-style, stroke currentColor) */
const svg=(p,s=15)=>`<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
const G={
  send:'<path d="M12 19V5M6 11l6-6 6 6"/>',
  file:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  dl:'<path d="M12 3v12M7 11l5 5 5-5"/><path d="M5 21h14"/>',
  spark:'<path d="M12 3l1.9 5.7L20 10l-5 3.6L16.4 20 12 16.6 7.6 20 9 13.6 4 10l6.1-1.3z"/>',
  branch:'<circle cx="6" cy="5" r="2.4"/><circle cx="6" cy="19" r="2.4"/><circle cx="18" cy="12" r="2.4"/><path d="M6 7.4v9.2M8.4 5.7c4 .8 5.4 2.7 7.2 5.1M8.4 18.3c4-.8 5.4-2.7 7.2-5.1"/>',
};
/* event kind -> visual group (drives dot colour; filters match these) */
const KG={run_started:"start",node_started:"start",node_completed:"ok",node_skipped:"state",
  state_delta:"state",control:"route",delegated:"route",artifact_written:"file",
  interrupted:"wait",resumed:"wait",run_completed:"ok",run_failed:"fail",node_failed:"fail",user_turn:"start"};
const GROUPS=["start","ok","state","route","file","wait","fail"];

/* ════════ i18n ════════ */
const I18N={
 en:{scenes:"Scenarios",crumb:"Pick a scenario and run it",run:"Run",send:"Send",
   ph:"Message the agent…",phOnce:"Input for this scenario…",empty:"Nothing has run yet.<br/>Pick a scenario and press Run.",
   idle:"idle",running:"running",suspended:"suspended",completed:"completed",failed:"failed",
   replay:"Session replay",replayDesc:"Replaying a finished session from its event ledger — nothing is re-run.",
   earlier:"earlier",
   tabTrace:"Trace",tabEvents:"Events",tabState:"State",tabFiles:"Files",tabGraph:"Graph",
   chipAll:"all",grp:{start:"lifecycle",ok:"ok",state:"state",route:"routing",file:"files",wait:"waiting",fail:"failures"},
   wfHint:"One bar per Run — width is wall time; children nest under their parent. Click a bar to inspect.",
   stHint:"Channels folded live per Run from its state_delta facts (arrays append, the rest last-write). Click a row to expand.",
   fHint:"Pointers recorded by artifact_written; bytes live in the BlobStore. Click to preview.",
   gHint:"Edges appear as control facts record them — the plan as it was actually walked.",
   gEmpty:"No node has started in this Run yet.",
   filterRun:"filter events",you:"you",spawn:"spawn",toolCall:"tool",fileWritten:"artifact",
   parked:"waiting for you",resumed:"resumed",failedAt:"failed at",done:"completed",runDone:"run completed",
   runFail:"run failed",approve:"Approve",reject:"Reject",dl:"Download",insp:"Panels",
   evHint:"The ledger — grouped by Run. Click any row for the raw fact.",
   names:{run_started:"run_started",node_started:"node_started",node_completed:"node_completed",
          node_skipped:"node_skipped",state_delta:"state_delta",control:"control",
          delegated:"delegated",artifact_written:"artifact",interrupted:"waiting",
          resumed:"resumed",run_completed:"run_completed",run_failed:"run_failed",
          node_failed:"node_failed",user_turn:"user"},
   counts:"events",children:"children"},
 zh:{scenes:"场景",crumb:"选一个场景，点「运行」",run:"运行",send:"发送",
   ph:"给 Agent 留言…",phOnce:"给这个场景的输入…",empty:"还没有运行。<br/>选一个场景，点「运行」。",
   idle:"空闲",running:"运行中",suspended:"已挂起",completed:"已完成",failed:"失败",
   replay:"会话回放",replayDesc:"正在从事件账本回放一个已结束的会话——不会重新运行。",
   earlier:"条更早",
   tabTrace:"追踪",tabEvents:"事件",tabState:"状态",tabFiles:"产物",tabGraph:"图",
   chipAll:"全部",grp:{start:"生命周期",ok:"完成",state:"状态",route:"路由",file:"产物",wait:"等待",fail:"失败"},
   wfHint:"一个 Run 一条杠——宽度即耗时，子 Run 嵌在父 Run 下。点击杠可下钻。",
   stHint:"按 Run 分别折叠的通道值（数组=append，其余=last）。点行展开。",
   fHint:"artifact_written 记录的指针；字节在 BlobStore 里。点击预览。",
   gHint:"边在 control 事实记录时才出现——这是实际走过来的图，不是声明的图。",
   gEmpty:"这个 Run 还没有节点启动。",
   filterRun:"只看该 Run 事件",you:"你",spawn:"委派",toolCall:"工具",fileWritten:"产物",
   parked:"等你决定",resumed:"已恢复",failedAt:"失败于",done:"完成",runDone:"运行完成",
   runFail:"运行失败",approve:"批准",reject:"拒绝",dl:"下载",insp:"投影面板",
   evHint:"账本本身——按 Run 分组。点任意行看原始事实。",
   names:{run_started:"run_started",node_started:"node_started",node_completed:"node_completed",
          node_skipped:"node_skipped",state_delta:"state_delta",control:"control",
          delegated:"delegated",artifact_written:"artifact",interrupted:"等待中",
          resumed:"resumed",run_completed:"run_completed",run_failed:"run_failed",
          node_failed:"node_failed",user_turn:"用户"},
   counts:"条事件",children:"个子 Run"},
};
let LANG=localStorage.getItem("pg-lang")||"en";
const t=k=>I18N[LANG][k];
const L=(s,k)=>LANG==="zh"?s[k+"_zh"]:s[k];
async function api(url,opts){const r=await fetch(url,opts);return r.json();}

/* ════════ chrome ════════ */
function setStatus(st){
  const p=$("hstatus");
  p.className="pill "+(st||"");
  $("hstatus-t").textContent=st?t(st):t("idle");
  const root=spans[0];
  $("runid").textContent=root?` · ${root.name||short(root.run_id)} ${root.duration_ms}ms`:"";
}
function applyLang(){
  document.documentElement.lang=LANG==="zh"?"zh-CN":"en";
  $("scenesH").textContent=t("scenes");
  $("tab-trace").textContent=t("tabTrace");$("tab-events").textContent=t("tabEvents");
  $("tab-state").textContent=t("tabState");$("tab-files").textContent=t("tabFiles");
  $("tab-graph").textContent=t("tabGraph");
  $("wfHint").textContent=t("wfHint");$("stHint").textContent=t("stHint");
  $("fHint").textContent=t("fHint");$("gHint").textContent=t("gHint");
  $("m-dl").textContent=t("dl");
  $("inspToggle").textContent=t("insp");
  const msg=$("msg");msg.placeholder=chat?t("ph"):t("phOnce");
  $("run").innerHTML=svg(G.send,15);
  $("run").setAttribute("aria-label",chat?t("send"):t("run"));
  $("langbtn").textContent=LANG==="zh"?"EN":"中文";
  buildChips();renderAll();
}

/* ════════ scenarios ════════ */
async function loadScenes(){
  const list=await api("/api/scenarios");
  const box=$("scenes");box.innerHTML="";
  let keep=null,keepEl=null;
  list.forEach((s,i)=>{
    const b=el("button","scn");
    b.innerHTML=`<span class="no">${String(i+1).padStart(2,"0")}</span>`+
      `<b>${esc(L(s,"title"))}</b><small>${esc(L(s,"desc"))}</small>`;
    b.onclick=()=>select(s,b);box.appendChild(b);
    if(current&&s.key===current.key){keep=s;keepEl=b;}
  });
  if(keep)select(keep,keepEl);
  else if(list.length)select(list[0],box.firstChild);
}
function resetViews(){
  EV=[];runs={};spans=[];spanById={};liveText={};pendingTools={};
  gseq=0;runFilter=null;graphRun=null;lastDelta=null;artifacts=[];statusWas="";
  $("transcript").innerHTML=`<div class="empty">${svg('<circle cx="12" cy="12" r="8"/><path d="M12 8v4M12 16h.01"/>',22)}<br/>${t("empty")}</div>`;
  renderWaterfall();renderLedger();renderState();renderFiles();renderGraph();
}
function select(s,btn){
  const prev=current;current=s;since=0;sid=null;clearInterval(timer);chat=false;running=false;
  setStatus("");
  document.querySelectorAll(".scn").forEach(x=>x.classList.remove("active"));
  if(btn)btn.classList.add("active");
  const msg=$("msg"),v=msg.value;
  const stock=prev?[prev.default,prev.default_zh]:[];
  if(!v||v===s.default||v===s.default_zh||stock.includes(v))msg.value=L(s,"default");
  $("desc").textContent=L(s,"desc");
  $("crumb").textContent=L(s,"title");$("runid").textContent="";
  resetViews();
}

/* ════════ transcript: the run as a conversation ════════ */
function clearEmpty(){const e=$("transcript").querySelector(".empty");if(e)e.remove();}
function card(cls){clearEmpty();const c=el("div","tc "+cls);$("transcript").appendChild(c);return c;}
function stick(){const b=$("transcript");
  if(b.scrollHeight-b.scrollTop-b.clientHeight<80)b.scrollTop=b.scrollHeight;}

function addUser(text){
  const c=card("user");
  c.innerHTML=`<div class="h">${svg(G.send,12)} ${esc(t("you"))}</div><div class="bd">${esc(text)}</div>`;
}
function ensureCard(r){
  /* lazy: the card is born with its first content, never as an empty shell —
     so the opening user message (a seed state_delta that arrives *after*
     run_started) always renders above the agent, and a Workflow root that
     streams nothing never paints an empty card at all */
  const info=runs[r]||{};
  if(info.card)return info.card;
  const c=card("turn"+(info.depth>0?" child":""));
  c.innerHTML=`<div class="h"><span class="who">${esc(info.name||short(r))}</span>`+
    `<span class="aux">${short(r)}</span><span class="badge"></span></div><div class="bd"></div>`;
  info.card=c;info.body=c.querySelector(".bd");
  if(info.task){const p=el("div");
    p.style.cssText="color:var(--ink-3);font-size:11.5px;margin-bottom:6px";
    p.textContent="\u21d2 "+info.task;info.body.prepend(p);}
  return c;
}
function feedStream(d){
  const ek=d.run_id+":"+(d.node_id||"?");
  const key=`${ek}:${streamEpoch[ek]||1}`;
  let s=liveText[key];
  if(!s){
    const info=runs[d.run_id]||{};
    info.streamed=1;
    const c=ensureCard(d.run_id);
    c.classList.add("live");
    const think=el("div","think"), body=(runs[d.run_id]||{}).body;
    body.appendChild(think);
    s=liveText[key]={c,think,body,txt:el("span","")};
    body.appendChild(s.txt);
  }
  if(d.kind==="reasoning")s.think.textContent+=d.text||"";
  else s.txt.textContent+=d.text||"";
  stick();
}
function endStreams(){
  Object.values(liveText).forEach(s=>{
    if(!s.c||!s.txt)return;
    s.c.classList.remove("live");
    s.txt.innerHTML=mdToHtml(s.txt.textContent); /* settle into markdown */
  });
  liveText={};
}
function toolCard(run,name,args){
  const c=el("div","tc tool");
  c.innerHTML=`<div class="h">${svg(G.spark,12)} ${esc(t("toolCall"))} · <span class="who">${esc(name)}</span></div>`+
    `<div class="bd"><span class="args">${esc(JSON.stringify(args??{},null,1))}</span><div class="res"></div></div>`;
  ensureCard(run).querySelector(".bd").appendChild(c);
  (pendingTools[run]=pendingTools[run]||[]).push(c);
  stick();return c;
}
function fillTool(run,content,isErr){
  const q=pendingTools[run];
  const c=q&&q.shift();
  if(!c)return;
  const r=c.querySelector(".res");
  if(isErr)r.classList.add("err");
  r.textContent=String(content??"");
  stick();
}
function messageFacts(run,delta){
  /* the messages channel carries the conversation: assistant tool_calls /
     tool results / assistant text. New items arrive folded per state_delta. */
  let msgs=delta&&delta.messages;
  if(!Array.isArray(msgs))return false;
  const info0=runs[run]||{};
  if(!info0.seedSeen){
    /* the seed's [*history, new user turn]: the history is bookkeeping for
       the model (a multi-turn continuation), already rendered by earlier
       runs — only messages from the last user turn on are new facts here */
    info0.seedSeen=1;
    let lastUser=-1;
    msgs.forEach((m,i)=>{if(m.role==="user")lastUser=i;});
    if(lastUser>0)msgs=msgs.slice(lastUser);
  }
  for(const m of msgs){
    const info=runs[run]||{};
    if(m.role==="user"){
      /* a child run's opening user message is the delegated TASK: a dim line
         inside its card, never a chat bubble from you */
      if(info.depth>0){if(!info.task)info.task=String(m.content??"");}
      else addUser(m.content);
      continue;
    }
    if(m.role==="assistant"){
      if(Array.isArray(m.tool_calls)&&m.tool_calls.length){
        ensureCard(run);
        for(const tc of m.tool_calls)toolCard(run,tc.name||tc.function?.name,tc.arguments??tc.function?.arguments);
      }else if(m.content&&!info.streamed){
        /* content that already streamed live into the card is not repeated */
        const p=el("div","md");p.innerHTML=mdToHtml(m.content);
        ensureCard(run).querySelector(".bd").appendChild(p);
      }
    }else if(m.role==="tool"){
      fillTool(run,m.content,m.is_error);
    }
  }
  return true;
}
function addDeleg(run,child){
  const c=card("deleg"+((runs[run]||{}).depth>0?" child":""));
  c.dataset.child=child;
  const nm=runs[child]?runs[child].name:short(child);
  c.innerHTML=`<div class="h">${svg(G.branch,13)} ${esc(t("spawn"))}</div>`+
    `<div class="bd">→ ${esc(nm)} <span style="color:var(--ink-4)">${short(child)}</span></div>`;
  c.onclick=()=>{showTab("trace");selectSpan(child);if(innerWidth<=1230)$("insp").classList.add("open");};
  stick();
}
function addFile(run,p){
  const c=el("div","tc file");
  c.innerHTML=`<div class="bd">${svg(G.file,15)}<span>${esc(p.filename)} <span style="color:var(--ink-4)">v${p.version}</span></span>`+
    `<span style="margin-left:auto;color:var(--ink-4)">${fmtSize(p.size)}</span></div>`;
  c.onclick=()=>openArtifact(p);
  const info=runs[run]||{};
  if(info.card)info.card.querySelector(".bd").appendChild(c);
  else{clearEmpty();$("transcript").appendChild(c);}
  stick();
}
function addPark(question,parked){
  const c=card("park");
  c.innerHTML=`<div class="h">${svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v6l3.5 2"/>',13)} ${esc(t("parked"))}</div>`+
    `<div class="q">${esc(question||t("parked"))}</div>`+
    (parked?`<pre class="pk">${esc(JSON.stringify(parked,null,2))}</pre>`:"")+
    `<div class="row"><button class="btn yes">${esc(t("approve"))}</button>`+
    `<button class="btn ghost no">${esc(t("reject"))}</button></div>`;
  c.querySelector(".yes").onclick=()=>decide(true,c);
  c.querySelector(".no").onclick=()=>decide(false,c);
  stick();
}
function finishRun(run,ok){
  /* only the root run ends with a banner; a child's end is a small badge on
     its own card — three delegated children must not mint three banners */
  const info=runs[run]||{};
  if(info.parent){
    if(info.card){
      const b=info.card.querySelector(".badge");
      const sp=spanById[run];
      b.textContent=(ok?"\u2713 ":"\u2715 ")+(sp?sp.duration_ms+"ms":"");
      b.style.color=ok?"var(--brand-2)":"var(--red)";
    }
    return;
  }
  addFin(ok);
}
function addFin(ok){
  const c=card("fin"+(ok?"":" bad"));
  c.innerHTML=`<div class="h">${ok?"✓":"✕"} ${esc(ok?t("runDone"):t("runFail"))}</div>`+
    `<div class="bd"><span class="dur"></span><span class="out"></span></div>`;
  (ok?FINS_OK:FINS_BAD).push(c);
  stick();
}
let FINS_OK=[],FINS_BAD=[];
function patchDurations(){
  if(!spans[0])return;
  [...FINS_OK,...FINS_BAD].forEach(c=>{
    const d=c.querySelector(".dur");
    if(d&&!d.textContent)d.textContent=spans[0].duration_ms+" ms";
  });
  Object.entries(runs).forEach(([r,info])=>{
    if(info.card&&info.parent&&spanById[r]){
      const b=info.card.querySelector(".badge");
      if(b&&!b.textContent)b.textContent="\u2713 "+spanById[r].duration_ms+"ms";
    }
  });
  setStatus(statusWas||"");
}
function addSys(text,bad){
  const c=card("sys");
  c.innerHTML=`<span style="color:${bad?"var(--red)":"var(--ink-4)"}">${bad?"✕":"·"}</span>${esc(text)}`;
}

/* one event -> transcript cards */
function renderEvent(ev){
  const d=ev.data||{};
  switch(ev.kind){
    case "user_turn": addUser(d.text);break;
    case "node_started":{
      const k=ev.run_id+":"+(d.node||"?");
      streamEpoch[k]=(streamEpoch[k]||0)+1;
      break;}
    case "run_started":{
      const parent=ev.parent;
      runs[ev.run_id]={name:d.name||"",parent,depth:parent&&runs[parent]?runs[parent].depth+1:0};
      addTurn(ev.run_id);
      /* patch any delegation card still holding the short id */
      document.querySelectorAll(`.tc.deleg[data-child="${ev.run_id}"] .bd`).forEach(b=>{
        b.innerHTML=`→ ${esc(d.name||short(ev.run_id))} <span style="color:var(--ink-4)">${short(ev.run_id)}</span>`;});
      break;}
    case "state_delta":{
      const delta=d.delta||{};
      messageFacts(ev.run_id,delta);
      lastDelta=delta;
      break;}
    case "artifact_written": addFile(ev.run_id,d);break;
    case "delegated": addDeleg(ev.run_id,d.child_run_id);break;
    case "node_failed": addSys(`${t("failedAt")} ${d.node||""} — ${d.error||""}`,true);break;
    case "interrupted": addPark(lastQuestion,d.parked);break;
    case "resumed": addSys(t("resumed"));break;
    case "run_completed": finishRun(ev.run_id,true);break;
    case "run_failed": finishRun(ev.run_id,false);break;
  }
}

/* ════════ fold 1: trace waterfall ════════ */
function flatten(sp,depth,out){
  out.push({s:sp,d:depth});
  (sp.children||[]).forEach(c=>flatten(c,depth+1,out));
  return out;
}
function renderWaterfall(){
  const box=$("wf");box.innerHTML="";
  if(!spans.length){box.innerHTML=`<div class="empty">${svg('<circle cx="12" cy="12" r="8"/>',20)}</div>`;return;}
  const rows=flatten(spans[0],0,[]); /* the playground drives one root run */
  spans.slice(1).forEach(s=>flatten(s,0,rows));
  const t0=Math.min(...rows.map(r=>r.s.start_ts));
  const t1=Math.max(...rows.map(r=>r.s.end_ts));
  const span=Math.max(t1-t0,1e-6);
  rows.forEach(({s,d})=>{
    spanById[s.run_id]=s;
    const left=((s.start_ts-t0)/span*100),width=Math.max((s.end_ts-s.start_ts)/span*100,1.2);
    const row=el("div","wrow");
    row.dataset.run=s.run_id;
    row.innerHTML=`<div class="wl"><span class="nm" style="padding-left:${d*13}px">`+
      `<span class="sd ${s.status}"></span>${esc(s.name||short(s.run_id))}${s.task?` <i style="font-style:normal;color:var(--ink-4)">${esc(String(s.task).slice(0,34))}</i>`:""}</span>`+
      `<span class="wtrack"><span class="wbar ${s.status}" style="left:${left}%;width:${width}%"></span></span>`+
      `<span class="ms">${s.duration_ms}ms</span></div>`+ /* closes .wl — a stray
         </span> here used to swallow .wdet into the grid as a 4th column */
      `<div class="wdet"></div>`;
    row.title=`${s.name||short(s.run_id)} ${s.task||""}`.trim();
    row.onclick=()=>showSpanDetail(row,s);
    box.appendChild(row);
  });
}
function showSpanDetail(row,s){
  const det=row.querySelector(".wdet");
  const open=det.classList.contains("show");
  document.querySelectorAll(".wdet.show").forEach(x=>x.classList.remove("show"));
  if(open)return;
  const evs=EV.filter(e=>e.run_id===s.run_id);
  const counts={};evs.forEach(e=>counts[e.kind]=(counts[e.kind]||0)+1);
  const stLabel={ok:"✓ ok",error:"✕ error",suspended:"⏸ suspended"}[s.status]||s.status;
  det.innerHTML=`<div class="w-t">${esc(s.name||short(s.run_id))}`+
    `<span class="w-st ${s.status}">${stLabel}</span></div>`+
    `<div class="w-m"><b>${esc(s.run_id)}</b> · ${s.duration_ms} ms · ${evs.length} ${esc(t("counts"))}</div>`+
    (s.task?`<div class="w-task">${esc(s.task)}</div>`:"")+
    `<div class="kcounts">${Object.entries(counts).map(([k,n])=>`<span class="kc">${esc(k)} ${n}</span>`).join("")}</div>`+
    `<span class="lnk" data-run="${esc(s.run_id)}">${esc(t("filterRun"))} →</span>`;
  det.querySelector(".lnk").onclick=e=>{
    e.stopPropagation();
    runFilter=s.run_id;$("runchips").innerHTML="";
    const x=el("span","chip run");x.innerHTML=`${esc(s.name||short(s.run_id))} <span class="x">✕</span>`;
    x.querySelector(".x").onclick=()=>{runFilter=null;$("runchips").innerHTML="";renderLedger();};
    $("runchips").appendChild(x);
    renderLedger();showTab("events");
  };
  det.classList.add("show");
}
function selectSpan(runId){
  const row=document.querySelector(`.wrow[data-run="${runId}"]`);
  const sp2=spanById[runId];
  if(row&&sp2){showSpanDetail(row,sp2);
    row.scrollIntoView({block:"center",behavior:"smooth"});}
}

/* ════════ fold 2: the ledger ════════ */
const FILTERS=[["all",null],["start",["start"]],["state",["state"]],["route",["route"]],
               ["file",["file"]],["wait",["wait"]],["fail",["fail"]]];
function buildChips(){
  const box=$("chips");box.innerHTML="";
  FILTERS.forEach(([g])=>{
    const c=el("button","chip"+(kindFilter===g?" active":""));
    c.textContent=g==="all"?t("chipAll"):t("grp")[g];
    c.onclick=()=>{kindFilter=g;buildChips();renderLedger();};
    box.appendChild(c);
  });
}
function brief(ev){
  const d=ev.data||{};
  switch(ev.kind){
    case "run_started":return d.name||"";
    case "node_started":case "node_completed":case "node_skipped":return d.node||"";
    case "node_failed":return `${d.node||""}  ${d.error||""}`;
    case "control":return `${d.op||""} ${d.target||d.template||""}`;
    case "delegated":return `→ ${short(d.child_run_id)}`;
    case "artifact_written":return `${d.filename||""} v${d.version}`;
    case "interrupted":return Object.keys(d.parked||{}).join(",")||"";
    case "run_failed":return d.reason||"";
    case "user_turn":return (d.text||"").slice(0,60);
    default:{
      if(ev.kind==="state_delta"&&d.delta){
        return Object.entries(d.delta).map(([k,v])=>`${k}:${Array.isArray(v)?`+${v.length}`:JSON.stringify(v)?.slice(0,30)}`).join("  ");
      }
      return "";
    }
  }
}
function renderLedger(){
  const box=$("ledger");box.innerHTML="";
  if(!EV.length){box.innerHTML=`<div class="empty">${t("evHint")}</div>`;return;}
  const kinds=FILTERS.find(f=>f[0]===kindFilter)[1];
  let lastRun=null,shown=0;
  EV.forEach(ev=>{
    if(ev.kind==="llm_delta")return; /* bus-direct tokens are ephemeral, not ledger facts */
    const grp=KG[ev.kind]||"state";
    if(kinds&&!kinds.includes(grp))return;
    if(runFilter&&ev.run_id!==runFilter)return;
    if(ev.run_id!==lastRun){
      lastRun=ev.run_id;
      if(!runFilter){
        const info=runs[ev.run_id]||{};
        const h=el("div","ev run-head");
        h.style.setProperty("--d",info.depth||0);
        h.innerHTML=`<span class="seq"></span><span class="dot"></span>`+
          `<span class="tx"><span class="b">${esc(info.name||"run")} · ${short(ev.run_id)}</span></span>`;
        h.style.paddingLeft=(8+(info.depth||0)*14)+"px";
        box.appendChild(h);
      }
    }
    const e=el("div","ev g-"+grp+(ev.kind==="interrupted"?" interrupted":""));
    e.innerHTML=`<span class="seq">${++gseq}</span><span class="dot"></span>`+
      `<span class="tx"><span class="k">${esc(t("names")[ev.kind]||ev.kind)}</span> <span class="b">${esc(brief(ev))}</span></span>`;
    e.onclick=()=>{
      document.querySelectorAll(".ev.sel").forEach(x=>x.classList.remove("sel"));
      e.classList.add("sel");
      let j=e.nextElementSibling;
      /* one JSON accordion per row: toggle */
      if(j&&j.classList.contains("evjson")){j.remove();return;}
      document.querySelectorAll(".evjson").forEach(x=>x.remove());
      const pre=el("pre","evjson");
      pre.textContent=JSON.stringify({seq:ev.seq,run_id:ev.run_id,parent:ev.parent,kind:ev.kind,data:ev.data},null,2);
      pre.classList.add("show");
      e.after(pre);
      if(ev.kind==="state_delta"&&ev.data?.delta)showDelta(ev.data.delta);
      if(ev.kind==="delegated")selectSpan(ev.data.child_run_id);
    };
    box.appendChild(e);shown++;
  });
  box.scrollTop=box.scrollHeight;
  gseq=0;box.querySelectorAll(".ev:not(.run-head) .seq").forEach(s=>s.textContent=String(++gseq));
}

/* ════════ fold 3: state channels ════════ */
function foldState(rid){
  const st={};
  for(const ev of EV){
    if(rid&&ev.run_id!==rid)continue;
    if(ev.kind!=="state_delta")continue;
    const delta=(ev.data||{}).delta||{};
    for(const[k,v]of Object.entries(delta)){
      if(Array.isArray(v))st[k]=[...(st[k]||[]),...v];
      else if(v&&typeof v==="object"&&!Array.isArray(v))st[k]=Object.assign({},st[k],v);
      else st[k]=v;
    }
  }
  return st;
}
function renderState(){
  const box=$("stateview");box.innerHTML="";
  /* every Run owns its own channels: the fold is per-Run, never merged */
  const ids=Object.keys(runs);
  const sel=runs[stateRun]?stateRun:(spans[0]?spans[0].run_id:ids[0]||null);
  stateRun=sel;
  if(ids.length>1){
    const bar=el("div","chips");
    ids.forEach(rid=>{
      const c=el("button","chip"+(rid===sel?" active":""));
      c.textContent=runs[rid].name||short(rid);
      c.onclick=()=>{stateRun=rid;renderState();};
      bar.appendChild(c);
    });
    box.appendChild(bar);
  }
  const st=foldState(sel);
  const keys=Object.keys(st).sort();
  if(!keys.length){box.innerHTML=`<div class="empty">${t("stHint")}</div>`;return;}
  keys.forEach(k=>{
    const v=st[k];
    const chat=k==="messages"&&Array.isArray(v);
    const row=el("div","strow");
    const writes=EV.filter(e=>e.kind==="state_delta"&&e.run_id===sel&&(e.data?.delta||{})[k]!==undefined).length;
    row.dataset.chat=chat?"1":"";
    row.innerHTML=`<span class="sk">${esc(k)}</span>`+
      `<span class="sv">${stateValueHtml(v,chat,false)}</span>`+
      `<span class="sc">${Array.isArray(v)?v.length+"\u00d7":"\u00d7"}${writes}</span>`;
    row.onclick=()=>{
      const open=row.classList.toggle("open");
      if(chat)row.querySelector(".sv").innerHTML=stateValueHtml(v,true,open);
    };
    box.appendChild(row);
  });
}
function stateValueHtml(v,chat,full){
  /* the messages channel IS a conversation: render it as one — role-tagged
     rows, tool calls compacted to "→ name(args)"; other channels stay JSON */
  if(!chat)return esc(JSON.stringify(v,null,1));
  const msgs=full?v:v.slice(-8);
  const more=v.length-msgs.length;
  const brief=x=>String(x??"").trim().replace(/\s+/g," ").slice(0,120);
  const rows=msgs.map(m=>{
    if(m.role==="user")return `<div class="m-row u"><i>user</i><span>${esc(brief(m.content))}</span></div>`;
    if(m.role==="assistant"&&Array.isArray(m.tool_calls)&&m.tool_calls.length)
      return `<div class="m-row a"><i>assistant</i><span>${m.tool_calls.map(tc=>
        `\u2192 ${esc(tc.name||tc.function?.name||"")}(${esc(JSON.stringify(tc.arguments??tc.function?.arguments??{}))})`).join(" ")}</span></div>`;
    if(m.role==="assistant")return `<div class="m-row a"><i>assistant</i><span>${esc(brief(m.content))}</span></div>`;
    if(m.role==="tool")return `<div class="m-row t"><i>tool\u00b7${esc(m.name||"")}</i><span>${esc(brief(m.content))}</span></div>`;
    return `<div class="m-row"><i>${esc(m.role||"?")}</i><span>${esc(brief(JSON.stringify(m)))}</span></div>`;
  }).join("");
  return (more?`<div class="m-more">\u2026 ${more} ${esc(t("earlier"))}</div>`:"")+rows;
}
function lastDeltaOf(rid){
  let out=null;
  for(const ev of EV){if(ev.kind==="state_delta"&&ev.run_id===rid)out=(ev.data||{}).delta||null;}
  return out;
}
function showDelta(delta,quiet){
  const box=$("stateview");
  let top=box.querySelector(".st-delta");
  if(!top){top=el("div","st-delta");box.prepend(top);}
  top.textContent=JSON.stringify(delta,null,2);
  if(!quiet){
    showTab("state");
    box.querySelectorAll(".strow").forEach(r=>{
      if(delta[r.querySelector(".sk").textContent]!==undefined){
        r.classList.remove("flash");void r.offsetWidth;r.classList.add("flash");
      }
    });
  }
}

/* ════════ fold 4: artifact pointers ════════ */
function fmtSize(n){return n<1024?n+" B":(n/1024).toFixed(1)+" KB";}
function renderFiles(){
  const box=$("files");box.innerHTML="";
  if(!artifacts.length){box.innerHTML=`<div class="empty">${t("fHint")}</div>`;return;}
  artifacts.forEach(p=>{
    const f=el("div","file");
    f.innerHTML=`${svg(G.file,15)}<span class="nm">${esc(p.filename)}<i>v${p.version}</i></span>`+
      `<span class="mt">${esc(p.mime||"")} · ${fmtSize(p.size)}</span>`;
    f.onclick=()=>openArtifact(p);
    box.appendChild(f);
  });
}

/* ════════ fold 5: the walked graph ════════ */
function graphData(){
  const rid=graphRun||(spans[0]?spans[0].run_id:null);
  if(!rid)return null;
  const evs=EV.filter(e=>e.run_id===rid);
  const nodes={},edges=[];
  let first=null,active=null,cur=null;
  for(const ev of evs){
    const d=ev.data||{};
    if(ev.kind==="node_started"){
      const n=d.node||"?";
      if(!first)first=n;
      /* a Send copy is "template#key": the template itself gets a ghost node
         so the fan-out reads base <- copies, and send targets resolve to it */
      const base=n.split("#")[0];
      if(base!==n&&nodes[base]===undefined)nodes[base]={status:"template"};
      nodes[n]=nodes[n]||{status:"running"};
      nodes[n].status="running";active=n;
      if(base!==n)edges.push([base,n]);
    }else if(ev.kind==="node_completed"){if(nodes[d.node])nodes[d.node].status="done";cur=d.node;}
    else if(ev.kind==="node_skipped"){nodes[d.node]={status:"skipped"};}
    else if(ev.kind==="node_failed"){nodes[d.node]={status:"failed"};active=d.node;}
    else if(ev.kind==="control"){
      /* a control fact names its target, not its source; the source is the
         node that just completed (the one whose body returned the command) */
      const to=d.target||d.template;
      if(to)edges.push([cur||first,to]);
    }
  }
  /* de-dup edges, keep order for "hot" (most recent) marking */
  const uniq=[];const have=new Set();
  edges.forEach(([a,b])=>{const k=a+"→"+b;if(!have.has(k)){have.add(k);uniq.push([a,b]);}});
  return {rid,nodes,edges:uniq,active,first};
}
function renderGraph(){
  const box=$("graphview");
  /* run selector: root + every delegated child */
  $("gchips").innerHTML="";
  const all=Object.keys(runs);
  if(all.length>1){
    all.forEach(rid=>{
      const c=el("button","chip"+((graphRun||spans[0]?.run_id)===rid?" active":""));
      c.textContent=runs[rid].name||short(rid);
      c.onclick=()=>{graphRun=rid;renderGraph();};
      $("gchips").appendChild(c);
    });
  }
  const g=graphData();
  if(!g||!Object.keys(g.nodes).length){box.innerHTML=`<div class="empty">${t("gEmpty")}</div>`;return;}
  /* layered layout: BFS from the first node seen, fall back for orphans */
  const names=Object.keys(g.nodes);
  const level={};const q=[];
  const indeg={};names.forEach(n=>indeg[n]=0);
  g.edges.forEach(([a,b])=>{if(indeg[b]!==undefined)indeg[b]++;});
  names.forEach(n=>{if(!indeg[n]){level[n]=0;q.push(n);}});
  let head=0;
  while(head<q.length){
    const n=q[head++];const lvl=level[n];
    g.edges.forEach(([a,b])=>{
      if(a!==n||level[b]!==undefined)return;
      level[b]=lvl+1;q.push(b);
    });
  }
  names.forEach(n=>{if(level[n]===undefined)level[n]=0;});
  const byLvl={};names.forEach(n=>{(byLvl[level[n]]=byLvl[level[n]]||[]).push(n);});
  const NW=118,NH=34,GG=26,VL=64;
  const W=Math.max(...Object.entries(byLvl).map(([l,ns])=>0))+1;
  const cols=Math.max(...Object.values(byLvl).map(a=>a.length));
  const width=Math.max(NW*cols+GG*(cols-1),200),height=Object.keys(byLvl).length*(NH+VL)+14;
  const pos={};
  Object.entries(byLvl).forEach(([l,ns])=>{
    const lw=NW*ns.length+GG*(ns.length-1);
    ns.forEach((n,i)=>pos[n]={x:(width-lw)/2+i*(NW+GG),y:8+(+l)*(NH+VL)});
  });
  let s=`<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">`+
    `<defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">`+
    `<path d="M0 0L8 4L0 8z" fill="#b4b9ad"/></marker></defs>`;
  g.edges.forEach(([a,b])=>{
    if(!pos[a]||!pos[b])return;
    const x1=pos[a].x+NW,y1=pos[a].y+NH/2,x2=pos[b].x,y2=pos[b].y+NH/2;
    const my=(y1+y2)/2;
    s+=`<path class="gedge${b===g.active?" hot":""}" d="M${x1} ${y1} H${x1+12} V${my} H${x2-12} V${y2} H${x2}"/>`;
  });
  names.forEach(n=>{
    const st=g.nodes[n].status;
    const label=n.length>16?n.slice(0,15)+"…":n;
    s+=`<g class="gnode ${st}" data-node="${esc(n)}">`+
      `<rect x="${pos[n].x}" y="${pos[n].y}" width="${NW}" height="${NH}" rx="7"/>`+
      `<text x="${pos[n].x+NW/2}" y="${pos[n].y+NH/2+3.5}" text-anchor="middle">${esc(label)}</text></g>`;
  });
  s+="</svg>";
  box.innerHTML=s;
  box.querySelectorAll(".gnode").forEach(node=>{
    node.onclick=()=>{
      const n=node.dataset.node;
      runFilter=g.rid;$("runchips").innerHTML="";
      const x=el("span","chip run");x.innerHTML=`${esc(runs[g.rid]?.name||short(g.rid))} <span class="x">✕</span>`;
      x.querySelector(".x").onclick=()=>{runFilter=null;$("runchips").innerHTML="";renderLedger();};
      $("runchips").appendChild(x);
      kindFilter="all";buildChips();renderLedger();showTab("events");
    };
  });
}

/* ════════ artifact modal ════════ */
function mdToHtml(src){
  /* a small DOM-building markdown renderer: headings, fenced code, block
     quotes, ordered/unordered lists with nesting, bold/italic/inline code/
     links. Everything is escaped first; tags only ever come from here. */
  const el=(t)=>document.createElement(t);
  const inline=(txt)=>{
    const sp=el("span");
    sp.textContent=txt;
    sp.innerHTML=sp.innerHTML
      .replace(/`([^`]+)`/g,"<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>")
      .replace(/(^|[^*])\*([^*\n]+)\*(?![^<]*>)/g,"$1<i>$2</i>")
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
    return sp.innerHTML;
  };
  const root=el("div");
  let para=null, lists=[]; /* open <ol>/<ul> stack; depth = 2-space indents */
  const closePara=()=>{para=null;};
  const closeLists=()=>{lists=[];};
  const lines=String(src??"").replace(/\r/g,"").split("\n");
  for(let i=0;i<lines.length;i++){
    const L=lines[i];let m;
    if(/^\s*```/.test(L)){closePara();closeLists();
      const buf=[];i++;
      while(i<lines.length&&!/^\s*```/.test(lines[i]))buf.push(lines[i++]);
      const pre=el("pre"),code=el("code");code.textContent=buf.join("\n");
      pre.appendChild(code);root.appendChild(pre);continue;}
    if((m=L.match(/^(#{1,6})\s+(.*)$/))){closePara();closeLists();
      const h=el("h"+Math.min(m[1].length+1,4));h.innerHTML=inline(m[2]);
      root.appendChild(h);continue;}
    if(/^\s*(-{3,}|\*{3,})\s*$/.test(L)){closePara();closeLists();
      root.appendChild(el("hr"));continue;}
    if((m=L.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))){
      closePara();
      const depth=Math.min(Math.floor(m[1].replace(/\t/g,"  ").length/2),lists.length+1);
      const tag=/^\d/.test(m[2])?"ol":"ul";
      while(lists.length>depth+1)lists.pop();
      if(!lists.length||lists.length<depth+1){
        const ul=el(tag);(lists.length?lists[lists.length-1].lastChild:root).appendChild(ul);
        lists.push(ul);
      }else if(lists[lists.length-1].tagName.toLowerCase()!==tag){
        lists.pop();
        const ul=el(tag);(lists.length?lists[lists.length-1].lastChild:root).appendChild(ul);
        lists.push(ul);
      }
      const li=el("li");li.innerHTML=inline(m[3]);
      lists[lists.length-1].appendChild(li);continue;}
    if(/^\s*>"?/.test(L)){closePara();closeLists();
      const buf=[];while(i<lines.length&&/^\s*>/.test(lines[i]))
        buf.push(lines[i++].replace(/^\s*>\s?/,""));i--;
      const q=el("blockquote");q.innerHTML=inline(buf.join("<br/>"));
      root.appendChild(q);continue;}
    if(!L.trim()){closePara();closeLists();continue;}
    if(!para){para=el("p");root.appendChild(para);}
    para.innerHTML+=(para.innerHTML?"<br/>":"")+inline(L.trim());
  }
  return root.innerHTML;
}

async function openArtifact(p){
  const d=await api(`/api/artifact?sid=${sid}&uri=${encodeURIComponent(p.uri)}`);
  if(d.error)return;
  let body="";
  if(d.text!==undefined){
    if((p.mime||"").includes("markdown")||/\.(md|markdown)$/i.test(d.filename||""))
      body=`<div class="md">${mdToHtml(d.text)}</div>`;
    else if((p.mime||"").includes("json")||/\.json$/i.test(d.filename||""))
      body=`<pre class="ap-body">${esc(JSON.stringify(JSON.parse(d.text),null,2))}</pre>`;
    else body=`<pre class="ap-body">${esc(d.text)}</pre>`;
  }else if(p.mime&&p.mime.startsWith("image/"))
    body=`<img class="ap-img" src="data:${p.mime};base64,${d.b64}"/>`;
  else body=`<div class="empty">${t("dl")}</div>`;
  document.querySelector(".ap-title").textContent=d.filename||p.filename;
  document.querySelector(".ap-content").innerHTML=body;
  document.querySelector(".ap-dl").href=
    `/api/artifact?sid=${sid}&uri=${encodeURIComponent(p.uri)}&download=1`;
  $("overlay").style.display="flex";
}
$("m-close").onclick=()=>$("overlay").style.display="none";
$("overlay").onclick=e=>{if(e.target.id==="overlay")e.currentTarget.style.display="none";};
document.addEventListener("keydown",e=>{if(e.key==="Escape")$("overlay").style.display="none";});

/* ════════ tabs ════════ */
function showTab(name){
  document.querySelectorAll(".tabs button").forEach(b=>b.classList.toggle("active",b.dataset.t===name));
  document.querySelectorAll(".panel").forEach(p=>p.classList.toggle("active",p.id==="p-"+name));
}
document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>showTab(b.dataset.t));
$("inspToggle").onclick=()=>$("insp").classList.toggle("open");

/* ════════ full re-render (language switch resets nothing semantic) ════════ */
function renderAll(){
  const keep=EV.slice();const q=lastQuestion;const art=artifacts.slice();const sp=spans;
  resetViews();
  lastQuestion=q;artifacts=art;spans=sp;spans.forEach(s=>flatten(s,0,[]));
  keep.forEach(ev=>renderEvent(ev));
  renderWaterfall();renderLedger();renderState();renderFiles();renderGraph();
}

/* ════════ poll loop ════════ */
async function poll(){
  const d=await api(`/api/events?sid=${sid}&since=${since}`);
  const fresh=d.events||[];
  fresh.forEach(ev=>{
    since++;EV.push(ev);
    if(ev.kind==="llm_delta")feedStream(ev.data);
    else if(ev.kind==="state_delta"&&ev.data?.question)lastQuestion=ev.data.question;
    else if(ev.kind==="interrupted"&&ev.data?.question)lastQuestion=ev.data.question;
    else renderEvent(ev);
  });
  if(d.question)lastQuestion=d.question;
  if(fresh.length){endStreamsIfIdle(d.status);renderLedger();renderState();renderGraph();}
  spans=d.trace||[];spanById={};
  renderWaterfall();
  if(spanFocus&&spanById[spanFocus]){ /* a deep-linked span opens once its bar exists */
    const r=spanFocus;spanFocus=null;showTab("trace");selectSpan(r);}
  if(tabFocus&&document.getElementById("p-"+tabFocus)){ /* deep-linked tab */
    showTab(tabFocus);tabFocus=null;}
  artifacts=d.artifacts||[];renderFiles();
  statusWas=d.status||statusWas;
  setStatus(d.status);
  if(d.chat){chat=true;$("msg").placeholder=t("ph");$("run").setAttribute("aria-label",t("send"));}
  $("run").disabled=d.status==="running";
  if(d.status==="suspended"&&!document.querySelector(".tc.park"))
    addPark(d.question,d.error);
  if(d.status==="completed"||d.status==="failed"){
    endStreams();patchDurations();
    clearInterval(timer);
    /* a replayed session arrives as one burst; the incremental stick misses
       it — land at the newest card when the run is over */
    const tb=$("transcript");tb.scrollTop=tb.scrollHeight;
    if(d.output||d.error){
      const outs=document.querySelectorAll(".tc.fin .bd .out");
      const target=outs[outs.length-1];
      if(target&&!target.textContent)target.innerHTML=mdToHtml(d.output||d.error||"");
    }
  }
}
function endStreamsIfIdle(status){if(status!=="running")endStreams();}

/* ════════ actions ════════ */
$("run").onclick=async()=>{
  if(!current||running)return;
  const box=$("msg"),text=box.value.trim();
  if(!text)return;
  running=true;$("run").disabled=true;
  try{
    if(sid&&chat){
      await api("/api/turn",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({sid,input:text})});
      box.value="";
    }else{
      resetViews();
      const d=await api("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({scenario:current.key,input:text,lang:LANG})});
      sid=d.sid;since=0;
    }
    clearInterval(timer);timer=setInterval(poll,400);await poll();
  }finally{running=false;}
};
async function decide(approved,cardEl){
  if(cardEl)cardEl.remove();
  await api("/api/resume",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({sid,approved})});
  clearInterval(timer);timer=setInterval(poll,400);await poll();
}
$("msg").addEventListener("keydown",e=>{if(e.key==="Enter")$("run").click();});
$("langbtn").onclick=()=>{
  LANG=LANG==="en"?"zh":"en";localStorage.setItem("pg-lang",LANG);
  applyLang();loadScenes();
};

/* boot — deep link: #run=<key> picks the scenario and runs it with its
   default input, so a populated playground is one shareable URL away */
FINS_OK=[];FINS_BAD=[];
(async function boot(){
  applyLang();await loadScenes();showTab("trace");
  const v=location.hash.match(/^#view=(\w+)(?:&(\w+)=(\S+))?/);
  const tab=(v&&v[2]==="t"&&v[3])?v[3]:null;
  if(v){ /* replay a finished session from its ledger, without re-running;
            #view=<sid>&s=<run> additionally opens that span's detail */
    sid=v[1];since=0;
    document.querySelectorAll(".scn").forEach(x=>x.classList.remove("active"));
    $("desc").textContent=t("replayDesc");
    $("crumb").textContent=t("replay");
    $("msg").value="";
    clearInterval(timer);timer=setInterval(poll,400);
    if(v[2]==="s"&&v[3])spanFocus=v[3]; /* applied by the poll that renders it */
    if(tab)tabFocus=tab;
    await poll();
    return;}
  const m=location.hash.match(/^#run=(\w+)/);
  if(!m)return;
  const list=await api("/api/scenarios");
  const i=list.findIndex(x=>x.key===m[1]);
  const btn=$("scenes").children[i];
  if(btn){select(list[i],btn);$("msg").value=L(list[i],"default");$("run").click();}
})();
</script>
</body>
</html>
"""
