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
  tno:null,          // 선택된 Try 번호 (사이드바 2열)
  run:null,          // 선택된 그룹이 속한 원래 런 디렉토리명 (eid/클립 경로의 <run>)
  gid:null, group:null,
  eps:{},            // eid -> episode payload (현재 표시 중인 것만)
  epsAll:{},         // eid -> episode payload (현재 그룹에서 한 번이라도 받은 것 — 토글 시 재요청 방지)
  order:[],          // 표시 순서 eid
  showAll:false,     // "전체 시도 보기" — 끄면 그룹당 대표 시도 1개만 (그룹을 바꿔도 유지)
  focus:null,        // 포커스된 eid
  useFrames:{},      // "eid|cam" -> true (clip 실패 시 프레임 폴백, 카메라별로 따로)
  vmode:{},          // eid -> "attn"|"causal"|"orig" (오버레이 모드 — 카드가 다시 그려져도 유지)
  T:0, dur:0, fps:30, playing:false,
  hidden:new Set(),  // "j|kind" 숨김
  charts:[],         // 렌더된 차트 핸들
  ac:null,           // AbortController
};

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
