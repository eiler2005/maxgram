#!/usr/bin/env python3
"""Host-only, scoped Channel M fault injection with an independent cleanup timer.

Run as root on the production host. Never prints private network coordinates.
Only blocks this container's traffic to its host-side Channel M listener.
"""
import argparse
import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse

STATE = Path("/run/maxtg-egress-drill.json")
UNIT = "maxtg-egress-drill-cleanup"
COMMENT = "maxtg-egress-drill"


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--container")
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps({"drill_state_present": STATE.exists(), "timer": subprocess.run(
            ["systemctl", "is-active", UNIT + ".timer"], capture_output=True, text=True).stdout.strip()}))
        return
    if args.action == "stop":
        if STATE.exists():
            rule = json.loads(STATE.read_text())["rule"]
            result = subprocess.run(["iptables", "-w", "-C", "INPUT", *rule], capture_output=True)
            if result.returncode == 0:
                run(["iptables", "-w", "-D", "INPUT", *rule])
            STATE.unlink()
        subprocess.run(["systemctl", "stop", UNIT + ".timer"], capture_output=True)
        print("Channel M drill restriction removed")
        return
    if not args.container or not 60 <= args.seconds <= 1200 or STATE.exists():
        raise SystemExit("Need container, duration 60..1200s, and no existing drill")
    container = json.loads(run(["docker", "inspect", args.container]))[0]
    env = dict(item.split("=", 1) for item in container["Config"]["Env"] if "=" in item)
    proxy = urlparse(env.get("MAX_EGRESS_PROXY_URL", ""))
    if not proxy.hostname or not proxy.port:
        raise SystemExit("Channel M proxy missing")
    resolved = run(["docker", "exec", args.container, "getent", "ahostsv4", proxy.hostname]).split()[0]
    networks = container["NetworkSettings"]["Networks"].values()
    network = next((n for n in networks if n["Gateway"] == resolved), None)
    if network is None:
        raise SystemExit("Refusing to block anything except this container's host gateway")
    rule = ["-s", network["IPAddress"], "-d", resolved, "-p", "tcp", "--dport", str(proxy.port),
            "-m", "comment", "--comment", COMMENT, "-j", "REJECT", "--reject-with", "tcp-reset"]
    # Persist exact removal arguments before scheduling; no shell interpolation.
    STATE.write_text(json.dumps({"rule": rule}))
    STATE.chmod(0o600)
    try:
        run(["systemd-run", "--unit", UNIT, "--on-active", f"{args.seconds}s",
             "--timer-property=AccuracySec=1s", "iptables", "-w", "-D", "INPUT", *rule])
        run(["systemctl", "is-active", UNIT + ".timer"])
        run(["iptables", "-w", "-I", "INPUT", "1", *rule])
    except Exception:
        subprocess.run(["iptables", "-w", "-D", "INPUT", *rule], capture_output=True)
        subprocess.run(["systemctl", "stop", UNIT + ".timer"], capture_output=True)
        STATE.unlink(missing_ok=True)
        raise
    print(json.dumps({"blocked": "bridge-to-Channel-M-only", "automatic_cleanup_seconds": args.seconds}))


if __name__ == "__main__":
    main()
