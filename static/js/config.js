"use strict";
/* ══════════════════════════════════════════════════════════════════
   상수 — 참조 규격 좌표계 그대로
   ══════════════════════════════════════════════════════════════════ */
const SVGNS = "http://www.w3.org/2000/svg";
const JOINTS = ["shoulder_pan.pos","shoulder_lift.pos","elbow_flex.pos",
                "wrist_flex.pos","wrist_roll.pos","gripper.pos"];
const SERIES_COLORS = ["#f97316","#3b82f6","#22c55e"];   // 1·2·3번째 시리즈 색
const CHART_SPECS = [{joints:[0,1,2]}, {joints:[3,4]}, {joints:[5]}];

const VB   = "0 0 673 324";      // viewBox
const X0=47, X1=661, Y0=12, Y1=286;      // 플롯 영역
const GRID_Y = [12, 80.5, 149, 217.5, 286];
const GRID_COLOR = "rgb(51,65,85)";
const TICK_FILL  = "rgb(148,163,184)";
const MAXPTS = 700;              // 다운샘플 상한
const CAMS = ["front","wrist"];  // 카드마다 두 카메라를 동시에 띄운다(위: front, 아래: wrist)
const OC_INIT  = {success:"S", fail:"F", unstable:"U", unlabeled:"?", deleted:"D"};
