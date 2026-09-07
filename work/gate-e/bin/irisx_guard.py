#!/usr/bin/env python3
"""IRISX writer guard: no lease => refuse start; lost lease => stop marked PIDs."""
from __future__ import annotations
import json, os, signal, sys, time, urllib.request
from pathlib import Path

LEASE_URL = os.environ.get("IRISX_LEASE_URL", "https://irisx-lease.irisx-tracker.workers.dev")
TOKEN = os.environ.get("IRISX_LEASE_TOKEN") or Path(
    os.environ.get("IRISX_LEASE_TOKEN_FILE", "/home/box/irisx-failover-restore/20260906/gate-e/data/node.token")
).read_text().strip()
NODE = os.environ.get("IRISX_NODE", "standby-cursor-grokbot")
PIDFILE = Path(os.environ.get("IRISX_PIDFILE", "/tmp/irisx-guarded.pid"))
STATE = Path(os.environ.get("IRISX_GUARD_STATE", "/tmp/irisx-guard-state.json"))


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        LEASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "X-Irisx-Node-Token": TOKEN, "User-Agent": "irisx-lease-guard/1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            obj = json.loads(raw) if raw else {"error": str(e)}
        except Exception:
            obj = {"error": raw or str(e)}
        obj["_http"] = e.code
        return obj


def cmd_status():
    print(json.dumps(call("GET", "/v1/lease"), ensure_ascii=False, indent=2))


def cmd_acquire(snapshot=None):
    body = {"node": NODE}
    if snapshot:
        body["snapshot_id"] = snapshot
    if NODE.startswith("primary"):
        body["allow_empty_snapshot"] = True
    print(json.dumps(call("POST", "/v1/lease/acquire", body), ensure_ascii=False, indent=2))


def cmd_renew():
    body = {"node": NODE}
    if STATE.exists():
        st = json.loads(STATE.read_text())
        if "generation" in st:
            body["generation"] = st["generation"]
    res = call("POST", "/v1/lease/renew", body)
    if res.get("ok"):
        lease = res["lease"]
        STATE.write_text(json.dumps({"node": NODE, "generation": lease["generation"], "expires_at": lease["expires_at"]}))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return res.get("ok", False)


def cmd_release():
    print(json.dumps(call("POST", "/v1/lease/release", {"node": NODE}), ensure_ascii=False, indent=2))


def cmd_check_start():
    lease = call("GET", "/v1/lease")
    if not lease.get("active") or lease.get("holder") != NODE:
        print("REFUSE_START: no active lease for", NODE, file=sys.stderr)
        print(json.dumps(lease, ensure_ascii=False))
        return 2
    print("ALLOW_START", NODE, "generation", lease.get("generation"))
    STATE.write_text(json.dumps({"node": NODE, "generation": lease["generation"], "expires_at": lease["expires_at"]}))
    return 0


def gateway_pid():
    """Resolve Hermes's current PID, checking its profile and process birth time."""
    try:
        record = json.loads(PIDFILE.read_text())
        pid = record["pid"]
        if type(pid) is not int or pid <= 1 or record.get("kind") != "hermes-gateway":
            return None
        if Path(record["hermes_home"]).resolve() != PIDFILE.parent.resolve():
            return None
        proc = Path("/proc") / str(pid)
        started = int((proc / "stat").read_text().rsplit(")", 1)[1].split()[19])
        env = (proc / "environ").read_bytes().split(b"\0")
        expected = ("HERMES_HOME=" + str(PIDFILE.parent)).encode()
        if started != record.get("start_time") or expected not in env:
            return None
        return pid
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def stop_gateway():
    pid = gateway_pid()
    if pid is None:
        print("NO_MATCHING_GATEWAY")
        return
    try:
        # pidfd prevents a recycled PID from receiving the shutdown signal.
        fd = os.pidfd_open(pid)
        try:
            if gateway_pid() == pid:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                print("STOPPED_PID", pid)
        finally:
            os.close(fd)
    except ProcessLookupError:
        pass


def cmd_watch():
    """Renew every 10s; if renew fails, SIGTERM pidfile processes."""
    while True:
        ok = False
        try:
            ok = cmd_renew()
        except Exception as e:
            print("renew_error", e, file=sys.stderr)
            ok = False
        if not ok:
            stop_gateway()
            print("LOST_LEASE_STOP")
            return 3
        time.sleep(int(os.environ.get("IRISX_HEARTBEAT", "10")))


def main():
    if len(sys.argv) < 2:
        print("usage: irisx_guard.py status|acquire|renew|release|check-start|watch [snapshot_id]")
        return 1
    cmd = sys.argv[1]
    if cmd == "status":
        cmd_status(); return 0
    if cmd == "acquire":
        cmd_acquire(sys.argv[2] if len(sys.argv) > 2 else None); return 0
    if cmd == "renew":
        return 0 if cmd_renew() else 1
    if cmd == "release":
        cmd_release(); return 0
    if cmd == "check-start":
        return cmd_check_start()
    if cmd == "watch":
        return cmd_watch()
    print("unknown", cmd); return 1


if __name__ == "__main__":
    raise SystemExit(main())
