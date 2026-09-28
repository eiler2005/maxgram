#!/usr/bin/env python3
"""Read a safe egress snapshot, or send an explicit owner-only drill notice.

Run inside the existing bridge container; never creates another MAX client.
"""
import argparse
import json
import os
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notify-owner")
    args = parser.parse_args()
    if args.notify_owner:
        body = urllib.parse.urlencode({"chat_id": os.environ["TG_OWNER_ID"],
                                       "text": args.notify_owner}).encode()
        url = "https://api.telegram.org/bot" + os.environ["TG_BOT_TOKEN"] + "/sendMessage"
        request = urllib.request.Request(url, data=body)
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
        print(json.dumps({"owner_notice_sent": bool(result.get("ok"))}))
        return
    request = urllib.request.Request("http://127.0.0.1:18140/status",
        headers={"Authorization": "Bearer " + os.environ["BRIDGE_STATUS_TOKEN"]})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    print(json.dumps({key: payload.get(key) for key in (
        "generated_at", "overall_status", "heartbeat_age_seconds", "max_egress_active",
        "max_egress_controller", "alert_outbox_size", "queues", "worker_restart_count",
    )}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(type(error).__name__) from None
