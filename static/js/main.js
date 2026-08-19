"use strict";
/* ══════════════════════════════════════════════════════════════════
   부트스트랩
   ══════════════════════════════════════════════════════════════════ */
init();
async function init(){
  try{
    const [idx] = await Promise.all([getJSON("/api/index"), loadMontageManifest()]);
    S.idx = idx;
    S.fps = idx.fps || 30;
    buildTree();
    renderStats();
    renderSidebar();
    const h = parseHash();
    const x0 = (h && S.exps.find(x=>x.name===h.exp)) || S.exps[0];
    if(x0){
      if(h && h.task!=null) S.task = h.task;    // selectExp 가 같은 Task(step) 를 찾아 준다
      selectExp(x0.name);                      // 첫 실험 → 첫 Task 화면까지 열린다
    }else{ clearMain(); $("gSub").textContent = "no groups"; }
  }catch(e){
    $("gSub").textContent = "failed to load index: " + e.message;
  }
}

/* URL 해시 — #<run>/T<step>  (Run/Task 단위로만 저장).
   이전 형식(#<run>/<tryNo>, 숫자만)이 오면 Task 는 무시하고 그 Run 의 첫 Task 로 간다. */
function parseHash(){
  const h = (location.hash || "").replace(/^#/, "");
  if(!h) return null;
  const [e, t] = h.split("/");
  const m = /^T(\d+)$/i.exec(t || "");
  return {exp: decodeURIComponent(e||""), task: m ? Number(m[1]) : null};
}
function writeHash(){
  if(!S.exp) return;
  const h = "#" + encodeURIComponent(S.exp) + (S.task!=null ? "/T" + S.task : "");
  if(location.hash !== h) history.replaceState(null, "", h);
}
/* 주소창에서 해시만 바꾼 경우(같은 문서) — 그 Run/Task 로 이동 */
window.addEventListener("hashchange", ()=>{
  const h = parseHash(); if(!h || !S.exps.length) return;
  const x = S.exps.find(x=>x.name===h.exp); if(!x) return;
  if(x.name===S.exp && (h.task==null || h.task===S.task)) return;
  if(h.task!=null) S.task = h.task;
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
   사이드바 트리 — Run(실험) → Task 2단. (선택한 Task 를 모든 Try 에서 수행한 카드를 메인에 펼친다)
   그룹(g)에는 서버가 experiment / try_no 를 붙여 준다.
     · coffee_newNN  → experiment "NorRec_RW___Red", try_no = NN
     · 그 외 런      → experiment = 런 디렉토리명,  try_no = cycle
   없으면(구버전 인덱스) runExp()/runTryNo() 로 같은 규칙을 클라이언트에서 적용한다.
   Task 의 정체성은 step 번호(g.step)다 — 커스텀 스텝(103/104 …)도 그대로 한 Task 행이 된다.
   ══════════════════════════════════════════════════════════════════ */
function runExp(run){ return /^coffee_new\d+$/.test(String(run||"")) ? "NorRec_RW___Red" : String(run||""); }
function runTryNo(run, cycle){
  const m = /^coffee_new(\d+)$/.exec(String(run||""));
  return m ? Number(m[1]) : Number(cycle||0);
}
function gExp(g){ return g.experiment || runExp(g.run); }
function gTry(g){ return (g.try_no!=null) ? Number(g.try_no) : runTryNo(g.run, g.cycle); }

function curExp(){ return S.exps.find(x=>x.name===S.exp) || null; }
function curTask(){ const x=curExp(); return x ? (x.tasks.find(t=>t.step===S.task) || null) : null; }

/* 그룹 하나의 결과 요약 — 삭제분을 뺀 시도 중 실패가 있으면 "fail", 아니면 불안정이 있으면 "unstable" */
function groupFlag(g){
  const live = (g.attempts||[]).filter(a=>!a.deleted);
  if(live.some(a=>a.outcome==="fail")) return "fail";
  if(live.some(a=>a.outcome==="unstable")) return "unstable";
  return null;
}

/* 순수 함수: 인덱스 → 실험 트리. (node 로도 실행해 검증할 수 있게 전역 상태를 건드리지 않는다)
   exps[i] = {name, tries:[{no, runs, groups, fail, unstable, multiCycle, nEps}],
              tasks:[{step, instruction, tries:[{no, groups, multiCycle}], nTries, nEps, fail, unstable}], …}
   tries 는 통계·Try 라벨용, tasks 가 사이드바 2열 + 메인 화면의 단위다. */
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
  const byGroup = (a,b)=>(runOrder.indexOf(a.run)-runOrder.indexOf(b.run)) || (a.cycle-b.cycle) || (a.step-b.step);
  return order.filter(n=>emap.has(n)).map(name=>{
    const tm = emap.get(name);
    const tries = Array.from(tm.keys()).sort((a,b)=>a-b).map(no=>{
      const gs = tm.get(no).slice().sort(byGroup);
      let fail=0, unstable=0;                     // 실패/불안정이 섞인 "태스크" 수
      for(const g of gs){ const f=groupFlag(g); if(f==="fail") fail++; else if(f==="unstable") unstable++; }
      const runs = Array.from(new Set(gs.map(g=>g.run)));
      const cycles = new Set(gs.map(g=>g.cycle));
      const nEps = gs.reduce((n,g)=>n + (g.attempts||[]).length, 0);
      return {no, key:name+"|"+no, run:runs[0], runs, groups:gs, fail, unstable,
              multiCycle:cycles.size>1, nEps};
    });
    // Task 축: step 번호별로 모든 Try 의 그룹을 모은다 (Try 오름차순, 같은 Try 안은 cycle 순)
    const meta = metaByName.get(name) || {};
    const taskMeta = meta.tasks || {};
    const tmap = new Map();                       // step -> Map(tryNo -> groups)
    for(const t of tries) for(const g of t.groups){
      if(!tmap.has(g.step)) tmap.set(g.step, new Map());
      const m = tmap.get(g.step);
      if(!m.has(t.no)) m.set(t.no, []);
      m.get(t.no).push(g);
    }
    const tasks = Array.from(tmap.keys()).sort((a,b)=>a-b).map(step=>{
      const m = tmap.get(step);
      const ttries = Array.from(m.keys()).sort((a,b)=>a-b).map(no=>{
        const gs = m.get(no).slice().sort(byGroup);
        return {no, groups:gs, multiCycle:new Set(gs.map(g=>g.cycle)).size>1};
      });
      let fail=0, unstable=0;                     // 실패/불안정이 섞인 Try 수 (Try 안에 그룹이 여럿이면 하나라도)
      for(const t of ttries){
        const fs = t.groups.map(groupFlag);
        if(fs.indexOf("fail")>=0) fail++; else if(fs.indexOf("unstable")>=0) unstable++;
      }
      const g0 = ttries[0].groups[0];
      const instruction = taskMeta[String(step)] || g0.instruction || "—";
      const nEps = ttries.reduce((n,t)=>n + t.groups.reduce((k,g)=>k + (g.attempts||[]).length, 0), 0);
      return {step, instruction, tries:ttries, nTries:ttries.length, nEps, fail, unstable};
    });
    const nEps = tries.reduce((n,t)=>n + t.nEps, 0);
    const nGroups = tries.reduce((n,t)=>n + t.groups.length, 0);
    const fail = tries.reduce((n,t)=>n + t.fail, 0);
    const unstable = tries.reduce((n,t)=>n + t.unstable, 0);
    return {name, tries, tasks, nEps, nGroups, fail, unstable, model:meta.model||null,
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
            + " · " + x.tasks.length + (x.tasks.length===1 ? " task" : " tasks")
            + " · " + x.nEps + " eps"
            + (x.model ? " · " + x.model : "")
            + (x.fail ? " · fail " + x.fail : "") + (x.unstable ? " · unstable " + x.unstable : "");
    b.dataset.exp = x.name;
    b.onclick = ()=>selectExp(x.name);
    box.appendChild(b);
  }
}

/* Finder 2열: 선택된 실험의 Task 세로 목록 — "T3 · press the blue …" + 오른쪽 Try 수 · 실패 수 배지 */
function renderTasks(){
  const box = $("tlist"); box.textContent="";
  const x = curExp(), tasks = x ? x.tasks : [];
  $("tcount").textContent = "(" + tasks.length + ")";
  if(!tasks.length){ box.appendChild(el("div","empty","Select a run")); return; }
  for(const t of tasks){
    const on = t.step===S.task;
    const b = el("button","row trow" + (on ? " on" : ""));
    b.setAttribute("role","option");
    b.setAttribute("aria-selected", on ? "true" : "false");
    const nm = el("span","nm");
    nm.appendChild(el("span","tstep","T" + t.step));
    nm.appendChild(document.createTextNode(" " + shortInstr(t.instruction, 40)));
    b.appendChild(nm);
    // 오른쪽 끝: Try 수 + 실패/불안정 Try 수 (결과색 체계는 카드와 동일)
    const bd = el("span","badges");
    bd.appendChild(el("span","bdg", t.nTries + "×"));
    if(t.fail) bd.appendChild(el("span","bdg fail", "F" + t.fail));
    if(t.unstable) bd.appendChild(el("span","bdg unstable", "U" + t.unstable));
    b.appendChild(bd);
    if(t.fail || t.unstable){
      const m = el("span","meter" + (t.fail ? "" : " warn"));
      m.style.width = (100 * (t.fail || t.unstable) / (t.nTries || 1)).toFixed(1) + "%";
      b.appendChild(m);
    }
    b.title = "Task " + t.step + " · " + t.instruction
            + "\n" + t.nTries + (t.nTries===1 ? " try" : " tries") + " · " + t.nEps + " eps"
            + (t.fail ? " · fail " + t.fail : "") + (t.unstable ? " · unstable " + t.unstable : "");
    b.dataset.step = String(t.step);
    b.onclick = ()=>selectTask(t.step);
    box.appendChild(b);
  }
}
function renderSidebar(){ renderExps(); renderTasks(); }

/* Run(실험) 선택 → 같은 Task(step) 가 있으면 유지, 없으면 첫 Task. */
function selectExp(name){
  const x = S.exps.find(e=>e.name===name); if(!x) return;
  S.exp = name;
  const t = x.tasks.find(t=>t.step===S.task) || x.tasks[0];
  if(t) selectTask(t.step);
  else { S.task=null; renderSidebar(); clearMain(); }
}

/* Task 선택 → 메인에 그 Task 를 수행한 모든 Try 의 카드를 펼친다 */
function selectTask(step){
  const x = curExp(); if(!x) return;
  const t = x.tasks.find(t=>t.step===step); if(!t) return;
  S.task = step;
  renderSidebar();
  writeHash();
  renderTask(t);
}

/* 화면을 비운다 — 전역 플레이어 정지, 관찰자 해제, 카드 영상의 src 를 놓는다 */
function teardownPlayers(){
  const G = S.G;
  if(G) pause(G);
  if(S.io) S.io.disconnect();
  // 떼어낼 카드의 영상은 src 를 비워 네트워크/디코더를 즉시 놓게 한다 (GC 전까지 로딩을 붙들지 않게)
  document.querySelectorAll("#grid video").forEach(v=>{
    clearTimeout(v._wd); v.onerror = null; v.onloadeddata = null;
    try{ v.pause(); v.removeAttribute("src"); v.load(); }catch(e){}
  });
  S.order = []; S.eidGid = new Map(); S.feid = null; S.CP = null; S.epsReq++;
  if(G){ G.order = []; G.T = 0; G.dur = 0; G.charts = []; updateCursor(G); }
  // 몽타주 모드 해제 — 몽타주 영상·Focus 패널 정리, 차트 박스는 그리드 아래 원위치
  teardownMontage(); renderFocusPanel(); placeChartsBox(false); srcablTeardown();
  $("grid").textContent = ""; $("grid").hidden = false;
  $("charts").textContent = "";
  $("chartsSum").textContent = "Charts";
  $("gFocus").textContent = "";
}
function clearMain(){
  if(S.ac) S.ac.abort();
  teardownPlayers();
  S.eps = {};
  $("gTitle").textContent = "rollout viewer";
  $("gSub").textContent   = "no task to show";
}

/* ══════════════════════════════════════════════════════════════════
   Task 렌더 — 카드 그리드 하나. 선택한 Task 를 Try 순으로 가로 타일링하고
   같은 Try 의 시도들은 .tgroup 으로 묶어 인접 배치한다 (Show all 때 실패 → 성공 순).
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

/* 라벨 — 카드 윗줄 "Try 6 · A2/2" (Task 는 헤더에 한 번), 차트/툴팁 "Try 6 · Task 3 · press …" */
function shortInstr(s, n){
  s = String(s || "—").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n-1).trimEnd() + "…" : s;
}
function tryLabel(g, multiCycle){
  return "Try " + gTry(g) + (multiCycle ? " · c" + g.cycle : "");
}
function cardLabel(g, a, total, multiCycle){
  return tryLabel(g, multiCycle) + " · A" + (a.attempt||1) + "/" + (total||1);
}
function taskLabel(g, multiCycle){
  return tryLabel(g, multiCycle) + " · Task " + g.step + " · " + (g.instruction || "—");
}
/* 그룹이 속한 Try 묶음이 다중 cycle 인지 (현재 Task 기준) — 라벨에 cN 을 덧붙일지 결정 */
function gMultiCycle(g){
  const t = curTask(); if(!t || !g) return false;
  const tr = t.tries.find(x=>x.no===gTry(g));
  return !!(tr && tr.multiCycle);
}

async function renderTask(t, opts){
  const keepT = !!(opts && opts.keepT);
  const sameTask = !!(opts && opts.sameTask);
  const G = S.G;
  const prevT = G ? G.T : 0;
  const prevFocus = S.feid;

  if(S.ac) S.ac.abort();                         // 진행 중 fetch 취소
  const ac = new AbortController(); S.ac = ac;
  teardownPlayers();
  if(!sameTask){                                 // Task 가 바뀌면 에피소드 캐시·카드별 모드 오버라이드를 버리고 맨 위로
    S.eps = {}; S.vmode = {}; $("main").scrollTop = 0;
  }

  const tries = t.tries || [];
  const x = curExp();
  $("gTitle").textContent = (x ? x.name : "") + " · Task " + t.step;
  $("gSub").textContent = t.instruction
                        + " · " + tries.length + (tries.length===1 ? " try" : " tries")
                        + " · " + t.nEps + (t.nEps===1 ? " episode" : " episodes")
                        + (S.showAll ? " · showing all attempts" : " · showing final attempt of each try");
  const grid = $("grid");
  if(!tries.length){ grid.appendChild(el("div","empty","no tries for this task")); return; }

  // 0) 화면에 올릴 시도 목록 — Try 순 × (cycle 순 ×) 시도 순 (몽타주 타일 순서와 동일해야 한다)
  const items = [];
  tries.forEach((tr, ti)=>{
    for(const g of tr.groups){
      const atts = visibleAttempts(g);
      const total = (g.attempts||[]).length;
      for(const a of atts){
        S.eidGid.set(a.eid, g.gid); S.order.push(a.eid);
        items.push({g, a, total, multiCycle:tr.multiCycle, ti, tryNo:tr.no});
      }
    }
  });
  G.order = S.order.slice();

  // 1) 몽타주(미리 구운 Task 영상 1편)가 있으면 그걸로 — 없거나 타일 순서가 안 맞으면 카드 그리드 폴백
  let meta = null;
  try{ meta = await fetchMontageMeta(montageName(x ? x.name : "", t.step, S.gmode, S.showAll), ac.signal); }
  catch(e){ if(e.name==="AbortError") return; }
  if(ac.signal.aborted) return;
  if(meta && montageMatches(meta, S.order)){
    renderTaskMontage(t, items, meta, {keepT, prevT, prevFocus});
    return;
  }

  // 1') 폴백: 카드 DOM — 같은 Try 의 시도들은 .tgroup 으로 묶어 인접 배치. 차트는 에피소드가 오면 (포커스 그룹만) 그린다
  let k = 0;
  tries.forEach((tr, ti)=>{
    const cards = [];
    while(k < items.length && items[k].tryNo === tr.no){
      const it = items[k++];
      cards.push(renderCard(it.g, it.a, it.total, it.multiCycle));
    }
    const tg = el("div", "tgroup" + (cards.length>1 ? " multi" : ""));
    tg.dataset.tryno = String(tr.no);
    tg.style.setProperty("--gc", GROUP_COLORS[ti % GROUP_COLORS.length]);
    const g0 = tr.groups[0];
    tg.title = "Try " + tr.no + " · Task " + t.step + " · " + t.instruction
             + " · " + Array.from(new Set(tr.groups.map(g=>g.run))).join(", ")
             + (tr.multiCycle ? "" : " · c" + (g0 ? g0.cycle : "?"));
    for(const c of cards) tg.appendChild(c);
    grid.appendChild(tg);
  });
  setFocus((prevFocus && S.eidGid.has(prevFocus)) ? prevFocus : S.order[0], {quiet:true});

  // 2) 에피소드 로드 — 병렬, 도착하는 대로 타임라인 길이를 늘린다 (캐시 재사용)
  let loaded = 0;
  await Promise.all(S.order.map(async eid=>{
    if(!S.eps[eid]){
      try{
        const ep = await getJSON("/api/episode?eid="+encodeURIComponent(eid), ac.signal);
        if(ac.signal.aborted) return;
        S.eps[eid] = ep;
      }catch(e){ if(e.name!=="AbortError") console.warn("episode load failed", eid, e); return; }
    }
    loaded++;
    S.fps = S.eps[eid].fps || S.fps;
    G.dur = Math.max(G.dur, epDur(S.eps[eid]));
    paintT();
  }));
  if(ac.signal.aborted) return;
  if(!loaded){ $("charts").appendChild(el("div","empty","failed to load episodes")); return; }
  seekTo(G, keepT ? prevT : 0);
  refreshCharts();
  srcablSync();                                  // 소스 기여도 배지·미니차트 (srcabl.js)
}

/* 몽타주 모드 렌더 — 그리드 자리에 몽타주 영상 + 오버레이, 아래 Focus 패널(원본 카드 + 차트).
   타임라인 길이는 몽타주 길이(= 가장 긴 클립). 에피소드 JSON 은 포커스 그룹 것만 (차트용) 필요할 때 받는다. */
function renderTaskMontage(t, items, meta, o){
  const G = S.G;
  $("grid").hidden = true;
  S.M = buildMontage(meta, items);
  S.fps = meta.fps || S.fps;
  G.dur = meta.dur || 0;
  setFocus((o.prevFocus && S.eidGid.has(o.prevFocus)) ? o.prevFocus : S.order[0], {quiet:true});
  renderFocusPanel();
  placeChartsBox(true);
  seekTo(G, o.keepT ? o.prevT : 0);
  refreshCharts();
  srcablSync();                                  // 소스 기여도 배지·미니차트 (srcabl.js)
}

/* 에피소드 JSON 을 (없는 것만) 받아 S.eps 에 넣는다 — 몽타주 모드에서 차트·Focus 카드용 */
async function loadEps(eids, signal){
  await Promise.all(eids.map(async eid=>{
    if(S.eps[eid] || S.epsFail.has(eid)) return;
    try{
      const ep = await getJSON("/api/episode?eid="+encodeURIComponent(eid), signal);
      if(signal && signal.aborted) return;
      S.eps[eid] = ep;
    }catch(e){ if(e.name!=="AbortError"){ S.epsFail.add(eid); console.warn("episode load failed", eid, e); } }
  }));
}

/* "전체 시도 보기" 토글 — 상태는 전역이라 Task 를 바꿔도 유지된다 */
$("showAll").onchange = (e)=>{
  S.showAll = !!e.target.checked;
  e.target.blur();                              // 포커스를 놔줘야 Space 가 다시 재생 토글로 간다
  const t = curTask();
  if(t) renderTask(t, {keepT:true, sameTask:true});
};

/* 전역 오버레이 모드 세그먼트 — Attention / Causal / Original (화면 전체 카드를 한 번에) */
function paintModeSeg(){
  $("vmodeSeg").querySelectorAll("button[data-mode]").forEach(b=>{
    b.classList.toggle("on", b.dataset.mode === S.gmode);
    b.setAttribute("aria-pressed", b.dataset.mode === S.gmode ? "true" : "false");
  });
}
$("vmodeSeg").querySelectorAll("button[data-mode]").forEach(b=>{
  b.onclick = ()=>{ setModeAll(b.dataset.mode); paintModeSeg(); b.blur(); };
});
paintModeSeg();

/* ══════════════════════════════════════════════════════════════════
   시도 카드 — 윗줄 라벨("Try n · Ak/m") + 결과 배지 / front·wrist 영상 (front 위에 ◉ 모드 토글)
   ══════════════════════════════════════════════════════════════════ */
function renderCard(g, a, total, multiCycle){
  const eid = a.eid;
  const card = el("div","panel acard"); card.dataset.eid = eid; card.dataset.gid = g.gid;
  card.onmousedown = ()=>setFocus(eid);
  const oc = a.deleted ? "deleted" : (a.outcome||"unlabeled");
  card.classList.add("oc-" + oc);
  const m = a.metrics || {};
  card.title = taskLabel(g, multiCycle) + "\nAttempt " + (a.attempt||1) + "/" + (total||1) + " · ep" + a.ep + " · " + oc
             + (a.memo ? "\n" + a.memo : "")
             + "\njerk p95 " + fx(m.jerk_p95,2) + " · track " + fx(m.track_err,2)
             + " · rev " + fx(m.reversals,1) + " · frames " + (a.n_frames ?? "—");

  // 윗줄: "Try 6 · A2/2" + 결과 배지 (Task 는 헤더에 한 번만). 띠 전체가 결과색 틴트를 받는다
  const h = el("div","arow");
  const nm = el("div","aname");
  nm.appendChild(el("span","tno", tryLabel(g, multiCycle)));
  nm.appendChild(document.createTextNode(" · A" + (a.attempt||1) + "/" + (total||1)));
  h.appendChild(nm);
  h.appendChild(el("span","bdg "+oc, oc));
  card.appendChild(h);

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
    if(cam === "front"){
      // ◉ 오버레이 토글 — 클립 존재 확인 후에만 노출. 전역 세그먼트를 개별로 덮어쓴다
      const ab = el("button","chip attn-chip","Attention");
      ab.style.display = "none";
      ab.title = "Cycle overlay: attention / causal / original (this card only)";
      ab.onmousedown = (ev)=>ev.stopPropagation();
      ab.onclick = (ev)=>{ ev.stopPropagation(); cycleMode(eid); ab.blur(); };
      pane.appendChild(ab);
      // 클립 존재 확인(HEAD, 세션 캐시) 뒤에 src 를 물린다 — 원본을 먼저 걸었다가 갈아끼우는 이중 로드를 피한다
      Promise.all([probeAttn(eid), probeCausal(eid)]).then(([hasA, hasC])=>{
        if(!ab.isConnected) return;                      // 폐기된 카드는 무시
        ab.style.display = (hasA || hasC) ? "" : "none";
        // 기본값: 전역 모드(그 클립이 없으면 원본). 사용자가 이 카드를 직접 바꾼 적 있으면(같은 Try 재렌더) 그대로
        if(S.vmode[eid] === undefined){
          const m = S.gmode, ok = (m === "orig") || (m === "attn" ? hasA : hasC);
          S.vmode[eid] = ok ? m : "orig";
        }
        paintModeBtn(eid);
        initMedia(eid, card);
      });
    }
    media.appendChild(pane);
  }
  card.appendChild(media);

  // 메모(채점 시 남긴 한 줄) — 카드 아래 항상 표시. 없으면 "—"
  card.appendChild(el("div","memo", a.memo || ""));

  observeCard(card);                            // 뷰포트 밖이면 영상은 멈춘 채(메타데이터만)
  return card;
}

/* ══════════════════════════════════════════════════════════════════
   포커스 — 카드 하나. 차트는 그 카드의 (Run, Try, Task) 그룹(시도 겹침)을 그린다
   ══════════════════════════════════════════════════════════════════ */
function setFocus(eid, opts){
  if(!eid || !S.eidGid.has(eid)) return;
  const gidPrev = S.CP ? S.CP.gid : null;
  const changed = (S.feid !== eid);
  S.feid = eid;
  applyCardFocus();
  const g = S.byGid.get(S.eidGid.get(eid));
  const a = g ? (g.attempts||[]).find(x=>x.eid===eid) : null;
  $("gFocus").textContent = (g && a) ? ("Try " + gTry(g) + " · A" + (a.attempt||1)) : "";
  srcablRefresh();                               // 소스 기여도 미니차트 (srcabl.js)
  if(opts && opts.quiet) return;                // renderTask 안에서는 에피소드가 온 뒤 refreshCharts() 가 그린다
  if(!changed) return;
  if(S.M) renderFocusPanel();                   // 몽타주 모드: Focus 패널의 원본 카드를 새 포커스로 교체
  if(S.CP && S.CP.gid === gidPrev && S.CP.gid === S.eidGid.get(eid)){
    S.CP.focus = eid; applyFocus(S.CP); paintT();   // 같은 그룹 안에서 시도만 바뀜 → 강조만 갱신
  }else refreshCharts();
}
function applyCardFocus(){
  document.querySelectorAll("#grid .acard").forEach(c=>c.classList.toggle("focus", c.dataset.eid===S.feid));
  document.querySelectorAll("#montage .mtile").forEach(c=>c.classList.toggle("focus", c.dataset.eid===S.feid));
}
/* charts.js 의 buildCharts() 가 끝에 호출한다 — 카드 강조 + 차트 선 불투명도 */
function applyFocus(P){
  applyCardFocus();
  if(!P) return;
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

/* 접이식 차트 — 펼쳐져 있을 때만 포커스 그룹 차트를 그린다 (접히면 비우고 S.CP=null) */
function refreshCharts(){
  const box = $("charts"), det = $("chartsBox");
  const gid = S.feid ? S.eidGid.get(S.feid) : null;
  const g = gid ? S.byGid.get(gid) : null;
  $("chartsSum").textContent = g ? "Charts · " + taskLabel(g, gMultiCycle(g)) : "Charts";
  box.textContent = ""; S.CP = null;
  if(!g || !det.open) return;
  // 몽타주 모드에서는 에피소드를 미리 받지 않는다 — 이 그룹 것이 없으면 받아 온 뒤 다시 그린다
  const need = S.order.filter(e=>S.eidGid.get(e)===gid && !S.eps[e] && !S.epsFail.has(e));
  if(need.length){
    const req = ++S.epsReq;
    box.appendChild(el("div","empty","loading episodes…"));
    loadEps(need, S.ac ? S.ac.signal : undefined).then(()=>{ if(req===S.epsReq && det.open) refreshCharts(); });
    return;
  }
  const C = newPlayer(gid, g);
  C.order = S.order.filter(e=>S.eidGid.get(e)===gid && S.eps[e]);
  if(!C.order.length){ box.appendChild(el("div","empty","episodes not loaded")); return; }
  C.focus = C.order.indexOf(S.feid)>=0 ? S.feid : C.order[0];
  C.dur = Math.max(0, ...C.order.map(eid=>epDur(S.eps[eid])));
  S.CP = C;
  buildCharts(C, box);
  paintT();
}
$("chartsBox").addEventListener("toggle", ()=>refreshCharts());

/* ══════════════════════════════════════════════════════════════════
   전역 동기 재생 — 타임라인은 화면 전체 최장 길이. 짧은 카드는 끝 프레임 정지,
   전체가 끝나면 멈추고 다음 재생에서 0 으로 같이 리셋
   ══════════════════════════════════════════════════════════════════ */
/* 재생바·차트 커서를 현재 T 로 — 차트 플레이어는 전역 T 를 따라간다 */
function paintT(){
  const G = S.G; if(!G) return;
  updateCursor(G);
  if(S.CP){ S.CP.T = G.T; updateCursor(S.CP); }
  srcablTick();                                  // 소스 기여도 배지·미니차트 커서 (srcabl.js)
}
function tick(P, ts){
  if(!P.playing){ P.raf=null; return; }
  const dt = P.lastTS ? (ts-P.lastTS)/1000 : 0; P.lastTS = ts;
  const mv = (S.M && P===S.G) ? S.M.video : null;
  // 몽타주 모드: 재생 중인 몽타주 영상이 시계다 (드리프트 보정 점프 없이 매끈하게). 아니면 벽시계
  if(mv && !mv.paused && mv.readyState >= 2) P.T = mv.currentTime;
  else P.T += dt;
  if(P.T >= P.dur){ P.T = P.dur; paintT(); syncPlayer(P); pause(P); return; }
  paintT(); syncPlayer(P);
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
function seekTo(P, t){ if(!P) return; P.T = clamp(t, 0, P.dur); paintT(); syncPlayer(P); }
function stepFrames(P, n){ if(!P) return; pause(P); seekTo(P, P.T + n/(S.fps||30)); }

/* 하단 전역 재생바 — 한 번만 묶는다 */
function bindGlobalPlayer(){
  const G = newPlayer("__all__", null);
  const bar = $("gbar");
  G.ui = {
    block:bar, bPlay:$("gPlay"), icPlay:bar.querySelector(".icPlay"), icPause:bar.querySelector(".icPause"),
    seek:$("gSeek"), tdisp:$("gTdisp"),
  };
  G.ui.bPlay.onclick = ()=>{ toggle(G); G.ui.bPlay.blur(); };
  $("gBack").onclick  = ()=>{ pause(G); seekTo(G, G.T-5); };
  $("gFwd").onclick   = ()=>{ pause(G); seekTo(G, G.T+5); };
  $("gReset").onclick = ()=>{ pause(G); seekTo(G, 0); };
  G.ui.seek.oninput   = (e)=>{ pause(G); seekTo(G, G.dur * (Number(e.target.value)/1000)); };
  G.ui.seek.onchange  = (e)=>e.target.blur();   // 슬라이더에 포커스가 남으면 ←→ 가 프레임 이동 대신 슬라이더를 움직인다
  S.G = G;
  updateCursor(G);
}
bindGlobalPlayer();

/* ══════════════════════════════════════════════════════════════════
   키보드
   ══════════════════════════════════════════════════════════════════ */
/* ↑↓ / PageUp·PageDown : Task 이동 */
function moveTask(d){
  const x = curExp(); if(!x || !x.tasks.length) return;
  const i = x.tasks.findIndex(t=>t.step===S.task);
  const n = clamp((i<0?0:i) + d, 0, x.tasks.length-1);
  if(n===i || !x.tasks[n]) return;
  selectTask(x.tasks[n].step);
  const ch = $("tlist").querySelector('.row[data-step="'+CSS.escape(String(x.tasks[n].step))+'"]');
  if(ch) ch.scrollIntoView({block:"nearest"});
}
/* ⌥↑↓ : Run(실험) 이동 (같은 Task(step) 유지) */
function moveExp(d){
  if(!S.exps.length) return;
  const i = S.exps.findIndex(x=>x.name===S.exp);
  const n = clamp((i<0?0:i) + d, 0, S.exps.length-1);
  if(n===i || !S.exps[n]) return;
  selectExp(S.exps[n].name);
  const ch = $("elist").querySelector('.row[data-exp="'+CSS.escape(S.exps[n].name)+'"]');
  if(ch) ch.scrollIntoView({block:"nearest"});
}
/* [ ] : 포커스 카드 이동 (그리드 순서 = Try 순 × 시도 순, 화면 스크롤 따라감) */
function moveCard(d){
  if(!S.order.length) return;
  const i = S.order.indexOf(S.feid);
  const n = clamp((i<0?0:i) + d, 0, S.order.length-1);
  if(n===i) return;
  setFocus(S.order[n]);
  const c = S.M ? tileOf(S.order[n]) : cardOf(S.order[n]);
  if(c) c.scrollIntoView({block:"nearest", behavior:"smooth"});
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
    if(e.altKey) moveExp(d); else moveTask(d);
    return;
  }
  if(k==="PageUp" || k==="PageDown"){          // ↑↓ 대신 써도 되는 Task 이동
    e.preventDefault();
    moveTask(k==="PageDown" ? 1 : -1);
    return;
  }
  if(k==="[" || k==="]"){
    e.preventDefault();
    moveCard(k==="]" ? 1 : -1);
    return;
  }
});

/* node 검증용 (브라우저에서는 module 이 없어 무시된다) */
if(typeof module !== "undefined" && module.exports){ module.exports = {buildExpTree, runExp, runTryNo, pickDefaultAttempt}; }
