"use strict";
/* ══════════════════════════════════════════════════════════════════
   부트스트랩
   ══════════════════════════════════════════════════════════════════ */
init();
async function init(){
  try{
    const idx = await getJSON("/api/index");
    S.idx = idx;
    S.fps = idx.fps || 30;
    renderStats();
    buildTree();
    renderRuns();
    if(S.runs.length) selectRun(S.runs[0].run);   // 첫 런의 첫 태스크까지 열린다
    else { renderTasks(); clearMain(); $("gSub").textContent = "no groups"; }
  }catch(e){
    $("gSub").textContent = "failed to load index: " + e.message;
  }
}

/* ── 사이드바 통계 ────────────────────────────────────────────────── */
function renderStats(){
  const tot = S.idx.totals || {};
  const by  = tot.by_outcome || {};
  const box = $("stats"); box.textContent="";
  const items = [
    ["episodes", tot.episodes ?? "—"],
    ["groups",   tot.groups   ?? "—"],
    ["fps",      S.fps],
    ["success",  by.success ?? 0],
    ["fail",     by.fail ?? 0],
    ["deleted",  by.deleted ?? 0],
  ];
  for(const [k,v] of items){
    const d = el("div","stat");
    d.appendChild(el("div","lbl",k));
    d.appendChild(el("div","v",String(v)));
    box.appendChild(d);
  }
}

/* ══════════════════════════════════════════════════════════════════
   사이드바 트리 — 런(트라이) → 태스크(스텝) 2단
   ══════════════════════════════════════════════════════════════════ */
/* "coffee_new07" → "7". 숫자가 없으면 등장 순서로 매긴다. */
function runNo(run, i){
  const m = /(\d+)\s*$/.exec(String(run||""));
  return m ? String(Number(m[1])) : String(i+1);
}
function curRun(){ return S.runs.find(r=>r.run===S.run) || null; }
function curTasks(){ const r=curRun(); return r ? r.groups : []; }

function buildTree(){
  S.byGid = new Map();
  const map = new Map();                      // run -> groups
  const order = (S.idx.runs||[]).slice();     // 인덱스가 준 런 순서를 그대로 쓴다
  for(const g of (S.idx.groups||[])){
    S.byGid.set(g.gid, g);
    if(!map.has(g.run)){ map.set(g.run, []); if(order.indexOf(g.run)<0) order.push(g.run); }
    map.get(g.run).push(g);
  }
  S.runs = order.filter(r=>map.has(r)).map((run,i)=>{
    const gs = map.get(run).slice().sort((a,b)=> (a.cycle-b.cycle) || (a.step-b.step));
    let fail=0, unstable=0;            // 실패/불안정이 섞인 "태스크" 수
    for(const g of gs){
      const live = (g.attempts||[]).filter(a=>!a.deleted);
      if(live.some(a=>a.outcome==="fail")) fail++;
      else if(live.some(a=>a.outcome==="unstable")) unstable++;
    }
    const cycles = new Set(gs.map(g=>g.cycle));
    return {run, no:runNo(run,i), groups:gs, fail, unstable, multiCycle:cycles.size>1};
  });
}

/* 왼쪽 컬럼: 트라이 세로 목록 (Finder 1열) */
function renderRuns(){
  const box = $("rlist"); box.textContent="";
  $("rcount").textContent = "(" + S.runs.length + ")";
  for(const r of S.runs){
    const b = el("button","row" + (r.run===S.run ? " on" : ""));
    b.setAttribute("role","option");
    b.setAttribute("aria-selected", r.run===S.run ? "true" : "false");
    b.appendChild(el("span","nm","Try " + r.no));
    const n = r.groups.length || 1;
    if(r.fail || r.unstable){
      const m = el("span","meter" + (r.fail ? "" : " warn"));
      m.style.width = (100 * (r.fail || r.unstable) / n).toFixed(1) + "%";
      b.appendChild(m);
    }
    // 원래 런 이름은 폭이 좁으니 툴팁으로
    b.title = r.run + " · " + r.groups.length + " tasks"
            + (r.fail ? " · fail " + r.fail : "") + (r.unstable ? " · unstable " + r.unstable : "");
    b.dataset.run = r.run;
    b.onclick = ()=>selectRun(r.run);
    box.appendChild(b);
  }
}

/* 오른쪽 컬럼: 선택된 트라이의 태스크 세로 목록 (Finder 2열) */
function renderTasks(){
  const box = $("tlist"); box.textContent="";
  const r = curRun(), gs = r ? r.groups : [];
  $("tcount").textContent = "(" + gs.length + ")";
  if(!gs.length){ box.appendChild(el("div","empty","Select a try")); return; }
  for(const g of gs){
    const b = el("button","row" + (g.gid===S.gid ? " on" : ""));
    b.setAttribute("role","option");
    b.setAttribute("aria-selected", g.gid===S.gid ? "true" : "false");
    b.dataset.gid = g.gid;
    b.appendChild(el("span","nm", (r.multiCycle ? "c"+g.cycle+" · " : "") + "Task " + g.step));
    const bd = el("span","badges");
    for(const a of (g.attempts||[])){
      const oc = a.deleted ? "deleted" : (a.outcome||"unlabeled");
      bd.appendChild(el("span","bdg "+oc, OC_INIT[oc]||"?"));
    }
    b.appendChild(bd);
    // 지시문 요약은 폭이 좁으니 툴팁으로
    b.title = "Task " + g.step + (g.instruction ? " · " + g.instruction : "");
    b.onclick = ()=>selectGroup(g.gid);
    box.appendChild(b);
  }
}

/* 런 선택 → 태스크 칸 갱신. 같은 스텝을 유지해 런끼리 바로 비교되게 한다. */
function selectRun(run){
  const r = S.runs.find(x=>x.run===run); if(!r) return;
  const keepStep = S.group ? S.group.step : null;
  S.run = run;
  renderRuns(); renderTasks();
  const next = r.groups.find(g=>g.step===keepStep) || r.groups[0];
  if(next) selectGroup(next.gid);
  else clearMain();
}

/* 표시할 그룹이 없을 때 메인을 비운다 */
function clearMain(){
  if(S.ac) S.ac.abort();
  pause();
  S.gid=null; S.group=null; S.eps={}; S.epsAll={}; S.order=[]; S.focus=null;
  S.T=0; S.dur=0; S.charts=[];
  $("gTitle").textContent = "rollout viewer";
  $("gSub").textContent   = "no group to show";
  $("instr").textContent  = "—";
  $("aCount").textContent = "";
  $("attempts").textContent = "";
  $("charts").textContent = "";
  updateCursor();
}

/* ══════════════════════════════════════════════════════════════════
   그룹 선택 / 에피소드 로드
   ══════════════════════════════════════════════════════════════════ */
/* 그룹의 대표 시도 하나 — 기본 표시는 이것만이다.
   1) 삭제되지 않은 시도 중 outcome==="success" 인 마지막(attempt 최대) 시도
   2) 성공이 없으면 마지막 unstable 시도 (unstable 로 끝난 그룹들)
   3) 그것도 없으면 마지막 시도, 전부 삭제분이면 원본의 마지막 시도 */
function pickDefaultAttempt(atts){
  if(!atts || !atts.length) return null;
  const byNo = atts.slice().sort((a,b)=> (a.attempt||0) - (b.attempt||0));
  const live = byNo.filter(a=>!a.deleted);
  const pool = live.length ? live : byNo;
  const last = (oc)=>{ for(let i=pool.length-1;i>=0;i--) if(pool[i].outcome===oc) return pool[i]; return null; };
  return last("success") || last("unstable") || pool[pool.length-1];
}
/* 화면에 올릴 시도 목록 — 토글이 꺼져 있으면 대표 시도 하나 */
function visibleAttempts(g){
  const atts = (g && g.attempts) ? g.attempts.slice() : [];
  if(S.showAll || atts.length<=1) return atts;
  const pick = pickDefaultAttempt(atts);
  return pick ? [pick] : atts;
}

async function selectGroup(gid, opts){
  const keepT = !!(opts && opts.keepT);
  const prevT = S.T;
  if(S.ac) S.ac.abort();                       // 진행 중 fetch 취소
  const ac = new AbortController(); S.ac = ac;

  pause();
  if(S.gid !== gid) S.epsAll = {};             // 그룹이 바뀌면 에피소드 캐시를 버린다
  S.gid = gid;
  S.group = S.byGid.get(gid) || null;
  if(S.group && S.group.run !== S.run) S.run = S.group.run;   // 다른 런의 그룹을 직접 열어도 트리가 따라온다
  S.eps = {}; S.order = []; S.focus = null; S.T=0; S.dur=0;
  // 이전 그룹의 차트 핸들은 곧 DOM 에서 떨어져 나간다. 로드가 실패해 buildCharts()
  // 까지 못 가면 updateCursor() 가 죽은 노드를 계속 만지므로 여기서 비운다.
  S.charts = [];
  renderRuns(); renderTasks();

  const g = S.group;
  if(!g){ renderAttemptCount(0,0); return; }
  const rno = (curRun()||{}).no || "?";
  const total = (g.attempts||[]).length;
  $("gTitle").textContent = "Try " + rno + " · Task " + g.step;
  $("instr").textContent  = g.instruction || "—";
  $("attempts").textContent = "";
  $("charts").textContent = "";
  $("charts").appendChild(el("div","empty","loading charts…"));

  const atts = visibleAttempts(g);
  const pick = atts[0];
  $("gSub").textContent   = g.run + " · c" + g.cycle + " · " + g.gid
                          + " · " + total + (total===1 ? " attempt" : " attempts")
                          + (atts.length < total
                              ? " · showing final " + (pick ? (pick.deleted ? "deleted" : (pick.outcome||"unlabeled")) : "—")
                                + " only (attempt " + (pick ? (pick.attempt||1) : "—") + ")"
                              : "");
  renderAttemptCount(atts.length, total);
  S.order = atts.map(a=>a.eid);
  renderAttempts(atts);                         // 영상/컨트롤 먼저

  // 에피소드 병렬 로드 (같은 그룹에서 토글만 바꾼 경우 캐시 재사용 — 재요청 없음)
  let loaded = 0;
  await Promise.all(atts.map(async a=>{
    if(S.epsAll[a.eid]){ S.eps[a.eid] = S.epsAll[a.eid]; loaded++; return; }
    try{
      const ep = await getJSON("/api/episode?eid="+encodeURIComponent(a.eid), ac.signal);
      if(ac.signal.aborted) return;
      S.epsAll[a.eid] = ep; S.eps[a.eid] = ep; loaded++;
    }catch(e){ if(e.name!=="AbortError") console.warn("episode load failed", a.eid, e); }
  }));
  if(ac.signal.aborted) return;

  S.fps = (S.eps[S.order[0]] || {}).fps || S.fps;
  S.dur = Math.max(0, ...S.order.map(eid=>epDur(S.eps[eid])));
  S.focus = S.order[0] || null;
  if(!loaded){ $("charts").textContent=""; $("charts").appendChild(el("div","empty","failed to load episodes")); return; }

  buildCharts();
  applyFocus();
  seekTo(keepT ? prevT : 0);
}

/* 시도 머리줄의 개수 표시 — 숨긴 시도가 있다는 사실은 여기서 드러난다 */
function renderAttemptCount(shown, total){
  $("aCount").textContent = total ? (shown < total ? "(" + shown + " / " + total + ")" : "(" + total + ")") : "";
}

/* "전체 시도 보기" 토글 — 상태는 전역이라 그룹을 바꿔도 유지된다 */
$("showAll").onchange = (e)=>{
  S.showAll = !!e.target.checked;
  e.target.blur();                              // 포커스를 놔줘야 Space 가 다시 재생 토글로 간다
  if(S.gid) selectGroup(S.gid, {keepT:true});
};

/* ══════════════════════════════════════════════════════════════════
   시도 카드
   ══════════════════════════════════════════════════════════════════ */
function renderAttempts(atts){
  const box = $("attempts"); box.textContent="";
  atts.forEach((a,i)=>{
    const eid = a.eid;
    const card = el("div","panel acard"); card.dataset.eid = eid;
    card.onmousedown = ()=>setFocus(eid);

    // 헤더: 시도 번호 + (있으면) Attention 토글 + 결과 배지
    const h = el("div","arow");
    h.appendChild(el("div","aname","Attempt " + (a.attempt||i+1) + " · ep" + a.ep));
    const rt = el("span","aright");
    const ab = el("button","chip attn-chip","Attention");
    ab.style.display = "none";                    // 오버레이 클립 존재 확인 후에만 노출
    ab.title = "Cycle overlay: attention / causal / original";
    ab.onclick = (ev)=>{ ev.stopPropagation(); cycleMode(eid); ab.blur(); };
    rt.appendChild(ab);
    const oc = a.deleted ? "deleted" : (a.outcome||"unlabeled");
    rt.appendChild(el("span","bdg "+oc, oc));
    h.appendChild(rt);
    card.appendChild(h);
    Promise.all([probeAttn(eid), probeCausal(eid)]).then(([hasA, hasC])=>{
      if((!hasA && !hasC) || !ab.isConnected) return;   // 폐기된 카드는 무시
      ab.style.display = "";
      // 기본값: 오버레이 클립이 있으면 켠 상태로 시작 (사용자가 바꾼 적 없을 때만)
      if(S.vmode[eid] === undefined){ setMode(eid, hasA ? "attn" : "causal"); return; }
      paintModeBtn(eid);
    });

    // 영상: front(위) / wrist(아래) 두 카메라를 동시에
    const media = el("div","media");
    for(const cam of CAMS){
      const pane = el("div","pane"); pane.dataset.cam = cam;
      const v = el("video"); v.muted=true; v.playsInline=true; v.preload="auto";
      v.setAttribute("playsinline",""); v.dataset.eid=eid; v.dataset.cam=cam;
      // 핸들러/타이머는 카드가 다시 그려지면 폐기된 DOM에 남는다.
      // isConnected 로 걸러 내지 않으면 죽은 엘리먼트가 전역 상태를 덮어쓴다.
      v.onerror = ()=>{ if(v.isConnected) toFrames(eid, cam, v); };
      // 로드 완료 시 프레임 폴백 복귀 + 현재 재생 시각 재동기(토글로 src 를 갈아끼운 직후 대비)
      v.onloadeddata = ()=>{ clearTimeout(v._wd); if(v.isConnected){ backToVideo(eid, cam, v); syncOne(eid, true); } };
      const img = el("img"); img.style.display="none"; img.alt=""; img.dataset.cam=cam;
      // 프레임이 없는 에피소드(삭제분 등)는 /frame 이 404 → 깨진 이미지 아이콘이 뜬다. 그냥 검은 박스로 둔다.
      img.onerror = ()=>{ img.style.visibility="hidden"; };
      img.onload  = ()=>{ img.style.visibility="visible"; };
      pane.appendChild(v); pane.appendChild(img);
      pane.appendChild(el("span","chip camlabel", cam));   // 토글이 아니라 라벨
      media.appendChild(pane);
    }
    card.appendChild(media);
    initMedia(eid, card);

    // 메모
    card.appendChild(el("div","memo", a.memo || ""));

    // 지표 칩
    const m = a.metrics || {};
    const chips = el("div","chips");
    chips.appendChild(el("span","chip","jerk p95 " + fx(m.jerk_p95,2)));
    chips.appendChild(el("span","chip","track " + fx(m.track_err,2)));
    chips.appendChild(el("span","chip","rev " + fx(m.reversals,1)));
    chips.appendChild(el("span","chip","frames " + (a.n_frames ?? "—")));
    card.appendChild(chips);

    box.appendChild(card);
  });
}

function setFocus(eid){
  if(!eid || S.focus===eid) return;
  S.focus = eid;
  applyFocus();
  updateCursor();
}

function applyFocus(){
  document.querySelectorAll(".acard").forEach(c=>c.classList.toggle("focus", c.dataset.eid===S.focus));
  // 차트 불투명도: 시도1 = 1.0, 포커스 = 1.0, 나머지 = 0.45
  for(const ch of S.charts){
    ch.svg.querySelectorAll("path[data-eid]").forEach(p=>{
      const eid = p.dataset.eid;
      p.setAttribute("opacity", (eid===S.order[0] || eid===S.focus) ? "1" : "0.45");
    });
    ch.svg.querySelectorAll("text[data-eid]").forEach(t=>{
      t.setAttribute("opacity", t.dataset.eid===S.focus ? "1" : "0.5");
    });
    drawTrim(ch);
  }
}

let raf=null, lastTS=0;
function tick(ts){
  if(!S.playing){ raf=null; return; }
  const dt = lastTS ? (ts-lastTS)/1000 : 0; lastTS = ts;
  S.T += dt;
  if(S.T >= S.dur){ S.T = S.dur; updateCursor(); syncAll(); pause(); return; }
  updateCursor(); syncAll();
  raf = requestAnimationFrame(tick);
}
function play(){
  if(S.playing || S.dur<=0) return;
  if(S.T >= S.dur - 1e-6) S.T = 0;
  S.playing = true; lastTS = 0;
  $("bPlay").classList.add("active");
  $("icPlay").style.display="none"; $("icPause").style.display="block";
  syncAll();
  raf = requestAnimationFrame(tick);
}
function pause(){
  S.playing = false;
  if(raf){ cancelAnimationFrame(raf); raf=null; }
  $("bPlay").classList.remove("active");
  $("icPlay").style.display="block"; $("icPause").style.display="none";
  syncAll();
}
function toggle(){ S.playing ? pause() : play(); }
function seekTo(t){ S.T = clamp(t, 0, S.dur); updateCursor(); syncAll(); }
function stepFrames(n){ pause(); seekTo(S.T + n/(S.fps||30)); }

$("bPlay").onclick  = toggle;
$("bBack").onclick  = ()=>{ pause(); seekTo(S.T-5); };
$("bFwd").onclick   = ()=>{ pause(); seekTo(S.T+5); };
$("bReset").onclick = ()=>{ pause(); seekTo(0); };
$("seek").oninput   = (e)=>{ pause(); seekTo(S.dur * (Number(e.target.value)/1000)); };

/* ══════════════════════════════════════════════════════════════════
   키보드
   ══════════════════════════════════════════════════════════════════ */
/* ↑↓ : 선택된 런 안에서 태스크 이동 */
function moveTask(d){
  const gs = curTasks(); if(!gs.length) return;
  const i = gs.findIndex(g=>g.gid===S.gid);
  const n = clamp((i<0?0:i) + d, 0, gs.length-1);
  if(n===i || !gs[n]) return;
  selectGroup(gs[n].gid);
  const it = $("tlist").querySelector('.row[data-gid="'+CSS.escape(gs[n].gid)+'"]');
  if(it) it.scrollIntoView({block:"nearest"});
}
/* ⇧↑↓ / PageUp·PageDown : 트라이 이동 (같은 태스크 번호 유지) */
function moveRun(d){
  if(!S.runs.length) return;
  const i = S.runs.findIndex(r=>r.run===S.run);
  const n = clamp((i<0?0:i) + d, 0, S.runs.length-1);
  if(n===i || !S.runs[n]) return;
  selectRun(S.runs[n].run);
  const ch = $("rlist").querySelector('.row[data-run="'+CSS.escape(S.runs[n].run)+'"]');
  if(ch) ch.scrollIntoView({block:"nearest"});
}

document.addEventListener("keydown", (e)=>{
  const tag = (e.target.tagName||"").toLowerCase();
  if(tag==="input" || tag==="textarea" || tag==="select") return;
  const k = e.key;
  if(k===" "){ e.preventDefault(); toggle(); return; }
  if(k==="ArrowLeft"){  e.preventDefault(); stepFrames(e.shiftKey?-10:-1); return; }
  if(k==="ArrowRight"){ e.preventDefault(); stepFrames(e.shiftKey? 10: 1); return; }
  if(k==="ArrowUp" || k==="ArrowDown"){
    e.preventDefault();
    const d = (k==="ArrowDown") ? 1 : -1;
    if(e.shiftKey) moveRun(d); else moveTask(d);
    return;
  }
  if(k==="PageUp" || k==="PageDown"){          // ⇧↑↓ 대신 써도 되는 런 이동
    e.preventDefault();
    moveRun(k==="PageDown" ? 1 : -1);
    return;
  }
  if(k==="[" || k==="]"){
    e.preventDefault();
    const i = S.order.indexOf(S.focus);
    const n = clamp((i<0?0:i) + (k==="]"?1:-1), 0, S.order.length-1);
    setFocus(S.order[n]);
    return;
  }
});
