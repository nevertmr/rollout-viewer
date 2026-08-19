"use strict";
/* ══════════════════════════════════════════════════════════════════
   상태
   ══════════════════════════════════════════════════════════════════ */
const S = {
  idx:null,
  exps:[],           // [{name, tries:[{no, key, run, runs, groups:[…], fail, unstable, multiCycle, nEps}], nEps, nGroups, model, runs}]
                     //   — 사이드바 1·2단 (Run(실험) → Try). buildExpTree() 가 만든다
  byGid:new Map(),   // gid -> group
  exp:null,          // 선택된 실험 이름 (사이드바 1열)
  tno:null,          // 선택된 Try 번호 (사이드바 2열) — Try 선택이 곧 화면 선택
  eps:{},            // eid -> episode payload (현재 Try 에서 받은 것 — Show all 토글 시 재요청 방지)
  order:[],          // 화면에 올라간 모든 eid (그룹 순 × 시도 순) — 프레임 프로브·전체 동기화용
  eidGid:new Map(),  // eid -> gid (카드 → 소속 그룹 플레이어)
  players:new Map(), // gid -> P (그룹별 독립 플레이어 — 아래 newPlayer())
  porder:[],         // 화면 그룹 순서 (gid 배열)
  fgid:null,         // 키보드 포커스 그룹 (Space / [ ] / ←→ 대상)
  showAll:false,     // "전체 시도 보기" — 끄면 그룹당 대표 시도 1개만 (Try 를 바꿔도 유지)
  useFrames:{},      // "eid|cam" -> true (clip 실패 시 프레임 폴백, 카메라별로 따로)
  vmode:{},          // eid -> "attn"|"causal"|"orig" (오버레이 모드 — 카드가 다시 그려져도 유지)
  fps:30,
  hidden:new Set(),  // "j|kind" 숨김 (모든 그룹 차트 공통)
  ac:null,           // AbortController
  io:null,           // IntersectionObserver — 뷰포트 밖 카드의 영상은 멈춘다
};

/* 그룹 플레이어 — 그룹(=Task step) 블록 하나가 갖는 독립 재생 상태.
   T/dur/playing/focus/charts 는 종전의 전역 재생 상태를 그룹 단위로 내린 것이다. */
function newPlayer(gid, group){
  return {
    gid, group,
    order:[],          // 이 그룹에서 표시 중인 eid (attempt 순)
    T:0, dur:0, playing:false, raf:null, lastTS:0,
    focus:null,        // 포커스된 eid (차트 강조·범례 수치·트림 음영 기준)
    charts:[],         // 렌더된 차트 핸들
    ui:null,           // {block, bPlay, icPlay, icPause, seek, tdisp, attempts, charts}
  };
}
function playerOfEid(eid){ const gid = S.eidGid.get(eid); return gid ? (S.players.get(gid) || null) : null; }
function focusedPlayer(){ return S.fgid ? (S.players.get(S.fgid) || null) : null; }

/* ══════════════════════════════════════════════════════════════════
   유틸
   ══════════════════════════════════════════════════════════════════ */
const $  = (id) => document.getElementById(id);
const el = (tag, cls, txt) => { const e=document.createElement(tag); if(cls)e.className=cls; if(txt!=null)e.textContent=txt; return e; };
const sv = (tag, attrs) => { const e=document.createElementNS(SVGNS,tag); for(const k in attrs) e.setAttribute(k, attrs[k]); return e; };
const clamp = (v,a,b) => v<a?a:(v>b?b:v);
const fx = (v,n) => (v==null||!isFinite(v)) ? "—" : Number(v).toFixed(n);

let toastT=null;
function toast(msg){
  const t=$("toast"); t.textContent=msg; t.classList.add("on");
  clearTimeout(toastT); toastT=setTimeout(()=>t.classList.remove("on"),1400);
}

function epDur(ep){
  if(!ep || !ep.t || !ep.t.length) return 0;
  return ep.t[ep.t.length-1] || 0;
}
function trimTime(ep){
  if(!ep || !ep.t) return null;
  const tf = ep.trim_from;
  if(!(typeof tf === "number") || tf<=0 || tf>=ep.t.length) return null;
  return ep.t[tf];
}

/* 시각 t 에 대응하는 프레임 인덱스 (이진 탐색) */
function frameAt(ep, t){
  const ts = ep.t; if(!ts||!ts.length) return 0;
  let lo=0, hi=ts.length-1;
  if(t<=ts[0]) return 0;
  if(t>=ts[hi]) return hi;
  while(lo<hi-1){ const mid=(lo+hi)>>1; if(ts[mid]<=t) lo=mid; else hi=mid; }
  return (t-ts[lo] <= ts[hi]-t) ? lo : hi;
}
