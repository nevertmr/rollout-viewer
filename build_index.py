"""DATA_ROOT 아래 모든 런 디렉토리(raw/ 가 있는 것)를 훑어 뷰어용 인덱스(cache/index.json)를 만든다.

3단 구조: Experiment(Run) > Try > Task
  - Experiment : 사용자에게 보이는 최상위. 디렉토리명 → 실험명은 EXPERIMENT_RULES 로 결정.
                 coffee_new01~15 는 한 실험 "NorRec_RW___Red" 로 묶이고, 그 외는 디렉토리명 그대로.
  - Try        : coffee_newNN 은 디렉토리 번호(NN), 그 외 런은 사이클 번호(c1, c2, …) 그대로.
                 사이클이 불연속이어도 재번호하지 않는다(원본 추적성).
  - Task       : step(task_index). 그룹 키는 (run, cycle, step) 로 종전과 동일.
  eid(<run>/epNNNN)·클립명·episode JSON 경로는 바뀌지 않는다. experiment / try_no 는 추가 필드.

실행:  python3 build_index.py            (기본: press 스텝만 램프 계산)
       python3 build_index.py --all-lamp (전 에피소드 램프 계산, 느림)

── /api/index 응답 스키마 ──────────────────────────────────────────────────
{
  "generated_at": float,            # unix ts
  "root": str,                      # intern_coffee 절대경로
  "fps": 30,
  "joint_names": [6 str],
  "lamp_roi": {"m1":[x1,y1,x2,y2], "m2":[...]},
  "lamp_thresh": {"m1":113.0, "m2":162.0},
  "press_steps": [3, 6],
  "tasks": [12 str],                # step_index 1..N 의 지시문 합집합 (tasks[step-1])
  "tasks_map": {"1": str, …, "103": str},   # 전역 합집합 (커스텀 태스크 포함)
  "experiments": [ {"name","runs":[str],"n_tries","n_episodes","n_groups","model",
                    "tasks":{step:str}, "tries":[{"try_no","run","cycle","n_episodes","n_groups"}]} ],
  "counts": {
      "experiments": int, "runs": int, "groups": int, "episodes": int, "deleted": int,
      "labels": {"success":149, "fail":31, "unstable":4},   # deleted 제외
      "labels_all": {...}                                   # deleted 포함
  },
  "runs": [ {"run","experiment","model","exec_mode","created_at","n_episodes","n_groups",
             "tasks":{step:str}, "custom_tasks":{step:str}, "try_nos":[int]} ],
  "episodes": [ EP, ... ],          # 정본 배열. run→cycle→step→attempt 순 정렬
  "groups":   [ GRP, ... ]          # 같은 (run,cycle,step) 재시도 묶음
}

EP = {
  "idx": int,                 # episodes 배열 내 위치 (groups.eps 가 가리키는 값)
  "run": "coffee_new01",
  "experiment": "NorRec_RW___Red",   # EXPERIMENT_RULES 로 결정 (사이드바 1열)
  "try_no": int,              # coffee_newNN → NN, 그 외 런 → cycle (사이드바 2열)
  "ep": int,                  # 런 내부 에피소드 id
  "dir": "ep0000_c1_t1",      # raw/ 아래 디렉토리명 (프레임 서빙에 사용)
  "cycle": int,
  "step": int,                # 1..12
  "attempt": int,             # 같은 (run,cycle,step) 안에서 ep id 오름차순 1,2,3…
  "n_attempts": int,          # 그 그룹의 총 시도 수
  "group_id": "coffee_new01|c1|s1",
  "instruction": str,
  "is_press": bool,           # step in (3,6)
  "label": "success"|"fail"|"unstable"|"unlabeled",
  "label_source": "update"|"bulk"|"none",
  "memo": str,
  "deleted": bool,            # 라벨과 별개 플래그. 뷰어에서는 보이되 배지로 구분
  "agg_index": int|null,      # 병합 HF 데이터셋 episode_index (deleted 는 null)
  "n_frames": int,            # steps.jsonl 실제 행 수
  "n_front_frames": int,      # frames/ 의 front jpg 개수
  "duration_s": float|null,   # 참고용 — 품질 지표로 쓰지 말 것
  "started_at": float|null,
  "ended_at": float|null,
  "metrics": {"jerk_p50","jerk_p95","track_err","reversals","grip_events","n_used","trim"},
  "lamp_m1_end": float|null,  # press 스텝만 계산, 나머지는 null
  "lamp_m2_end": float|null,
  "lamp_lit_frame": int|null  # 해당 스텝 머신 램프가 처음 임계를 넘은 프레임
}

GRP = {
  "group_id": str, "run": str, "experiment": str, "try_no": int, "cycle": int, "step": int,
  "instruction": str, "is_press": bool,
  "n_attempts": int,          # deleted 포함 전체 시도 수
  "n_kept": int,              # deleted 제외
  "outcome": str,             # 마지막 비삭제 시도의 label (없으면 "unlabeled")
  "any_fail": bool,           # 비삭제 시도 중 fail/unstable 이 하나라도 있는지
  "eps": [int, ...]           # episodes 배열 인덱스, attempt 순
}

부수 산출물: cache/lamp.json — press 에피소드의 램프 시계열 원본
             {"run/dir": {"m1":[...], "m2":[...]}}  (프레임 수 길이)
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics as M  # noqa: E402

BASE = Path("/Users/gimminseo/kai_pj/intern_coffee")
OUT_DIR = Path(__file__).resolve().parent / "cache"
OUT = OUT_DIR / "index.json"
LAMP_OUT = OUT_DIR / "lamp.json"
AGG_CSV = BASE / "merged" / "so101_coffee_rollouts" / "episode_labels.csv"
RUN_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# ── 실험(Experiment) 매핑 규칙 ─────────────────────────────────────────────
# (디렉토리명 정규식, 실험명, try 번호 규칙). 위에서부터 첫 매치. 매치 없으면 디렉토리명 = 실험명,
# try 번호 = 사이클 번호. "dirnum" 은 디렉토리명 끝 숫자를 try 번호로 쓴다.
EXPERIMENT_RULES = [
    (re.compile(r"^coffee_new\d+$"), "NorRec_RW___Red", "dirnum"),
]
# 실험 표시 순서: 규칙 테이블에 명시된 실험이 먼저, 나머지는 디렉토리명 순
_RULE_ORDER = {name: i for i, (_, name, _) in enumerate(EXPERIMENT_RULES)}


def experiment_of(run: str) -> tuple[str, str]:
    """디렉토리명 → (실험명, try 규칙 'dirnum'|'cycle')."""
    for rx, name, mode in EXPERIMENT_RULES:
        if rx.match(run):
            return name, mode
    return run, "cycle"


def try_no_of(run: str, cycle: int) -> int:
    name, mode = experiment_of(run)
    if mode == "dirnum":
        m = re.search(r"(\d+)$", run)
        if m:
            return int(m.group(1))
    return int(cycle)


def list_run_dirs() -> list[Path]:
    """raw/ 가 있는 런 디렉토리. 규칙 테이블에 명시된 실험(Red) 먼저, 그 외는 이름순."""
    dirs = [d for d in BASE.iterdir()
            if d.is_dir() and (d / "raw").is_dir() and RUN_RE.match(d.name)]
    return sorted(dirs, key=lambda d: (_RULE_ORDER.get(experiment_of(d.name)[0], 10**6), d.name))


def run_tasks(meta: dict) -> tuple[dict, dict]:
    """run_meta → ({step:str} 전체, {step:str} 커스텀만). 키는 int step."""
    tasks = {i + 1: t for i, t in enumerate(meta.get("tasks", []) or [])}
    custom = {}
    for k, v in (meta.get("custom_tasks") or {}).items():
        try:
            custom[int(k)] = str(v)
        except (TypeError, ValueError):
            continue
    tasks.update(custom)
    return tasks, custom


def load_agg_map() -> dict:
    """(run, source_episode) → 병합 데이터셋 episode_index."""
    out = {}
    if not AGG_CSV.exists():
        print(f"[warn] {AGG_CSV} 없음 — agg_index 전부 null", file=sys.stderr)
        return out
    with AGG_CSV.open() as f:
        for row in csv.DictReader(f):
            try:
                out[(row["run"], int(row["source_episode"]))] = int(row["episode_index"])
            except (KeyError, ValueError):
                continue
    return out


def build(all_lamp: bool = False) -> tuple[dict, dict]:
    agg = load_agg_map()
    episodes: list[dict] = []
    runs_info: list[dict] = []
    lamp_cache: dict[str, dict] = {}

    exps: dict[str, dict] = {}
    global_tasks: dict[int, str] = {}

    for run_dir in list_run_dirs():
        meta_path = run_dir / "run_meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
        else:
            print(f"[warn] {meta_path} 없음 — 라벨 없이(unlabeled) 인덱싱", file=sys.stderr)
            meta = {}
        exp_name, _mode = experiment_of(run_dir.name)
        tasks_map, custom = run_tasks(meta)
        labels = M.resolve_labels(meta)

        ep_dirs = sorted(d for d in (run_dir / "raw").glob("ep*") if d.is_dir())
        run_eps: list[dict] = []

        for d in ep_dirs:
            if not (d / "episode.json").exists():
                continue
            e = M.load_episode(run_dir, d.name)
            step = e["step"]
            lab = labels.get(e["id"], {"label": "unlabeled", "memo": "",
                                       "deleted": False, "source": "none"})

            instr = e["task"] or tasks_map.get(step, "")
            if instr and step not in tasks_map:
                tasks_map[step] = instr          # run_meta 에 없던 태스크(episode.json 에만 있음)
            rec = {
                "idx": -1,          # 아래에서 채움 (episodes 배열 위치)
                "run": run_dir.name,
                "experiment": exp_name,
                "try_no": try_no_of(run_dir.name, e["cycle"]),
                "ep": e["id"],
                "dir": d.name,
                "cycle": e["cycle"],
                "step": step,
                "attempt": 0,       # 아래에서 채움
                "n_attempts": 0,    # 아래에서 채움
                "group_id": f"{run_dir.name}|c{e['cycle']}|s{step}",
                "instruction": instr,
                "is_press": step in M.PRESS_STEPS,
                "label": lab["label"],
                "label_source": lab["source"],
                "memo": lab["memo"],
                "deleted": lab["deleted"],
                "agg_index": agg.get((run_dir.name, e["id"])),
                "n_frames": e["n"],
                "n_front_frames": len(list(e["frames_dir"].glob("f*_front.jpg"))),
                "duration_s": e["duration_s"],
                "started_at": e["started_at"],
                "ended_at": e["ended_at"],
                "metrics": M.compute_metrics(e["state"], e["action"]),
                "lamp_m1_end": None,
                "lamp_m2_end": None,
                "lamp_lit_frame": None,
            }

            # 램프는 비용이 커서 press 스텝만 계산 (--all-lamp 로 전체 가능)
            if (rec["is_press"] or all_lamp) and rec["n_front_frames"] > 0:
                ser = M.lamp_series(e["frames_dir"], stride=3)
                rec.update(M.lamp_summary(ser["m1"], ser["m2"], step))
                lamp_cache[f"{run_dir.name}/{d.name}"] = ser

            run_eps.append(rec)

        # attempt 번호: 같은 (run, cycle, step) 안에서 ep id 오름차순 1,2,3…
        by_group = defaultdict(list)
        for rec in run_eps:
            by_group[rec["group_id"]].append(rec)
        for gid, recs in by_group.items():
            recs.sort(key=lambda r: r["ep"])
            for i, r in enumerate(recs, 1):
                r["attempt"] = i
                r["n_attempts"] = len(recs)

        run_eps.sort(key=lambda r: (r["cycle"], r["step"], r["attempt"]))
        episodes.extend(run_eps)
        for k, v in tasks_map.items():
            global_tasks.setdefault(k, v)
        try_nos = sorted({r["try_no"] for r in run_eps})
        runs_info.append({
            "run": run_dir.name,
            "experiment": exp_name,
            "model": meta.get("model"),
            "exec_mode": meta.get("exec_mode"),
            "created_at": meta.get("created_at"),
            "n_episodes": len(run_eps),
            "n_groups": len(by_group),
            "tasks": {str(k): v for k, v in sorted(tasks_map.items())},
            "custom_tasks": {str(k): v for k, v in sorted(custom.items())},
            "try_nos": try_nos,
        })
        # 실험 집계 (Try = coffee_newNN 이면 디렉토리 번호, 그 외는 사이클)
        ex = exps.get(exp_name)
        if ex is None:
            ex = exps[exp_name] = {"name": exp_name, "runs": [], "n_tries": 0, "n_episodes": 0,
                                   "n_groups": 0, "model": meta.get("model"), "tasks": {},
                                   "tries": []}
        ex["runs"].append(run_dir.name)
        ex["n_episodes"] += len(run_eps)
        ex["n_groups"] += len(by_group)
        if not ex["model"]:
            ex["model"] = meta.get("model")
        for k, v in sorted(tasks_map.items()):
            ex["tasks"].setdefault(str(k), v)
        for tn in try_nos:
            trs = [r for r in run_eps if r["try_no"] == tn]
            ex["tries"].append({
                "try_no": tn, "run": run_dir.name,
                "cycle": sorted({r["cycle"] for r in trs}),
                "n_episodes": len(trs),
                "n_groups": len({r["group_id"] for r in trs}),
            })
        print(f"  {run_dir.name} [{exp_name}]: {len(run_eps)} eps / {len(by_group)} groups "
              f"/ tries {try_nos[0] if try_nos else '-'}..{try_nos[-1] if try_nos else '-'}", flush=True)

    for ex in exps.values():
        ex["tries"].sort(key=lambda t: (t["try_no"], t["run"]))
        ex["n_tries"] = len({t["try_no"] for t in ex["tries"]})
        ex["tasks"] = dict(sorted(ex["tasks"].items(), key=lambda kv: int(kv[0])))

    for i, r in enumerate(episodes):
        r["idx"] = i

    # 그룹 집계
    gmap: dict[str, dict] = {}
    for r in episodes:
        g = gmap.get(r["group_id"])
        if g is None:
            g = gmap[r["group_id"]] = {
                "group_id": r["group_id"], "run": r["run"],
                "experiment": r["experiment"], "try_no": r["try_no"], "cycle": r["cycle"],
                "step": r["step"], "instruction": r["instruction"],
                "is_press": r["is_press"], "n_attempts": 0, "n_kept": 0,
                "outcome": "unlabeled", "any_fail": False, "eps": [],
            }
        g["eps"].append(r["idx"])
        g["n_attempts"] += 1
        if not r["deleted"]:
            g["n_kept"] += 1
            g["outcome"] = r["label"]          # attempt 순이므로 마지막 비삭제가 최종
            if r["label"] in ("fail", "unstable"):
                g["any_fail"] = True
    groups = list(gmap.values())

    kept = [r for r in episodes if not r["deleted"]]
    # 전역 tasks: 리스트(step 1..N 연속분, 종전 호환) + 맵(커스텀 포함 합집합)
    n_seq = 0
    while (n_seq + 1) in global_tasks:
        n_seq += 1
    tasks_list = [global_tasks[i] for i in range(1, n_seq + 1)]
    index = {
        "generated_at": time.time(),
        "root": str(BASE),
        "fps": M.FPS,
        "joint_names": M.JOINT_NAMES,
        "lamp_roi": {k: list(v) for k, v in M.LAMP_ROI.items()},
        "lamp_thresh": dict(M.LAMP_THRESH),
        "press_steps": sorted(M.PRESS_STEPS),
        "tasks": tasks_list,
        "tasks_map": {str(k): v for k, v in sorted(global_tasks.items())},
        "experiments": list(exps.values()),
        "counts": {
            "experiments": len(exps),
            "runs": len(runs_info),
            "groups": len(groups),
            "episodes": len(episodes),
            "deleted": len(episodes) - len(kept),
            "labels": dict(Counter(r["label"] for r in kept)),
            "labels_all": dict(Counter(r["label"] for r in episodes)),
        },
        "runs": runs_info,
        "episodes": episodes,
        "groups": groups,
    }
    return index, lamp_cache


def main() -> int:
    all_lamp = "--all-lamp" in sys.argv
    t0 = time.time()
    print(f"인덱싱 시작 ({BASE})", flush=True)
    index, lamp_cache = build(all_lamp=all_lamp)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(index, ensure_ascii=False))
    LAMP_OUT.write_text(json.dumps(lamp_cache, ensure_ascii=False))

    c = index["counts"]
    lab = c["labels"]
    print("─" * 56)
    print(f"실험 {c['experiments']} · 런 {c['runs']} · 그룹 {c['groups']} · 에피소드 {c['episodes']} "
          f"(deleted {c['deleted']}, 유효 {c['episodes'] - c['deleted']})")
    for ex in index["experiments"]:
        print(f"  - {ex['name']}: tries {ex['n_tries']} · eps {ex['n_episodes']} · groups {ex['n_groups']} "
              f"· tasks {len(ex['tasks'])} · runs {len(ex['runs'])}")
    print(f"라벨(비삭제): success {lab.get('success', 0)} · fail {lab.get('fail', 0)} "
          f"· unstable {lab.get('unstable', 0)} · unlabeled {lab.get('unlabeled', 0)}")
    print(f"라벨(전체)  : {index['counts']['labels_all']}")
    n_lamp = sum(1 for r in index["episodes"] if r["lamp_lit_frame"] is not None)
    n_press = sum(1 for r in index["episodes"] if r["is_press"])
    print(f"press 에피소드 {n_press} 중 램프 점등 검출 {n_lamp}")
    n_agg = sum(1 for r in index["episodes"] if r["agg_index"] is not None)
    print(f"agg_index 매핑 {n_agg} / {c['episodes']}")
    print(f"저장: {OUT} ({OUT.stat().st_size / 1024:.0f} KB), "
          f"{LAMP_OUT} ({LAMP_OUT.stat().st_size / 1024:.0f} KB)")
    print(f"소요 {time.time() - t0:.1f}s")

    # 자체 검증 — 기존 Red 실험(coffee_new01~15)의 불변량만 고정 확인
    ok = True
    red = [r for r in index["episodes"] if r["experiment"] == "NorRec_RW___Red"]
    red_kept = [r for r in red if not r["deleted"]]
    if len(red) != 200:
        print(f"[FAIL] Red 에피소드 200 기대, 실제 {len(red)}"); ok = False
    if len(red) - len(red_kept) != 16:
        print(f"[FAIL] Red deleted 16 기대, 실제 {len(red) - len(red_kept)}"); ok = False
    exp = {"success": 149, "fail": 31, "unstable": 4}
    red_lab = Counter(r["label"] for r in red_kept)
    if {k: red_lab.get(k, 0) for k in exp} != exp:
        print(f"[FAIL] Red 라벨 분포 {exp} 기대, 실제 {dict(red_lab)}"); ok = False
    if sorted({r["try_no"] for r in red}) != list(range(1, 16)):
        print(f"[FAIL] Red try 번호 1..15 기대"); ok = False
    print("검증 " + ("OK" if ok else "실패"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
