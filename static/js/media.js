"use strict";
/* ══════════════════════════════════════════════════════════════════
   미디어 — 클립 재생·프레임 폴백·오버레이(attention/causal) 모드·전역 동기 재생
   ══════════════════════════════════════════════════════════════════ */
function cardOf(eid){ return document.querySelector('.acard[data-eid="'+CSS.escape(eid)+'"]'); }
function paneOf(card, cam){ return card ? card.querySelector('.pane[data-cam="'+cam+'"]') : null; }
const fkey = (eid, cam) => eid + "|" + cam;

/* 현재 살아있는 카드에 영상/프레임 모드를 반영 (카메라 하나 또는 전부) */
function applyMediaMode(eid, cam){
  const c = cardOf(eid); if(!c) return;
  for(const cm of (cam ? [cam] : CAMS)){
    const p = paneOf(c, cm); if(!p) continue;
    const v = p.querySelector("video"), img = p.querySelector("img");
    if(!v || !img) continue;
    const useF = !!S.useFrames[fkey(eid, cm)];
    v.style.display   = useF ? "none" : "block";
    img.style.display = useF ? "block" : "none";
  }
}
/* 영상 → 프레임 스크럽 폴백 (에러 / 로드 지연 공통) — 카메라 단위 */
/* 프레임 서빙 가능 여부 — 배포본은 원본 프레임이 없어 /frame 이 404 다.
   그 환경에서 프레임 폴백으로 넘어가면 화면이 그냥 검게 남으므로, 한 번 확인해서
   불가능하면 폴백을 쓰지 않고 영상 로딩을 계속 기다린다(탭이 백그라운드라 느린 경우 등). */
let FRAMES_OK = null, framesProbe = null;
function probeFrames(){
  if(framesProbe) return framesProbe;
  const any = S.order && S.order[0];
  if(!any) return Promise.resolve(true);
  framesProbe = fetch("/frame?eid="+encodeURIComponent(any)+"&cam=front&i=0", {method:"HEAD"})
    .then(r => { FRAMES_OK = r.ok; return r.ok; })
    .catch(() => { FRAMES_OK = false; return false; });
  return framesProbe;
}
function toFrames(eid, cam, v){
  if(v) clearTimeout(v._wd);
  probeFrames().then(ok => {
    if(!ok){                              // 프레임이 없는 배포 환경 → 영상 재시도
      if(v && v.isConnected && v.readyState < 2){ try{ v.load(); }catch(e){} armWatchdog(eid, cam, v); }
      return;
    }
    S.useFrames[fkey(eid, cam)] = true;
    applyMediaMode(eid, cam); syncOne(eid, true);
  });
}
/* 클립이 뒤늦게라도 열리면 영상 모드로 복귀 — 카메라 단위 */
function backToVideo(eid, cam, v){
  if(v) clearTimeout(v._wd);
  if(!S.useFrames[fkey(eid, cam)]) return;
  S.useFrames[fkey(eid, cam)] = false;
  applyMediaMode(eid, cam); syncOne(eid, true);
}

/* 에러 없이 멈춰 있는 경우(코덱 미지원·미디어 차단·백그라운드 탭·클립 생성 지연) 대비.
   정상 환경에선 가장 큰 클립도 1초 내로 열린다. 카메라마다 따로 건다.
   뷰포트 밖 카드는 preload=metadata 라 일부러 로드를 안 한 상태이므로 판정하지 않고 뒤로 미룬다. */
function armWatchdog(eid, cam, v){
  clearTimeout(v._wd);
  v._wd = setTimeout(()=>{
    if(!v.isConnected) return;              // 폐기된 카드의 타이머는 무시
    if(!cardVisible(cardOf(eid))){ armWatchdog(eid, cam, v); return; }
    if(v.readyState < 2) toFrames(eid, cam, v);
  }, 8000);
}

/* ── 오버레이 클립 (attention / causal) ───────────────────────────────
   해당 클립이 있는 에피소드에만 토글을 보인다. 존재 확인은 HEAD 한 번
   (GET 404 는 콘솔 스팸이 된다), 결과는 세션 동안 캐시하고 실패는 조용히 무시. */
const ATTN_OK = new Map();                        // eid -> Promise<boolean>
const CAUSAL_OK = new Map();                      // eid -> Promise<boolean>
function probeClip(cache, eid, cam){
  if(cache.has(eid)) return cache.get(eid);
  const p = fetch("/clip?eid="+encodeURIComponent(eid)+"&cam="+cam, {method:"HEAD"})
    .then(r=>r.ok).catch(()=>false);
  cache.set(eid, p);
  return p;
}
const probeAttn   = (eid)=>probeClip(ATTN_OK, eid, "front_attn");
const probeCausal = (eid)=>probeClip(CAUSAL_OK, eid, "front_causal");

/* 모드 순환 — Attention → Causal → Original (없는 모드는 건너뜀).
   initMedia 가 src 교체·워치독 재무장·현재 재생 시각 재동기까지 해 주므로
   동기 재생 로직은 그대로 유지된다. */
const MODE_LABEL = {attn:"Attention", causal:"Causal", orig:"Original"};
function modeOf(eid){ return S.vmode[eid] || "orig"; }
function paintModeBtn(eid){
  const c = cardOf(eid); if(!c) return;
  const b = c.querySelector("button.attn-chip"); if(!b) return;
  const m = modeOf(eid);
  b.textContent = MODE_LABEL[m];
  b.classList.toggle("on", m !== "orig");
  b.classList.toggle("causal", m === "causal");
}
function setMode(eid, m){
  const same = (modeOf(eid) === m);           // 이미 그 클립이 물려 있으면 src 교체 없이 표시만 맞춘다
  S.vmode[eid] = m;
  paintModeBtn(eid);
  if(!same) initMedia(eid, cardOf(eid));
}
/* 전역 모드 — 화면의 모든 카드를 한 번에 m 으로 (해당 클립이 없는 카드는 원본).
   카드별 ◉ 토글은 그 뒤에도 개별로 덮어쓸 수 있다. */
function setModeAll(m){
  S.gmode = m;
  for(const eid of S.order) applyModeFor(eid, m);
}
/* 카드 하나에 모드 m 을 적용 — 클립 존재를 확인한 뒤 없으면 원본 */
function applyModeFor(eid, m){
  return Promise.all([probeAttn(eid), probeCausal(eid)]).then(([hasA, hasC])=>{
    if(!cardOf(eid)) return;                    // 그 사이 화면이 바뀜
    const ok = (m === "orig") || (m === "attn" ? hasA : hasC);
    setMode(eid, ok ? m : "orig");
  });
}
async function cycleMode(eid){
  const [hasA, hasC] = await Promise.all([probeAttn(eid), probeCausal(eid)]);
  const cyc = ["attn","causal","orig"].filter(m => m==="orig" || (m==="attn" ? hasA : hasC));
  const i = cyc.indexOf(modeOf(eid));
  setMode(eid, cyc[(i+1) % cyc.length]);
}

/* 카드의 두 카메라 클립을 한 번에 물린다.
   attn 모드: 두 캠 모두 _attn 클립. causal 모드: front 만 causal (wrist 는 원본). */
function initMedia(eid, card){
  const c = card || cardOf(eid); if(!c) return;
  const m = modeOf(eid);
  for(const cam of CAMS){
    const p = paneOf(c, cam); if(!p) continue;
    const v = p.querySelector("video"), img = p.querySelector("img");
    if(!v || !img) continue;
    clearTimeout(v._wd);
    S.useFrames[fkey(eid, cam)] = false;
    img.dataset.want = "";
    let clip = cam;
    if(m === "attn") clip = cam + "_attn";
    else if(m === "causal" && cam === "front") clip = "front_causal";
    // 뷰포트 밖이면 메타데이터만 — 화면에 들어올 때(onCardVisible) auto 로 올린다
    v.preload = cardVisible(c) ? "auto" : "metadata";
    v.src = "/clip?eid="+encodeURIComponent(eid)+"&cam="+clip;
    v.load();
    armWatchdog(eid, cam, v);
  }
  applyMediaMode(eid);
  syncOne(eid, true);
}

/* ══════════════════════════════════════════════════════════════════
   뷰포트 가드 — IntersectionObserver 로 카드 가시성을 추적하고,
   화면 밖 카드의 영상은 멈춘 채로 둔다(그룹이 재생 중이어도).
   ══════════════════════════════════════════════════════════════════ */
/* 카드가 뷰포트 안인가 — 관찰자 판정이 우선이고, 관찰자 콜백이 아직/전혀 오지 않은 경우
   (백그라운드 탭은 렌더 프레임이 없어 IntersectionObserver 가 멈춘다) 기하 계산으로 보강한다. */
function inViewport(node){
  const r = node.getBoundingClientRect();
  if(r.width<=0 || r.height<=0) return false;
  const W = window.innerWidth || document.documentElement.clientWidth;
  const H = window.innerHeight || document.documentElement.clientHeight;
  return r.right > 0 && r.left < W && r.bottom > 0 && r.top < H;
}
function cardVisible(card){
  if(!card) return false;
  if(card.dataset.vis === "1") return true;
  if(card.dataset.vis === "0" && inViewport(card)){ card.dataset.vis = "1"; onCardVisible(card); return true; }
  return false;
}
function ensureObserver(){
  if(S.io || typeof IntersectionObserver === "undefined") return S.io;
  S.io = new IntersectionObserver((entries)=>{
    for(const en of entries){
      const c = en.target, was = c.dataset.vis === "1";
      c.dataset.vis = en.isIntersecting ? "1" : "0";
      if(en.isIntersecting && !was) onCardVisible(c);
      else if(!en.isIntersecting && was) onCardHidden(c);
    }
  }, {root: document.getElementById("main"), rootMargin: "200px 0px"});
  return S.io;
}
function observeCard(card){
  const io = ensureObserver();
  if(io){ card.dataset.vis = "0"; io.observe(card); }
  else card.dataset.vis = "1";                 // 관찰자가 없으면 항상 보이는 것으로
}
function onCardVisible(card){
  const eid = card.dataset.eid;
  card.querySelectorAll("video").forEach(v=>{
    if(v.preload !== "auto"){ v.preload = "auto"; if(v.readyState < 2){ try{ v.load(); }catch(e){} } }
  });
  syncOne(eid, true);                          // 재생 중인 그룹이면 여기서 play() 로 합류
}
function onCardHidden(card){
  card.querySelectorAll("video").forEach(v=>{ if(!v.paused) v.pause(); });
}

/* ══════════════════════════════════════════════════════════════════
   동기 재생 — 전역 플레이어의 절대 시간(초) 기준으로 카드 영상 제어
   (카드마다 길이가 달라 짧은 카드는 끝 프레임에서 정지, 전체 최장 길이가 타임라인)
   ══════════════════════════════════════════════════════════════════ */
function syncOne(eid, force){
  const c = cardOf(eid); if(!c) return;
  const P = playerOfEid(eid);
  const ep = S.eps[eid];
  const T = P ? P.T : 0, playing = !!(P && P.playing);
  const dur = ep ? epDur(ep) : 0;
  const over = dur>0 && T > dur;                   // 짧은 영상은 끝에서 정지
  const t = over ? Math.max(0, dur-0.04) : T;
  const vis = cardVisible(c);
  const seekOk = !!force || vis;                   // 성능 가드

  for(const cam of CAMS){
    const p = paneOf(c, cam); if(!p) continue;
    const v = p.querySelector("video"), img = p.querySelector("img");
    if(!v || !img) continue;

    if(S.useFrames[fkey(eid, cam)]){
      if(!ep || !vis) continue;
      const i = frameAt(ep, t);
      const want = "/frame?eid="+encodeURIComponent(eid)+"&cam="+cam+"&i="+i;
      if(img.dataset.want !== want){ img.dataset.want = want; img.src = want; }
      continue;
    }
    if(playing && !over && vis){
      if(v.paused) v.play().catch(()=>{});
      if(Math.abs(v.currentTime - t) > 0.22){ try{ v.currentTime = t; }catch(e){} }
    }else{
      if(!v.paused) v.pause();
      if(seekOk && Math.abs(v.currentTime - t) > 0.04){ try{ v.currentTime = t; }catch(e){} }
    }
  }
}
function syncPlayer(P){ for(const eid of P.order) syncOne(eid); }
function syncAll(){ for(const eid of S.order) syncOne(eid); }
