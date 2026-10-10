"""Read-only local dashboard for agent coordination (teamwork-agent).

Run:
    python scripts/agent_dashboard.py
    Open http://127.0.0.1:18900

The server only reads coordination files and never writes to them. It is
observation, not infrastructure: if it is not installed or not running, the
team's work continues unchanged.

Requires fastapi + uvicorn. Nothing else in the template depends on them, so a
project can adopt the protocol without installing a web stack.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

ROOT = Path(__file__).resolve().parent.parent
HANDOFF = ROOT / ".agent-handoff.json"
PROTOCOL = ROOT / ".agent-protocol.json"
STATUS = ROOT / "docs" / "agent-status.json"
EVENTS = ROOT / ".agent-events.jsonl"
HTML = Path(__file__).with_name("agent_dashboard.html")

#: Порт дашборда. Держите его в диапазоне из boundaries.test_ports протокола,
#: иначе два локальных сервера столкнутся, и это будет выглядеть как падение.
PORT = 18900

app = FastAPI(title="Agent Control Room")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_bytes().decode("utf-8-sig"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _read_events(limit: int) -> list[dict]:
    if not EVENTS.exists():
        return []
    try:
        lines = EVENTS.read_text(encoding="utf-8").splitlines()[-limit:]
    except (OSError, UnicodeDecodeError):
        return []
    result = []
    for line in reversed(lines):
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result

_NET_LAST = {"ts": time.monotonic(), "bytes": None}


def _system_metrics() -> dict:
    """Best-effort host metrics; failures never affect the dashboard."""
    now = time.monotonic()
    cpu = None
    try:
        if platform.system() == "Windows":
            import ctypes
            class FILETIME(ctypes.Structure):
                _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]
            idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
            if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                def value(x): return (x.high << 32) | x.low
                current = (value(kernel), value(user), value(idle))
                previous = getattr(_system_metrics, "_times", None)
                _system_metrics._times = current
                if previous:
                    total = sum(current[i] - previous[i] for i in range(3))
                    busy = (current[0] - previous[0]) + (current[1] - previous[1])
                    cpu = round(100 * busy / total, 1) if total else None
        else:
            load = os.getloadavg()[0]
            cpu = round(100 * load / max(os.cpu_count() or 1, 1), 1)
    except Exception:
        pass
    ram = None
    try:
        import psutil
        mem = psutil.virtual_memory()
        ram = {"percent": mem.percent, "used_mb": round(mem.used / 1048576), "total_mb": round(mem.total / 1048576)}
    except Exception:
        pass
    disk = None
    try:
        usage = shutil.disk_usage(ROOT)
        disk = {"percent": round(100 * usage.used / usage.total, 1), "free_gb": round(usage.free / 1073741824, 1), "total_gb": round(usage.total / 1073741824, 1)}
    except OSError:
        pass
    network = None
    try:
        import psutil
        counters = psutil.net_io_counters()
        previous = _NET_LAST.get("bytes")
        elapsed = max(now - _NET_LAST["ts"], 0.001)
        _NET_LAST.update(ts=now, bytes=(counters.bytes_sent, counters.bytes_recv))
        network = {"sent_mb": round(counters.bytes_sent / 1048576, 1), "recv_mb": round(counters.bytes_recv / 1048576, 1), "send_kbps": None, "recv_kbps": None}
        if previous:
            network["send_kbps"] = round((counters.bytes_sent - previous[0]) / 1024 / elapsed, 1)
            network["recv_kbps"] = round((counters.bytes_recv - previous[1]) / 1024 / elapsed, 1)
    except Exception:
        pass
    temperature = {"available": False, "cpu_c": None, "gpu_c": None, "reason": "hardware sensors unavailable"}
    try:
        import psutil
        sensors = psutil.sensors_temperatures()
        values = [item.current for group in sensors.values() for item in group if item.current is not None]
        if values:
            temperature.update(available=True, cpu_c=round(max(values), 1), reason=None)
    except Exception:
        pass
    return {"cpu_percent": cpu, "memory": ram, "disk": disk, "network": network, "temperature": temperature, "platform": platform.platform(), "python": platform.python_version()}


def _summary() -> dict:
    """Сводка для верхних плиток дашборда.

    Health score — ЧИСЛО, придуманное этим файлом, и оно обязано это
    признавать: у него нет калибровки, нет базы сравнения и оно не является
    гейтом. Показывать его можно только рядом с исходными счётчиками
    (open_p0, open_p1, live_claims), по которым его можно пересчитать руками.

    Приоритеты берутся ИЗ ПРОТОКОЛА (backlog), а не из журнала: backlog —
    канонический список P0–P4, а список `priorities` в журнале заполняют
    агенты вручную и он молча устаревает. Раньше расхождение двух источников
    давало «здоровый» дашборд при открытых P0 в протоколе.

    Claims считаются ЖИВЫМИ (через is_stale_claim из agent_handoff), иначе
    ушедший агент с протухшим claim'ом выглядит как занятый слот и «съедает»
    лимит агентов.
    """
    status_data = _read_json(STATUS)
    handoff = _read_json(HANDOFF)
    protocol = _read_json(PROTOCOL)
    claims = handoff.get("team_protocol", {}).get("active_claims", {})

    backlog = protocol.get("backlog") or []
    open_p0 = sum(1 for i in backlog if i.get("priority") == "P0" and i.get("status") != "done")
    open_p1 = sum(1 for i in backlog if i.get("priority") == "P1" and i.get("status") != "done")

    try:
        from agent_handoff import is_stale_claim  # тот же критерий, что и в CLI
    except ImportError:  # дашборд запущен без scripts/ в sys.path
        is_stale_claim = lambda c: False  # noqa: E731
    live_claims = sum(1 for c in claims.values() if not is_stale_claim(c))
    stale_claims = len(claims) - live_claims

    active = sum(
        1 for agent in status_data.get("agents", {}).values()
        if str(agent.get("status", "")).upper() in ("IN_PROGRESS", "WIP", "WORKING")
    )

    max_agents = (protocol.get("claims") or {}).get("max_agents")
    health = max(0, 100 - open_p0 * 40 - open_p1 * 15 - stale_claims * 5)
    return {
        "health_score": health,
        "health_score_is_advisory": True,
        "active_agents": active,
        "active_claims": live_claims,
        "stale_claims": stale_claims,
        "max_agents": max_agents,
        "open_p0": open_p0,
        "open_p1": open_p1,
        "task_log_size": len(handoff.get("task_log", [])),
        "metrics": _system_metrics(),
        "last_gate": status_data.get("current_gate", {}),
    }

@app.get("/api/summary")
def summary() -> dict:
    return _summary()

@app.get("/api/status")
def status() -> dict:
    status_data = _read_json(STATUS)
    handoff = _read_json(HANDOFF)
    protocol = _read_json(PROTOCOL)
    claims = handoff.get("team_protocol", {}).get("active_claims", {})
    return {
        "status": status_data,
        "claims": claims,
        "hints": handoff.get("hints", []),
        "task_log_size": len(handoff.get("task_log", [])),
        "protocol_version": protocol.get("protocol_version"),
        "project": protocol.get("project"),
        "max_agents": (protocol.get("claims") or {}).get("max_agents"),
        "read_only": True,
    }


@app.get("/api/metrics")
def metrics() -> dict:
    return _system_metrics()


@app.get("/api/tasks")
def tasks(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
    return _read_json(HANDOFF).get("task_log", [])[-limit:][::-1]


@app.get("/api/events")
def events(limit: int = Query(200, ge=1, le=1000)) -> list[dict]:
    return _read_events(limit)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    try:
        return HTML.read_text(encoding="utf-8")
    except OSError:
        return "<h1>Agent Dashboard</h1><p>UI file is unavailable.</p>"


if __name__ == "__main__":
    import uvicorn

    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    except KeyboardInterrupt:
        print("\nдашборд остановлен")
