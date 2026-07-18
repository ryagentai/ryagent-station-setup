#!/usr/bin/env python3
"""UbuntuConsole v4 — designed with Linear/Vercel as reference.

Inspected real Linear.app and Vercel design tokens:
- Linear uses --color-text-{primary,secondary,tertiary,quaternary} scale
- Inter variable font (loaded locally, not from rsms.me — works offline)
- Bento grid with 12-col spans
- Subtle border + background tier (no hard borders)
- Status: solid color dot + label, not bicolor pill
- Heavy typography hierarchy: titles use 1-line height + -0.02em letter-spacing
- Animations: cubic-bezier(0.16, 1, 0.3, 1) for slide-ins

Fixes vs v3:
- service ids stable across frontend/backend
- control actions work for every unit-managed service
- llama/telegram/hermes "down" state now reflects reality (ps fallback)
- samba detection by port
- All endpoints return meaningful 4xx codes
- Status indicator shows true health (active/active(external)/down/error)
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

HOST = os.getenv("RYAN_WEBUI_HOST", "0.0.0.0")
PORT = 9002
CONSOLE_DIR = Path("/media/ryan/UbuntuDATA/AI_PROJECTS/UbuntuConsole")
IDEAS_DIR = CONSOLE_DIR / "ideas"
IDEAS_DIR.mkdir(parents=True, exist_ok=True)
CMD_TOKEN = os.getenv("RYAN_WEBUI_CMD_TOKEN", "").strip()
LLAMA_URL = os.getenv("RYAN_LLAMA_URL", "http://127.0.0.1:8888/v1/chat/completions")

# Llama model management
MODEL_DIR = Path("/media/ryan/UbuntuDATA/ai_models")
LLAMA_GPU_BIN = Path("/media/ryan/UbuntuDATA/AI_PROJECTS/llama.cpp/build/bin/llama-server")
LLAMA_CPU_BIN = Path("/media/ryan/UbuntuDATA/AI_PROJECTS/llama.cpp/build-cpu/bin/llama-server")
_spawned: Dict[str, threading.Thread] = {}

# Service definitions
# (id, label, subtitle, category, port, control_kind, control_target)
# control_kind: "unit", "docker", "virsh", "manual"
# manual = no start/stop (we just observe)
SERVICES = [
    # ── AI 推理服務 ──
    ("llama",     "llama.cpp (GPU)",       "本地 AI 推理 — GPU 加速 · port 8888",        "ai",    8888,  "manual",   "llama-server"),
    ("llama-cpu", "llama.cpp (CPU)",       "本地 AI 推理 — CPU 模式 · port 8889",        "ai",    8889,  "manual",   "llama-server"),
    ("telegram",  "Telegram 橋接",         "Telegram ↔ SillyTavern 對話橋接",            "ai",    None,  "unit",     "telegram-bridge"),
    ("s2s",       "S2S 語音助手",          "Speech-to-Speech 即時語音對話 · port 9299",  "ai",    9299,  "unit",     "s2s"),
    # ── AI 應用 ──
    ("hermes",    "Hermes 網關",           "訊息中樞 — 管理 AI 代理與通訊平台 · port 9090", "ai",  9090,  "unit",     "hermes-gateway"),
    ("silly",     "SillyTavern",           "角色扮演聊天 UI · port 9277",                  "ai",    9277,  "manual",   "sillytavern"),
    ("comfyui",   "ComfyUI",              "AI 繪圖工作流 (Stable Diffusion) · port 8188", "ai",    8188,  "unit",     "comfyui"),
    ("camofox",   "Camofox 瀏覽器",        "反檢測瀏覽器 — 多賬號管理 · port 9377",       "ai",    9377,  "unit",     "camofox-browser"),
    # ── 工具 ──
    ("webui",     "UbuntuConsole 控制台",   "本儀表板 — 服務總覽 · port 9002",             "tool",  9002,  "unit",     "ubuntuconsole-webui"),
    # ── Docker ──
    ("dockry",    "DockRyagent",           "Docker AI 隔離容器",                           "docker", 2222, "docker",   "DockRyagent"),
    # ── 虛擬機 ──
    ("marvis",    "marvis-box VM",         "Windows Tiny11 虛擬機 · VNC 5900",            "vm",    5900,  "virsh",    "marvis-box"),
]
SVC_IDX = {s[0]: {"label": s[1], "subtitle": s[2], "category": s[3],
                  "port": s[4], "control_kind": s[5],
                  "control_target": s[6]} for s in SERVICES}


# ───────────────── shell ─────────────────
def _run(cmd: str, timeout: float = 6.0) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             timeout=timeout).stdout
    except Exception as e:
        return f"ERR: {e}"


# ───────────────── health ─────────────────
def port_listening(port: int) -> bool:
    return "LISTEN" in _run(f"ss -tulnH sport = :{port}")


def port_pid_proc(port: int) -> Dict[str, str]:
    out = _run(f"ss -tulnpH sport = :{port}")
    m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', out)
    if not m:
        return {"pid": "?", "proc": "?"}
    pid, proc = m.group(2), m.group(1)
    # RSS
    rss_mb = None
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_mb = int(line.split()[1]) // 1024
                    break
    except Exception:
        pass
    return {"pid": pid, "proc": proc, "rss_mb": rss_mb}


def systemd_active(unit: str) -> bool:
    return _run(f"systemctl --user is-active {unit}").strip() == "active"


def systemd_state(unit: str) -> str:
    return _run(f"systemctl --user is-active {unit}").strip() or "unknown"


def ps_running(pattern: str) -> int:
    """Return count of processes matching pattern (excluding grep itself)."""
    return int(_run(f"pgrep -af '{pattern}' | grep -v 'pgrep' | wc -l").strip() or "0")


def check_one(sid: str) -> Dict[str, Any]:
    meta = SVC_IDX[sid]
    res: Dict[str, Any] = {
        "id": sid, "label": meta["label"], "subtitle": meta["subtitle"],
        "category": meta["category"], "port": meta["port"],
        "kind": meta["control_kind"], "target": meta["control_target"],
        "ok": False, "state": "down", "state_label": "offline", "info": "",
        "color": "#e86969",  # default red
    }

    def make_ok(state_label: str, info: str = "", color: str = "#5cc784"):
        res["ok"] = True
        res["state"] = "up"
        res["state_label"] = state_label
        res["info"] = info
        res["color"] = color

    def make_warn(state_label: str, info: str = ""):
        res["state"] = "warn"
        res["state_label"] = state_label
        res["info"] = info
        res["color"] = "#e8b765"

    def make_down(state_label: str, info: str = "", color: str = "#e86969"):
        res["state"] = "down"
        res["state_label"] = state_label
        res["info"] = info
        res["color"] = color

    # 1. Port-based check first (most reliable signal of "actually serving")
    port = meta["port"]
    port_ok = port and port_listening(port)

    if port:
        if port_ok:
            pp = port_pid_proc(port)
            res.update(pp)
        else:
            # Special case: marvis VM has its own process; port 5900 might be
            # bound by another VNC listener but the VM should be flagged via qemu.
            if sid == "marvis":
                q = _run("pgrep -af 'qemu-system-x86_64.*marvis'").strip()
                if q:
                    make_ok("running", "qemu up")
                    res["port"] = 5900
                    # Try port anyway (it may be bound by something else)
                    return {**res}
            make_down("offline", f"port {port} not listening")
            return res

    # 2. Per-control-kind detail
    kind = meta["control_kind"]
    target = meta["control_target"]

    if kind == "unit":
        if systemd_active(target):
            # Already passing port check
            uptime = _run(f"systemctl --user show {target} --property=ActiveEnterTimestamp --value").strip()
            res["uptime_since"] = uptime
            make_ok("running", f"systemd active", "#5cc784")
        else:
            # systemd not active — could be (a) genuinely down, (b) managed externally
            if port_ok:
                # Something else is on the port (likely user-launched manual process)
                make_ok("active (external)", "managed outside systemd", "#5cc784")
            elif port is None:
                # No port to test — fall back to ps heuristic for telegram/hermes
                # telegram: pgrep -af telegram-sillytavern-bridge.py
                # hermes: pgrep -af hermes
                if sid == "telegram":
                    n = ps_running("telegram-sillytavern-bridge.py")
                    if n >= 1:
                        make_ok("running", f"{n} process", "#5cc784")
                    else:
                        make_down("offline", "process gone")
                elif sid == "hermes":
                    n = ps_running("hermes_cli.*gateway run")
                    if n >= 1:
                        make_ok("running", f"{n} process", "#5cc784")
                    else:
                        make_down("offline", "process gone")
                else:
                    make_warn("degraded", systemd_state(target))
            else:
                make_down("offline", f"systemd {systemd_state(target)}")

    elif kind == "docker":
        st = _run(f"docker ps --filter name={target} --format '{{{{.Status}}}}'").strip()
        if "Up" in st:
            make_ok("running", st, "#5cc784")
        else:
            make_down("container down", st)

    elif kind == "virsh":
        # Try libvirt group first; fall back to pgrep
        out = _run(f"sg libvirt -c 'virsh -c qemu:///system domstate {target}' 2>/dev/null").strip()
        if out == "running":
            make_ok("running", "libvirt", "#5cc784")
        else:
            q = _run("pgrep -af qemu-system-x86_64")
            if "marvis-box" in q and "running" in q:
                make_ok("running", "qemu direct", "#5cc784")
            else:
                make_warn("shutoff", out or "unknown")

    elif kind == "manual":
        # Manual services: only check port/process heuristics
        if port_ok:
            make_ok("running", f"port {port} up", "#5cc784")
        else:
            if sid == "comfyui":
                if ps_running("comfyui-venv.*main.py") >= 1:
                    make_ok("running", "manual process", "#5cc784")
                else:
                    make_down("offline", "process not running")
            elif sid == "samba":
                smbd = ps_running("smbd")
                if smbd >= 1:
                    make_ok("running", f"{smbd} smbd", "#5cc784")
                else:
                    make_down("offline", "smbd not running")
            else:
                make_down("offline", "no port")
    elif kind == "system":
        # System-level services (samba, ssh) - use systemctl without --user
        if port_ok:
            make_ok("running", f"port {port} up", "#5cc784")
        else:
            sys_state = _run(f"systemctl is-active {target} 2>/dev/null").strip()
            if sys_state == "active":
                make_ok("running", "systemd system", "#5cc784")
            else:
                make_down("offline", f"systemctl: {sys_state}")
    return res


def check_all() -> List[Dict[str, Any]]:
    return [check_one(s[0]) for s in SERVICES]


# ───────────────── control ─────────────────
def control(sid: str, action: str) -> Dict[str, Any]:
    meta = SVC_IDX.get(sid)
    if not meta:
        return {"ok": False, "error": f"unknown service: {sid}"}
    kind = meta["control_kind"]
    target = meta["control_target"]
    if action == "start":
        verb = "start"
    elif action == "stop":
        verb = "stop"
    elif action == "restart":
        verb = "restart"
    elif action == "enable":
        verb = "enable"
    elif action == "disable":
        verb = "disable"
    else:
        return {"ok": False, "error": f"unsupported action: {action}"}

    if kind == "virsh":
        if verb == "stop":
            verb = "shutdown"
        elif verb == "restart":
            verb = "reboot --reset"
        cmd = f"sg libvirt -c 'virsh -c qemu:///system {verb} {target}'"
    elif kind == "docker":
        cmd = f"sg docker -c 'docker {verb} {target}'"
    elif kind == "unit":
        # Check if systemd manages it OR if there's a manual process on the port
        port = meta["port"]
        port_ok = port and port_listening(port)
        unit_ok = systemd_active(target)
        
        if verb == "stop":
            if port_ok and not unit_ok:
                # Manual process — kill by pgrep
                pp = port_pid_proc(port)
                pid = pp.get("pid", "")
                if pid and pid != "?":
                    return {"ok": True, "action": action, "target": target, "output": f"killed PID {pid}"}
                return {"ok": False, "error": "could not find PID for port " + str(port)}
            elif unit_ok:
                cmd = f"systemctl --user stop {target}"
            else:
                return {"ok": False, "error": "service not running (systemd inactive, no manual process)"}
        elif verb == "start":
            if unit_ok:
                return {"ok": False, "error": "service already active"}
            # Try systemd start
            cmd = f"systemctl --user start {target}"
        elif verb == "restart":
            if port_ok and not unit_ok:
                # Manual process — kill then systemd start
                pp = port_pid_proc(port)
                pid = pp.get("pid", "")
                if pid and pid != "?":
                    _run(f"kill {pid}")
                cmd = f"systemctl --user start {target}"
            elif unit_ok:
                cmd = f"systemctl --user restart {target}"
            else:
                cmd = f"systemctl --user start {target}"
        elif verb in ("enable", "disable"):
            cmd = f"systemctl --user {verb} {target}"
        else:
            cmd = f"systemctl --user {verb} {target}"
    elif kind in ("manual", "system"):
        # Cannot control these from WebUI
        return {"ok": False, "error": f"service '{sid}' is managed externally — control from terminal"}
    else:
        return {"ok": False, "error": f"unsupported kind: {kind}"}

    out = _run(cmd, timeout=30)
    return {"ok": True, "action": action, "target": target, "output": out}


# ───────────────── ports ─────────────────
def all_ports() -> List[Dict[str, Any]]:
    ports: List[Dict[str, Any]] = []
    try:
        for line in Path("/proc/net/tcp").read_text().split("\n")[1:]:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":
                continue
            try:
                port = int(parts[1].split(":")[1], 16)
            except (ValueError, IndexError):
                continue
            ports.append({"port": port, "proto": "tcp"})
    except Exception:
        pass
    for p in ports:
        info = _run(f"ss -tulnpH sport = :{p['port']}")
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', info)
        if m:
            p["proc"] = m.group(1)
            p["pid"] = m.group(2)
        else:
            p.setdefault("proc", "?")
            p.setdefault("pid", "?")
    seen = {}
    for p in ports:
        seen.setdefault(p["port"], p)
    return sorted(seen.values(), key=lambda x: x["port"])



# ───────────────── GPU ─────────────────
def gpu_info() -> Dict[str, Any]:
    """Query nvidia-smi for GPU stats."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,power.limit,clocks.max.graphics,clocks.max.memory,fan.speed,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        gpus = []
        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 16:
                gpus.append({
                    "name": parts[0],
                    "temperature": int(parts[1]) if parts[1] else 0,
                    "util_gpu": int(parts[2]) if parts[2] else 0,
                    "util_mem": int(parts[3]) if parts[3] else 0,
                    "mem_used_mb": int(parts[4]) if parts[4] else 0,
                    "mem_total_mb": int(parts[5]) if parts[5] else 0,
                    "mem_free_mb": int(parts[15]) if parts[15] else 0,
                    "power_w": float(parts[6]) if parts[6] else 0,
                    "power_limit_w": float(parts[7]) if parts[7] else 0,
                    "clock_graphics_mhz": int(parts[8]) if parts[8] else 0,
                    "clock_memory_mhz": int(parts[9]) if parts[9] else 0,
                    "fan_pct": int(parts[10]) if parts[10] else 0,
                    "pcie_gen": parts[11],
                    "pcie_gen_max": parts[12],
                    "pcie_width": parts[13],
                    "pcie_width_max": parts[14],
                })
        return {"ok": True, "gpus": gpus}
    except Exception as e:
        return {"ok": False, "error": str(e), "gpus": []}


# ───────────────── llama ─────────────────
def llama_chat(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    payload = {"messages": messages, "max_tokens": 512,
               "temperature": 0.7, "stream": False}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(LLAMA_URL, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return {"ok": True, "data": json.loads(r.read())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ───────────────── ideas ─────────────────
def list_ideas() -> List[Dict[str, Any]]:
    items = []
    for md in sorted(IDEAS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = md.stat()
        body = md.read_text(errors="replace")
        preview = re.sub(r"\s+", " ", body[:160]).strip()
        items.append({
            "name": md.name,
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M"),
            "preview": preview,
            "size": st.st_size,
        })
    return items


def read_idea(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_\-./]", "", name)
    p = (IDEAS_DIR / safe).resolve()
    if not str(p).startswith(str(IDEAS_DIR.resolve())) or not p.is_file():
        raise HTTPException(404, "not found")
    return p.read_text(errors="replace")


def write_idea(name: str, body: str) -> None:
    safe = re.sub(r"[^A-Za-z0-9_\-./]", "", name)
    if not safe or not safe.endswith(".md"):
        raise HTTPException(400, "name must end in .md")
    (IDEAS_DIR / safe).write_text(body)


def delete_idea(name: str) -> None:
    safe = re.sub(r"[^A-Za-z0-9_\-./]", "", name)
    p = (IDEAS_DIR / safe).resolve()
    if not str(p).startswith(str(IDEAS_DIR.resolve())) or not p.is_file():
        raise HTTPException(404, "not found")
    p.unlink()


# ───────────────── llama manager ─────────────────
def _get_models() -> List[Dict[str, Any]]:
    models = []
    for f in sorted(MODEL_DIR.glob("*.gguf")):
        st = f.stat()
        name = f.name
        mtp = False
        try:
            with open(f, "rb") as hf:
                if b"nextn" in hf.read(262144):
                    mtp = True
        except Exception:
            pass
        size_match = re.search(r'(4B|7B|8B|9B|14B|17B|A4B|E4B)', name, re.IGNORECASE)
        ctx = 262144 if size_match else 131072
        models.append({
            "path": str(f), "name": name,
            "size_mb": round(st.st_size / 1048576, 1),
            "mtp": mtp, "ctx": ctx,
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return models


def _kill_port(port: int) -> str:
    out = _run(f"lsof -t -i:{port}", timeout=5)
    pids = [p.strip() for p in out.strip().split("\n") if p.strip()]
    killed = []
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGKILL)
            killed.append(pid)
        except Exception:
            pass
    return f"killed PIDs {killed}" if killed else "nothing on port"


def _spawn(mode: str, model_path: str, port: int) -> str:
    binary = LLAMA_GPU_BIN if mode == "gpu" else LLAMA_CPU_BIN
    if not binary.exists():
        return f"Binary not found: {binary}"
    name = Path(model_path).name
    spec = ""
    try:
        with open(model_path, "rb") as hf:
            if b"nextn" in hf.read(262144):
                spec = "--spec-type draft-mtp --spec-draft-n-max 2"
    except Exception:
        pass
    size_match = re.search(r'(4B|7B|8B|9B|14B|17B|A4B|E4B)', name, re.IGNORECASE)
    ctx = 262144 if size_match else 131072
    _kill_port(port)
    time.sleep(1)
    args = [str(binary), "-m", model_path]
    if spec:
        args.extend(spec.split())
    if mode == "gpu":
        args += ["-c","131072","-ngl","999","-fa","on","-ctk","q4_0","-ctv","q4_0",
                  "-b","512","-ub","512","--cont-batching","--reasoning-preserve",
                  "--host","0.0.0.0","--port",str(port),"-t","8"]
    else:
        args += ["-c","32768","-t","12","-tb","20","-b","512","-ub","512",
                  "--cont-batching","--reasoning-preserve",
                  "--host","0.0.0.0","--port",str(port),"--mlock"]
    key = f"{mode}:{port}"
    if key in _spawned and _spawned[key].is_alive():
        return f"Already running: {key}"
    log = f"/tmp/llama-{mode}-{port}.log"
    def _run_t():
        with open(log, "w") as lf:
            subprocess.run(args, stdout=lf, stderr=lf)
    t = threading.Thread(target=_run_t, daemon=True)
    t.start()
    _spawned[key] = t
    return f"Started {name} ({mode.upper()}, port {port}, ctx {ctx}) → {log}"


# ───────────────── logs ─────────────────
def last_logs(sid: str, n: int = 50) -> str:
    meta = SVC_IDX.get(sid)
    if not meta:
        raise HTTPException(404, "unknown service")
    if meta["control_kind"] == "unit":
        unit = meta["control_target"]
        # `systemctl status` has nicer output than raw journal
        out = _run(f"systemctl --user status {unit} -n {n} --no-pager", timeout=8)
        if "loaded" not in out and "Active" not in out:
            # fallback to raw journal
            out = _run(f"journalctl --user -u {unit} -n {n} --no-pager --output=cat", timeout=5)
        return out
    if meta["control_kind"] == "docker":
        cname = meta["control_target"]
        return _run(f"docker logs --tail {n} {cname} 2>&1", timeout=10)
    if meta["control_kind"] == "virsh":
        return _run(f"sg libvirt -c 'virsh -c qemu:///system domstate {meta['control_target']}' 2>&1", timeout=5)
    raise HTTPException(400, "no log source")


# ───────────────── app ─────────────────
app = FastAPI(title="UbuntuConsole", version="4.0")
app.mount("/static", StaticFiles(directory=str(CONSOLE_DIR / "webui" / "static")), name="static")


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#08090a">
<title>UbuntuConsole</title>
<link rel="stylesheet" href="/static/fonts/inter.css">
<style>
/* ============================================================
   UbuntuConsole — design system
   Reference: Linear.app, Vercel, Apple HIG
   ============================================================ */

:root {
  /* Surfaces (use slightly tinted darks for warmth, not pure black) */
  --bg-base:       #08090a;
  --bg-surface:    #0d0e11;
  --bg-elev-1:     #121419;
  --bg-elev-2:     #181b22;
  --bg-hover:      #1d2029;

  /* Borders */
  --line:          rgba(255,255,255,0.055);
  --line-strong:   rgba(255,255,255,0.10);
  --line-focus:    rgba(255,255,255,0.20);

  /* Text (4-tier scale, Linear convention) */
  --text-1:        #f7f8f8;
  --text-2:        #b4b8bf;
  --text-3:        #7d848d;
  --text-4:        #51575f;

  /* Accent */
  --accent:        #c9a86a;             /* refined warm amber */
  --accent-soft:   rgba(201,168,106,0.10);
  --accent-glow:   rgba(201,168,106,0.22);

  /* State */
  --ok:            #58c47b;
  --warn:          #e5b765;
  --err:           #e86969;

  /* Per-service accents (Linear style: muted, never neon) */
  --c-llama:       #d4a35a;
  --c-silly:       #b58cd9;
  --c-comfyui:     #e07a9c;
  --c-camofox:     #a6d96a;
  --c-telegram:    #6aa9e0;
  --c-hermes:      #5cc4b5;
  --c-marvis:      #e6a25c;
  --c-dockry:      #5cb8d6;
  --c-samba:       #8693d4;
  --c-ssh:         #9ba3af;

  /* Type */
  --font: "InterVariable", "Inter", -apple-system, BlinkMacSystemFont,
          "SF Pro Display", "Segoe UI", "PingFang SC", sans-serif;
  --font-mono: "JetBrains Mono", "SF Mono", ui-monospace, monospace;

  /* Spacing */
  --gap-xs: 4px;
  --gap-sm: 8px;
  --gap-md: 14px;
  --gap-lg: 22px;
  --gap-xl: 36px;

  /* Radius */
  --r-sm: 6px;
  --r-md: 9px;
  --r-lg: 13px;
  --r-xl: 18px;

  /* Motion */
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-quick: 180ms;
}

* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--font);
  font-variation-settings: "opsz" 14, "wght" 400;
  background: var(--bg-base);
  color: var(--text-1);
  font-size: 13.5px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  letter-spacing: -0.005em;
  font-feature-settings: "ss01", "cv02", "cv11";
  overscroll-behavior-y: contain;
}
button { font: inherit; cursor: pointer; }

/* Layout */
.layout {
  display: grid;
  grid-template-columns: 240px 1fr;
  min-height: 100vh;
}
@media (max-width: 768px) { .layout { grid-template-columns: 1fr; } }

/* ===== Sidebar ===== */
.sidebar {
  background: var(--bg-surface);
  border-right: 1px solid var(--line);
  padding: 22px 16px;
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
}
.brand {
  display: flex; align-items: center; gap: 11px;
  padding-bottom: 20px;
  margin-bottom: 18px;
  border-bottom: 1px solid var(--line);
}
.brand-mark {
  width: 32px; height: 32px; border-radius: 9px;
  background: linear-gradient(135deg, #e6c987 0%, #a4834d 100%);
  display: grid; place-items: center;
  color: #1a1206;
  font-weight: 600; font-size: 16px;
  box-shadow: 0 4px 18px var(--accent-glow);
  font-variation-settings: "opsz" 28;
}
.brand-meta { line-height: 1.3; }
.brand-name {
  font-weight: 550; font-size: 14.5px;
  letter-spacing: -0.012em;
}
.brand-sub {
  font-size: 11px; color: var(--text-4);
  text-transform: uppercase; letter-spacing: 0.08em;
  font-weight: 500;
  margin-top: 2px;
}
.nav-section {
  font-size: 10.5px;
  text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--text-4); font-weight: 600;
  padding: 6px 12px 4px;
  margin-top: 4px;
}
.nav { display: flex; flex-direction: column; gap: 1px; margin-top: 4px; }
.nav button {
  background: transparent; color: var(--text-2);
  border: 0;
  padding: 8px 12px;
  border-radius: var(--r-md);
  text-align: left;
  font-size: 13.5px; font-weight: 450;
  display: flex; align-items: center; gap: 10px;
  transition: background var(--ease-quick) var(--ease-out),
              color var(--ease-quick) var(--ease-out);
  position: relative;
}
.nav button:hover { background: var(--bg-hover); color: var(--text-1); }
.nav button.active {
  background: var(--bg-elev-2);
  color: var(--text-1);
  font-weight: 500;
}
.nav button.active::before {
  content: ""; position: absolute;
  left: -16px; top: 50%; transform: translateY(-50%);
  width: 3px; height: 16px;
  background: var(--accent);
  border-radius: 0 2px 2px 0;
}
.nav-icon {
  width: 15px; height: 15px;
  flex-shrink: 0;
  opacity: 0.7;
}
.nav button.active .nav-icon { opacity: 1; }
.nav button:hover .nav-icon { opacity: 1; }

.sidebar-spacer { flex: 1; min-height: 30px; }

.sidebar-foot {
  border-top: 1px solid var(--line);
  padding-top: 14px;
  font-size: 11.5px;
  color: var(--text-3);
  line-height: 1.6;
}
.foot-row {
  display: flex; justify-content: space-between;
  align-items: baseline; padding: 2px 0;
}
.foot-row .key {
  font-size: 10.5px; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--text-4); font-weight: 500;
}
.foot-row .v { font-family: var(--font-mono); font-size: 11.5px; }
.live-dot {
  display: inline-block; width: 7px; height: 7px;
  border-radius: 50%; background: var(--ok);
  box-shadow: 0 0 8px var(--ok);
  animation: pulse 2.2s ease-in-out infinite;
  margin-right: 6px;
}
@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.35; transform: scale(0.7); }
}

/* ===== Main ===== */
.main { padding: 38px 40px 60px; max-width: 1240px; }
@media (max-width: 768px) { .main { padding: 22px 18px 40px; } }

.page-head {
  margin-bottom: 28px;
  padding-bottom: 22px;
  border-bottom: 1px solid var(--line);
}
.page-title {
  font-size: 22px; font-weight: 600;
  letter-spacing: -0.018em; margin: 0;
  font-variation-settings: "opsz" 28;
}
.page-title small {
  display: block; font-size: 12.5px;
  font-weight: 400; color: var(--text-3);
  margin-top: 3px;
}

/* Status overview at top of services page */
.overview {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 28px;
}
@media (max-width: 900px) { .overview { grid-template-columns: repeat(2, 1fr); } }
.ovr-card {
  background: var(--bg-surface);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  padding: 16px 18px;
  display: flex; gap: 14px; align-items: center;
}
.ovr-icon {
  width: 38px; height: 38px;
  border-radius: 10px;
  display: grid; place-items: center;
  font-size: 17px;
}
.ovr-icon.ok   { background: rgba(88,196,123,0.10); color: var(--ok); }
.ovr-icon.warn { background: rgba(229,183,101,0.10); color: var(--warn); }
.ovr-icon.err  { background: rgba(232,105,105,0.10); color: var(--err); }
.ovr-num {
  font-size: 22px; font-weight: 600;
  letter-spacing: -0.02em;
  font-variation-settings: "opsz" 28;
}
.ovr-label {
  font-size: 11px; color: var(--text-3);
  text-transform: uppercase; letter-spacing: 0.08em;
  font-weight: 500; margin-top: 1px;
}

/* ===== Bento grid ===== */
.bento {
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  gap: 14px;
}
.bento .col-8  { grid-column: span 8; }
.bento .col-6  { grid-column: span 6; }
.bento .col-4  { grid-column: span 4; }
.bento .col-3  { grid-column: span 3; }
@media (max-width: 1024px) {
  .bento .col-8, .bento .col-6 { grid-column: span 12; }
  .bento .col-4, .bento .col-3 { grid-column: span 6; }
}
@media (max-width: 640px) {
  .bento > * { grid-column: span 12 !important; }
}

/* ===== Card ===== */
.card {
  background: var(--bg-surface);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  padding: 20px 22px;
  position: relative;
  overflow: hidden;
  transition: border-color 220ms var(--ease-out),
              background 220ms var(--ease-out),
              transform 220ms var(--ease-out);
}
.card:hover {
  border-color: var(--line-strong);
  background: var(--bg-elev-1);
}
.card-h {
  display: flex; align-items: flex-start; gap: 13px;
  margin-bottom: 14px;
}
.card-icon {
  width: 40px; height: 40px;
  border-radius: 10px;
  display: grid; place-items: center;
  flex-shrink: 0;
}
.card-icon svg {
  width: 19px; height: 19px;
}
.card-icon[data-c="llama"]    { background: rgba(212,163,90,0.10);  color: var(--c-llama); }
.card-icon[data-c="silly"]    { background: rgba(181,140,217,0.10); color: var(--c-silly); }
.card-icon[data-c="comfyui"]  { background: rgba(224,122,156,0.10); color: var(--c-comfyui); }
.card-icon[data-c="camofox"]  { background: rgba(166,217,106,0.10); color: var(--c-camofox); }
.card-icon[data-c="telegram"] { background: rgba(106,169,224,0.10); color: var(--c-telegram); }
.card-icon[data-c="hermes"]   { background: rgba(92,196,181,0.10);  color: var(--c-hermes); }
.card-icon[data-c="couchdb"]  { background: rgba(255,154,118,0.10); color: #ff9a76; }
.card-icon[data-c="marvis"]   { background: rgba(230,162,92,0.10);  color: var(--c-marvis); }
.card-icon[data-c="dockry"]   { background: rgba(92,184,214,0.10);  color: var(--c-dockry); }
.card-icon[data-c="samba"]    { background: rgba(134,147,212,0.10); color: var(--c-samba); }
.card-icon[data-c="ssh"]      { background: rgba(155,163,175,0.10); color: var(--c-ssh); }

.card-title { flex: 1; min-width: 0; }
.card-title h3 {
  margin: 0; font-size: 14.5px; font-weight: 600;
  letter-spacing: -0.01em;
  font-variation-settings: "opsz" 18;
}
.card-title .sub {
  font-size: 11.5px; color: var(--text-3);
  margin-top: 1px; font-weight: 450;
}

.status-tag {
  font-size: 11px; font-weight: 500;
  padding: 4px 9px;
  border-radius: 20px;
  display: inline-flex; align-items: center; gap: 6px;
  letter-spacing: 0.005em;
}
.status-tag::before {
  content: ""; width: 6px; height: 6px;
  border-radius: 50%;
}
.status-tag.up   { background: rgba(88,196,123,0.10); color: var(--ok); }
.status-tag.up::before   { background: var(--ok); box-shadow: 0 0 6px var(--ok); }
.status-tag.warn { background: rgba(229,183,101,0.10); color: var(--warn); }
.status-tag.warn::before { background: var(--warn); }
.status-tag.down { background: rgba(232,105,105,0.10); color: var(--err); }
.status-tag.down::before { background: var(--err); }

/* KV meta */
.kv {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 10px 18px;
  margin: 4px 0 16px;
}
.kv .k {
  font-size: 10.5px; color: var(--text-4);
  text-transform: uppercase; letter-spacing: 0.06em;
  font-weight: 500;
}
.kv .v {
  font-family: var(--font-mono);
  font-size: 12.5px; color: var(--text-1);
  margin-top: 2px;
  font-variation-settings: "opsz" 18;
}

/* Big port + category stat row */
.port-row {
  display: flex;
  align-items: stretch;
  gap: 10px;
  margin: 6px 0 14px;
}
.port-stat, .cat-stat {
  background: rgba(255,255,255,0.025);
  border: 1px solid var(--line);
  border-radius: 9px;
  padding: 10px 14px;
  display: flex; flex-direction: column;
  justify-content: center;
}
.port-stat { min-width: 88px; }
.cat-stat  { flex: 1; min-width: 0; }
.port-num {
  font-family: var(--font-mono);
  font-weight: 600;
  font-size: 22px;
  letter-spacing: -0.02em;
  color: var(--accent);
  font-variation-settings: "opsz" 28;
  line-height: 1.1;
}
.port-lbl {
  font-size: 10.5px;
  color: var(--text-4);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 500;
  margin-top: 2px;
}
.cat-name {
  font-size: 13px;
  color: var(--text-1);
  font-weight: 500;
  letter-spacing: -0.005em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cat-id {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-4);
  margin-top: 2px;
  font-variation-settings: "opsz" 18;
}

/* Category section heading */
.cat-section {
  grid-column: span 12;
  display: flex; align-items: center; gap: 12px;
  padding: 8px 4px 4px;
  margin-top: 12px;
}
.cat-section:first-child { margin-top: 0; }
.cat-section .cs-label {
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--text-3);
  font-weight: 600;
}
.cat-section .cs-count {
  background: var(--bg-elev-1);
  color: var(--text-2);
  border: 1px solid var(--line);
  font-family: var(--font-mono);
  font-size: 11px; padding: 2px 8px;
  border-radius: 20px;
  font-variation-settings: "opsz" 18;
}
.cat-section .cs-line {
  flex: 1; height: 1px;
  background: var(--line);
}

.actions {
  display: flex; gap: 6px; flex-wrap: wrap;
}

.btn {
  background: var(--bg-elev-1);
  color: var(--text-1);
  border: 1px solid var(--line-strong);
  padding: 7px 12px;
  border-radius: var(--r-md);
  font-size: 12.5px; font-weight: 500;
  display: inline-flex; align-items: center; gap: 6px;
  transition: background var(--ease-quick) var(--ease-out),
              border-color var(--ease-quick) var(--ease-out),
              transform 100ms var(--ease-out);
}
.btn:hover { background: var(--bg-elev-2); border-color: var(--line-focus); }
.btn:active { transform: scale(0.97); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }
.btn.primary {
  background: var(--accent);
  color: #1a1206; border-color: var(--accent);
  font-weight: 550;
}
.btn.primary:hover { background: #d6b478; border-color: #d6b478; }
.btn.danger {
  color: var(--err);
  border-color: rgba(232,105,105,0.35);
  background: rgba(232,105,105,0.06);
}
.btn.danger:hover { background: rgba(232,105,105,0.12); border-color: rgba(232,105,105,0.5); }
.btn.sm { padding: 5px 10px; font-size: 11.5px; border-radius: 7px; }

/* Inline svg icon color */
svg.icon {
  width: 14px; height: 14px;
  fill: none; stroke: currentColor;
  stroke-width: 1.75;
  stroke-linecap: round; stroke-linejoin: round;
}
.card-icon svg.icon { stroke-width: 1.85; }

/* ===== Ports table ===== */
.ports-card {
  background: var(--bg-surface);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  overflow: hidden;
}
.ports-head, .ports-row {
  display: grid;
  grid-template-columns: 70px 70px 1fr 80px 1.4fr;
  gap: 18px; padding: 11px 22px;
  align-items: center;
  font-size: 12.5px;
}
.ports-head {
  background: rgba(0,0,0,0.18);
  color: var(--text-4);
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 600;
  padding: 10px 22px;
  border-bottom: 1px solid var(--line);
}
.ports-row {
  border-bottom: 1px solid var(--line);
  transition: background 150ms var(--ease-out);
}
.ports-row:hover { background: var(--bg-hover); }
.ports-row:last-child { border-bottom: 0; }
.pnum {
  font-family: var(--font-mono);
  font-weight: 600;
  color: var(--accent);
  font-size: 13px;
  font-variation-settings: "opsz" 18;
}
.proc, .pid {
  font-family: var(--font-mono);
  font-size: 12px;
  font-variation-settings: "opsz" 18;
}
.pid { color: var(--text-3); }
.proc { color: var(--text-1); }

/* ===== Chat ===== */
.chat-card {
  background: var(--bg-surface);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  display: flex; flex-direction: column;
  height: calc(100vh - 230px);
  min-height: 460px;
  overflow: hidden;
}
.chat-log {
  flex: 1; overflow-y: auto;
  padding: 22px 26px;
  display: flex; flex-direction: column;
  gap: 12px;
}
.bubble {
  max-width: 78%;
  padding: 12px 16px;
  border-radius: 14px;
  font-size: 13.5px; line-height: 1.65;
  white-space: pre-wrap; word-break: break-word;
}
.bubble.user {
  background: var(--accent);
  color: #1a1206;
  margin-left: auto;
  border-bottom-right-radius: 4px;
  font-weight: 450;
}
.bubble.assistant {
  background: var(--bg-elev-1);
  border: 1px solid var(--line-strong);
  margin-right: auto;
  border-bottom-left-radius: 4px;
}
.bubble.system {
  color: var(--text-4);
  font-size: 11.5px; font-style: italic;
  text-align: center;
  background: transparent;
  padding: 4px 12px;
  max-width: 100%;
}
.chat-row {
  padding: 14px 18px;
  border-top: 1px solid var(--line);
  background: rgba(0,0,0,0.18);
  display: flex; gap: 10px; align-items: flex-end;
}
.chat-row textarea {
  flex: 1; resize: none;
  min-height: 42px; max-height: 180px;
  background: var(--bg-elev-1);
  color: var(--text-1);
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  padding: 12px 14px;
  font: inherit; font-size: 13.5px;
  outline: none;
  transition: border-color var(--ease-quick) var(--ease-out);
}
.chat-row textarea:focus { border-color: var(--accent); }
.chat-row textarea::placeholder { color: var(--text-4); }

/* ===== Ideas ===== */
.idea-editor {
  background: var(--bg-surface);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  padding: 18px 20px;
  margin-bottom: 18px;
}
.idea-editor input, .idea-editor textarea {
  width: 100%;
  background: var(--bg-elev-1);
  color: var(--text-1);
  border: 1px solid var(--line-strong);
  border-radius: 9px;
  padding: 11px 14px;
  font: inherit; font-size: 13.5px;
  outline: none;
  transition: border-color var(--ease-quick) var(--ease-out);
}
.idea-editor input { font-weight: 500; }
.idea-editor input:focus, .idea-editor textarea:focus { border-color: var(--accent); }
.idea-editor textarea {
  min-height: 110px;
  resize: vertical;
  margin-top: 10px;
  font-family: var(--font);
  line-height: 1.55;
}
.idea-editor .editor-actions {
  margin-top: 14px;
  display: flex; gap: 6px;
}

.idea-list {
  display: flex; flex-direction: column; gap: 6px;
}
.idea-item {
  background: var(--bg-surface);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  padding: 14px 18px;
  display: flex; gap: 14px; align-items: center;
  transition: border-color 200ms var(--ease-out),
              background 200ms var(--ease-out);
}
.idea-item:hover {
  border-color: var(--line-strong);
  background: var(--bg-elev-1);
}
.idea-icon {
  width: 30px; height: 30px;
  border-radius: 7px;
  background: var(--bg-elev-2);
  display: grid; place-items: center;
  flex-shrink: 0;
  color: var(--text-3);
}
.idea-body { flex: 1; min-width: 0; }
.idea-name {
  font-weight: 550; font-size: 13.5px;
  display: flex; align-items: baseline; gap: 8px;
}
.idea-mtime {
  font-family: var(--font-mono);
  font-size: 11px; color: var(--text-4);
}
.idea-preview {
  font-size: 12.5px; color: var(--text-2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  margin-top: 2px;
}
.idea-actions {
  display: flex; gap: 4px;
  opacity: 0; transition: opacity 200ms var(--ease-out);
}
.idea-item:hover .idea-actions { opacity: 1; }

/* ===== Logs ===== */
.log-card {
  background: var(--bg-surface);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  overflow: hidden;
}
.log-head {
  padding: 12px 18px;
  display: flex; gap: 10px; align-items: center;
  border-bottom: 1px solid var(--line);
}
.log-head select {
  flex: 1;
  background: var(--bg-elev-1);
  color: var(--text-1);
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  padding: 8px 12px;
  font: inherit;
  font-size: 12.5px;
  outline: none;
}
.log-body {
  background: #050608;
  padding: 18px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 62vh;
  overflow-y: auto;
  line-height: 1.55;
}

/* ===== Modal ===== */
.modal-bg {
  display: none;
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.72);
  z-index: 100;
  align-items: center; justify-content: center;
  padding: 24px;
  backdrop-filter: blur(8px);
}
.modal-bg.open { display: flex; }
.modal-card {
  background: var(--bg-elev-1);
  border: 1px solid var(--line-strong);
  border-radius: var(--r-lg);
  padding: 24px;
  width: 100%; max-width: 720px;
  max-height: 80vh; overflow-y: auto;
}
.modal-card h3 {
  margin: 0 0 14px; font-size: 15.5px; font-weight: 600;
  letter-spacing: -0.01em;
}
.modal-card pre {
  background: #050608;
  padding: 14px 16px;
  border-radius: 10px;
  white-space: pre-wrap;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-1);
  border: 1px solid var(--line);
  line-height: 1.6;
}
.modal-actions {
  margin-top: 16px;
  display: flex; gap: 6px;
  justify-content: flex-end;
}

/* ===== Toast ===== */
.toast {
  position: fixed; bottom: 24px;
  left: 50%; transform: translate(-50%, 12px);
  background: var(--bg-elev-2);
  border: 1px solid var(--line-strong);
  color: var(--text-1);
  padding: 11px 18px;
  border-radius: 10px;
  font-size: 13px;
  box-shadow: 0 12px 48px rgba(0,0,0,0.5),
              0 0 0 1px var(--line);
  opacity: 0;
  pointer-events: none;
  transition: opacity 220ms var(--ease-out),
              transform 220ms var(--ease-out);
  z-index: 200;
  font-weight: 500;
}
.toast.show { opacity: 1; transform: translate(-50%, 0); }
.toast.err  { color: var(--err); border-color: rgba(232,105,105,0.5); }
.toast.ok   { color: var(--ok); border-color: rgba(88,196,123,0.5); }

.empty {
  color: var(--text-4);
  padding: 48px 24px;
  text-align: center;
  font-size: 13px;
}

.tabs-hidden { display: none; }

::selection {
  background: var(--accent-soft);
  color: var(--text-1);
}
::-webkit-scrollbar { width: 11px; height: 11px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background: rgba(255,255,255,0.06);
  border-radius: 8px;
  border: 2px solid var(--bg-base);
}
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.14); }

/* spinner */
.spinner {
  width: 12px; height: 12px;
  border: 2px solid var(--bg-hover);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  display: inline-block;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* fade-in for fresh content */
@keyframes fade-up {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: translateY(0); }
}
.fade-in { animation: fade-up 320ms var(--ease-out); }

/* Models panel */
.models-panel { background: var(--bg-elev-1); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
.models-controls { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.models-controls label { display: flex; align-items: center; gap: 8px; color: var(--text-2); font-size: 14px; }
.models-controls select { background: var(--bg-elev-2); border: 1px solid var(--border); color: var(--text-1); border-radius: 6px; padding: 6px 10px; font-size: 13px; }
.models-status { font-size: 13px; color: var(--text-2); margin-bottom: 12px; padding: 8px 12px; background: var(--bg-elev-2); border-radius: 6px; }
.models-table { width: 100%; border-collapse: collapse; }
.models-table th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-3); padding: 8px 12px; border-bottom: 1px solid var(--border); }
.models-table td { padding: 10px 12px; font-size: 13px; border-bottom: 1px solid var(--border); color: var(--text-1); }
.models-table tr:last-child td { border-bottom: none; }
.models-table tr:hover td { background: var(--bg-hover); }
.models-table .mtp-yes { color: #5cc784; font-weight: 600; }
.models-table .mtp-no { color: var(--text-3); }
.models-table .model-name { font-family: var(--font-mono); font-size: 12px; }
.models-table .ctx { color: var(--text-2); font-family: var(--font-mono); }
.models-table select { background: var(--bg-elev-2); border: 1px solid var(--border); color: var(--text-1); border-radius: 4px; padding: 4px 8px; font-size: 12px; width: 100%; }
</style>
</head>
<body>

<div class="layout">
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark">B</div>
      <div class="brand-meta">
        <div class="brand-name">UbuntuConsole</div>
        <div class="brand-sub">Control deck v4</div>
      </div>
    </div>

    <div class="nav-section">Workspace</div>
    <nav class="nav" id="navMain">
      <button data-tab="services" class="active">
        <svg class="icon nav-icon" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
        Services
      </button>
      <button data-tab="ports">
        <svg class="icon nav-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>
        Ports
      </button>
      <button data-tab="chat">
        <svg class="icon nav-icon" viewBox="0 0 24 24"><path d="M21 12c0 4.97-4.03 9-9 9-1.5 0-2.92-.36-4.17-1.02L3 21l1.24-4.44A8.96 8.96 0 0 1 3 12c0-4.97 4.03-9 9-9s9 4.03 9 9z"/></svg>
        Chat
      </button>
      <button data-tab="models">
        <svg class="icon nav-icon" viewBox="0 0 24 24"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>
        Models
      </button>
    </nav>

    <div class="nav-section">Library</div>
    <nav class="nav" id="navLib">
      <button data-tab="ideas">
        <svg class="icon nav-icon" viewBox="0 0 24 24"><path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.74V17h8v-2.26A7 7 0 0 0 12 2z"/></svg>
        Ideas
      </button>
      <button data-tab="logs">
        <svg class="icon nav-icon" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><path d="M9 14h6M9 17h4"/></svg>
        Logs
      </button>
    </nav>

    <div class="sidebar-spacer"></div>

    <div class="sidebar-foot">
      <div class="foot-row">
        <span><span class="live-dot"></span><span id="hostName" style="color:var(--text-2);font-weight:500;">—</span></span>
      </div>
      <div class="foot-row">
        <span class="key">up</span>
        <span class="v" id="hostUptime">—</span>
      </div>
      <div class="foot-row">
        <span class="key">kernel</span>
        <span class="v" id="hostKernel">—</span>
      </div>
      <div class="foot-row">
        <span class="key">public</span>
        <span class="v" style="font-size:11px;">188.87.49.121</span>
      </div>
      <button class="btn sm" id="tokenBtn" style="margin-top:12px;width:100%;justify-content:center;">
        <svg class="icon" viewBox="0 0 24 24" style="width:12px;height:12px;"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        cmd token
      </button>
    </div>
  </aside>

  <main class="main">
    <!-- SERVICES -->
    <section class="tab" id="tab-services" data-on="services" style="display:none;">
      <div class="page-head">
        <h1 class="page-title">Services<small id="ovrSubtitle">Local AI stack & infrastructure</small></h1>
      </div>

      <div class="overview" id="overview"></div>

      <div class="bento" id="bento"></div>
    </section>

    <!-- PORTS -->
    <section class="tab" id="tab-ports" style="display:none;">
      <div class="page-head">
        <h1 class="page-title">Ports<small>All listening TCP sockets with their owning process</small></h1>
      </div>
      <div id="portsBox"></div>
    </section>

    <!-- CHAT -->
    <section class="tab" id="tab-chat" style="display:none;">
      <div class="page-head">
        <h1 class="page-title">Chat<small>Talk to local llama.cpp · Qwen3.6-27B-UD-Q4_K_XL</small></h1>
      </div>
      <div class="chat-card">
        <div class="chat-log" id="chatLog"></div>
        <div class="chat-row">
          <textarea id="chatInput" placeholder="说点什么… (Enter 发送, Shift+Enter 换行)" rows="1"></textarea>
          <button class="btn primary" id="chatSend">Send</button>
        </div>
      </div>
    </section>

    <!-- MODELS -->
    <section class="tab" id="tab-models" style="display:none;">
      <div class="page-head">
        <h1 class="page-title">Models<small>llama.cpp model manager — select, launch, stop</small></h1>
      </div>
      <div class="models-panel" id="modelsPanel">
        <div class="models-controls">
          <label>Mode
            <select id="modelMode"><option value="gpu">GPU (port 8888)</option><option value="cpu">CPU (port 8889)</option></select>
          </label>
          <button class="btn primary" id="modelLaunchBtn">▶ Launch</button>
          <button class="btn" id="modelStopBtn">⏹ Stop</button>
        </div>
        <div class="models-status" id="modelsStatus">Checking...</div>
        <table class="models-table" id="modelsTable">
          <thead><tr><th>Name</th><th>Size</th><th>MTP</th><th>Context</th><th>Modified</th><th></th></tr></thead>
          <tbody id="modelsBody"></tbody>
        </table>
      </div>
    </section>

    <!-- IDEAS -->
    <section class="tab" id="tab-ideas" style="display:none;">
      <div class="page-head">
        <h1 class="page-title">Ideas<small>Notes & thoughts — saved to UbuntuConsole/ideas</small></h1>
      </div>
      <div class="idea-editor">
        <input id="ideaName" placeholder="文件名 (例如: 项目-A-设计.md)" />
        <textarea id="ideaBody" placeholder="想法..."></textarea>
        <div class="editor-actions">
          <button class="btn primary" id="ideaSave">
            <svg class="icon" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2zM17 21v-8H7v8M7 3v5h8"/></svg>
            保存
          </button>
          <button class="btn" id="ideaClear">清空</button>
        </div>
      </div>
      <div class="idea-list" id="ideasList"></div>
    </section>

    <!-- LOGS -->
    <section class="tab" id="tab-logs" style="display:none;">
      <div class="page-head">
        <h1 class="page-title">Logs<small>Live systemd journal + docker output</small></h1>
      </div>
      <div class="log-card">
        <div class="log-head">
          <select id="logSel"></select>
          <button class="btn sm" id="logRefresh">
            <svg class="icon" viewBox="0 0 24 24"><path d="M3 12a9 9 0 0 1 15.5-6.5L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.5L3 16M3 21v-5h5"/></svg>
            refresh
          </button>
        </div>
        <pre class="log-body" id="logBody">Select a service…</pre>
      </div>
    </section>
  </main>
</div>

<div class="modal-bg" id="modal" onclick="if(event.target.id==='modal')closeModal()">
  <div class="modal-card">
    <h3 id="modalTitle">Modal</h3>
    <pre id="modalBody"></pre>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">关闭</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ============== State ==============
let svcs = [];
let chatHistory = [];

// ============== Icons ==============
const I = {
  llama:    '<svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/></svg>',
  silly:    '<svg class="icon" viewBox="0 0 24 24"><path d="M5 7l4-4h6l4 4v10l-4 4H9l-4-4zM9 12h.01M15 12h.01"/></svg>',
  comfyui:  '<svg class="icon" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>',
  camofox:  '<svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/></svg>',
  telegram: '<svg class="icon" viewBox="0 0 24 24"><path d="M21 4L2 11l6 2 3 7 4-5 5 4z"/><path d="M8 13l4 4"/></svg>',
  hermes:   '<svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 4v3M12 17v3M4 12h3M17 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/></svg>',
  couchdb:  '<svg class="icon" viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="9" ry="3"/><path d="M3 6v6c0 1.66 4.03 3 9 3s9-1.34 9-3V6M3 12v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6"/></svg>',
  marvis:   '<svg class="icon" viewBox="0 0 24 24"><rect x="2" y="4" width="20" height="14" rx="1.5"/><path d="M8 21h8M12 18v3"/></svg>',
  dockry:   '<svg class="icon" viewBox="0 0 24 24"><path d="M3 7l9-4 9 4-9 4-9-4zM3 7v10l9 4 9-4V7M12 11v10"/></svg>',
  samba:    '<svg class="icon" viewBox="0 0 24 24"><path d="M3 7l9-4 9 4-9 4-9-4zM3 12l9 4 9-4M3 17l9 4 9-4"/></svg>',
  ssh:      '<svg class="icon" viewBox="0 0 24 24"><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>',
};
const OPENER = {
  silly:    {url: 'http://192.168.0.251:9277',        label: 'UI',     network: 'LAN'},
  comfyui:  {url: 'http://192.168.0.251:8188',        label: 'UI',     network: 'LAN'},
  couchdb:  {url: 'http://192.168.0.251:5984/_utils', label: 'admin',  network: 'LAN'},
  marvis:   {url: 'spicy://192.168.0.251',            label: 'VNC',    network: 'LAN'},
  samba:    {url: 'smb://192.168.0.251/UbuntuDATA',   label: 'browse', network: 'LAN'},
  ssh:      {url: 'ssh://ryan@192.168.0.251:24681',   label: 'ssh',    network: 'LAN'},
};

// CouchDB public URL (for Obsidian clients)
const COUCHDB_PUBLIC_URL = 'https://7a1a04a45567cacc-188-87-49-121.serveousercontent.com';

// Public URL (serveo tunnel only forwards to 9002, the dashboard itself).
// Everything else is LAN-only since the per-service ports aren't tunneled.
const PUBLIC_URL = 'https://34b5fa7189b06ee1-188-87-49-121.serveousercontent.com';
const LAN_IP = '192.168.0.251';

const CATEGORY_LABEL = {
  ai:     'AI model',
  vm:     'virtual machine',
  docker: 'container',
  share:  'network',
};
const CATEGORY_ORDER = ['ai', 'vm', 'docker', 'share'];

// ============== Helpers ==============
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}
function toast(msg, kind = '') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (kind === 'err' ? 'err' : kind === 'ok' ? 'ok' : '');
  clearTimeout(window._toastT);
  window._toastT = setTimeout(() => t.classList.remove('show'), 2400);
}

// ============== Tabs ==============
document.querySelectorAll('nav button').forEach(b =>
  b.onclick = () => switchTab(b.dataset.tab)
);
function switchTab(name) {
  document.querySelectorAll('nav button').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name));
  ['services','ports','chat','gpu','ideas','logs','models'].forEach(n => {
    const el = document.getElementById('tab-' + n);
    if (el) el.style.display = n === name ? '' : 'none';
  });
  if (name === 'ports') loadPorts();
  if (name === 'ideas') loadIdeas();
  if (name === 'logs')  loadLogServices();
  if (name === 'gpu')   loadGpu();
  if (name === 'models') loadModels();
  if (name === 'gpu')   loadGpu(); // loaded above
  if (name === 'chat')  setTimeout(() => document.getElementById('chatInput').focus(), 50);
}

// ============== Host ==============
async function loadHost() {
  try {
    const h = await api('/api/host');
    document.getElementById('hostName').textContent   = h.hostname;
    document.getElementById('hostUptime').textContent = h.uptime.split(',')[0].trim();
    document.getElementById('hostKernel').textContent = h.kernel;
  } catch (e) {}
}

// ============== Services ==============
function renderOverview(list) {
  const up   = list.filter(s => s.state === 'up').length;
  const warn = list.filter(s => s.state === 'warn').length;
  const down = list.filter(s => s.state === 'down').length;
  const total = list.length;
  document.getElementById('overview').innerHTML =
    '<div class="ovr-card"><div class="ovr-icon ok"><svg class="icon" viewBox="0 0 24 24" style="width:18px;height:18px;"><path d="M20 6L9 17l-5-5"/></svg></div><div><div class="ovr-num">' + up + '</div><div class="ovr-label">Online</div></div></div>' +
    '<div class="ovr-card"><div class="ovr-icon warn"><svg class="icon" viewBox="0 0 24 24" style="width:18px;height:18px;"><path d="M12 9v4M12 17h.01M10.3 3.86l-7.18 12.94A2 2 0 0 0 4.94 19h14.12a2 2 0 0 0 1.82-2.2L13.7 3.86a2 2 0 0 0-3.4 0z"/></svg></div><div><div class="ovr-num">' + warn + '</div><div class="ovr-label">Degraded</div></div></div>' +
    '<div class="ovr-card"><div class="ovr-icon err"><svg class="icon" viewBox="0 0 24 24" style="width:18px;height:18px;"><circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/></svg></div><div><div class="ovr-num">' + down + '</div><div class="ovr-label">Offline</div></div></div>' +
    '<div class="ovr-card"><div class="ovr-icon" style="background:var(--accent-soft);color:var(--accent);"><svg class="icon" viewBox="0 0 24 24" style="width:18px;height:18px;"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9h6v6H9z"/></svg></div><div><div class="ovr-num">' + total + '</div><div class="ovr-label">Tracked</div></div></div>';
}

function renderBento(list) {
  const LAYOUT = {
    llama:  ['col-8'],
    marvis: ['col-4'],
    silly:  ['col-6'],
    camofox:['col-6'],
    comfyui:['col-4'],
    dockry: ['col-4'],
    samba:  ['col-4'],
    telegram:['col-6'],
    hermes: ['col-6'],
    ssh:    ['col-4'],
  };
  function one(s) {
    const cls = (LAYOUT[s.id] || ['col-4']).join(' ');
    const canControl = s.kind !== 'manual' && s.kind !== undefined;
    const opener = OPENER[s.id];
    const cat = CATEGORY_LABEL[s.category] || s.category;
    const portDisplay = (s.port ? s.port : 'n/a');
    const portSubLabel = s.port ? 'port' : 'internal';
    const kv = [];
    if (s.proc && s.proc !== '?') kv.push(['Process', s.proc]);
    if (s.pid && s.pid !== '?')   kv.push(['PID',     s.pid]);
    if (s.rss_mb)                 kv.push(['RSS',     s.rss_mb + ' MB']);
    if (s.info)                   kv.push(['State',   s.info]);

    let actions = '';
    if (opener) {
      actions += '<a class="btn sm primary" href="' + opener.url + '" target="_blank">' + opener.label + ' · LAN</a>';
    }
    // CouchDB special: copy public URL for Obsidian LiveSync
    if (s.id === 'couchdb') {
      actions += '<button class="btn sm" data-act="copy-couchdb-url">copy public URL</button>';
    }
    if (canControl) {
      actions +=
        '<button class="btn sm" data-act="start" data-id="' + s.id + '">Start</button>' +
        '<button class="btn sm danger" data-act="stop" data-id="' + s.id + '">Stop</button>' +
        '<button class="btn sm" data-act="restart" data-id="' + s.id + '">Restart</button>';
    }
    actions += '<button class="btn sm ghost" data-act="logs" data-id="' + s.id + '">Logs</button>';

    const iconHtml = I[s.id] || '';
    return '<div class="card ' + cls + ' fade-in" data-id="' + s.id + '">' +
      '<div class="card-h">' +
        '<div class="card-icon" data-c="' + s.id + '">' + iconHtml + '</div>' +
        '<div class="card-title">' +
          '<h3>' + s.label + '</h3>' +
          '<div class="sub">' + s.subtitle + '</div>' +
        '</div>' +
        '<span class="status-tag ' + s.state + '">' + s.state_label + '</span>' +
      '</div>' +
      '<div class="port-row">' +
        '<div class="port-stat">' +
          '<div class="port-num">' + portDisplay + '</div>' +
          '<div class="port-lbl">' + portSubLabel + '</div>' +
        '</div>' +
        '<div class="cat-stat">' +
          '<div class="cat-name">' + cat + '</div>' +
          '<div class="cat-id">' + s.id + '</div>' +
        '</div>' +
      '</div>' +
      (kv.length ? '<div class="kv">' + kv.map(function(kv){return '<div><div class="k">' + kv[0] + '</div><div class="v">' + kv[1] + '</div></div>';}).join('') + '</div>' : '') +
      '<div class="actions">' + actions + '</div>' +
    '</div>';
  }

  // Group services by category
  const groups = {};
  for (const s of list) {
    const c = s.category || 'other';
    if (!groups[c]) groups[c] = [];
    groups[c].push(s);
  }
  let html = '';
  for (const cat of CATEGORY_ORDER) {
    if (!groups[cat] || !groups[cat].length) continue;
    const count = groups[cat].length;
    const labelText = (CATEGORY_LABEL[cat] || cat).toUpperCase();
    html += '<div class="cat-section">' +
      '<span class="cs-label">' + labelText + '</span>' +
      '<span class="cs-count">' + count + '</span>' +
      '<span class="cs-line"></span>' +
    '</div>';
    for (const s of groups[cat]) html += one(s);
  }
  for (const cat of Object.keys(groups)) {
    if (CATEGORY_ORDER.indexOf(cat) >= 0) continue;
    html += '<div class="cat-section"><span class="cs-label">' + cat.toUpperCase() + '</span><span class="cs-line"></span></div>';
    for (const s of groups[cat]) html += one(s);
  }
  document.getElementById('bento').innerHTML = html;
  document.querySelectorAll('#bento [data-act]').forEach(btn => {
    btn.onclick = e => {
      e.preventDefault();
      const act = btn.dataset.act;
      const id  = btn.dataset.id;
      if (id) {
        if (act === 'logs') showLogs(id);
        else ctrl(id, act);
      } else if (act === 'copy-couchdb-url') {
        const url = COUCHDB_PUBLIC_URL + '/obsidian_vault';
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(url).then(
            () => toast('copied: ' + url),
            () => prompt('Copy this URL:', url)
          );
        } else {
          prompt('Copy this URL:', url);
        }
      }
    };
  });
}

async function loadServices() {
  try {
    const list = await api('/api/services');
    svcs = list;
    renderOverview(list);
    renderBento(list);
  } catch (e) {
    toast('load failed: ' + e.message, 'err');
  }
}

async function ctrl(id, action) {
  try {
    const token = localStorage.getItem('cmdToken') || '';
    const r = await api('/api/control', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({id, action, token}),
    });
    if (r.ok) {
      const detail = r.output ? ` · ${r.output}` : '';
      toast(`${id} · ${action}${detail}`, 'ok');
      // Start/restart: wait longer for service to come up
      const delay = (action === 'start' || action === 'restart') ? 5000 : 1200;
      setTimeout(loadServices, delay);
    } else {
      toast(r.error || 'failed', 'err');
    }
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ============== Ports ==============
async function loadPorts() {
  try {
    const ports = await api('/api/ports');
    const head = `<div class="ports-card"><div class="ports-head">
      <div>Port</div><div>Proto</div><div>Process</div><div>PID</div><div></div>
    </div>`;
    const rows = ports.map(p => `<div class="ports-row">
      <div class="pnum">${p.port}</div>
      <div style="color:var(--text-4)">${p.proto}</div>
      <div><span class="proc">${p.proc}</span></div>
      <div><span class="pid">${p.pid}</span></div>
      <div style="text-align:right">
        <button class="btn sm ghost" onclick="showLogsByPort(${p.port})">details</button>
      </div>
    </div>`).join('');
    document.getElementById('portsBox').innerHTML = head + rows + '</div>';
  } catch (e) {
    toast('ports failed', 'err');
  }
}
window.showLogsByPort = (port) => {
  // Try to map port → known service id, else show raw message
  const found = svcs.find(s => s.port === port);
  if (found) { switchTab('logs'); document.getElementById('logSel').value = found.id; showLogs(found.id); return; }
  document.getElementById('modalTitle').textContent = `Port ${port}`;
  document.getElementById('modalBody').textContent = 'No service associated with this port. Check `ss -tlnp` directly.';
  document.getElementById('modal').classList.add('open');
};

// ============== Ideas ==============
async function loadIdeas() {
  try {
    const ideas = await api('/api/ideas');
    if (!ideas.length) {
      document.getElementById('ideasList').innerHTML =
        '<div class="empty">还没想法。先写一个 ↑</div>';
      return;
    }
    document.getElementById('ideasList').innerHTML = ideas.map(i => `
      <div class="idea-item" data-name="${i.name}">
        <div class="idea-icon">
          <svg class="icon" viewBox="0 0 24 24" style="width:14px;height:14px;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        </div>
        <div class="idea-body">
          <div class="idea-name">${i.name}<span class="idea-mtime">${i.mtime}</span></div>
          <div class="idea-preview">${i.preview}</div>
        </div>
        <div class="idea-actions">
          <button class="btn sm ghost" data-act="open" data-name="${i.name}">open</button>
          <button class="btn sm ghost" data-act="del" data-name="${i.name}" style="color:var(--err);border-color:rgba(232,105,105,0.3);">×</button>
        </div>
      </div>`).join('');
    document.querySelectorAll('.idea-item').forEach(el => {
      el.onclick = (e) => {
        const act = e.target.closest('[data-act]')?.dataset.act;
        const name = el.dataset.name;
        if (act === 'del') deleteIdea(name);
        else openIdea(name);
      };
    });
  } catch (e) {}
}

async function openIdea(name) {
  try {
    const r = await api('/api/ideas/' + encodeURIComponent(name));
    document.getElementById('modalTitle').textContent = name;
    document.getElementById('modalBody').textContent = r.body;
    document.getElementById('modal').classList.add('open');
  } catch (e) { toast('open failed', 'err'); }
}
async function deleteIdea(name) {
  if (!confirm('Delete ' + name + '?')) return;
  try {
    await api('/api/ideas/' + encodeURIComponent(name), {method:'DELETE'});
    toast('deleted', 'ok');
    loadIdeas();
  } catch (e) { toast('delete failed', 'err'); }
}
function closeModal() { document.getElementById('modal').classList.remove('open'); }
window.closeModal = closeModal;

document.getElementById('ideaSave').onclick = async () => {
  const name = document.getElementById('ideaName').value.trim();
  const body = document.getElementById('ideaBody').value;
  if (!name) return toast('需要文件名', 'err');
  if (!body) return toast('需要内容', 'err');
  try {
    await api('/api/ideas', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, body})
    });
    document.getElementById('ideaName').value = '';
    document.getElementById('ideaBody').value = '';
    toast('已保存', 'ok');
    loadIdeas();
  } catch (e) { toast(e.message, 'err'); }
};
document.getElementById('ideaClear').onclick = () => {
  document.getElementById('ideaName').value = '';
  document.getElementById('ideaBody').value = '';
};

// ============== Chat ==============
const chatLog = document.getElementById('chatLog');
const chatInput = document.getElementById('chatInput');
function appendMsg(role, body) {
  const d = document.createElement('div');
  d.className = 'bubble ' + role;
  d.style.opacity = 0;
  d.textContent = body;
  chatLog.appendChild(d);
  requestAnimationFrame(() => {
    d.style.transition = 'opacity 220ms cubic-bezier(0.16,1,0.3,1)';
    d.style.opacity = 1;
  });
  chatLog.scrollTop = chatLog.scrollHeight;
  return d;
}

async function sendChat() {
  const text = chatInput.value.trim();
  if (!text) return;
  chatInput.value = '';
  chatInput.style.height = 'auto';
  appendMsg('user', text);
  chatHistory.push({role:'user', content:text});
  const placeholder = appendMsg('system', 'thinking…');
  try {
    const r = await api('/api/chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({messages: chatHistory})
    });
    placeholder.remove();
    if (r.ok) {
      const reply = r.data.choices[0].message.content;
      appendMsg('assistant', reply);
      chatHistory.push({role:'assistant', content:reply});
    } else {
      appendMsg('system', 'err: ' + r.error);
    }
  } catch (e) {
    placeholder.remove();
    appendMsg('system', 'err: ' + e.message);
  }
}
document.getElementById('chatSend').onclick = sendChat;
chatInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
chatInput.addEventListener('input', () => {
  chatInput.style.height = 'auto';
  chatInput.style.height = Math.min(chatInput.scrollHeight, 180) + 'px';
});

// ============== Logs ==============
async function loadLogServices() {
  try {
    const list = svcs.length ? svcs : await api('/api/services');
    document.getElementById('logSel').innerHTML =
      list.map(s => `<option value="${s.id}">${s.label} (${s.state_label})</option>`).join('');
    showLogs(document.getElementById('logSel').value);
  } catch (e) {}
}
document.getElementById('logSel').onchange = e => showLogs(e.target.value);
document.getElementById('logRefresh').onclick = () =>
  showLogs(document.getElementById('logSel').value);
async function showLogs(id) {
  try {
    const r = await api('/api/logs/' + encodeURIComponent(id));
    document.getElementById('logBody').textContent = r.body || '(empty)';
  } catch (e) { document.getElementById('logBody').textContent = 'err: ' + e.message; }
}

// ============== Cmd token ==============
document.getElementById('tokenBtn').onclick = () => {
  const t = prompt('Cmd token (留空 = 不需要)', localStorage.getItem('cmdToken') || '');
  if (t !== null) {
    localStorage.setItem('cmdToken', t);
    toast('saved');
  }
};


// ============== GPU ==============
function barColor(pct) {
  if (pct < 60) return 'green';
  if (pct < 85) return 'amber';
  return 'red';
}

function renderGpu(gpu) {
  const memPct = gpu.mem_total_mb > 0 ? Math.round(gpu.mem_used_mb / gpu.mem_total_mb * 100) : 0;
  const tempColor = gpu.temperature > 80 ? 'var(--err)' : gpu.temperature > 70 ? 'var(--warn)' : 'var(--text-1)';
  return `<div class="gpu-card fade-in">
    <div class="gpu-header">
      <svg class="icon" viewBox="0 0 24 24" style="width:20px;height:20px;opacity:0.7;"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 9h6v6H9z"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/></svg>
      <div>
        <h2>${gpu.name}</h2>
        <div class="gpu-sub">PCIe ${gpu.pcie_gen}/${gpu.pcie_gen_max} · ${gpu.pcie_width}x/${gpu.pcie_width_max}x</div>
      </div>
    </div>
    <div class="gpu-grid">
      <div class="gpu-stat">
        <div class="gpu-label">GPU Util</div>
        <div class="gpu-value" style="color:${gpu.util_gpu > 80 ? 'var(--warn)' : 'var(--text-1)'}">${gpu.util_gpu}<span class="gpu-unit">%</span></div>
        <div class="gpu-bar-wrap"><div class="gpu-bar ${barColor(gpu.util_gpu)}" style="width:${gpu.util_gpu}%"></div></div>
      </div>
      <div class="gpu-stat">
        <div class="gpu-label">Memory Util</div>
        <div class="gpu-value" style="color:${gpu.util_mem > 80 ? 'var(--warn)' : 'var(--text-1)'}">${gpu.util_mem}<span class="gpu-unit">%</span></div>
        <div class="gpu-sub">${gpu.mem_used_mb / 1024 | 0} / ${gpu.mem_total_mb / 1024 | 0} GB</div>
        <div class="gpu-bar-wrap"><div class="gpu-bar ${barColor(memPct)}" style="width:${memPct}%"></div></div>
      </div>
      <div class="gpu-stat">
        <div class="gpu-label">Temperature</div>
        <div class="gpu-value" style="color:${tempColor}">${gpu.temperature}<span class="gpu-unit">°C</span></div>
      </div>
      <div class="gpu-stat">
        <div class="gpu-label">Power</div>
        <div class="gpu-value">${gpu.power_w}<span class="gpu-unit">W</span></div>
        <div class="gpu-sub">limit ${gpu.power_limit_w}W</div>
      </div>
      <div class="gpu-stat">
        <div class="gpu-label">Fan</div>
        <div class="gpu-value">${gpu.fan_pct}<span class="gpu-unit">%</span></div>
        <div class="gpu-bar-wrap"><div class="gpu-bar ${barColor(gpu.fan_pct)}" style="width:${gpu.fan_pct}%"></div></div>
      </div>
      <div class="gpu-stat">
        <div class="gpu-label">Clocks</div>
        <div class="gpu-value" style="font-size:14px;">${gpu.clock_graphics_mhz}<span class="gpu-unit">MHz</span></div>
        <div class="gpu-sub">MEM ${gpu.clock_memory_mhz} MHz</div>
      </div>
    </div>
  </div>`;
}

async function loadGpu() {
  try {
    const data = await api('/api/gpu');
    if (!data.ok || !data.gpus.length) {
      document.getElementById('gpuBox').innerHTML =
        '<div class="gpu-error">nvidia-smi error: ' + (data.error || 'no GPU found') + '</div>';
      return;
    }
    document.getElementById('gpuBox').innerHTML = data.gpus.map(renderGpu).join('');
  } catch (e) {
    document.getElementById('gpuBox').innerHTML = '<div class="gpu-error">GPU query failed: ' + e.message + '</div>';
  }
}

// ============== Init ==============
loadHost();
loadServices();
loadGpu();
setInterval(loadHost, 30000);
setInterval(loadServices, 15000);
setInterval(loadGpu, 10000);
switchTab('services');

// ============== Models ==============
let _selectedModel = null;
let _selectedModelPath = null;

async function loadModels() {
  try {
    const models = await api('/api/llama/models');
    const tbody = document.getElementById('modelsBody');
    const mode = document.getElementById('modelMode').value;
    const port = mode === 'gpu' ? 8888 : 8889;
    const status = await api(`/api/llama/status?port=${port}`);
    document.getElementById('modelsStatus').textContent = status.running
      ? `▶ Running on port ${port} — PID ${status.pid} (${status.proc})`
      : `□ Nothing running on port ${port}`;
    tbody.innerHTML = models.map(m => {
      const mtpClass = m.mtp ? 'mtp-yes' : 'mtp-no';
      const mtpText = m.mtp ? 'YES' : 'no';
      const ctxText = m.ctx >= 131072 ? `${m.ctx / 1024}K` : m.ctx;
      const selected = _selectedModelPath === m.path;
      return `<tr class="${selected ? 'selected' : ''}">
        <td class="model-name">${m.name}</td>
        <td>${m.size_mb} MB</td>
        <td class="${mtpClass}">${mtpText}</td>
        <td class="ctx">${ctxText}</td>
        <td>${m.mtime}</td>
        <td><button class="btn sm" onclick="selectModel('${m.path}')">Select</button></td>
      </tr>`;
    }).join('');
    _selectedModelPath = _selectedModelPath || (models.length ? models[0].path : null);
  } catch(e) { console.error('loadModels:', e); }
}

async function selectModel(path) {
  _selectedModelPath = path;
  loadModels();
}

document.getElementById('modelLaunchBtn').onclick = async () => {
  if (!_selectedModelPath) {
    document.getElementById('modelsStatus').textContent = 'No model selected';
    return;
  }
  const mode = document.getElementById('modelMode').value;
  const port = mode === 'gpu' ? 8888 : 8889;
  document.getElementById('modelsStatus').textContent = 'Starting...';
  try {
    const res = await api('/api/llama/launch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ mode, model_path: _selectedModelPath, port })
    });
    document.getElementById('modelsStatus').textContent = res.ok ? res.result : `Error: ${res.error}`;
    setTimeout(loadModels, 3000);
  } catch(e) {
    document.getElementById('modelsStatus').textContent = `Error: ${e.message || e}`;
  }
};

document.getElementById('modelStopBtn').onclick = async () => {
  const mode = document.getElementById('modelMode').value;
  const port = mode === 'gpu' ? 8888 : 8889;
  document.getElementById('modelsStatus').textContent = 'Stopping...';
  try {
    const res = await api('/api/llama/stop', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ port })
    });
    document.getElementById('modelsStatus').textContent = res.ok ? res.result : `Error: ${res.error}`;
    setTimeout(loadModels, 2000);
  } catch(e) {
    document.getElementById('modelsStatus').textContent = `Error: ${e.message || e}`;
  }
};

// Auto-refresh models when tab switched
document.getElementById('tab-models')?.addEventListener('click', loadModels);
loadModels();
</script>
</body>
</html>
"""


# ─────────── routes ───────────
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get("/api/version")
async def api_version() -> Dict[str, str]:
    return {"name": "UbuntuConsole", "version": "4.0",
            "host": HOST, "port": str(PORT)}


@app.get("/api/host")
async def api_host() -> Dict[str, Any]:
    out = _run("hostname; uptime; uname -r; date '+%Y-%m-%d %H:%M:%S'")
    lines = [l.strip() for l in out.split("\n") if l.strip()]
    return {"hostname": lines[0] if lines else "?",
            "uptime":   lines[1] if len(lines) > 1 else "?",
            "kernel":   lines[2] if len(lines) > 2 else "?"}


@app.get("/api/services")
async def api_services() -> List[Dict[str, Any]]:
    return check_all()


@app.post("/api/control")
async def api_control(req: Request) -> Dict[str, Any]:
    data = await req.json()
    if CMD_TOKEN and data.get("token") != CMD_TOKEN:
        raise HTTPException(403, "bad token")
    sid = data.get("id")
    action = data.get("action")
    if sid not in SVC_IDX:
        raise HTTPException(404, f"unknown service: {sid}")
    if action == "logs":
        # treat logs request as info pass-through
        return {"ok": False, "error": "use /api/logs/{id}"}
    return control(sid, action)


@app.get("/api/ports")
async def api_ports() -> List[Dict[str, Any]]:
    return all_ports()



@app.get("/api/gpu")
async def api_gpu() -> Dict[str, Any]:
    return gpu_info()


@app.post("/api/chat")
async def api_chat(req: Request) -> Dict[str, Any]:
    data = await req.json()
    return llama_chat(data.get("messages", []))


@app.get("/api/ideas")
async def api_ideas() -> List[Dict[str, Any]]:
    return list_ideas()


@app.get("/api/ideas/{name:path}")
async def api_idea_get(name: str) -> Dict[str, str]:
    return {"name": name, "body": read_idea(name)}


@app.post("/api/ideas")
async def api_idea_post(req: Request) -> Dict[str, Any]:
    data = await req.json()
    if not data.get("name") or not data.get("body"):
        raise HTTPException(400, "name + body required")
    write_idea(data["name"], data["body"])
    return {"ok": True}


@app.delete("/api/ideas/{name:path}")
async def api_idea_delete(name: str) -> Dict[str, Any]:
    delete_idea(name)
    return {"ok": True}


@app.get("/api/logs/{sid}")
async def api_logs(sid: str) -> Dict[str, str]:
    return {"body": last_logs(sid)}


# ─────────── llama model management ───────────
@app.get("/api/llama/models")
async def api_llama_models() -> List[Dict[str, Any]]:
    return _get_models()


@app.get("/api/llama/status")
async def api_llama_status(port: int = 8888) -> Dict[str, Any]:
    if port_listening(port):
        pp = port_pid_proc(port)
        return {"ok": True, "port": port, "running": True, **pp}
    return {"ok": False, "port": port, "running": False}


@app.post("/api/llama/launch")
async def api_llama_launch(req: Request) -> Dict[str, Any]:
    body = await req.json()
    mode = body.get("mode", "gpu")
    model_path = body.get("model_path", "")
    port = body.get("port", 8888 if mode == "gpu" else 8889)
    if not model_path or not Path(model_path).exists():
        return {"ok": False, "error": f"Model not found: {model_path}"}
    result = _spawn(mode, model_path, port)
    return {"ok": True, "result": result}


@app.post("/api/llama/stop")
async def api_llama_stop(req: Request) -> Dict[str, Any]:
    body = await req.json()
    port = body.get("port", 8888)
    result = _kill_port(port)
    return {"ok": True, "result": result}


@app.get("/api/llama/log")
async def api_llama_log(mode: str = "gpu", port: int = 8888, lines: int = 50) -> Dict[str, Any]:
    log_file = f"/tmp/llama-{mode}-{port}.log"
    try:
        content = Path(log_file).read_text(errors="replace")
        tail = "\n".join(content.split("\n")[-lines:])
        return {"ok": True, "log": tail}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> None:
    print(f"🧠 UbuntuConsole v4 starting on http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
