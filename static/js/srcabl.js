"use strict";
/* ══════════════════════════════════════════════════════════════════
   소스별 인과 기여도 (source ablation) — build_srcabl.py 가 구운
   dist/api/srcabl/<run>_<ep4>.json 을 읽어

     ① 카드/타일 우하단 배지 : 현재 프레임의 상위 소스와 비중  ("state 62% · front 14%")
     ② Focus 패널 미니차트   : 4소스 실선 + lang_swap 점선 + 자연 드리프트 수평 점선
                              + 재생 커서 수직선

   지표 = ‖(소스 하나를 중립화한 액션청크) − (원본 액션청크)‖₂ (50스텝×6관절, deg).
   ⚠ 해석 규약(REPORT.md §8-3): 소스 간 절대 배수 비교 금지 — 제거되는 정보량이 소스마다
   다르다. 순위·시간 추세·"같은 소스의 조건 간 변화"만 유효하다.

   데이터가 없는 에피소드(record_for_Exp4/5/6 외)는 배지도 차트도 만들지 않는다.
   ══════════════════════════════════════════════════════════════════ */

/* 색 — fig_source_shares.png 규격: front 파랑 / wrist 청록 / state 빨강 / lang 주황 / swap 보라(점선) */
const SRC_COLORS = {
  front_gray:"#3b82f6", wrist_gray:"#22d3ee", state_mid:"#ef4444",
  lang_empty:"#f97316", lang_swap:"#a78bfa", both_gray:"#94a3b8",
};
const SRC_NAME = {front_gray:"front", wrist_gray:"wrist", state_mid:"state",
                  lang_empty:"lang", lang_swap:"swap", both_gray:"both"};
const SRC_KEYS = ["front_gray","wrist_gray","state_mid","lang_empty"];   // 비중의 분모가 되는 4소스
const SRC_WARN = "⚠ 소스 간 절대 배수 비교 금지 — 제거되는 정보량이 소스마다 다르다. "
               + "같은 소스의 조건 간 변화·순위·시간 추세만 유효.";

const SRCABL = {
  index: null,          // Set<eid> — 데이터가 있는 에피소드 (null = 아직 안 받음)
  idxReq: null,         // Promise
  data: new Map(),      // eid -> payload | null(없음)
  req: new Map(),       // eid -> Promise
  badges: [],           // [{node, p}] — 현재 화면의 배지 (tick 에서 갱신)
  ch: null,             // 미니차트 핸들 {svg, cursor, dur, p, vals…}
  feid: null,           // 미니차트가 그리고 있는 eid
};

/* ── 데이터 ─────────────────────────────────────────────────────── */
function srcablLoadIndex(){
  if(SRCABL.idxReq) return SRCABL.idxReq;
  // no-store: 재베이크(build_srcabl.py) 결과가 브라우저 캐시에 막히지 않게
  SRCABL.idxReq = getJSON("/api/srcabl/index", undefined, {cache:"no-store"})
    .then(j => { SRCABL.index = new Set((j && j.eids) || []); return SRCABL.index; })
    .catch(() => { SRCABL.index = new Set(); return SRCABL.index; });   // 없으면 기능 자체를 끈다
  return SRCABL.idxReq;
}
function srcablHas(eid){ return !!(SRCABL.index && SRCABL.index.has(eid)); }
function srcablOf(eid){ return SRCABL.data.get(eid) || null; }

function srcablFetch(eid){
  if(SRCABL.data.has(eid)) return Promise.resolve(SRCABL.data.get(eid));
  if(SRCABL.req.has(eid)) return SRCABL.req.get(eid);
  const p = getJSON("/api/srcabl?eid=" + encodeURIComponent(eid), undefined, {cache:"no-store"})
    .then(j => { SRCABL.data.set(eid, j); return j; })
    .catch(() => { SRCABL.data.set(eid, null); return null; })          // 404 = 그 에피소드는 미측정
    .finally(() => SRCABL.req.delete(eid));
  SRCABL.req.set(eid, p);
  return p;
}

/* ── 보간 — 분석 프레임(stride 5)을 시각 t(초)에서 선형 보간 ───────── */
function srcablPos(p, t){
  const s = p.sec, n = s.length;
  if(!n) return null;
  if(t <= s[0]) return {a:0, b:0, w:0};
  if(t >= s[n-1]) return {a:n-1, b:n-1, w:0};
  let lo = 0, hi = n-1;
  while(lo < hi-1){ const m = (lo+hi)>>1; if(s[m] <= t) lo = m; else hi = m; }
  const d = s[hi] - s[lo];
  return {a:lo, b:hi, w: d>0 ? (t - s[lo])/d : 0};
}
function srcablLerp(arr, q){
  if(!arr || !q) return null;
  const v0 = arr[q.a], v1 = arr[q.b];
  if(v0 == null || !isFinite(v0)) return null;
  return v0 + ((v1 != null && isFinite(v1)) ? (v1 - v0) * q.w : 0);
}
/* 이 에피소드의 현재 시각 — 전역 타임라인 T 를 자기 길이로 클램프(짧은 카드는 끝에서 정지) */
function srcablT(p){
  const G = S.G, T = G ? G.T : 0;
  return clamp(T, 0, p.dur || 0);
}
/* 현재 프레임의 소스별 {deg, share} — 배지·범례 공용 */
function srcablSample(p, t){
  const q = srcablPos(p, t);
  if(!q) return null;
  const out = {frame: p.frames[q.w >= 0.5 ? q.b : q.a], deg:{}, share:{}};
  for(const k of Object.keys(p.deg || {})) out.deg[k] = srcablLerp(p.deg[k], q);
  for(const k of SRC_KEYS) out.share[k] = srcablLerp(p.share[k], q);
  return out;
}

/* ── 배지 (몽타주 타일 · 폴백 카드 · Focus 카드) ────────────────────── */
/* 배지를 붙일 자리: 타일은 메모 띠 위, 카드는 front 영상 우하단 */
function srcablBadgeHost(eid){
  const tile = (typeof tileOf === "function") ? tileOf(eid) : null;
  if(tile) return {host:tile, kind:"tile"};
  const card = document.querySelector('.acard[data-eid="'+CSS.escape(eid)+'"]');
  if(card){
    const pane = card.querySelector('.pane[data-cam="front"]');
    return {host: pane || card, kind:"card"};
  }
  return null;
}
function srcablMakeBadge(eid, p){
  const h = srcablBadgeHost(eid); if(!h) return null;
  let b = h.host.querySelector(":scope > .srcbadge");
  if(!b){
    b = el("div","srcbadge" + (h.kind==="tile" ? " tile" : ""));
    b.dataset.eid = eid;
    if(h.kind === "tile"){
      const memo = h.host.querySelector(".mmemo");
      b.style.bottom = ((memo ? memo.offsetHeight : 26) + 2) + "px";
    }
    b.onmousedown = (ev)=>ev.stopPropagation();       // 배지 클릭이 카드 포커스를 훔치지 않게
    h.host.appendChild(b);
  }
  b.title = "source ablation · " + eid + "\n" + SRC_WARN;
  return b;
}
/* 화면에 올라온 eid 중 데이터가 있는 것에만 배지를 만든다 */
function srcablAttachBadges(){
  SRCABL.badges = [];
  const seen = new Set();
  for(const eid of (S.order || [])){
    const p = srcablOf(eid); if(!p) continue;
    const b = srcablMakeBadge(eid, p);
    if(b){ SRCABL.badges.push({node:b, p}); seen.add(eid); }
  }
  // Focus 패널의 큰 카드(.fcard)도 같은 eid 를 다시 그리므로 별도로 붙인다
  const fcard = document.querySelector('.acard.fcard[data-eid]');
  if(fcard){
    const eid = fcard.dataset.eid, p = srcablOf(eid);
    const pane = fcard.querySelector('.pane[data-cam="front"]');
    if(p && pane){
      let b = pane.querySelector(":scope > .srcbadge");
      if(!b){ b = el("div","srcbadge big"); b.dataset.eid = eid;
              b.onmousedown = (ev)=>ev.stopPropagation(); pane.appendChild(b); }
      b.title = "source ablation · " + eid + "\n" + SRC_WARN;
      SRCABL.badges.push({node:b, p});
    }
  }
  srcablTick();
}
function srcablClearBadges(){
  document.querySelectorAll(".srcbadge").forEach(n=>n.remove());
  SRCABL.badges = [];
}

/* ── 미니차트 ─────────────────────────────────────────────────────── */
function srcablBox(){ return $("srcablBox"); }
/* 몽타주 모드면 Focus 패널 안(차트 박스 위), 아니면 그리드 아래 차트 박스 위 */
function srcablPlace(){
  const box = srcablBox(); if(!box) return;
  const det = $("chartsBox");
  if(S.M){
    const fb = $("focusBox");
    if(det && det.parentNode === fb){ if(box.nextSibling !== det || box.parentNode !== fb) fb.insertBefore(box, det); }
    else if(box.parentNode !== fb) fb.appendChild(box);
  }else{
    const wrap = document.querySelector(".wrap");
    if(!wrap) return;
    if(det && det.parentNode === wrap){ if(box.nextSibling !== det || box.parentNode !== wrap) wrap.insertBefore(box, det); }
    else if(box.parentNode !== wrap) wrap.insertBefore(box, wrap.querySelector(".credit"));
  }
}

function srcablHide(){
  const box = srcablBox(); if(!box) return;
  box.textContent = ""; box.hidden = true;
  SRCABL.ch = null; SRCABL.feid = null;
}

/* 포커스 카드의 시계열 차트를 (다시) 그린다. 데이터가 없으면 박스를 숨긴다. */
function srcablRefresh(){
  const box = srcablBox(); if(!box) return;
  const eid = S.feid;
  if(!eid || !srcablHas(eid)){ srcablHide(); return; }
  const p = srcablOf(eid);
  if(!p){ srcablFetch(eid).then(()=>{ if(S.feid === eid) srcablRefresh(); }); return; }
  if(SRCABL.feid === eid && SRCABL.ch && box.parentNode){ srcablPlace(); srcablTick(); return; }
  srcablPlace();
  box.textContent = "";
  box.hidden = false;
  box.appendChild(srcablChart(p, eid));
  SRCABL.feid = eid;
  srcablTick();
}

function srcablChart(p, eid){
  const panel = el("div","panel cpanel srcpanel");
  panel.appendChild(el("div","ctitle",
    "Source ablation · deg (action-chunk L2, 50×6) — " + eid
    + " · " + (p.task || "") ));
  panel.appendChild(el("div","srcsub",
    "share % = 4소스(front+wrist+state+lang) 합 대비 상대 비중 · 가로 점선 = 자연 드리프트 "
    + fx(p.nat_adj_frame_deg, 1) + " deg (인접 분석프레임 0.167초 사이 원본 액션 변화)"));
  panel.appendChild(el("div","srcwarn", SRC_WARN));

  const dur = p.dur || (p.sec.length ? p.sec[p.sec.length-1] : 0);
  const P = {dur, order:[], group:null, charts:[], focus:null};
  const ch = chartSkeleton(P, "");                    // 축·그리드·커서 골격은 charts.js 규격 그대로
  ch.panel.querySelector(".ctitle").remove();         // 제목은 위에서 따로 붙였다
  const svg = ch.svg;
  ch.dur = dur;

  // y 도메인: 0 ~ (4소스·swap·자연드리프트 최대) — 0 기준이라 소스 간 크기 비교가 눈에 그대로 보인다
  let hi = 0;
  for(const k of SRC_KEYS.concat(["lang_swap"])){
    const a = p.deg[k]; if(!a) continue;
    for(const v of a) if(v != null && isFinite(v) && v > hi) hi = v;
  }
  if(p.nat_adj_frame_deg != null && p.nat_adj_frame_deg > hi) hi = p.nat_adj_frame_deg;
  hi = hi > 0 ? hi * 1.08 : 1;
  const lo = 0;
  ch.lo = lo; ch.hi = hi;
  drawXAxis(ch);
  drawYAxis(ch, lo, hi);

  // 자연 드리프트 수평 점선 (해석 기준선)
  if(p.nat_adj_frame_deg != null && isFinite(p.nat_adj_frame_deg)){
    const y = scaleY(p.nat_adj_frame_deg, lo, hi);
    ch.gMark.appendChild(sv("line",{x1:X0,x2:X1,y1:y.toFixed(1),y2:y.toFixed(1),
      stroke:"#cbd5e1","stroke-dasharray":"2 4","stroke-width":"1",opacity:"0.75"}));
    const t = sv("text",{x:X1-4, y:(y-4).toFixed(1), "font-size":"11px", fill:"#cbd5e1",
                         "text-anchor":"end", opacity:"0.85"});
    t.textContent = "natural drift " + fx(p.nat_adj_frame_deg,0);
    ch.gMark.appendChild(t);
  }
  // 파지 시점 세로선
  if(p.grasp_sec != null && isFinite(p.grasp_sec) && dur > 0){
    const x = scaleX(clamp(p.grasp_sec,0,dur), dur);
    ch.gMark.appendChild(sv("line",{x1:x.toFixed(1),x2:x.toFixed(1),y1:Y0,y2:Y1,
      stroke:"#22c55e","stroke-width":"1",opacity:"0.5","stroke-dasharray":"6 3"}));
    const t = sv("text",{x:(x+4).toFixed(1), y:(Y0+12), "font-size":"11px", fill:"#22c55e",
                         opacity:"0.8"});
    t.textContent = "grasp";
    ch.gMark.appendChild(t);
  }

  // 4소스 실선 + lang_swap 점선
  const draw = (key, dash)=>{
    const arr = p.deg[key]; if(!arr) return;
    const d = linePath(p.sec, i=>arr[i], lo, hi, dur);
    if(!d) return;
    const at = {d, fill:"none", stroke:SRC_COLORS[key], "stroke-width":"1.8",
                "data-src":key, "stroke-linejoin":"round"};
    if(dash) at["stroke-dasharray"] = dash;
    ch.gData.appendChild(sv("path", at));
  };
  for(const k of SRC_KEYS) draw(k, null);
  draw("lang_swap", "5 4");

  panel.appendChild(svg);                        // chartSkeleton 이 만든 svg 를 우리 패널로 옮긴다
  panel.appendChild(srcablLegend(ch, p));
  SRCABL.ch = ch;
  ch.p = p;
  return panel;
}

/* 범례 — 색·이름·현재 프레임 deg·비중%. 에피소드 평균은 오른쪽에 회색으로. */
function srcablLegend(ch, p){
  const box = el("div","srclegend");
  ch.rows = {};
  const row = (key, label, note, dashed)=>{
    const r = el("div","srcrow");
    const sw = el("span","sw"); sw.style.background = SRC_COLORS[key] || "#94a3b8";
    if(dashed) sw.classList.add("dash");
    r.appendChild(sw);
    r.appendChild(el("span","nm", label));
    const v = el("span","v","—");
    const sh = el("span","sh","");
    const mn = el("span","mn", note || "");
    r.appendChild(v); r.appendChild(sh); r.appendChild(mn);
    box.appendChild(r);
    ch.rows[key] = {deg:v, share:sh};
  };
  const mean = p.mean || {}, shm = p.share_mean || {};
  for(const k of SRC_KEYS){
    row(k, SRC_NAME[k], "ep mean " + fx(mean[k],0) + " deg · " + fx((shm[k]||0)*100,1) + "%", false);
  }
  row("lang_swap", "swap (" + (p.swap_kind === "cross_task" ? "cross-task" : "relational") + ")",
      "ep mean " + fx(mean.lang_swap,0) + " deg", true);
  // 참고값 — 비중 분모에는 들어가지 않는다
  const r = el("div","srcrow ref");
  const sw = el("span","sw"); sw.style.background = SRC_COLORS.both_gray; r.appendChild(sw);
  r.appendChild(el("span","nm","both cams (ref)"));
  r.appendChild(el("span","v", fx(mean.both_gray,0)));
  r.appendChild(el("span","sh",""));
  r.appendChild(el("span","mn","에피소드 평균 · 비중 분모 제외"));
  box.appendChild(r);
  return box;
}

/* ── 매 프레임 갱신 (main.js paintT 에서 호출) ────────────────────── */
function srcablTick(){
  // 배지
  for(const b of SRCABL.badges){
    const p = b.p, s = srcablSample(p, srcablT(p));
    if(!s){ b.node.textContent = ""; continue; }
    // 4소스를 모두, 고정 순서(front·wrist·state·lang)로 — 순위가 바뀌어도 자리가 안 흔들린다
    const rank = SRC_KEYS.map(k=>({k, sh:s.share[k]||0, deg:s.deg[k]||0}));
    const top = rank.reduce((a,x)=> x.sh > a.sh ? x : a, rank[0]);
    const pct = (v)=>Math.round((v||0)*100) + "%";
    b.node.textContent = "";
    b.node.title = rank.map(e=>SRC_NAME[e.k] + " " + pct(e.sh)
                              + " (" + Math.round(e.deg) + " deg)").join("  ·  ");
    rank.forEach((e, i)=>{
      const isTop = (e.k === top.k);
      if(i && !b.node.classList.contains("tile")) b.node.appendChild(el("span","dot"," · "));
      const w = el("span","w");
      const n = el("span","n", SRC_NAME[e.k]); n.style.color = SRC_COLORS[e.k];
      w.appendChild(n);
      const compact = b.node.classList.contains("tile");
      const val = compact ? String(Math.round((e.sh||0)*100)) : pct(e.sh);
      const pv = el("span","p" + (isTop ? " top" : ""), " " + val);
      w.appendChild(pv);
      b.node.appendChild(w);
    });
  }
  // 미니차트 커서 + 범례 수치
  const ch = SRCABL.ch;
  if(!ch || !ch.p) return;
  const p = ch.p, t = srcablT(p);
  const x = scaleX(clamp(t, 0, ch.dur), ch.dur).toFixed(1);
  ch.cursor.setAttribute("x1", x); ch.cursor.setAttribute("x2", x);
  const s = srcablSample(p, t);
  for(const k in ch.rows){
    const r = ch.rows[k];
    const d = s ? s.deg[k] : null;
    r.deg.textContent = (d == null || !isFinite(d)) ? "—" : d.toFixed(0);
    if(r.share) r.share.textContent = (s && s.share[k] != null)
      ? (s.share[k]*100).toFixed(0) + "%" : "";
  }
}

/* ── 화면 렌더 훅 ─────────────────────────────────────────────────── */
/* 현재 화면(S.order)의 ablation 데이터를 받아 배지·차트를 붙인다 */
function srcablSync(){
  srcablLoadIndex().then(()=>{
    const eids = (S.order || []).filter(srcablHas);
    if(!eids.length){ srcablClearBadges(); srcablHide(); return; }
    return Promise.all(eids.map(srcablFetch)).then(()=>{
      srcablAttachBadges();
      srcablRefresh();
    });
  }).catch(()=>{});
}
function srcablTeardown(){
  srcablClearBadges();
  srcablHide();
}

if(typeof module !== "undefined" && module.exports){
  module.exports = {srcablPos, srcablLerp, SRC_KEYS, SRC_COLORS};
}
