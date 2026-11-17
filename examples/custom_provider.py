#!/usr/bin/env python3
"""Template for a SubUsage custom provider.

Add it in the widget: ⚙ Settings → + Add subscription → Custom → Command:
    python /path/to/custom_provider.py

The widget runs the command on each refresh and reads JSON from stdout:

    {"plan": "Pro",
     "windows": [{"label": "Daily", "used_pct": 42.5,
                  "resets_at": "2026-11-17T00:00:00Z", "length_seconds": 86400,
                  "detail": "optional text"}],
     "notes": ["optional extra line"]}

used_pct is 0–100. resets_at may be ISO-8601, epoch seconds or epoch ms. length_seconds enables the
pace marker. Exit non-zero with a message on stderr to show an error under the provider.

Run with --demo to see sample output without calling anything.
"""
import datetime as dt
import json
import os
import sys
import urllib.request


def demo() -> dict:
    tomorrow = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0)
    return {"plan": "Demo", "windows": [{"label": "Daily", "used_pct": 42.5,
                                         "resets_at": tomorrow.isoformat(), "length_seconds": 86400}]}


def fetch() -> dict:
    # Replace this with your provider's usage endpoint. Keep secrets in environment variables,
    # not in this file.
    key = os.environ.get("MY_PROVIDER_API_KEY")
    if not key:
        raise SystemExit("Set MY_PROVIDER_API_KEY")  # message shows under the provider
    req = urllib.request.Request("https://api.example.com/v1/usage", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    # Map the vendor's fields onto the widget's format:
    return {
        "plan": data.get("plan", ""),
        "windows": [{"label": "Monthly", "used_pct": data["used"] / data["limit"] * 100,
                     "resets_at": data.get("resets_at")}],
    }


if __name__ == "__main__":
    try:
        print(json.dumps(demo() if "--demo" in sys.argv else fetch()))
    except SystemExit:
        raise
    except Exception as e:  # report errors on stderr with a non-zero exit
        sys.stderr.write(f"{type(e).__name__}: {e}")
        sys.exit(1)
