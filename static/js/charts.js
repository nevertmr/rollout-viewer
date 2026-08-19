"use strict";
/* ══════════════════════════════════════════════════════════════════
   차트 — SVG (viewBox "0 0 673 324", 플롯 47~661 / 12~286)
   ══════════════════════════════════════════════════════════════════ */
function scaleX(t, dur){ return X0 + (dur>0 ? t/dur : 0) * (X1-X0); }
function scaleY(v, lo, hi){ const r = (hi-lo) || 1; return Y1 - (v-lo)/r * (Y1-Y0); }

// 축 눈금 간격 (nice number, 목표 12개 / 최대 16개)
function tickStep(max, target){
  if(!(max>0)) return 1;
  const raw = max/target, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  let best=null, bestD=Infinity;
  for(const m of [1,2,2.5,5,10]){
    for(const s of [mag*0.1, mag, mag*10]){
      const c = m*s, n = Math.floor(max/c + 1e-9);
      if(n < 2 || n > 16) continue;
      if(Math.abs(c*10 - Math.round(c*10)) > 1e-9) continue;   // 라벨이 소수 1자리이므로 0.1 배수만
      const d = Math.abs(n-target);
      if(d < bestD || (d===bestD && c < best)){ best=c; bestD=d; }   // 동률이면 촘촘한 쪽
    }
  }
  return best != null ? best : max/target;
}

// 균등 다운샘플(최대 700점) 경로 생성
function linePath(ts, get, lo, hi, dur){
  const n = ts.length; if(!n) return "";
  const stride = Math.max(1, Math.ceil(n/MAXPTS));
  let d = "", started=false;
  const put = (i)=>{
    const v = get(i);
    if(v==null || !isFinite(v)) return;
    d += (started?"L":"M") + scaleX(ts[i], dur).toFixed(1) + "," + scaleY(v,lo,hi).toFixed(1);
    started = true;
  };
  for(let i=0;i<n;i+=stride) put(i);
  if((n-1) % stride !== 0) put(n-1);
  return d;
}

/* 차트 플레이어 P(포커스 Task 그룹) 의 차트를 box 에 그린다 (관절 3패널 + press 스텝이면 램프 패널) */
function buildCharts(P, box){
  box.textContent=""; P.charts = [];
  const specs = CHART_SPECS.slice();
  const isPress = P.group && (P.group.step===3 || P.group.step===6);
  for(const spec of specs) box.appendChild(makeChart(P, spec).panel);
  if(isPress){
    const lamp = makeLampChart(P);
    if(lamp) box.appendChild(lamp.panel);
  }
  applyFocus(P);
}

/* 공통 차트 골격 생성 */
function chartSkeleton(P, title){
  const panel = el("div","panel cpanel");
  panel.appendChild(el("div","ctitle", title));
  const svg = sv("svg", {viewBox:VB, width:"100%", preserveAspectRatio:"xMidYMid meet"});
  panel.appendChild(svg);

  const gBg   = sv("g",{class:"bg"});      svg.appendChild(gBg);
  const gGrid = sv("g",{class:"grid"});    svg.appendChild(gGrid);
  // y축 선 (아래쪽 x축 선은 없음)
  svg.appendChild(sv("path",{d:"M"+X0+","+Y0+"L"+X0+","+Y1, stroke:"#ccc", "stroke-width":"1", fill:"none"}));
  const gLab  = sv("g",{class:"lab"});     svg.appendChild(gLab);
  const gData = sv("g",{class:"data"});    svg.appendChild(gData);
  const gMark = sv("g",{class:"mark"});    svg.appendChild(gMark);
  const cursor = sv("line",{x1:X0,x2:X0,y1:Y0,y2:Y1,stroke:"rgba(255,255,255,0.75)","stroke-width":"1"});
  svg.appendChild(cursor);
  const gLg = sv("g",{class:"alg"});       svg.appendChild(gLg);

  // 가로 그리드 5줄
  for(const y of GRID_Y){
    gGrid.appendChild(sv("line",{x1:X0,x2:X1,y1:y,y2:y,
      stroke:GRID_COLOR,"stroke-dasharray":"3 3","stroke-width":"1"}));
  }
  return {P, dur:P.dur, panel, svg, gBg, gGrid, gLab, gData, gMark, gLg, cursor};
}

/* x축 눈금(세로 그리드 + 라벨) — 초 단위 */
function drawXAxis(ch){
  const step = tickStep(ch.dur, 12);
  for(let t=step; t<=ch.dur+1e-9; t+=step){
    const x = scaleX(t, ch.dur);
    ch.gGrid.appendChild(sv("line",{x1:x,x2:x,y1:Y0,y2:Y1,
      stroke:GRID_COLOR,"stroke-dasharray":"3 3","stroke-width":"1"}));
    const tx = sv("text",{x:x.toFixed(1), y:294, "font-size":"12px", fill:TICK_FILL, "text-anchor":"middle"});
    tx.textContent = t.toFixed(1)+"s";
    ch.gLab.appendChild(tx);
  }
}

/* y축 눈금 라벨 5개 (정수 반올림) */
function drawYAxis(ch, lo, hi){
  for(let k=0;k<5;k++){
    const y = GRID_Y[k];
    const v = hi - (hi-lo) * (k/4);
    const tx = sv("text",{x:41, y:(y+4).toFixed(1), "font-size":"12px", fill:TICK_FILL, "text-anchor":"end"});
    tx.textContent = String(Math.round(v));
    ch.gLab.appendChild(tx);
  }
}

/* 시도 범례(차트 우상단) */
function drawAttemptLegend(ch){
  let k=0;
  const P = ch.P;
  for(const eid of P.order){
    const a = (P.group.attempts||[]).find(x=>x.eid===eid); if(!a) continue;
    const oc = a.deleted ? "deleted" : (a.outcome||"unlabeled");
    const t = sv("text",{x:X1-4, y:24+k*13, "font-size":"11px", fill:"#cbd5e1",
                         "text-anchor":"end", "data-eid":eid});
    t.textContent = "Attempt " + (a.attempt||k+1) + " · " + oc;
    ch.gLg.appendChild(t); k++;
  }
}

/* 정지 트림 구간(포커스 시도 기준) — 배경 사각형 + 경계 세로 점선 */
function drawTrim(ch){
  ch.gBg.textContent = "";
  const old = ch.gMark.querySelector('[data-role="trim"]'); if(old) old.remove();
  const ep = S.eps[ch.P.focus]; if(!ep) return;
  const tt = trimTime(ep); if(tt==null) return;
  const x = scaleX(tt, ch.dur);
  ch.gBg.appendChild(sv("rect",{x:x.toFixed(1), y:Y0, width:Math.max(0,X1-x).toFixed(1),
    height:(Y1-Y0), fill:"rgba(255,80,80,0.06)"}));
  ch.gMark.appendChild(sv("line",{x1:x.toFixed(1), x2:x.toFixed(1), y1:Y0, y2:Y1,
    stroke:"rgba(255,120,120,0.35)","stroke-dasharray":"4 4","stroke-width":"1","data-role":"trim"}));
}

/* 관절 차트 */
function makeChart(P, spec){
  const names = spec.joints.map(j=>JOINTS[j]);
  const ch = chartSkeleton(P, names.join(", "));
  ch.spec = spec;

  // y 도메인: 그룹 내 모든 시도 × 모든 시리즈(state/action)
  let lo=Infinity, hi=-Infinity;
  for(const eid of P.order){
    const ep = S.eps[eid]; if(!ep) continue;
    for(const src of [ep.state, ep.action]){
      if(!src) continue;
      for(let i=0;i<src.length;i++){
        const row = src[i]; if(!row) continue;
        for(const j of spec.joints){
          const v = row[j];
          if(v!=null && isFinite(v)){ if(v<lo) lo=v; if(v>hi) hi=v; }
        }
      }
    }
  }
  if(!isFinite(lo)||!isFinite(hi)){ lo=0; hi=1; }
  if(hi-lo < 1e-6){ lo-=1; hi+=1; }
  const pad = (hi-lo)*0.06; lo-=pad; hi+=pad;
  ch.lo=lo; ch.hi=hi;

  drawXAxis(ch);
  drawYAxis(ch, lo, hi);

  // 데이터 선: state = 실선, action = 점선("5 5")
  for(const eid of P.order){
    const ep = S.eps[eid]; if(!ep || !ep.t) continue;
    spec.joints.forEach((j, si)=>{
      const color = SERIES_COLORS[si % SERIES_COLORS.length];
      if(ep.state){
        const d = linePath(ep.t, i=>(ep.state[i]||[])[j], lo, hi, ch.dur);
        if(d) ch.gData.appendChild(sv("path",{d, fill:"none", stroke:color, "stroke-width":"1.5",
              "data-eid":eid, "data-j":String(j), "data-kind":"state"}));
      }
      if(ep.action){
        const d = linePath(ep.t, i=>(ep.action[i]||[])[j], lo, hi, ch.dur);
        if(d) ch.gData.appendChild(sv("path",{d, fill:"none", stroke:color, "stroke-width":"1.5",
              "stroke-dasharray":"5 5", "data-eid":eid, "data-j":String(j), "data-kind":"action"}));
      }
    });
  }
  drawAttemptLegend(ch);
  drawTrim(ch);

  // 하단 범례
  ch.panel.appendChild(buildLegend(ch, spec.joints.map((j,si)=>({
    key:String(j), name:JOINTS[j], color:SERIES_COLORS[si % SERIES_COLORS.length],
    kinds:[["action","action"],["state","observation.state"]],
  }))));

  applyHidden(ch);
  P.charts.push(ch);
  return ch;
}

/* 램프 차트 (press 스텝 3·6) */
function makeLampChart(P){
  const any = P.order.map(e=>S.eps[e]).find(ep=>ep && ep.lamp && ep.lamp.m1);
  if(!any) return null;
  const ch = chartSkeleton(P, "lamp m1, m2");
  ch.spec = {lamp:true};

  let lo=Infinity, hi=-Infinity;
  for(const eid of P.order){
    const ep=S.eps[eid]; if(!ep||!ep.lamp) continue;
    for(const arr of [ep.lamp.m1, ep.lamp.m2]){
      if(!arr) continue;
      for(const v of arr){ if(v!=null&&isFinite(v)){ if(v<lo)lo=v; if(v>hi)hi=v; } }
    }
    for(const thr of [ep.lamp.m1_thr, ep.lamp.m2_thr]){
      if(thr!=null&&isFinite(thr)){ if(thr<lo)lo=thr; if(thr>hi)hi=thr; }
    }
  }
  if(!isFinite(lo)||!isFinite(hi)){ lo=0; hi=255; }
  if(hi-lo<1e-6){ lo-=1; hi+=1; }
  const pad=(hi-lo)*0.06; lo-=pad; hi+=pad;
  ch.lo=lo; ch.hi=hi;

  drawXAxis(ch);
  drawYAxis(ch, lo, hi);

  for(const eid of P.order){
    const ep = S.eps[eid]; if(!ep||!ep.lamp||!ep.t) continue;
    [["m1",0],["m2",1]].forEach(([key,si])=>{
      const arr = ep.lamp[key]; if(!arr) return;
      const d = linePath(ep.t, i=>arr[i], lo, hi, ch.dur);
      if(d) ch.gData.appendChild(sv("path",{d, fill:"none", stroke:SERIES_COLORS[si],
            "stroke-width":"1.5", "data-eid":eid, "data-j":key, "data-kind":"state"}));
    });
  }
  // 임계선(점선) + 점등 시점 세로선 — 포커스 기준이 아니라 첫 시도 기준으로 고정
  const ep0 = S.eps[P.order[0]] || any;
  [["m1_thr",0],["m2_thr",1]].forEach(([key,si])=>{
    const thr = ep0.lamp ? ep0.lamp[key] : null;
    if(thr==null||!isFinite(thr)) return;
    const y = scaleY(thr, lo, hi);
    ch.gMark.appendChild(sv("line",{x1:X0,x2:X1,y1:y.toFixed(1),y2:y.toFixed(1),
      stroke:SERIES_COLORS[si], "stroke-dasharray":"5 5", "stroke-width":"1", opacity:"0.55"}));
  });
  for(const eid of P.order){
    const ep = S.eps[eid]; if(!ep) continue;
    const lf = (ep.metrics||{}).lamp_lit_frame;
    if(lf==null || !ep.t || lf>=ep.t.length) continue;
    const x = scaleX(ep.t[lf], ch.dur);
    ch.gMark.appendChild(sv("line",{x1:x.toFixed(1),x2:x.toFixed(1),y1:Y0,y2:Y1,
      stroke:"#22c55e","stroke-width":"1", opacity:"0.8"}));
  }
  drawAttemptLegend(ch);
  drawTrim(ch);

  ch.panel.appendChild(buildLegend(ch, [
    {key:"m1", name:"lamp m1", color:SERIES_COLORS[0], kinds:[["state","value"]]},
    {key:"m2", name:"lamp m2", color:SERIES_COLORS[1], kinds:[["state","value"]]},
  ]));
  applyHidden(ch);
  P.charts.push(ch);
  return ch;
}

/* 차트 아래 범례: 관절명 한 줄 + 들여쓴 action / observation.state 두 줄 */
function buildLegend(ch, entries){
  const box = el("div","legend");
  ch.valEls = {};
  for(const e of entries){
    const blk = el("div","lg-j");

    const top = el("label");
    const cbj = el("input"); cbj.type="checkbox"; cbj.checked=true; cbj.style.accentColor=e.color;
    top.appendChild(cbj); top.appendChild(document.createTextNode(e.name));
    blk.appendChild(top);

    const sub = el("div","lg-sub");
    const subCbs = [];
    for(const [kind, label] of e.kinds){
      const hk = e.key+"|"+kind;
      const l = el("label");
      const cb = el("input"); cb.type="checkbox"; cb.checked = !S.hidden.has(hk); cb.style.accentColor = e.color; cb.dataset.hk = hk;
      cb.onchange = ()=>{ cb.checked ? S.hidden.delete(hk) : S.hidden.add(hk); applyHiddenAll(); };
      const val = el("span","val","—");
      ch.valEls[hk] = val;
      l.appendChild(cb); l.appendChild(document.createTextNode(label)); l.appendChild(val);
      sub.appendChild(l); subCbs.push([cb,hk]);
    }
    blk.appendChild(sub);
    cbj.onchange = ()=>{
      for(const [cb,hk] of subCbs){ cb.checked = cbj.checked; cbj.checked ? S.hidden.delete(hk) : S.hidden.add(hk); }
      applyHiddenAll();
    };
    box.appendChild(blk);
  }
  return box;
}

/* 숨김 상태는 전역 — 현재 차트 플레이어(포커스 Task 그룹)의 모든 패널(과 범례 체크박스)에 반영한다.
   차트를 다시 그릴 때는 buildLegend/applyHidden 이 S.hidden 을 읽으므로 Task 가 바뀌어도 유지된다. */
function applyHiddenAll(){
  for(const P of (S.CP ? [S.CP] : [])){
    for(const ch of P.charts){
      applyHidden(ch);
      ch.panel.querySelectorAll(".lg-sub input[type=checkbox]").forEach(cb=>{
        const hk = cb.dataset.hk; if(hk) cb.checked = !S.hidden.has(hk);
      });
    }
  }
}
function applyHidden(ch){
  ch.svg.querySelectorAll("path[data-j]").forEach(p=>{
    const hk = p.dataset.j+"|"+p.dataset.kind;
    p.style.display = S.hidden.has(hk) ? "none" : "";
  });
}

/* ══════════════════════════════════════════════════════════════════
   재생 커서 / 범례 수치 — 재생 중에는 이것만 갱신
   ══════════════════════════════════════════════════════════════════ */
function updateCursor(P){
  if(!P) return;
  const x = scaleX(clamp(P.T,0,P.dur), P.dur).toFixed(1);
  const ep = S.eps[P.focus];
  const i  = ep ? frameAt(ep, P.T) : 0;
  const past = ep ? (P.T > epDur(ep) + 1e-6) : false;

  for(const ch of P.charts){
    ch.cursor.setAttribute("x1", x); ch.cursor.setAttribute("x2", x);
    if(!ch.valEls) continue;
    for(const hk in ch.valEls){
      const [key, kind] = hk.split("|");
      let v = null;
      if(ep && !past){
        if(ch.spec && ch.spec.lamp){ const arr = ep.lamp && ep.lamp[key]; v = arr ? arr[i] : null; }
        else { const src = kind==="action" ? ep.action : ep.state; v = src ? (src[i]||[])[Number(key)] : null; }
      }
      ch.valEls[hk].textContent = (v==null||!isFinite(v)) ? "—" : Number(v).toFixed(2);
    }
  }
  if(!P.ui) return;
  P.ui.tdisp.textContent = P.T.toFixed(2) + " / " + P.dur.toFixed(2) + " s";
  const pct = P.dur>0 ? (P.T/P.dur*100) : 0;
  const sk = P.ui.seek;
  sk.value = String(Math.round(pct*10));
  sk.style.setProperty("--pct", pct.toFixed(2)+"%");
}
