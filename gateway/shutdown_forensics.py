"""Shutdown forensics — capture context when the gateway receives SIGTERM/SIGINT.

The gateway's ``shutdown_signal_handler`` runs synchronously inside the
asyncio event loop.  We can't safely block it for long, but we DO want a
durable record of who/what triggered the shutdown so that "the gateway
keeps dying" incidents can be diagnosed after the fact.

This module exposes :func:`snapshot_shutdown_context`, a fast (<10ms),
non-blocking probe that returns a structured dict the signal handler can
log immediately, plus :func:`spawn_async_diagnostic`, a fire-and-forget
``ps`` walk that runs as a detached subprocess so it can't block teardown
even if /proc is wedged.

Anything that needs to wait (e.g. shelling out to ``ps aux``) belongs in
the async helper, never in the synchronous probe.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_SIGNAL_NAME_BY_NUM: Dict[int, str] = {}
for _name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2"):
    _val = getattr(signal, _name, None)
    if _val is not None:
        _SIGNAL_NAME_BY_NUM[int(_val)] = _name


def _signal_name(sig: Any) -> str:
    """Return a human-readable signal name (or ``str(sig)`` as fallback)."""
    if sig is None:
        return "UNKNOWN"
    try:
        sig_int = int(sig)
    except (TypeError, ValueError):
        return str(sig)
    return _SIGNAL_NAME_BY_NUM.get(sig_int, f"signal#{sig_int}")


_SYSTEMD_LOOKUP_BUDGET_SECONDS = 0.25
_SYSTEMD_STATUS_TIMEOUT_SECONDS = 0.20
_GATEWAY_SHUTDOWN_MARKER = "hermes-gateway-last-shutdown.json"
_SYSTEMD_UNIT_SUFFIXES = (".service", ".scope")


def _remaining_timeout(deadline: float, cap: float = _SYSTEMD_STATUS_TIMEOUT_SECONDS) -> float:
    """Return a bounded subprocess timeout for the remaining lookup budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0.0
    return max(0.001, min(cap, remaining))


def _parse_systemctl_show(stdout: str) -> Dict[str, str]:
    """Parse stable ``systemctl show`` Key=Value rows."""
    values: Dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _parse_busctl_uint(stdout: str) -> Optional[int]:
    """Parse ``busctl get-property`` integer output such as ``u 3``."""
    match = re.search(r"(?:^|\s)(?:u|t|i|x)\s+(\d+)(?:\s|$)", stdout.strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _parse_busctl_object_path(stdout: str) -> Optional[str]:
    """Parse ``busctl call ... GetUnit`` output, returning the object path."""
    match = re.search(r'"([^"]+)"', stdout)
    if match:
        return match.group(1)
    for part in stdout.strip().split():
        if part.startswith("/"):
            return part
    return None


def _derive_unit_name_from_cgroup(path: str = "/proc/self/cgroup") -> Optional[str]:
    """Best-effort cgroup slice → systemd unit derivation."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except (FileNotFoundError, PermissionError, OSError):
        return None

    fallback: Optional[str] = None
    for line in lines:
        cgroup = line.strip().split(":", 2)[-1]
        for segment in reversed([p for p in cgroup.split("/") if p]):
            if not segment.endswith(_SYSTEMD_UNIT_SUFFIXES):
                continue
            if not fallback:
                fallback = segment
            if "gateway" in segment or "hermes" in segment:
                return segment
    return fallback


def _read_proc_field(pid: int, key: str) -> Optional[str]:
    """Read a single field from /proc/<pid>/status.  Linux only; None elsewhere."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return line.split(":", 1)[1].strip()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return None


def _read_proc_cmdline(pid: int) -> Optional[str]:
    """Read /proc/<pid>/cmdline as a printable string.  Linux only; None elsewhere."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            data = fh.read()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if not data:
        return None
    # cmdline uses NUL separators
    return data.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _proc_summary(pid: int) -> Dict[str, Any]:
    """Compact /proc/<pid> snapshot: pid, ppid, state, uid, cmdline.

    Best-effort.  Missing fields are simply omitted rather than raising.
    """
    summary: Dict[str, Any] = {"pid": pid}
    if pid <= 0:
        return summary
    name = _read_proc_field(pid, "Name")
    if name is not None:
        summary["name"] = name
    state = _read_proc_field(pid, "State")
    if state is not None:
        summary["state"] = state
    ppid = _read_proc_field(pid, "PPid")
    if ppid is not None:
        try:
            summary["ppid"] = int(ppid)
        except ValueError:
            pass
    uid = _read_proc_field(pid, "Uid")
    if uid is not None:
        # "real effective saved fs"
        summary["uid"] = uid.split()[0] if uid else uid
    cmdline = _read_proc_cmdline(pid)
    if cmdline:
        # Truncate aggressively — these can be 4KB
        summary["cmdline"] = cmdline[:300]
    return summary


def _candidate_unit_names(ctx: Dict[str, Any]) -> List[str]:
    """Return unit-name candidates without emitting ``(unknown)``."""
    candidates: List[str] = []
    cgroup_unit = _derive_unit_name_from_cgroup()
    if cgroup_unit:
        candidates.append(cgroup_unit)
    env_unit = os.environ.get("SYSTEMD_UNIT")
    if env_unit:
        candidates.append(env_unit)
    try:
        from hermes_cli.gateway import get_service_name

        service_name = get_service_name()
        if service_name:
            if not service_name.endswith(".service"):
                service_name = f"{service_name}.service"
            candidates.append(service_name)
    except Exception:
        pass
    parent = ctx.get("parent") or {}
    parent_cmd = str(parent.get("cmdline") or "")
    if "hermes-gateway" in parent_cmd:
        candidates.append("hermes-gateway.service")

    seen = set()
    unique = []
    for unit in candidates:
        if unit and unit not in seen:
            unique.append(unit)
            seen.add(unit)
    return unique


def _run_bounded(
    cmd: List[str],
    *,
    deadline: float,
    field: str,
    timeouts: List[str],
    cap: float = _SYSTEMD_STATUS_TIMEOUT_SECONDS,
) -> Optional[subprocess.CompletedProcess[str]]:
    """Run a subprocess only while the shared lookup budget has time left."""
    timeout = _remaining_timeout(deadline, cap)
    if timeout <= 0:
        timeouts.append(field)
        return None
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        timeouts.append(field)
    except (FileNotFoundError, OSError):
        pass
    return None


def _systemctl_show_unit(
    unit_name: str,
    *,
    deadline: float,
    timeouts: List[str],
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    properties = (
        "Id,NRestarts,StartLimitBurst,StartLimitIntervalUSec,"
        "ActiveState,SubState,Result"
    )
    for scope in (["--user"], []):
        result = _run_bounded(
            [
                "systemctl",
                *scope,
                "show",
                unit_name,
                "--no-pager",
                f"--property={properties}",
            ],
            deadline=deadline,
            field="systemctl_show",
            timeouts=timeouts,
        )
        if result is None or result.returncode != 0:
            continue
        values = _parse_systemctl_show(result.stdout or "")
        if values:
            return values, "systemctl_user" if scope else "systemctl_system"
    return None, None


def _dbus_n_restarts(
    unit_name: str,
    *,
    deadline: float,
    timeouts: List[str],
) -> Optional[int]:
    """Read Service.NRestarts through systemd's D-Bus API via busctl."""
    for scope in (["--user"], []):
        get_unit = _run_bounded(
            [
                "busctl",
                *scope,
                "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "GetUnit",
                "s",
                unit_name,
            ],
            deadline=deadline,
            field="dbus_get_unit",
            timeouts=timeouts,
        )
        if get_unit is None or get_unit.returncode != 0:
            continue
        object_path = _parse_busctl_object_path(get_unit.stdout or "")
        if not object_path:
            continue
        prop = _run_bounded(
            [
                "busctl",
                *scope,
                "get-property",
                "org.freedesktop.systemd1",
                object_path,
                "org.freedesktop.systemd1.Service",
                "NRestarts",
            ],
            deadline=deadline,
            field="n_restarts",
            timeouts=timeouts,
        )
        if prop is None or prop.returncode != 0:
            continue
        parsed = _parse_busctl_uint(prop.stdout or "")
        if parsed is not None:
            return parsed
    return None


def _recent_status_reason(
    unit_name: str,
    *,
    deadline: float,
    timeouts: List[str],
) -> Optional[str]:
    """Return ``Active: ...; Reason: ...`` from ``systemctl status`` if quick."""
    for scope in (["--user"], []):
        result = _run_bounded(
            ["systemctl", *scope, "status", unit_name, "--no-pager", "--full"],
            deadline=deadline,
            field="recent_status_reason",
            timeouts=timeouts,
            cap=_SYSTEMD_STATUS_TIMEOUT_SECONDS,
        )
        if result is None or result.returncode != 0:
            continue
        active = None
        reason = None
        for line in (result.stdout or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("Active:") and active is None:
                active = stripped
            if "Reason:" in stripped:
                reason = stripped[stripped.rfind("Reason:"):]
        parts = [part for part in (active, reason) if part]
        if parts:
            return "; ".join(parts)[:240]
    return None


def _detect_paired_shutdowns(hermes_home: Optional[Path] = None) -> Optional[str]:
    """Best-effort sibling shutdown heuristic from the local gateway log.

    We inspect only the last two existing ``Shutdown context:`` entries in
    ``$HERMES_HOME/logs/gateway.log``.  If the file was touched within five
    seconds and those entries include ``pid=<n>``, we report those PIDs as
    ``paired_with``.  This intentionally avoids journal scans in the signal path
    and may miss older legacy lines that did not include the gateway PID.
    """
    try:
        home = hermes_home or Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        log_path = home / "logs" / "gateway.log"
        if not log_path.exists() or time.time() - log_path.stat().st_mtime > 5.0:
            return None
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-200:]
    except (OSError, ValueError):
        return None
    pids: List[str] = []
    for line in [line for line in lines if "Shutdown context:" in line][-2:]:
        match = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", line)
        if match and match.group(1) != str(os.getpid()) and match.group(1) not in pids:
            pids.append(match.group(1))
    return ",".join(pids) if pids else None


def enrich_systemd_shutdown_context(ctx: Dict[str, Any]) -> None:
    """Add bounded systemd triage fields to a shutdown context in-place."""
    if not ctx.get("under_systemd"):
        return
    deadline = time.monotonic() + _SYSTEMD_LOOKUP_BUDGET_SECONDS
    timeouts: List[str] = []
    degraded = False

    invocation_id = os.environ.get("INVOCATION_ID")
    if invocation_id:
        ctx["invocation_id"] = invocation_id

    unit_name = None
    systemctl_values: Optional[Dict[str, str]] = None
    for candidate in _candidate_unit_names(ctx):
        if unit_name is None:
            unit_name = candidate
        values, source = _systemctl_show_unit(candidate, deadline=deadline, timeouts=timeouts)
        if values:
            systemctl_values = values
            unit_name = values.get("Id") or candidate
            ctx["unit_name_source"] = source
            break
    if unit_name:
        ctx["unit_name"] = unit_name

    n_restarts = _dbus_n_restarts(unit_name, deadline=deadline, timeouts=timeouts) if unit_name else None
    if n_restarts is not None:
        ctx["n_restarts"] = n_restarts
        ctx["n_restarts_source"] = "dbus"
    elif systemctl_values:
        raw = systemctl_values.get("NRestarts")
        if raw and raw.isdigit():
            ctx["n_restarts"] = int(raw)
            ctx["n_restarts_source"] = "systemctl"
    else:
        degraded = True

    if systemctl_values:
        for src, dst in (
            ("StartLimitBurst", "start_limit_burst"),
            ("StartLimitIntervalUSec", "start_limit_interval_usec"),
        ):
            raw = systemctl_values.get(src)
            if raw:
                ctx[dst] = raw

    if unit_name:
        status = _recent_status_reason(unit_name, deadline=deadline, timeouts=timeouts)
        if status:
            ctx["recent_status_reason"] = status

    paired = _detect_paired_shutdowns()
    if paired:
        ctx["paired_with"] = paired

    if timeouts:
        ctx["_lookup_timeout"] = ",".join(dict.fromkeys(timeouts))
    if degraded or timeouts:
        ctx["_introspection"] = "degraded"


def snapshot_shutdown_context(received_signal: Any = None) -> Dict[str, Any]:
    """Fast (<10ms) snapshot of who/what is asking us to shut down.

    Captures:

    * The signal number/name (so SIGINT vs SIGTERM is visible)
    * Our own PID/ppid + parent process info from /proc (Linux)
    * Whether systemd is our parent (``ppid==1`` or ``INVOCATION_ID`` set)
    * Whether takeover/planned-stop markers exist (consumed lazily by the caller)
    * /proc/self limits + load average (1-min)
    * Wall-clock and monotonic timestamps for cross-correlating later phases

    Pure stdlib, never raises, never blocks on subprocesses.
    """
    now = time.time()
    monotonic = time.monotonic()
    pid = os.getpid()
    ppid = os.getppid()

    ctx: Dict[str, Any] = {
        "ts": now,
        "ts_monotonic": monotonic,
        "signal": _signal_name(received_signal),
        "signal_num": int(received_signal) if received_signal is not None else None,
        "pid": pid,
        "ppid": ppid,
        "parent": _proc_summary(ppid),
        "self": _proc_summary(pid),
    }

    # systemd context.  If we were started by a systemd unit, INVOCATION_ID
    # is set in our env.  ppid==1 (init) is also a strong signal that
    # systemd reaped+forwarded the SIGTERM.
    invocation_id = os.environ.get("INVOCATION_ID")
    if invocation_id:
        ctx["systemd_invocation_id"] = invocation_id
    journal_stream = os.environ.get("JOURNAL_STREAM")
    if journal_stream:
        ctx["systemd_journal_stream"] = journal_stream
    ctx["under_systemd"] = bool(invocation_id) or ppid == 1

    # Load average — high load points the finger at "something else
    # crushing the box" rather than "external killer".
    try:
        ctx["loadavg_1m"] = os.getloadavg()[0]
    except (OSError, AttributeError):
        pass

    # /proc/self/status TracerPid: nonzero means a debugger / strace is
    # attached.  Useful when "phantom SIGKILL" turns out to be a manual
    # gdb session.
    try:
        tracer = _read_proc_field(pid, "TracerPid")
        if tracer is not None and tracer != "0":
            ctx["tracer_pid"] = int(tracer) if tracer.isdigit() else tracer
            ctx["tracer"] = _proc_summary(int(tracer)) if tracer.isdigit() else None
    except (TypeError, ValueError):
        pass

    # Race-detection hint: did somebody recently start a sibling gateway
    # with --replace?  We can't see the new process directly here, but if
    # there's a takeover marker on disk that DOESN'T name us, that's a
    # smoking gun for "another --replace instance is killing us".
    # Filenames mirror gateway.status (._TAKEOVER_MARKER_FILENAME /
    # _PLANNED_STOP_MARKER_FILENAME); we use string literals here so the
    # signal-handler path stays import-light.
    try:
        hermes_home_str = os.environ.get("HERMES_HOME")
        if hermes_home_str:
            takeover_path = Path(hermes_home_str) / ".gateway-takeover.json"
            if takeover_path.exists():
                try:
                    raw = takeover_path.read_text(encoding="utf-8")
                    ctx["takeover_marker"] = raw[:300]
                    ctx["takeover_marker_for_self"] = (
                        f'"target_pid": {pid}' in raw
                        or f"'target_pid': {pid}" in raw
                    )
                except OSError:
                    pass
            planned_stop_path = Path(hermes_home_str) / ".gateway-planned-stop.json"
            if planned_stop_path.exists():
                try:
                    raw = planned_stop_path.read_text(encoding="utf-8")
                    ctx["planned_stop_marker"] = raw[:300]
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 — never raise from a signal handler
        pass

    try:
        enrich_systemd_shutdown_context(ctx)
    except Exception as exc:  # noqa: BLE001 — never raise from a signal handler
        ctx["_introspection"] = "degraded"
        ctx["_introspection_error"] = type(exc).__name__

    return ctx


def spawn_async_diagnostic(
    log_path: Path,
    signal_name: str,
    *,
    timeout_seconds: float = 5.0,
) -> Optional[int]:
    """Fire-and-forget ``ps``-style snapshot written to ``log_path``.

    Runs as a detached subprocess so it can't block the asyncio event loop
    or compete with platform teardown.  The subprocess uses its own
    ``timeout`` so a wedged ``ps`` still self-cleans within
    ``timeout_seconds``.

    Returns the subprocess PID on success, ``None`` on failure.  Never
    raises.

    We deliberately avoid ``subprocess.run(["ps", "aux"])`` from inside the
    signal handler (the pre-existing pattern): on a busy host with hundreds
    of processes, ``ps aux`` can take >2s to walk /proc, during which the
    asyncio loop is frozen and adapter teardown can't begin.
    """
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    # Inline shell so we don't have to ship a helper script.  bash -c is
    # available on every POSIX target we support; on Windows we just skip
    # the snapshot (the platform doesn't ship ps anyway).
    if sys.platform == "win32":
        return None

    script = (
        f"echo '=== shutdown diagnostic @ {signal_name} ==='; "
        "echo '--- date ---'; date -u +%Y-%m-%dT%H:%M:%SZ; "
        "echo '--- ps auxf (top 60 by cpu) ---'; "
        "ps auxf --sort=-pcpu 2>/dev/null | head -60; "
        "echo '--- pstree of self ---'; "
        f"pstree -plau {os.getpid()} 2>/dev/null | head -40 || true; "
        "echo '--- /proc/loadavg ---'; "
        "cat /proc/loadavg 2>/dev/null || true; "
        "echo '--- recent dmesg (oom/killed) ---'; "
        "dmesg -T 2>/dev/null | tail -20 || journalctl --user -n 20 --no-pager 2>/dev/null | tail -20 || true; "
        "echo '=== end ==='"
    )

    try:
        # Open the log file in append mode and let the subprocess inherit.
        # We use os.O_APPEND so concurrent diagnostics from rapid signals
        # don't trample each other.
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:
        return None

    try:
        # Detach from our process group so the subprocess survives even
        # if systemd kills our cgroup with KillMode=control-group (which
        # would also reap us anyway, but defense in depth).  Without
        # start_new_session, a SIGKILL on our cgroup takes the diag down
        # before it can flush.
        proc = subprocess.Popen(
            ["timeout", f"{timeout_seconds:.0f}", "bash", "-c", script],
            stdout=fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (FileNotFoundError, OSError):
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    finally:
        # Subprocess inherited the fd; we can drop our handle.
        try:
            os.close(fd)
        except OSError:
            pass

    return proc.pid


def format_context_for_log(ctx: Dict[str, Any]) -> str:
    """Render a shutdown context dict as a single, scannable log line."""
    sig = ctx.get("signal", "?")
    parent = ctx.get("parent") or {}
    parent_cmd = parent.get("cmdline", "(unknown)")
    parent_name = parent.get("name") or "?"
    parent_pid = parent.get("pid") or "?"
    under_systemd_bool = bool(ctx.get("under_systemd"))
    under_systemd = "yes" if under_systemd_bool else "no"
    load = ctx.get("loadavg_1m")
    load_str = f"{load:.2f}" if isinstance(load, (int, float)) else "?"
    extras: List[str] = []
    if under_systemd_bool:
        if ctx.get("unit_name"):
            extras.append(f"unit_name={ctx['unit_name']}")
        if ctx.get("invocation_id"):
            extras.append(f"invocation_id={ctx['invocation_id']}")
        if ctx.get("n_restarts") is not None:
            extras.append(f"n_restarts={ctx['n_restarts']}")
        if ctx.get("recent_status_reason"):
            extras.append(f"recent_status_reason={ctx['recent_status_reason']!r}")
        if ctx.get("paired_with"):
            extras.append(f"paired_with={ctx['paired_with']}")
        if ctx.get("_lookup_timeout"):
            extras.append(f"_lookup_timeout={ctx['_lookup_timeout']}")
        if ctx.get("_introspection"):
            extras.append(f"_introspection={ctx['_introspection']}")
        extras.append(f"pid={ctx.get('pid', '?')}")
    if ctx.get("takeover_marker") is not None:
        for_self = ctx.get("takeover_marker_for_self")
        extras.append(
            f"takeover_marker_present={'self' if for_self else 'other'}"
        )
    if ctx.get("planned_stop_marker") is not None:
        extras.append("planned_stop_marker_present=yes")
    if ctx.get("tracer_pid"):
        extras.append(f"tracer_pid={ctx['tracer_pid']}")
    extras_str = (" " + " ".join(extras)) if extras else ""
    # Parent cmdline is the most useful single signal — log it prominently.
    return (
        f"signal={sig} "
        f"under_systemd={under_systemd} "
        f"parent_pid={parent_pid} "
        f"parent_name={parent_name} "
        f"loadavg_1m={load_str}"
        f"{extras_str} "
        f"parent_cmdline={parent_cmd!r}"
    )


def _shutdown_marker_path(hermes_home: Optional[Path] = None) -> Path:
    runtime_dir = os.environ.get("RUNTIME_DIR") or os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / _GATEWAY_SHUTDOWN_MARKER
    home = hermes_home or Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / f".{_GATEWAY_SHUTDOWN_MARKER}"


def record_gateway_shutdown_marker(
    ctx: Dict[str, Any],
    *,
    shutdown_kind: str = "signal",
    hermes_home: Optional[Path] = None,
) -> None:
    """Persist the previous shutdown cause for the next successful startup."""
    if not ctx.get("under_systemd"):
        return
    try:
        path = _shutdown_marker_path(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "shutdown_kind": shutdown_kind,
            "signal": ctx.get("signal"),
            "ts": time.time(),
            "pid": ctx.get("pid") or os.getpid(),
            "unit_name": ctx.get("unit_name"),
            "n_restarts": ctx.get("n_restarts"),
            "start_limit_burst": ctx.get("start_limit_burst"),
            "start_limit_interval_usec": ctx.get("start_limit_interval_usec"),
        }
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        pass


def _systemd_interval_enabled(raw: Any) -> bool:
    if raw is None:
        return False
    text = str(raw).strip()
    if not text or text in {"0", "0s", "0us", "infinity"}:
        return False
    parsed = _parse_systemd_duration_to_us(text)
    return parsed is None or parsed > 0


def _classify_post_start_reason(
    marker: Optional[Dict[str, Any]],
    systemd_details: Optional[Dict[str, Any]] = None,
) -> str:
    details = systemd_details or {}
    n_restarts = details.get("n_restarts")
    if n_restarts is None and marker:
        n_restarts = marker.get("n_restarts")
    burst = details.get("start_limit_burst")
    if burst is None and marker:
        burst = marker.get("start_limit_burst")
    interval = details.get("start_limit_interval_usec")
    if interval is None and marker:
        interval = marker.get("start_limit_interval_usec")
    try:
        n_restarts_int = int(n_restarts)
        burst_int = int(burst)
    except (TypeError, ValueError):
        n_restarts_int = 0
        burst_int = 0
    if burst_int > 0 and n_restarts_int >= burst_int and _systemd_interval_enabled(interval):
        return "restart_after_crash_loop"
    if not marker:
        return "boot"
    if marker.get("shutdown_kind") == "exception":
        return "crash_recovery"
    signal_name = marker.get("signal")
    if signal_name == "SIGTERM":
        return "restart_after_signal"
    if signal_name in {"SIGINT", None, "UNKNOWN"}:
        return "boot"
    return "crash_recovery"


def _startup_systemd_details() -> Dict[str, Any]:
    ctx: Dict[str, Any] = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "parent": _proc_summary(os.getppid()),
        "under_systemd": bool(os.environ.get("INVOCATION_ID")) or os.getppid() == 1,
    }
    enrich_systemd_shutdown_context(ctx)
    return ctx


def emit_gateway_post_start_reason(
    logger_obj: Any,
    *,
    hermes_home: Optional[Path] = None,
    systemd_details: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Emit the once-per-start post-start reason INFO line under systemd."""
    marker_path = _shutdown_marker_path(hermes_home)
    marker: Optional[Dict[str, Any]] = None
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            marker = None
        try:
            marker_path.unlink()
        except OSError:
            pass
    under_systemd = bool(os.environ.get("INVOCATION_ID")) or os.getppid() == 1
    if not under_systemd and systemd_details is None:
        return None
    details = systemd_details if systemd_details is not None else _startup_systemd_details()
    reason = _classify_post_start_reason(marker, details)
    fields = [f"post_start_reason={reason}"]
    if details.get("unit_name"):
        fields.append(f"unit_name={details['unit_name']}")
    if details.get("n_restarts") is not None:
        fields.append(f"n_restarts={details['n_restarts']}")
    logger_obj.info("Post-start context: %s", " ".join(fields))
    return reason


def context_as_json(ctx: Dict[str, Any]) -> str:
    """JSON-serialise a context dict for structured ingestion.  Never raises."""
    try:
        return json.dumps(ctx, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return "{}"


def check_systemd_timing_alignment(drain_timeout: float) -> Optional[Dict[str, Any]]:
    """At startup, sanity-check that systemd's TimeoutStopSec >= drain_timeout.

    When the gateway is run under a stale systemd unit file (e.g. the user
    upgraded hermes-agent but never re-ran ``hermes setup`` to regenerate
    the unit), ``TimeoutStopSec`` can be smaller than the configured
    ``restart_drain_timeout``.  Result: SIGTERM arrives, the drain starts,
    and systemd SIGKILLs the cgroup mid-drain — looks like a phantom kill
    in the journal because the journal only logs ``code=killed status=9``.

    Returns ``None`` when the alignment is fine OR we can't determine it
    (not running under systemd, ``systemctl`` unavailable, etc.).  Returns
    a dict with ``timeout_stop_sec`` + ``drain_timeout`` + ``mismatch``
    bool when we have data to report.

    Best-effort.  Never raises.
    """
    invocation_id = os.environ.get("INVOCATION_ID")
    if not invocation_id:
        return None  # Not running under systemd (or at least not directly)

    # Try to identify our unit name and ask systemctl for its config.
    unit_name: Optional[str] = None
    try:
        # /proc/self/cgroup gives us "0::/user.slice/.../hermes-gateway.service"
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            for line in fh:
                # systemd cgroup line ends with the unit name
                if ".service" in line:
                    parts = line.strip().split("/")
                    for p in reversed(parts):
                        if p.endswith(".service"):
                            unit_name = p
                            break
                    if unit_name:
                        break
    except (OSError, FileNotFoundError):
        pass
    if not unit_name:
        return None

    # Query systemctl for TimeoutStopUSec.  Use --user OR system depending
    # on which manager actually owns the unit.  Try user first since
    # that's the common case for hermes.
    timeout_us: Optional[int] = None
    for flag in (["--user"], []):
        try:
            result = subprocess.run(
                ["systemctl", *flag, "show", unit_name, "--property=TimeoutStopUSec"],
                capture_output=True, text=True, timeout=2.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        # Output: "TimeoutStopUSec=1min 30s" or "TimeoutStopUSec=90000000"
        for line in result.stdout.splitlines():
            if line.startswith("TimeoutStopUSec="):
                value = line.split("=", 1)[1].strip()
                # Try numeric microseconds first
                if value.isdigit():
                    timeout_us = int(value)
                else:
                    timeout_us = _parse_systemd_duration_to_us(value)
                if timeout_us is not None:
                    break
        if timeout_us is not None:
            break

    if timeout_us is None:
        return None

    timeout_stop_sec = timeout_us / 1_000_000.0
    # systemd needs headroom for: post-interrupt kill, adapter disconnect,
    # SessionDB close, file unlinks, etc.  30s matches the unit-template
    # constant in hermes_cli/gateway.py.
    headroom = 30.0
    expected = drain_timeout + headroom
    return {
        "unit": unit_name,
        "timeout_stop_sec": timeout_stop_sec,
        "drain_timeout": drain_timeout,
        "expected_min": expected,
        "mismatch": timeout_stop_sec < expected,
    }


def _parse_systemd_duration_to_us(raw: str) -> Optional[int]:
    """Parse 'TimeoutStopUSec=1min 30s' / '90s' style values to microseconds.

    systemd accepts a wide grammar; we cover the common cases (s, ms, min,
    h) and return None on anything unexpected.  Never raises.
    """
    if not raw:
        return None
    units = {
        "us": 1,
        "ms": 1_000,
        "s": 1_000_000,
        "sec": 1_000_000,
        "min": 60_000_000,
        "h": 3_600_000_000,
        "hr": 3_600_000_000,
    }
    total_us = 0
    token = ""
    digits = ""
    for ch in raw + " ":
        if ch.isdigit() or ch == ".":
            if token:
                # End previous unit, start new number
                multiplier = units.get(token.lower())
                if multiplier is None or not digits:
                    return None
                try:
                    total_us += int(float(digits) * multiplier)
                except ValueError:
                    return None
                digits = ""
                token = ""
            digits += ch
        elif ch.isalpha():
            token += ch
        elif digits and token:
            multiplier = units.get(token.lower())
            if multiplier is None:
                return None
            try:
                total_us += int(float(digits) * multiplier)
            except ValueError:
                return None
            digits = ""
            token = ""
        elif digits and not token:
            # Bare number = seconds (rare but valid)
            try:
                total_us += int(float(digits) * 1_000_000)
            except ValueError:
                return None
            digits = ""
    return total_us if total_us > 0 else None
