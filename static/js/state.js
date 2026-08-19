"use strict";
/* ══════════════════════════════════════════════════════════════════
   상태
   ══════════════════════════════════════════════════════════════════ */
const S = {
  idx:null,
  exps:[],           // [{name, tries:[{no, key, run, runs, groups:[…], fail, unstable, multiCycle, nEps}],
                     //   tasks:[{step, instruction, tries:[{no, groups, multiCycle}], nTries, nEps, fail, unstable}], nEps, nGroups, model, runs}]
                     //   — 사이드바 1·2단 (Run(실험) → Task). buildExpTree() 가 만든다
  byGid:new Map(),   // gid -> group
  exp:null,          // 선택된 실험 이름 (사이드바 1열)
  task:null,         // 선택된 Task(step 번호) (사이드바 2열) — Task 선택이 곧 화면 선택 (모든 Try 의 그 Task 카드)
  eps:{},            // eid -> episode payload (현재 Task 에서 받은 것 — Show all 토글 시 재요청 방지)
  order:[],          // 화면에 올라간 모든 eid (Try 순 × 시도 순) — 전역 동기 재생·프레임 프로브·[ ] 이동 순서
  eidGid:new Map(),  // eid -> gid (카드 → 소속 Task 그룹)
  G:null,            // 전역 플레이어 (화면의 모든 카드를 한 타임라인으로 동기 재생) — newPlayer()
  CP:null,           // 차트 플레이어 — 포커스 카드의 Task 그룹 차트(시도 겹침). 차트 박스가 접혀 있으면 null
  feid:null,         // 포커스 카드 eid (차트 대상·[ ] 이동 기준)
  gmode:"attn",      // 전역 오버레이 모드 "attn"|"causal"|"orig" — 새로 그리는 카드의 기본값 (클립이 없으면 원본)
  showAll:false,     // "전체 시도 보기" — 끄면 그룹당 대표 시도 1개만 (Task 를 바꿔도 유지)
  useFrames:{},      // "eid|cam" -> true (clip 실패 시 프레임 폴백, 카메라별로 따로)
  vmode:{},          // eid -> "attn"|"causal"|"orig" (오버레이 모드 — 카드가 다시 그려져도 유지)
  fps:30,
  hidden:new Set(),  // "j|kind" 숨김 (모든 그룹 차트 공통)
  ac:null,           // AbortController
  io:null,           // IntersectionObserver — 뷰포트 밖 카드의 영상은 멈춘다
  M:null,            // 몽타주 모드 핸들 {meta, video, stage, box, ro, scale} — montage.js buildMontage(). null 이면 카드 그리드
  epsFail:new Set(), // 에피소드 JSON 로드 실패 eid (재시도 루프 방지)
  epsReq:0,          // 차트용 에피소드 로드 요청 토큰 (늦게 온 응답이 새 화면을 덮지 않게)
};

/* 플레이어 — 재생 상태 묶음. 전역 플레이어(S.G: 화면 전체 카드, ui=하단 재생바)와
   차트 플레이어(S.CP: 포커스 카드의 그룹, ui 없음, T 는 S.G 를 따라간다) 둘 다 이 모양이다. */
function newPlayer(gid, group){
  return {
    gid, group,
    order:[],          // 이 플레이어가 다루는 eid 목록
    T:0, dur:0, playing:false, raf:null, lastTS:0,
    focus:null,        // 포커스된 eid (차트 강조·범례 수치·트림 음영 기준)
    charts:[],         // 렌더된 차트 핸들
    ui:null,           // {block, bPlay, icPlay, icPause, seek, tdisp} — 전역 플레이어만
  };
}
function playerOfEid(eid){ return S.G; }          // 모든 카드는 전역 플레이어 하나를 따른다
function focusedPlayer(){ return S.G; }

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
