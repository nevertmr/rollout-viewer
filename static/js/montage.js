"use strict";
/* ══════════════════════════════════════════════════════════════════
   Task 몽타주 — build_montage.py 가 미리 구운 "Task × 모드 × (all|final)" mp4 한 편을
   그리드 자리에 띄우고, 그 위에 타일 좌표대로 HTML 오버레이(결과색 테두리·라벨·배지·메모)를 얹는다.
   카드마다 <video> 를 수십 개 띄우던 방식(디코더 한계 → 렉)을 대체한다. 동기 재생은 영상 자체가 보장.
   몽타주가 없는 Task(베이크 누락·인덱스 불일치)는 main.js 의 기존 카드 그리드로 폴백한다.
   ══════════════════════════════════════════════════════════════════ */
const MONT = {
  manifest: null,        // Set<name> — dist/montage/manifest.json (없으면 빈 Set → 전부 폴백)
  metas: new Map(),      // name -> layout meta (세션 캐시)
  MAX_SCALE: 1.2,        // 몽타주를 컨테이너 폭에 맞춰 키울 때 상한 (200px 타일을 너무 뻥튀기하면 흐려진다)
};

function montageName(exp, step, mode, showAll){
  return exp + "__t" + step + "__" + mode + "__" + (showAll ? "all" : "final");
}
/* 존재하는 몽타주 목록 — 404 프로브 없이 폴백을 판단하려고 한 번만 받는다 (서버는 없으면 {names:[]}) */
async function loadMontageManifest(){
  try{
    const j = await getJSON("/montage/manifest.json");
    MONT.manifest = new Set((j && j.names) || []);
  }catch(e){ MONT.manifest = new Set(); }
  return MONT.manifest;
}
async function fetchMontageMeta(name, signal){
  if(!MONT.manifest) await loadMontageManifest();
  if(!MONT.manifest.has(name)) return null;
  if(MONT.metas.has(name)) return MONT.metas.get(name);
  try{
    const m = await getJSON("/montage/" + encodeURIComponent(name) + ".json", signal);
    if(m && Array.isArray(m.tiles)) MONT.metas.set(name, m);
    return m;
  }catch(e){
    if(e.name === "AbortError") throw e;
    return null;                               // 깨진 메타 → 폴백
  }
}
/* 몽타주 타일 순서가 지금 화면에 올릴 eid 순서와 정확히 같아야 오버레이가 맞는다 (인덱스가 바뀌었으면 폴백) */
function montageMatches(meta, order){
  if(!meta || !meta.tiles || meta.tiles.length !== order.length) return false;
  for(let i=0;i<order.length;i++) if(meta.tiles[i].eid !== order[i]) return false;
  return true;
}

/* ── DOM ───────────────────────────────────────────────────────────── */
function tileOf(eid){ return document.querySelector('.mtile[data-eid="'+CSS.escape(eid)+'"]'); }

/* 몽타주 화면을 만든다: #montage > .mstage(원본 px 좌표계, CSS scale) > video + .mtile* */
function buildMontage(meta, items){
  const box = $("montage"); box.textContent = "";
  const stage = el("div","mstage");
  stage.style.width = meta.width + "px"; stage.style.height = meta.height + "px";
  stage.style.background = meta.bg || "#0b0f17";
  const v = el("video");
  v.muted = true; v.playsInline = true; v.preload = "auto"; v.setAttribute("playsinline","");
  v.setAttribute("muted","");
  v.src = "/montage/" + encodeURIComponent(meta.name) + ".mp4";
  v.onloadeddata = ()=>{ if(v.isConnected && S.G){ syncMontage(S.G); } };
  v.onerror = ()=>{ if(v.isConnected) toast("montage failed to load: " + meta.name); };
  stage.appendChild(v);

  const T = meta.tile || {};
  const bandTop = T.band_top || 22, bandBot = T.band_bottom || 28;
  meta.tiles.forEach((tl, i)=>{
    const it = items[i];                       // renderTask 가 만든 {g, a, total, multiCycle, ti}
    const g = it.g, a = it.a;
    const oc = a.deleted ? "deleted" : (a.outcome || "unlabeled");
    const d = el("div", "mtile oc-" + oc);
    d.dataset.eid = tl.eid; d.dataset.gid = g.gid;
    d.style.left = tl.x + "px"; d.style.top = tl.y + "px";
    d.style.width = tl.w + "px"; d.style.height = tl.h + "px";
    d.style.setProperty("--gc", GROUP_COLORS[(it.ti||0) % GROUP_COLORS.length]);
    const m = a.metrics || {};
    d.title = taskLabel(g, it.multiCycle) + "\nAttempt " + (a.attempt||1) + "/" + (it.total||1) + " · ep" + a.ep + " · " + oc
            + (a.memo ? "\n" + a.memo : "")
            + "\njerk p95 " + fx(m.jerk_p95,2) + " · track " + fx(m.track_err,2)
            + " · rev " + fx(m.reversals,1) + " · frames " + (a.n_frames ?? "—")
            + (tl.mode_used && tl.mode_used !== meta.mode ? "\n(" + meta.mode + " clip missing → original)" : "");
    // 윗줄 띠: "Try n · Ak/m" + 결과 배지 (베이크 시 비워 둔 22px 띠 자리)
    const row = el("div","mrow"); row.style.height = (bandTop - 2) + "px";
    const nm = el("div","aname");
    nm.appendChild(el("span","tno", tryLabel(g, it.multiCycle)));
    nm.appendChild(document.createTextNode(" · A" + (a.attempt||1) + "/" + (it.total||1)));
    row.appendChild(nm);
    const right = el("span","mright");
    if(tl.mode_used && tl.mode_used !== meta.mode) right.appendChild(el("span","bdg dimbdg","orig"));
    right.appendChild(el("span","bdg "+oc, oc));
    row.appendChild(right);
    d.appendChild(row);
    // 아래 메모 띠 (28px)
    const memo = el("div","mmemo", a.memo || ""); memo.style.height = (bandBot - 2) + "px";
    d.appendChild(memo);
    d.onmousedown = (ev)=>{ if(ev.button===0) setFocus(tl.eid); };
    stage.appendChild(d);
  });
  box.appendChild(stage);
  box.hidden = false;
  const M = {meta, video:v, stage, box, ro:null, scale:1};
  fitMontage(M);
  if(typeof ResizeObserver !== "undefined"){
    M.ro = new ResizeObserver(()=>fitMontage(M));
    M.ro.observe(box);
  }
  return M;
}
/* 몽타주를 컨테이너 폭에 맞춰 비율 유지 스케일 — 오버레이도 같은 .mstage 안이라 좌표가 그대로 맞는다 */
function fitMontage(M){
  if(!M || !M.box || !M.stage) return;
  const W = M.box.clientWidth || M.meta.width;
  const s = Math.min(MONT.MAX_SCALE, W / M.meta.width);
  M.scale = s;
  M.stage.style.transform = "scale(" + s + ")";
  M.box.style.height = Math.round(M.meta.height * s) + "px";
}
function teardownMontage(){
  const M = S.M; if(!M) return;
  if(M.ro){ try{ M.ro.disconnect(); }catch(e){} }
  const v = M.video;
  if(v){ v.onloadeddata = null; v.onerror = null; try{ v.pause(); v.removeAttribute("src"); v.load(); }catch(e){} }
  const box = $("montage"); box.textContent = ""; box.hidden = true; box.style.height = "";
  S.M = null;
}

/* ── 동기 재생 — 몽타주 video 하나가 전역 타임라인을 따른다(재생 중엔 영상이 시계, tick() 참조) ── */
function syncMontage(P){
  const M = S.M; if(!M || !M.video || !P) return;
  const v = M.video;
  const dur = P.dur || M.meta.dur || 0;
  const over = dur>0 && P.T >= dur;
  const t = over ? Math.max(0, dur - 0.04) : P.T;
  if(P.playing && !over){
    if(v.paused) v.play().catch(()=>{});
    if(v.readyState >= 1 && Math.abs(v.currentTime - t) > 0.25){ try{ v.currentTime = t; }catch(e){} }
  }else{
    if(!v.paused) v.pause();
    if(Math.abs(v.currentTime - t) > 0.03){ try{ v.currentTime = t; }catch(e){} }
  }
}

/* ── Focus 패널 — 포커스 카드의 원본 해상도 카드(기존 renderCard 재사용) + 차트 ── */
function renderFocusPanel(){
  const box = $("focusBox"), slot = $("focusCard");
  // 이전 포커스 카드 영상 해제
  slot.querySelectorAll("video").forEach(v=>{
    clearTimeout(v._wd); v.onerror = null; v.onloadeddata = null;
    try{ v.pause(); v.removeAttribute("src"); v.load(); }catch(e){}
  });
  slot.textContent = "";
  if(!S.M || !S.feid || !S.eidGid.has(S.feid)){ box.hidden = true; return; }
  const g = S.byGid.get(S.eidGid.get(S.feid));
  const a = g ? (g.attempts||[]).find(x=>x.eid===S.feid) : null;
  if(!g || !a){ box.hidden = true; return; }
  const card = renderCard(g, a, (g.attempts||[]).length, gMultiCycle(g));
  card.classList.add("focus","fcard");
  if(S.io){ try{ S.io.unobserve(card); }catch(e){} }
  card.dataset.vis = "1";                      // 뷰포트 정책 제외 — Focus 카드는 화면 밖이어도 몽타주 T 에 항상 동기 재생
  slot.appendChild(card);
  $("focusSum").textContent = "Focus · " + cardLabel(g, a, (g.attempts||[]).length, gMultiCycle(g)) + " · " + S.feid;
  box.hidden = false;
}
/* 차트 박스를 Focus 패널 안(몽타주 모드)/그리드 아래(폴백) 로 옮긴다 */
function placeChartsBox(inFocus){
  const det = $("chartsBox");
  if(inFocus){
    const box = $("focusBox");
    if(det.parentNode !== box) box.appendChild(det);
    if(!det.open){ det.dataset.autoOpen = "1"; det.open = true; }   // Focus 패널 안에서는 펼쳐 둔다
  }else{
    const wrap = document.querySelector(".wrap"), credit = wrap.querySelector(".credit");
    if(det.parentNode !== wrap){
      wrap.insertBefore(det, credit);
      if(det.dataset.autoOpen === "1"){ det.open = false; delete det.dataset.autoOpen; }  // 우리가 펼친 거면 원래대로 접는다
    }
  }
}
