#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""clone_smoketest.py — VisualizeBetter "fresh clone user" harness (independent audit).

WHAT THIS IS
    An architect-side, worker-independent test of the one claim a public repo has to
    honour: *someone who clones this and follows the README can run the app.*
    It clones the repo at a given git ref into a temp dir (so only COMMITTED files
    come along), extracts the shell commands the English README actually prints,
    runs ONLY those, then starts `serve` and decides — with no browser — whether a
    real SPA is being served.

    Nothing in the repo is modified. The user's real data dir
    (%LOCALAPPDATA%\visualizebetter) is never touched: serve always gets --data-dir
    pointing into the temp run dir, and the harness verifies the real dir afterwards.

USAGE
    # baseline of the pre-fix state (grade "before")
    python clone_smoketest.py --ref cd40e42 --label pre-fix

    # grade the worker's fix (HEAD), README path only
    python clone_smoketest.py --ref HEAD --label post-fix

    # same, but also run the supplement steps the README omits (flagged in the
    # report as "README에 없어서 보충함") — use to prove *what* the missing step is
    python clone_smoketest.py --ref cd40e42 --label pre-fix-supplemented --supplement

    # other flags
    --repo PATH     repo to clone (default: auto-detected, else DEFAULT_REPO)
    --port N        serve port (default: first free port from 8965; README says
                    8765, which is usually already taken by the dev instance)
    --timeout-npm S per-step timeout for npm steps (default 900)
    --keep-clone    keep the temp clone (default: kept; --no-keep-clone to delete)

EXIT CODE
    0 = a fresh clone following the README alone reaches a working SPA + API
    1 = it does not (or the harness could not get that far)

OUTPUT
    <script dir>/_baselines/<label>.json   machine-readable baseline
    <script dir>/_baselines/<label>.txt    human-readable report
    <temp>/vb-clonetest-<label>/           clone + serve logs (evidence)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BASELINE_DIR = SCRIPT_DIR / "_baselines"
DEFAULT_REPO = Path(r"C:\Users\User\Desktop\VisualizeBetter")

# Env vars that break uv/npm on this machine (a mock CA left over from another
# project). Removing them restores a normal user's environment; every removal is
# recorded in the report so the deviation is visible.
CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")

# --- what a clone must / must not contain -----------------------------------
# "must not": gitignored local state or internal-only documents. If any of these
# arrive in a clone it is a public-exposure problem, not a convenience problem.
MUST_BE_ABSENT = [
    "frontend/dist",
    ".venv",
    "node_modules",
    "frontend/node_modules",
    "VISUALIZEBETTER_PROJECT_PLAN.txt",
    "reports",
    "CLAUDE.md",
    "PROGRESS.md",
    "serve.json",
    "build",
    "dist",
    ".pytest_cache",
    ".hypothesis",
    "frontend/test-results",
    "frontend/playwright-report",
]
# "must be present": everything needed to build + run + read the docs the README links.
MUST_BE_PRESENT = [
    "README.md",
    "README.ko.md",
    "LICENSE",
    "KNOWN_ISSUES.md",
    ".gitignore",
    "pyproject.toml",
    "uv.lock",
    "visualizebetter/__init__.py",
    "visualizebetter/cli.py",
    "visualizebetter/server.py",
    "visualizebetter/mcp_server.py",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/index.html",
    "frontend/vite.config.ts",
    "frontend/tsconfig.json",
    "frontend/tsconfig.app.json",
    "frontend/tsconfig.node.json",
    "frontend/src/main.tsx",
    "frontend/public/favicon.svg",
    "docs/handoff.md",
    "docs/filter-dsl.md",
    "docs/screenshot-self-visualization.png",
    ".github/workflows/ci.yml",
]
# Internal-governance markers that should not appear in a published tree's docs.
LEAK_MARKERS = ["STOP&ASK", "계획서", "Fable", "Orca 오케스트레이션", "PROJECT_PLAN"]


# ---------------------------------------------------------------- utilities
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def clean_env() -> tuple[dict, list[str]]:
    env = dict(os.environ)
    removed = [v for v in CA_ENV_VARS if v in env]
    for v in removed:
        env.pop(v, None)
    return env, removed


def free_port(start: int) -> int:
    for port in range(start, start + 200):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port")


def tail(text: str, n: int = 700) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]


def head(text: str, n: int = 300) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def run_cmd(cmd, cwd: Path, env: dict, timeout: int) -> dict:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=env, shell=isinstance(cmd, str),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        dur = time.monotonic() - t0
        return {
            "exit_code": proc.returncode, "seconds": round(dur, 1),
            "stdout_head": head(proc.stdout), "stdout_tail": tail(proc.stdout),
            "stderr_tail": tail(proc.stderr), "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None, "seconds": round(time.monotonic() - t0, 1),
            "stdout_head": head(exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")),
            "stdout_tail": "", "stderr_tail": f"TIMEOUT after {timeout}s", "timed_out": True,
        }


def http_probe(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, method="GET")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return {
                "url": url, "status": resp.status,
                "content_type": resp.headers.get("content-type", ""),
                "bytes": len(body), "body_head": head(body.decode("utf-8", "replace"), 400),
                "body_full": body.decode("utf-8", "replace"),
                "seconds": round(time.monotonic() - t0, 2), "error": None,
            }
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a body worth reading
        body = exc.read()
        return {
            "url": url, "status": exc.code,
            "content_type": exc.headers.get("content-type", "") if exc.headers else "",
            "bytes": len(body), "body_head": head(body.decode("utf-8", "replace"), 400),
            "body_full": body.decode("utf-8", "replace"),
            "seconds": round(time.monotonic() - t0, 2), "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — connection refused / reset / timeout
        return {
            "url": url, "status": None, "content_type": "", "bytes": 0,
            "body_head": "", "body_full": "",
            "seconds": round(time.monotonic() - t0, 2), "error": f"{type(exc).__name__}: {exc}",
        }


def port_listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_tree(pid: int) -> str:
    """Kill the serve process *and its children* — `uv run` spawns python as a child,
    so killing only the parent leaves an orphan holding the port."""
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True)
        return f"taskkill rc={r.returncode} {tail(r.stdout + r.stderr, 200)}"
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return "killpg SIGKILL"
    except Exception as exc:  # noqa: BLE001
        return f"killpg failed: {exc}"


def stray_processes(marker: str) -> list[str]:
    """Best effort: any process whose command line still mentions the temp clone."""
    if os.name != "nt":
        return []
    ps = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
        "ForEach-Object { \"$($_.ProcessId) $($_.Name)\" }"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
        # drop the query's own powershell.exe (its command line contains the marker)
        return [ln.strip() for ln in r.stdout.splitlines()
                if ln.strip() and "powershell.exe" not in ln.lower()]
    except Exception:  # noqa: BLE001
        return []


def dir_fingerprint(path: Path) -> dict:
    """Cheap before/after fingerprint so we can prove we didn't touch the real data dir."""
    if not path.exists():
        return {"exists": False}
    entries = {}
    try:
        for p in sorted(path.rglob("*")):
            try:
                entries[str(p.relative_to(path))] = round(p.stat().st_mtime, 3)
            except OSError:
                pass
    except OSError:
        pass
    return {"exists": True, "count": len(entries), "entries": entries}


# ------------------------------------------------------- README extraction
FENCE_RE = re.compile(r"^```([A-Za-z0-9_+-]*)\s*$")
SHELL_LANGS = {"bash", "sh", "shell", "console", "zsh"}


def extract_readme_commands(readme: str) -> tuple[list[dict], list[dict]]:
    """Pull the shell commands the README actually prints — nothing else.

    Returns (commands, blocks). Only fences tagged as a shell language count;
    ``` blocks holding ASCII diagrams or a User/Claude transcript are ignored, and
    so is the jsonc MCP-client snippet (config, not a command).
    """
    lines = readme.splitlines()
    blocks: list[dict] = []
    cur_lang, cur_start, buf = None, 0, []
    for i, line in enumerate(lines, start=1):
        m = FENCE_RE.match(line)
        if m and cur_lang is None:
            cur_lang, cur_start, buf = m.group(1).lower(), i, []
            continue
        if line.strip() == "```" and cur_lang is not None:
            blocks.append({"lang": cur_lang, "start_line": cur_start, "end_line": i,
                           "lines": buf, "is_shell": cur_lang in SHELL_LANGS})
            cur_lang = None
            continue
        if cur_lang is not None:
            buf.append((i, line))

    commands: list[dict] = []
    for bi, blk in enumerate(blocks):
        if not blk["is_shell"]:
            continue
        for lineno, raw in blk["lines"]:
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            s = re.sub(r"\s{2,}#.*$", "", s).strip()  # trailing "  # comment"
            if s.startswith("$ "):
                s = s[2:].strip()
            if s:
                commands.append({"cmd": s, "readme_line": lineno,
                                 "block": f"block#{bi + 1} ({blk['lang']}) L{blk['start_line']}-{blk['end_line']}"})
    return commands, blocks


def readme_link_targets(readme: str) -> list[str]:
    out = set(re.findall(r"\]\(\./([^)#\s]+)\)", readme))
    out |= set(re.findall(r'src="\./([^"]+)"', readme))
    return sorted(out)


# ------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--label", default="run")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--supplement", action="store_true",
                    help="also run the steps the README omits (flagged as such)")
    ap.add_argument("--skip-frontend-build", action="store_true",
                    help="deliberately skip the README's npm steps, to grade the README's own "
                         "claim that skipping them is non-fatal and yields a 503 naming the "
                         "missing command. Expected root_kind = GUIDANCE_PAGE.")
    ap.add_argument("--timeout-npm", type=int, default=900)
    ap.add_argument("--timeout-step", type=int, default=600)
    ap.add_argument("--serve-ready-timeout", type=int, default=420)
    ap.add_argument("--keep-clone", action="store_true", default=True)
    ap.add_argument("--no-keep-clone", dest="keep_clone", action="store_false")
    args = ap.parse_args()

    repo = Path(args.repo).resolve() if args.repo else None
    if repo is None:
        probe = SCRIPT_DIR
        for cand in [probe, *probe.parents]:
            if (cand / "pyproject.toml").exists() and (cand / ".git").exists():
                repo = cand
                break
        repo = repo or DEFAULT_REPO
    if not (repo / ".git").exists():
        print(f"FATAL: {repo} is not a git repo", file=sys.stderr)
        return 1

    env, removed_env = clean_env()
    port = args.port or free_port(8965)
    run_dir = Path(tempfile.gettempdir()) / f"vb-clonetest-{args.label}"
    clone = run_dir / "VisualizeBetter"
    data_dir = run_dir / "serve-data"
    logs = run_dir / "logs"

    report: dict = {
        "harness": "clone_smoketest.py",
        "harness_path": str(Path(__file__).resolve()),
        "label": args.label,
        "started": now_iso(),
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "cwd_repo": str(repo),
        },
        "env_vars_removed_for_uv_npm": removed_env,
        "port": port,
        "run_dir": str(run_dir),
        "supplement_enabled": args.supplement,
        "steps": [],
        "deviations": [],
        "probes": {},
        "clone_check": {},
        "prereq": {},
        "readme": {},
    }

    def add_step(**kw):
        report["steps"].append(kw)
        st = kw.get("status")
        print(f"  [{st}] {kw.get('id')}: {kw.get('cmd')} ({kw.get('seconds', '?')}s)")

    # ---------------------------------------------------- 0. prerequisites
    print("== 0. prerequisites ==")
    for name, cmd in (("python", "python --version"), ("uv", "uv --version"),
                      ("node", "node --version"), ("npm", "npm --version"),
                      ("git", "git --version")):
        r = run_cmd(cmd, SCRIPT_DIR, env, 60)
        report["prereq"][name] = {
            "available": r["exit_code"] == 0,
            "version": (r["stdout_head"] or r["stderr_tail"]).strip().splitlines()[0] if (r["stdout_head"] or r["stderr_tail"]) else "",
        }
        print(f"  {name}: {report['prereq'][name]}")

    # ---------------------------------------------------------- 1. clone
    print(f"== 1. clone {repo} @ {args.ref} ==")
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    logs.mkdir(exist_ok=True)

    sha = run_cmd(["git", "-C", str(repo), "rev-parse", args.ref], SCRIPT_DIR, env, 60)
    report["ref"] = {"requested": args.ref, "resolved_sha": sha["stdout_tail"].strip()}
    subj = run_cmd(["git", "-C", str(repo), "log", "-1", "--format=%h %s", args.ref], SCRIPT_DIR, env, 60)
    report["ref"]["subject"] = subj["stdout_tail"].strip()
    dirty = run_cmd(["git", "-C", str(repo), "status", "--porcelain"], SCRIPT_DIR, env, 60)
    report["ref"]["source_worktree_dirty"] = [
        ln for ln in dirty["stdout_tail"].splitlines() if ln.strip()
    ]

    r = run_cmd(["git", "clone", "--no-hardlinks", "--quiet", str(repo), str(clone)],
                run_dir, env, 300)
    add_step(id="clone", cmd=f"git clone {repo} -> {clone}", source="harness (stands in for the README's `git clone <github url>`)",
             status="PASS" if r["exit_code"] == 0 else "FAIL", **r)
    if r["exit_code"] != 0:
        return finish(report, 1, args)
    r = run_cmd(["git", "-C", str(clone), "checkout", "--quiet", "--detach", report["ref"]["resolved_sha"]],
                run_dir, env, 120)
    add_step(id="checkout-ref", cmd=f"git checkout --detach {args.ref}",
             source="harness (pins the graded state)",
             status="PASS" if r["exit_code"] == 0 else "FAIL", **r)

    # -------------------------------------------- 2. what came / didn't come
    print("== 2. clone content check ==")
    present_ok, present_missing = [], []
    for rel in MUST_BE_PRESENT:
        (present_ok if (clone / rel).exists() else present_missing).append(rel)
    absent_ok, absent_leaked = [], []
    for rel in MUST_BE_ABSENT:
        (absent_leaked if (clone / rel).exists() else absent_ok).append(rel)

    readme_text = (clone / "README.md").read_text(encoding="utf-8") if (clone / "README.md").exists() else ""
    link_targets = readme_link_targets(readme_text)
    broken_links = [t for t in link_targets if not (clone / t).exists()]

    # Every committed doc, not just the top README: a doc that points at a file the
    # clone never received is the same class of defect as the missing SPA bundle —
    # the repo promises something a fresh clone does not have.
    md_broken: list[dict] = []
    for md in sorted(clone.rglob("*.md")):
        if ".git" in md.parts or "node_modules" in md.parts:
            continue
        try:
            txt = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        refs = set(re.findall(r"\]\(([^)#\s]+)\)", txt)) | set(re.findall(r'src="([^"]+)"', txt))
        refs |= {m for m in re.findall(r"`([^`\s]+\.(?:md|py|ts|tsx|json|toml|lock))`", txt)}
        for ref in sorted(refs):
            if ref.startswith(("http", "#", "mailto:", "/")):
                continue
            # a doc may cite a path relative to itself OR to the repo root
            if not ((md.parent / ref).exists() or (clone / ref).exists()):
                md_broken.append({"doc": str(md.relative_to(clone)), "missing_ref": ref})

    leaks = []
    for p in clone.rglob("*"):
        if ".git" in p.parts or not p.is_file():
            continue
        if p.suffix.lower() not in {".md", ".txt", ".py", ".ts", ".tsx", ".json", ".yml", ".yaml", ".toml"}:
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = [m for m in LEAK_MARKERS if m in txt]
        if hits:
            leaks.append({"file": str(p.relative_to(clone)), "markers": hits})

    tracked_n = None
    try:  # full output, not the truncated tail run_cmd keeps
        tp = subprocess.run(["git", "-C", str(clone), "ls-files"], capture_output=True,
                            text=True, env=env, timeout=120)
        tracked_n = len([l for l in tp.stdout.splitlines() if l.strip()])
    except Exception:  # noqa: BLE001
        pass
    report["clone_check"] = {
        "tracked_file_count": tracked_n,
        "expected_present_ok": present_ok,
        "expected_present_MISSING": present_missing,
        "must_be_absent_ok": absent_ok,
        "LEAKED_into_clone": absent_leaked,
        "readme_relative_links": link_targets,
        "readme_broken_links": broken_links,
        "committed_docs_broken_refs": md_broken,
        "internal_marker_hits": leaks,
    }
    print(f"  missing-but-needed: {present_missing or 'none'}")
    print(f"  leaked (should not be in a clone): {absent_leaked or 'none'}")
    print(f"  broken README links: {broken_links or 'none'}")
    print(f"  internal-marker files: {[l['file'] for l in leaks] or 'none'}")

    # ------------------------------------------- 3. extract README commands
    print("== 3. README command extraction ==")
    cmds, blocks = extract_readme_commands(readme_text)
    report["readme"] = {
        "lines": len(readme_text.splitlines()),
        "shell_blocks": [{k: v for k, v in b.items() if k != "lines"} for b in blocks],
        "extracted_commands": cmds,
        "mentions": {
            label: bool(re.search(pat, readme_text, re.I))
            for label, pat in (
                ("npm ci", r"npm ci"),
                ("npm run build", r"npm run build"),
                ("prerequisit", r"prerequisit|requirements"),
                ("node", r"node\.js|node\s*\|"),
                # "| Python | 3.11 or newer |" must count, so allow table cruft between
                ("python 3", r"python[^\n]{0,24}3\.\d+"),
                ("uv run", r"uv run"),
                ("git clone", r"git clone"),
            )
        },
    }
    for c in cmds:
        print(f"  L{c['readme_line']}: {c['cmd']}")

    # ------------------------------------------------- 4. run README steps
    print("== 4. execute README steps (README-only) ==")
    cwd = clone
    serve_cmd_seen = None
    for i, c in enumerate(cmds, start=1):
        cmd = c["cmd"]
        src = f"README {c['block']} L{c['readme_line']}"
        low = cmd.lower()
        if low.startswith("git clone"):
            add_step(id=f"readme-{i}", cmd=cmd, source=src, status="SUBSTITUTED", seconds=0,
                     exit_code=0, note="the harness already cloned the LOCAL repo at the graded ref "
                                       "instead of the GitHub URL — committed content is identical",
                     stdout_head="", stdout_tail="", stderr_tail="", timed_out=False)
            continue
        if re.fullmatch(r"cd\s+\S+", cmd):
            target = cmd.split(None, 1)[1]
            if target in {"VisualizeBetter", "visualizebetter"} and cwd == clone:
                add_step(id=f"readme-{i}", cmd=cmd, source=src, status="SUBSTITUTED", seconds=0,
                         exit_code=0, note="already inside the clone", stdout_head="",
                         stdout_tail="", stderr_tail="", timed_out=False)
                continue
            new = (cwd / target).resolve()
            ok = new.is_dir()
            cwd = new if ok else cwd
            add_step(id=f"readme-{i}", cmd=cmd, source=src, status="PASS" if ok else "FAIL",
                     seconds=0, exit_code=0 if ok else 1, note=f"cwd -> {cwd}",
                     stdout_head="", stdout_tail="", stderr_tail="" if ok else "no such dir",
                     timed_out=False)
            continue
        if "visualizebetter serve" in low:
            serve_cmd_seen = (cmd, src) if serve_cmd_seen is None else serve_cmd_seen
            add_step(id=f"readme-{i}", cmd=cmd, source=src, status="DEFERRED", seconds=0,
                     exit_code=None, note="long-running; started in step 5 with recorded deviations",
                     stdout_head="", stdout_tail="", stderr_tail="", timed_out=False)
            continue
        if args.skip_frontend_build and low.startswith("npm"):
            add_step(id=f"readme-{i}", cmd=cmd, source=src, status="SKIPPED-ON-PURPOSE",
                     seconds=0, exit_code=None,
                     note="--skip-frontend-build: grading the README's claim that omitting "
                          "this step is non-fatal", stdout_head="", stdout_tail="",
                     stderr_tail="", timed_out=False)
            continue
        to = args.timeout_npm if low.startswith("npm") else args.timeout_step
        r = run_cmd(cmd, cwd, env, to)
        add_step(id=f"readme-{i}", cmd=cmd, source=src,
                 status="PASS" if r["exit_code"] == 0 else "FAIL", cwd=str(cwd), **r)

    # optional supplement — everything here is NOT in the README
    if args.supplement:
        print("== 4b. supplement steps (README 에 없어서 보충함) ==")
        for sid, cmd, wd in (("sup-npm-ci", "npm ci", clone / "frontend"),
                             ("sup-npm-build", "npm run build", clone / "frontend")):
            already = any(s["cmd"] == cmd and s["status"] == "PASS" for s in report["steps"])
            if already:
                add_step(id=sid, cmd=cmd, source="SUPPLEMENT (README 에 없어서 보충함)",
                         status="SKIP", seconds=0, exit_code=0,
                         note="the README already prescribed this step", stdout_head="",
                         stdout_tail="", stderr_tail="", timed_out=False)
                continue
            r = run_cmd(cmd, wd, env, args.timeout_npm)
            add_step(id=sid, cmd=cmd, source="SUPPLEMENT (README 에 없어서 보충함)",
                     status="PASS" if r["exit_code"] == 0 else "FAIL", cwd=str(wd), **r)

    # ------------------------------------------------- 5. serve + probes
    print("== 5. serve and probe ==")
    real_data = Path(os.environ.get("LOCALAPPDATA", "")) / "visualizebetter"
    before_fp = dir_fingerprint(real_data)

    serve_source = serve_cmd_seen[1] if serve_cmd_seen else "harness (README printed no serve command!)"
    serve_argv = ["uv", "run", "visualizebetter", "serve", "--port", str(port),
                  "--no-open", "--data-dir", str(data_dir)]
    report["deviations"] = [
        {"what": f"--port {port} instead of the README's 8765",
         "why": "8765 is normally held by the developer's live instance; the port does not "
                "affect the verdict (Host allow-list is derived from the port)"},
        {"what": "--no-open added", "why": "README's command opens a real browser tab; the harness is headless. "
                                          "Marked as a deviation: NOT in the README."},
        {"what": f"--data-dir {data_dir} added",
         "why": "keeps the real %LOCALAPPDATA%/visualizebetter untouched. NOT in the README."},
    ]
    out_f = (logs / "serve.out.log").open("w", encoding="utf-8")
    err_f = (logs / "serve.err.log").open("w", encoding="utf-8")
    t0 = time.monotonic()
    creation = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(serve_argv, cwd=str(clone), env=env, stdout=out_f,
                            stderr=err_f, creationflags=creation,
                            start_new_session=(os.name != "nt"))
    ready, died = False, False
    base = f"http://127.0.0.1:{port}"
    while time.monotonic() - t0 < args.serve_ready_timeout:
        if proc.poll() is not None:
            died = True
            break
        p = http_probe(f"{base}/graph.json", timeout=5)
        if p["status"] is not None:
            ready = True
            break
        time.sleep(2)
    startup = round(time.monotonic() - t0, 1)

    probes = {}
    if ready:
        for name, path in (("root", "/"), ("index_html", "/index.html"),
                           ("graph_json", "/graph.json"), ("mcp", "/mcp")):
            probes[name] = http_probe(base + path, timeout=15)
        # (b) is the root response an actual SPA? resolve its first script asset.
        rootp = probes["root"]
        asset = None
        m = re.search(r'<script[^>]+src="([^"]+)"', rootp.get("body_full", ""))
        if m:
            asset = http_probe(base + m.group(1) if m.group(1).startswith("/") else f"{base}/{m.group(1)}", timeout=15)
            probes["root_script_asset"] = asset

        body = rootp.get("body_full", "")
        if rootp["status"] == 404:
            kind = "BARE_404 — browser lands on a plain Not Found, nothing actionable"
        elif rootp["status"] is None:
            kind = f"NO_RESPONSE ({rootp['error']})"
        elif "text/html" in rootp["content_type"] and '<div id="root"' in body and m:
            if asset and asset["status"] == 200 and asset["bytes"] > 1000:
                kind = "REAL_SPA — index.html + a served JS bundle"
            else:
                kind = "SPA_SHELL_BROKEN — index.html present but its script asset does not load"
        elif rootp["status"] in (200, 503) and "text/plain" in rootp["content_type"] and re.search(r"npm (ci|run build)", body):
            kind = f"GUIDANCE_PAGE ({rootp['status']}) — plain text naming the missing build command"
        elif rootp["status"] == 200 and rootp["bytes"] < 200:
            kind = "EMPTY_200 — 200 with a near-empty body"
        else:
            kind = f"OTHER (status={rootp['status']}, ct={rootp['content_type']!r})"
        gj = probes["graph_json"]
        gj_ok = False
        if gj["status"] == 200:
            try:
                d = json.loads(gj["body_full"])
                gj_ok = all(k in d for k in ("nodes", "edges", "findings", "seq"))
            except Exception:  # noqa: BLE001
                gj_ok = False
        probes["verdict_root_kind"] = kind
        probes["graph_json_is_api"] = gj_ok
        probes["browser_usable"] = kind.startswith("REAL_SPA")
    else:
        probes["verdict_root_kind"] = "SERVE_NEVER_CAME_UP" + (" (process exited)" if died else " (timeout)")
        probes["graph_json_is_api"] = False
        probes["browser_usable"] = False

    # ---- teardown: no orphans
    kill_note = kill_tree(proc.pid) if proc.poll() is None else f"already exited rc={proc.returncode}"
    for _ in range(20):
        if not port_listening(port):
            break
        time.sleep(0.5)
    out_f.close()
    err_f.close()
    still = port_listening(port)
    strays = stray_processes(run_dir.name)
    report["serve"] = {
        "argv": serve_argv, "source_of_command": serve_source,
        "startup_seconds_incl_dependency_install": startup,
        "became_ready": ready, "exited_early": died,
        "stdout_tail": tail((logs / "serve.out.log").read_text(encoding="utf-8", errors="replace"), 900),
        "stderr_tail": tail((logs / "serve.err.log").read_text(encoding="utf-8", errors="replace"), 1400),
        "teardown": kill_note, "port_still_listening_after_kill": still,
        "stray_processes_mentioning_run_dir": strays,
    }
    add_step(id="serve", cmd=" ".join(serve_argv), source=serve_source,
             status="PASS" if ready else "FAIL", seconds=startup,
             exit_code=0 if ready else 1, note=probes["verdict_root_kind"],
             stdout_head="", stdout_tail="", stderr_tail=report["serve"]["stderr_tail"][-400:],
             timed_out=False)
    report["probes"] = probes

    after_fp = dir_fingerprint(real_data)
    be, ae = before_fp.get("entries", {}), after_fp.get("entries", {})
    added = sorted(set(ae) - set(be))
    removed = sorted(set(be) - set(ae))
    touched = sorted(k for k in set(be) & set(ae) if be[k] != ae[k])
    report["real_data_dir_guard"] = {
        "path": str(real_data), "before": before_fp.get("count"), "after": after_fp.get("count"),
        "unchanged": before_fp == after_fp,
        "added": added, "removed": removed, "mtime_touched": touched,
        # An mtime-only change to the snapshot DB with nothing added/removed is the
        # developer's own live `serve` auto-snapshotting (measured: it rewrites
        # visualizebetter.sqlite3 about once a minute with no harness running). The
        # harness itself never points at this directory — it always passes --data-dir.
        "harness_wrote_here": bool(added or removed),
    }

    usable = bool(probes.get("browser_usable")) and bool(probes.get("graph_json_is_api"))
    if args.skip_frontend_build:
        # this variant grades a different claim: the README says omitting the build
        # is non-fatal and the browser gets a 503 naming the missing command.
        ok = (str(probes.get("verdict_root_kind", "")).startswith("GUIDANCE_PAGE")
              and bool(probes.get("graph_json_is_api")))
    else:
        ok = usable
    report["verdict"] = {
        "mode": "skip-frontend-build (grades the README's 503 claim)" if args.skip_frontend_build
                else ("README + supplement" if args.supplement else "README-only"),
        "readme_only_path_gives_working_app": usable and not args.supplement and not args.skip_frontend_build,
        "app_usable_in_this_run": usable,
        "expectation_met": ok,
        "root_kind": probes.get("verdict_root_kind"),
        "supplement_used": args.supplement,
    }
    return finish(report, 0 if ok else 1, args)


def finish(report: dict, code: int, args) -> int:
    report["finished"] = now_iso()
    report["exit_code"] = code
    # body_full is only needed for classification; keeping a 1.3 MB JS bundle in the
    # baseline would make the JSON useless for diffing two runs.
    for pr in report.get("probes", {}).values():
        if isinstance(pr, dict):
            pr.pop("body_full", None)
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    jpath = BASELINE_DIR / f"{args.label}.json"
    jpath.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    tpath = BASELINE_DIR / f"{args.label}.txt"
    tpath.write_text(render_txt(report), encoding="utf-8")
    if not args.keep_clone:
        shutil.rmtree(report["run_dir"], ignore_errors=True)
    print(f"\nbaseline JSON: {jpath}\nbaseline TXT : {tpath}\nexit {code}")
    return code


def render_txt(r: dict) -> str:
    L: list[str] = []
    A = L.append
    A("=" * 78)
    A(f"VisualizeBetter fresh-clone smoke test — label={r['label']}")
    A(f"started {r['started']}  finished {r.get('finished')}  exit={r.get('exit_code')}")
    A(f"repo {r['host']['cwd_repo']}  ref {r.get('ref', {}).get('requested')} -> {r.get('ref', {}).get('subject')}")
    A(f"host {r['host']['platform']}  harness python {r['host']['python']}")
    A(f"env removed for uv/npm: {r['env_vars_removed_for_uv_npm']}")
    A(f"supplement steps enabled: {r['supplement_enabled']}")
    dirty = r.get("ref", {}).get("source_worktree_dirty") or []
    if dirty:
        A(f"NOTE source worktree had {len(dirty)} uncommitted change(s) — NOT in the clone:")
        for d in dirty:
            A(f"       {d}")
    A("=" * 78)
    A("")
    A("-- prerequisites --------------------------------------------------------")
    for k, v in r["prereq"].items():
        A(f"  {k:7} available={v['available']!s:5} {v['version']}")
    m = r.get("readme", {}).get("mentions", {})
    A(f"  README states prerequisites? {m.get('prerequisit')}   python-version: {m.get('python 3')}   node: {m.get('node')}")
    A("")
    A("-- README-extracted commands -------------------------------------------")
    for c in r.get("readme", {}).get("extracted_commands", []):
        A(f"  L{c['readme_line']:>4}  {c['cmd']}")
    if not r.get("readme", {}).get("extracted_commands"):
        A("  (none)")
    A(f"  README mentions `npm ci`: {m.get('npm ci')}   `npm run build`: {m.get('npm run build')}")
    A("")
    A("-- steps ----------------------------------------------------------------")
    A(f"  {'id':14} {'status':11} {'secs':>7}  cmd / source")
    for s in r["steps"]:
        A(f"  {str(s.get('id'))[:14]:14} {str(s.get('status'))[:11]:11} {str(s.get('seconds')):>7}  {s.get('cmd')}")
        A(f"  {'':14} {'':11} {'':>7}  src: {s.get('source')}")
        if s.get("note"):
            A(f"  {'':14} {'':11} {'':>7}  note: {s['note']}")
        if s.get("stderr_tail"):
            A(f"  {'':14} {'':11} {'':>7}  stderr: {head(s['stderr_tail'], 220)}")
    A("")
    A("-- deviations from the README (harness additions) ------------------------")
    for d in r.get("deviations", []):
        A(f"  * {d['what']}")
        A(f"      why: {d['why']}")
    A("")
    A("-- serve --------------------------------------------------------------")
    sv = r.get("serve", {})
    A(f"  argv: {' '.join(sv.get('argv', []))}")
    A(f"  came up: {sv.get('became_ready')}  startup {sv.get('startup_seconds_incl_dependency_install')}s "
      f"(includes uv dependency install on a fresh clone)")
    A(f"  teardown: {sv.get('teardown')}  port still listening after kill: {sv.get('port_still_listening_after_kill')}")
    A(f"  stray processes mentioning the run dir: {sv.get('stray_processes_mentioning_run_dir')}")
    if sv.get("stderr_tail"):
        A("  server stderr tail:")
        for ln in sv["stderr_tail"].splitlines()[-12:]:
            A(f"      {ln}")
    A("")
    A("-- HTTP probes (browser-free verdict) ----------------------------------")
    p = r.get("probes", {})
    for name in ("root", "index_html", "graph_json", "mcp", "root_script_asset"):
        pr = p.get(name)
        if not pr:
            continue
        A(f"  {name:18} {str(pr['status']):>5}  {pr['bytes']:>8}B  {pr['content_type'][:40]:40} {pr['url']}")
        if pr.get("body_head"):
            A(f"  {'':18} body: {pr['body_head'][:220].replace(chr(10), ' | ')}")
        if pr.get("error"):
            A(f"  {'':18} error: {pr['error']}")
    A(f"  root classification : {p.get('verdict_root_kind')}")
    A(f"  /graph.json is API  : {p.get('graph_json_is_api')}")
    A(f"  browser usable      : {p.get('browser_usable')}")
    A("")
    A("-- clone content ------------------------------------------------------")
    cc = r.get("clone_check", {})
    A(f"  tracked files: {cc.get('tracked_file_count')}")
    A(f"  NEEDED BUT MISSING from the clone : {cc.get('expected_present_MISSING') or 'none'}")
    A(f"  LEAKED into the clone (must not be): {cc.get('LEAKED_into_clone') or 'none'}")
    A(f"  README relative links checked      : {len(cc.get('readme_relative_links', []))} -> broken: {cc.get('readme_broken_links') or 'none'}")
    A(f"  committed docs pointing at files the clone lacks:")
    for b in cc.get("committed_docs_broken_refs", []) or [{"doc": "(none)", "missing_ref": ""}]:
        A(f"      {b['doc']} -> {b['missing_ref']}")
    A(f"  files carrying internal markers    : {[h['file'] for h in cc.get('internal_marker_hits', [])] or 'none'}")
    A("")
    A("-- real user data dir guard -------------------------------------------")
    g = r.get("real_data_dir_guard", {})
    A(f"  {g.get('path')}  entries before={g.get('before')} after={g.get('after')} unchanged={g.get('unchanged')}")
    A(f"  harness wrote here: {g.get('harness_wrote_here')}   added={g.get('added')} removed={g.get('removed')}")
    A(f"  mtime-only touches (the developer's own live serve auto-snapshots): {g.get('mtime_touched')}")
    A("")
    A("-- VERDICT ------------------------------------------------------------")
    v = r.get("verdict", {})
    A(f"  mode                                  : {v.get('mode')}")
    A(f"  expectation met for this mode         : {v.get('expectation_met')}")
    A(f"  README-only path yields a working app : {v.get('readme_only_path_gives_working_app')}")
    A(f"  app usable in this run                : {v.get('app_usable_in_this_run')} (supplement={v.get('supplement_used')})")
    A(f"  root_kind                             : {v.get('root_kind')}")
    A(f"  exit code                             : {r.get('exit_code')}")
    A("")
    A(f"evidence kept in: {r.get('run_dir')}")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
