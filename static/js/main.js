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
    buildTree();
    renderStats();
    renderSidebar();
    const h = parseHash();
    const x0 = (h && S.exps.find(x=>x.name===h.exp)) || S.exps[0];
    if(x0){
      if(h && h.tno!=null) S.tno = h.tno;      // selectExp 가 같은 Try 번호를 찾아 준다
      selectExp(x0.name);                      // 첫 실험 → 첫 Try 화면까지 열린다
    }else{ clearMain(); $("gSub").textContent = "no groups"; }
  }catch(e){
    $("gSub").textContent = "failed to load index: " + e.message;
  }
}

/* URL 해시 — #<run>/<try>  (Run/Try 단위로만 저장) */
function parseHash(){
  const h = (location.hash || "").replace(/^#/, "");
  if(!h) return null;
  const [e, t] = h.split("/");
  const tno = (t!=null && t!=="") ? Number(t) : null;
  return {exp: decodeURIComponent(e||""), tno: (tno!=null && isFinite(tno)) ? tno : null};
}
function writeHash(){
  if(!S.exp) return;
  const h = "#" + encodeURIComponent(S.exp) + (S.tno!=null ? "/" + S.tno : "");
  if(location.hash !== h) history.replaceState(null, "", h);
}
/* 주소창에서 해시만 바꾼 경우(같은 문서) — 그 Run/Try 로 이동 */
window.addEventListener("hashchange", ()=>{
  const h = parseHash(); if(!h || !S.exps.length) return;
  const x = S.exps.find(x=>x.name===h.exp); if(!x) return;
  if(x.name===S.exp && (h.tno==null || h.tno===S.tno)) return;
  if(h.tno!=null) S.tno = h.tno;
  selectExp(x.name);
});

/* ── 사이드바 통계 ────────────────────────────────────────────────── */
function renderStats(){
  const tot = S.idx.totals || {};
  const by  = tot.by_outcome || {};
  const box = $("stats"); box.textContent="";
  const nTries = S.exps.reduce((n,x)=>n + x.tries.length, 0);
  const items = [
    ["runs",     S.exps.length],
    ["tries",    nTries],
    ["episodes", tot.episodes ?? "—"],
    ["groups",   tot.groups   ?? "—"],
    ["success",  by.success ?? 0],
    ["fail",     by.fail ?? 0],
    ["deleted",  by.deleted ?? 0],
    ["fps",      S.fps],
  ];
  for(const [k,v] of items){
    const d = el("div","stat");
    d.appendChild(el("div","lbl",k));
    d.appendChild(el("div","v",String(v)));
    box.appendChild(d);
  }
}

/* ══════════════════════════════════════════════════════════════════
   사이드바 트리 — Run(실험) → Try 2단. (Task 는 메인 화면에 전부 펼친다)
   그룹(g)에는 서버가 experiment / try_no 를 붙여 준다.
     · coffee_newNN  → experiment "NorRec_RW___Red", try_no = NN
     · 그 외 런      → experiment = 런 디렉토리명,  try_no = cycle
   없으면(구버전 인덱스) runExp()/runTryNo() 로 같은 규칙을 클라이언트에서 적용한다.
   ══════════════════════════════════════════════════════════════════ */
function runExp(run){ return /^coffee_new\d+$/.test(String(run||"")) ? "NorRec_RW___Red" : String(run||""); }
function runTryNo(run, cycle){
  const m = /^coffee_new(\d+)$/.exec(String(run||""));
  return m ? Number(m[1]) : Number(cycle||0);
}
function gExp(g){ return g.experiment || runExp(g.run); }
function gTry(g){ return (g.try_no!=null) ? Number(g.try_no) : runTryNo(g.run, g.cycle); }

function curExp(){ return S.exps.find(x=>x.name===S.exp) || null; }
function curTry(){ const x=curExp(); return x ? (x.tries.find(t=>t.no===S.tno) || null) : null; }

/* 순수 함수: 인덱스 → 실험 트리. (node 로도 실행해 검증할 수 있게 전역 상태를 건드리지 않는다) */
function buildExpTree(idx){
  const groups = idx.groups || [];
  const order = [];                               // 실험 등장 순서 (index.experiments 가 있으면 그 순서)
  for(const x of (idx.experiments||[])) if(order.indexOf(x.name)<0) order.push(x.name);
  const runOrder = (idx.runs||[]).slice();        // 런 순서 — 같은 실험 안에서 try 정렬의 보조 키
  const emap = new Map();                         // name -> Map(tryNo -> groups)
  for(const g of groups){
    const en = gExp(g), tn = gTry(g);
    if(!emap.has(en)){ emap.set(en, new Map()); if(order.indexOf(en)<0) order.push(en); }
    const tm = emap.get(en);
    if(!tm.has(tn)) tm.set(tn, []);
    tm.get(tn).push(g);
  }
  const metaByName = new Map((idx.experiments||[]).map(x=>[x.name, x]));
  return order.filter(n=>emap.has(n)).map(name=>{
    const tm = emap.get(name);
    const tries = Array.from(tm.keys()).sort((a,b)=>a-b).map(no=>{
      const gs = tm.get(no).slice().sort((a,b)=>
        (runOrder.indexOf(a.run)-runOrder.indexOf(b.run)) || (a.cycle-b.cycle) || (a.step-b.step));
      let fail=0, unstable=0;                     // 실패/불안정이 섞인 "태스크" 수
      for(const g of gs){
        const live = (g.attempts||[]).filter(a=>!a.deleted);
        if(live.some(a=>a.outcome==="fail")) fail++;
        else if(live.some(a=>a.outcome==="unstable")) unstable++;
      }
      const runs = Array.from(new Set(gs.map(g=>g.run)));
      const cycles = new Set(gs.map(g=>g.cycle));
      const nEps = gs.reduce((n,g)=>n + (g.attempts||[]).length, 0);
      return {no, key:name+"|"+no, run:runs[0], runs, groups:gs, fail, unstable,
              multiCycle:cycles.size>1, nEps};
    });
    const meta = metaByName.get(name) || {};
    const nEps = tries.reduce((n,t)=>n + t.nEps, 0);
    const nGroups = tries.reduce((n,t)=>n + t.groups.length, 0);
    const fail = tries.reduce((n,t)=>n + t.fail, 0);
    const unstable = tries.reduce((n,t)=>n + t.unstable, 0);
    return {name, tries, nEps, nGroups, fail, unstable, model:meta.model||null,
            runs: meta.runs || Array.from(new Set(tries.flatMap(t=>t.runs)))};
  });
}

function buildTree(){
  S.byGid = new Map();
  for(const g of (S.idx.groups||[])) S.byGid.set(g.gid, g);
  S.exps = buildExpTree(S.idx);
}

/* Finder 1열: Run(실험) 세로 목록 */
function renderExps(){
  const box = $("elist"); box.textContent="";
  $("ecount").textContent = "(" + S.exps.length + ")";
  for(const x of S.exps){
    const on = x.name===S.exp;
    const b = el("button","row" + (on ? " on" : ""));
    b.setAttribute("role","option");
    b.setAttribute("aria-selected", on ? "true" : "false");
    b.appendChild(el("span","nm", x.name));          // 사용자가 부르는 이름 그대로 (정리하지 않는다)
    if(x.fail || x.unstable){
      const m = el("span","meter" + (x.fail ? "" : " warn"));
      m.style.width = (100 * (x.fail || x.unstable) / (x.nGroups || 1)).toFixed(1) + "%";
      b.appendChild(m);
    }
    b.title = x.name + " · " + x.tries.length + (x.tries.length===1 ? " try" : " tries")
            + " · " + x.nEps + " eps"
            + (x.model ? " · " + x.model : "")
            + (x.fail ? " · fail " + x.fail : "") + (x.unstable ? " · unstable " + x.unstable : "");
    b.dataset.exp = x.name;
    b.onclick = ()=>selectExp(x.name);
    box.appendChild(b);
  }
}

/* Finder 2열: 선택된 실험의 Try 세로 목록 */
function renderTries(){
  const box = $("rlist"); box.textContent="";
  const x = curExp(), tries = x ? x.tries : [];
  $("rcount").textContent = "(" + tries.length + ")";
  if(!tries.length){ box.appendChild(el("div","empty","Select a run")); return; }
  for(const r of tries){
    const on = r.no===S.tno;
    const b = el("button","row" + (on ? " on" : ""));
    b.setAttribute("role","option");
    b.setAttribute("aria-selected", on ? "true" : "false");
    b.appendChild(el("span","nm","Try " + r.no));
    // 오른쪽 끝: 태스크 수 + 실패/불안정 태스크 수 (폭이 좁아 배지 대신 숫자)
    const bd = el("span","badges");
    bd.appendChild(el("span","bdg", r.groups.length + "t"));
    if(r.fail) bd.appendChild(el("span","bdg fail", "F" + r.fail));
    if(r.unstable) bd.appendChild(el("span","bdg unstable", "U" + r.unstable));
    b.appendChild(bd);
    const n = r.groups.length || 1;
    if(r.fail || r.unstable){
      const m = el("span","meter" + (r.fail ? "" : " warn"));
      m.style.width = (100 * (r.fail || r.unstable) / n).toFixed(1) + "%";
      b.appendChild(m);
    }
    // 원래 런 이름/사이클은 폭이 좁으니 툴팁으로
    b.title = r.runs.join(", ") + (r.multiCycle ? "" : " · c" + (r.groups[0] ? r.groups[0].cycle : "?"))
            + " · " + r.groups.length + " tasks · " + r.nEps + " eps"
            + (r.fail ? " · fail " + r.fail : "") + (r.unstable ? " · unstable " + r.unstable : "");
    b.dataset.tryno = String(r.no);
    b.onclick = ()=>selectTry(r.no);
    box.appendChild(b);
  }
}
function renderSidebar(){ renderExps(); renderTries(); }

/* Run(실험) 선택 → 같은 Try 번호가 있으면 유지, 없으면 첫 Try. */
function selectExp(name){
  const x = S.exps.find(e=>e.name===name); if(!x) return;
  S.exp = name;
  const t = x.tries.find(t=>t.no===S.tno) || x.tries[0];
  if(t) selectTry(t.no);
  else { S.tno=null; renderSidebar(); clearMain(); }
}

/* Try 선택 → 메인에 그 Try 의 그룹 전체를 펼친다 */
function selectTry(no){
  const x = curExp(); if(!x) return;
  const r = x.tries.find(t=>t.no===no); if(!r) return;
  S.tno = no;
  renderSidebar();
  writeHash();
  renderTry(r);
}

/* 모든 그룹 플레이어를 멈추고 화면을 비운다 */
function teardownPlayers(){
  for(const P of S.players.values()) pause(P);
  if(S.io) S.io.disconnect();
  // 떼어낼 카드의 영상은 src 를 비워 네트워크/디코더를 즉시 놓게 한다 (GC 전까지 로딩을 붙들지 않게)
  document.querySelectorAll("#groups video").forEach(v=>{
    clearTimeout(v._wd); v.onerror = null; v.onloadeddata = null;
    try{ v.pause(); v.removeAttribute("src"); v.load(); }catch(e){}
  });
  S.players = new Map(); S.porder = []; S.eidGid = new Map(); S.order = []; S.fgid = null;
  $("groups").textContent = "";
}
function clearMain(){
  if(S.ac) S.ac.abort();
  teardownPlayers();
  S.eps = {};
  $("gTitle").textContent = "rollout viewer";
  $("gSub").textContent   = "no try to show";
}

/* ══════════════════════════════════════════════════════════════════
   Try 렌더 — 그룹(=Task step) 블록 N개 × (대표 1개 | 모든 시도)
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

async function renderTry(r, opts){
  const keepT = !!(opts && opts.keepT);
  const prevT = new Map();                       // gid -> T (Show all 토글 시 재생 위치 유지)
  for(const P of S.players.values()) prevT.set(P.gid, P.T);
  const prevFocusGid = S.fgid;
  const sameTry = !!(opts && opts.sameTry);

  if(S.ac) S.ac.abort();                         // 진행 중 fetch 취소
  const ac = new AbortController(); S.ac = ac;
  teardownPlayers();
  if(!sameTry){ S.eps = {}; $("main").scrollTop = 0; }   // Try 가 바뀌면 에피소드 캐시를 버리고 맨 위로

  const gs = r.groups || [];
  const x = curExp();
  $("gTitle").textContent = (x ? x.name : "") + " · Try " + r.no;
  const nEps = gs.reduce((n,g)=>n + (g.attempts||[]).length, 0);
  $("gSub").textContent = r.runs.join(", ") + (r.multiCycle ? "" : " · c" + (gs[0] ? gs[0].cycle : "?"))
                        + " · " + gs.length + (gs.length===1 ? " task" : " tasks")
                        + " · " + nEps + (nEps===1 ? " episode" : " episodes")
                        + (S.showAll ? " · showing all attempts" : " · showing final attempt of each task");
  const box = $("groups");
  if(!gs.length){ box.appendChild(el("div","empty","no tasks in this try")); return; }

  // 1) 그룹 블록 DOM 먼저 (영상 카드 포함) — 차트는 에피소드가 오면 그린다
  const plan = [];                               // [{P, atts}]
  for(const g of gs){
    const P = newPlayer(g.gid, g);
    const atts = visibleAttempts(g);
    P.order = atts.map(a=>a.eid);
    for(const eid of P.order){ S.eidGid.set(eid, P.gid); S.order.push(eid); }
    S.players.set(g.gid, P); S.porder.push(g.gid);
    renderGroupBlock(P, r, atts, box);
    plan.push({P, atts});
  }
  S.fgid = (prevFocusGid && S.players.has(prevFocusGid)) ? prevFocusGid : S.porder[0];
  applyGroupFocus();

  // 2) 에피소드 로드 — 그룹 단위로 병렬, 도착한 그룹부터 차트를 그린다 (캐시 재사용)
  await Promise.all(plan.map(async ({P, atts})=>{
    let loaded = 0;
    await Promise.all(atts.map(async a=>{
      if(S.eps[a.eid]){ loaded++; return; }
      try{
        const ep = await getJSON("/api/episode?eid="+encodeURIComponent(a.eid), ac.signal);
        if(ac.signal.aborted) return;
        S.eps[a.eid] = ep; loaded++;
      }catch(e){ if(e.name!=="AbortError") console.warn("episode load failed", a.eid, e); }
    }));
    if(ac.signal.aborted) return;
    const cbox = P.ui.charts;
    if(!loaded){ cbox.textContent=""; cbox.appendChild(el("div","empty","failed to load episodes")); return; }
    S.fps = (S.eps[P.order[0]] || {}).fps || S.fps;
    P.dur = Math.max(0, ...P.order.map(eid=>epDur(S.eps[eid])));
    P.focus = P.order[0] || null;
    buildCharts(P, cbox);
    applyFocus(P);
    seekTo(P, keepT ? (prevT.get(P.gid) || 0) : 0);
  }));
}

/* "전체 시도 보기" 토글 — 상태는 전역이라 Try 를 바꿔도 유지된다 */
$("showAll").onchange = (e)=>{
  S.showAll = !!e.target.checked;
  e.target.blur();                              // 포커스를 놔줘야 Space 가 다시 재생 토글로 간다
  const r = curTry();
  if(r) renderTry(r, {keepT:true, sameTry:true});
};

/* ══════════════════════════════════════════════════════════════════
   그룹 블록 — 헤더 / 재생바 / 시도 카드 / 차트
   ══════════════════════════════════════════════════════════════════ */
function renderGroupBlock(P, r, atts, box){
  const g = P.group;
  const frag = $("tplGroup").content.cloneNode(true);
  const block = frag.querySelector(".gblock");
  block.dataset.gid = P.gid;
  const q = (sel)=>block.querySelector(sel);

  const total = (g.attempts||[]).length;
  const pick = atts[0];
  q(".gtitle").textContent = (r.multiCycle ? "c" + g.cycle + " · " : "") + "Task " + g.step;
  q(".ginstr").textContent = g.instruction || "—";
  q(".gcount").textContent = total
    ? (atts.length < total
        ? atts.length + " / " + total + " attempts · final " + (pick ? (pick.deleted ? "deleted" : (pick.outcome||"unlabeled")) : "—")
          + " (attempt " + (pick ? (pick.attempt||1) : "—") + ")"
        : total + (total===1 ? " attempt" : " attempts"))
    : "";
  const bd = q(".gbadges");
  for(const a of (g.attempts||[])){
    const oc = a.deleted ? "deleted" : (a.outcome||"unlabeled");
    bd.appendChild(el("span","bdg "+oc, OC_INIT[oc]||"?"));
  }
  block.title = g.run + " · c" + g.cycle + " · " + g.gid;

  // 재생 컨트롤 — 이 그룹만 움직인다
  P.ui = {
    block, bPlay:q(".bPlay"), icPlay:q(".icPlay"), icPause:q(".icPause"),
    seek:q(".seek"), tdisp:q(".tdisp"), attempts:q(".attempts"), charts:q(".charts"),
  };
  P.ui.bPlay.onclick  = ()=>{ setGroupFocus(P.gid); toggle(P); P.ui.bPlay.blur(); };
  q(".bBack").onclick = ()=>{ setGroupFocus(P.gid); pause(P); seekTo(P, P.T-5); };
  q(".bFwd").onclick  = ()=>{ setGroupFocus(P.gid); pause(P); seekTo(P, P.T+5); };
  q(".bReset").onclick= ()=>{ setGroupFocus(P.gid); pause(P); seekTo(P, 0); };
  P.ui.seek.oninput   = (e)=>{ setGroupFocus(P.gid); pause(P); seekTo(P, P.dur * (Number(e.target.value)/1000)); };
  block.addEventListener("mousedown", ()=>setGroupFocus(P.gid));

  P.ui.charts.appendChild(el("div","empty","loading charts…"));
  renderAttempts(P, atts, P.ui.attempts);
  box.appendChild(frag);
}

/* ══════════════════════════════════════════════════════════════════
   시도 카드
   ══════════════════════════════════════════════════════════════════ */
function renderAttempts(P, atts, box){
  box.textContent="";
  atts.forEach((a,i)=>{
    const eid = a.eid;
    const card = el("div","panel acard"); card.dataset.eid = eid;
    card.onmousedown = ()=>setFocus(P, eid);

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
      const v = el("video"); v.muted=true; v.playsInline=true; v.preload="metadata";
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
    observeCard(card);                            // 뷰포트 밖이면 영상은 멈춘 채(메타데이터만)
    initMedia(eid, card);
  });
}

/* 그룹 안 시도 포커스 (차트 강조·범례 수치 기준) */
function setFocus(P, eid){
  setGroupFocus(P.gid);
  if(!eid || P.focus===eid) return;
  P.focus = eid;
  applyFocus(P);
  updateCursor(P);
}
function applyFocus(P){
  if(!P.ui) return;
  P.ui.block.querySelectorAll(".acard").forEach(c=>c.classList.toggle("focus", c.dataset.eid===P.focus));
  // 차트 불투명도: 시도1 = 1.0, 포커스 = 1.0, 나머지 = 0.45
  for(const ch of P.charts){
    ch.svg.querySelectorAll("path[data-eid]").forEach(p=>{
      const eid = p.dataset.eid;
      p.setAttribute("opacity", (eid===P.order[0] || eid===P.focus) ? "1" : "0.45");
    });
    ch.svg.querySelectorAll("text[data-eid]").forEach(t=>{
      t.setAttribute("opacity", t.dataset.eid===P.focus ? "1" : "0.5");
    });
    drawTrim(ch);
  }
}
/* 키보드 대상 그룹 */
function setGroupFocus(gid){
  if(!gid || S.fgid===gid || !S.players.has(gid)) return;
  S.fgid = gid;
  applyGroupFocus();
}
function applyGroupFocus(){
  document.querySelectorAll(".gblock").forEach(b=>b.classList.toggle("focus", b.dataset.gid===S.fgid));
}

/* ══════════════════════════════════════════════════════════════════
   그룹별 독립 재생
   ══════════════════════════════════════════════════════════════════ */
function tick(P, ts){
  if(!P.playing){ P.raf=null; return; }
  const dt = P.lastTS ? (ts-P.lastTS)/1000 : 0; P.lastTS = ts;
  P.T += dt;
  if(P.T >= P.dur){ P.T = P.dur; updateCursor(P); syncPlayer(P); pause(P); return; }
  updateCursor(P); syncPlayer(P);
  P.raf = requestAnimationFrame((t)=>tick(P, t));
}
function paintPlayBtn(P){
  if(!P.ui) return;
  P.ui.bPlay.classList.toggle("active", P.playing);
  P.ui.icPlay.style.display  = P.playing ? "none" : "block";
  P.ui.icPause.style.display = P.playing ? "block" : "none";
  P.ui.block.classList.toggle("playing", P.playing);
}
function play(P){
  if(!P || P.playing || P.dur<=0) return;
  if(P.T >= P.dur - 1e-6) P.T = 0;
  P.playing = true; P.lastTS = 0;
  paintPlayBtn(P);
  syncPlayer(P);
  P.raf = requestAnimationFrame((t)=>tick(P, t));
}
function pause(P){
  if(!P) return;
  P.playing = false;
  if(P.raf){ cancelAnimationFrame(P.raf); P.raf=null; }
  paintPlayBtn(P);
  syncPlayer(P);
}
function toggle(P){ if(P) (P.playing ? pause(P) : play(P)); }
function seekTo(P, t){ if(!P) return; P.T = clamp(t, 0, P.dur); updateCursor(P); syncPlayer(P); }
function stepFrames(P, n){ if(!P) return; pause(P); seekTo(P, P.T + n/(S.fps||30)); }

/* ══════════════════════════════════════════════════════════════════
   키보드
   ══════════════════════════════════════════════════════════════════ */
/* ↑↓ / PageUp·PageDown : Try 이동 */
function moveTry(d){
  const x = curExp(); if(!x || !x.tries.length) return;
  const i = x.tries.findIndex(t=>t.no===S.tno);
  const n = clamp((i<0?0:i) + d, 0, x.tries.length-1);
  if(n===i || !x.tries[n]) return;
  selectTry(x.tries[n].no);
  const ch = $("rlist").querySelector('.row[data-tryno="'+CSS.escape(String(x.tries[n].no))+'"]');
  if(ch) ch.scrollIntoView({block:"nearest"});
}
/* ⌥↑↓ : Run(실험) 이동 (같은 Try 번호 유지) */
function moveExp(d){
  if(!S.exps.length) return;
  const i = S.exps.findIndex(x=>x.name===S.exp);
  const n = clamp((i<0?0:i) + d, 0, S.exps.length-1);
  if(n===i || !S.exps[n]) return;
  selectExp(S.exps[n].name);
  const ch = $("elist").querySelector('.row[data-exp="'+CSS.escape(S.exps[n].name)+'"]');
  if(ch) ch.scrollIntoView({block:"nearest"});
}
/* ⇧↑↓ : 포커스 그룹 이동 (화면 스크롤 따라감) */
function moveGroup(d){
  if(!S.porder.length) return;
  const i = S.porder.indexOf(S.fgid);
  const n = clamp((i<0?0:i) + d, 0, S.porder.length-1);
  if(n===i) return;
  setGroupFocus(S.porder[n]);
  const P = focusedPlayer();
  if(P && P.ui) P.ui.block.scrollIntoView({block:"start", behavior:"smooth"});
}

document.addEventListener("keydown", (e)=>{
  const tag = (e.target.tagName||"").toLowerCase();
  if(tag==="input" || tag==="textarea" || tag==="select") return;
  const k = e.key;
  const P = focusedPlayer();
  if(k===" "){ e.preventDefault(); toggle(P); return; }
  if(k==="ArrowLeft"){  e.preventDefault(); stepFrames(P, e.shiftKey?-10:-1); return; }
  if(k==="ArrowRight"){ e.preventDefault(); stepFrames(P, e.shiftKey? 10: 1); return; }
  if(k==="ArrowUp" || k==="ArrowDown"){
    e.preventDefault();
    const d = (k==="ArrowDown") ? 1 : -1;
    if(e.altKey) moveExp(d); else if(e.shiftKey) moveGroup(d); else moveTry(d);
    return;
  }
  if(k==="PageUp" || k==="PageDown"){          // ↑↓ 대신 써도 되는 Try 이동
    e.preventDefault();
    moveTry(k==="PageDown" ? 1 : -1);
    return;
  }
  if(k==="[" || k==="]"){
    e.preventDefault();
    if(!P) return;
    const i = P.order.indexOf(P.focus);
    const n = clamp((i<0?0:i) + (k==="]"?1:-1), 0, P.order.length-1);
    setFocus(P, P.order[n]);
    return;
  }
});

/* node 검증용 (브라우저에서는 module 이 없어 무시된다) */
if(typeof module !== "undefined" && module.exports){ module.exports = {buildExpTree, runExp, runTryNo, pickDefaultAttempt}; }
