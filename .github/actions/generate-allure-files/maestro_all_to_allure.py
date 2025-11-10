#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Create Allure 2 results (with proper nested steps) from BrowserStack Maestro logs.

Two modes:
1) Single log file (local path or URL):
   python maestro_all_to_allure.py \
     --url "https://your.ci/artifacts/maestro.log" \
     --out-dir ./allure-results \
     --suite "Wikipedia / Android" \
     --test "Search for article"

2) Whole BrowserStack Maestro build (creates one Allure test per BS test):
   export BROWSERSTACK_USERNAME="your_user"
   export BROWSERSTACK_ACCESS_KEY="your_access_key"
   python maestro_all_to_allure.py \
     --build-id 1d5dd0fe0353deaa55cc7b2cddae5cbe925ee49b \
     --out-dir ./allure-results \
     --suite "Wikipedia / Android"

Then build the report:
  allure generate ./allure-results -o ./allure-report --clean
"""

import argparse
import json
import os
import re
import string
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

try:
    import requests
except Exception:
    requests = None

# -------------------------------------------------------------------
# Parsing
# -------------------------------------------------------------------

LINE_RE = re.compile(
    r"""^(?P<time>\d{2}:\d{2}:\d{2}\.\d{3})\s+\[\s*\w+\]\s+maestro\.cli\.runner\.TestSuiteInteractor\.invoke:\s+(?P<name>.+?)\s+(?P<state>RUNNING|COMPLETED|FAILED)\s*$"""
)

TIME_RE = re.compile(r"^(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\.(?P<ms>\d{3})")


def parse_hms_ms(tstr: str) -> Optional[int]:
    m = TIME_RE.match(tstr)
    if not m:
        return None
    h = int(m.group("h"))
    m_ = int(m.group("m"))
    s = int(m.group("s"))
    ms = int(m.group("ms"))
    return ((h * 3600 + m_ * 60 + s) * 1000) + ms


def fetch_text(source: str, *, auth: Optional[tuple] = None, timeout: int = 60) -> str:
    """Read from URL or local path. Sends Basic Auth when provided."""
    if source.startswith(("http://", "https://")):
        if requests is None:
            print("ERROR: 'requests' is required to download via URL. Try: pip install requests", file=sys.stderr)
            sys.exit(2)
        r = requests.get(
            source,
            timeout=timeout,
            auth=auth,  # fixes 401 for BrowserStack assets
            headers={"User-Agent": "maestro-allure/1.1"},
        )
        if r.status_code == 401:
            who = (auth[0] if auth and auth[0] else os.getenv("BROWSERSTACK_USERNAME") or "<missing>")
            print(f"BrowserStack auth failed (401). Using username: {who}", file=sys.stderr)
        r.raise_for_status()
        return r.text
    return Path(source).read_text(encoding="utf-8", errors="replace")

# -------------------------------------------------------------------
# Step tree builder (supports nesting / subflows indentation)
# -------------------------------------------------------------------

class StepNode:
    def __init__(self, name: str, start: Optional[int] = None):
        self.name = name
        self.start = start  # relative ms from log
        self.stop: Optional[int] = None  # relative ms from log
        self.status: str = "passed"
        self.stage: str = "finished"
        self.children: List["StepNode"] = []

    def to_allure(self, *, base_epoch_ms: int = 0, first_rel_ms: Optional[int] = None) -> dict:
        """Convert to Allure step dict with epochized timestamps."""
        def shift(v: Optional[int]) -> int:
            if v is None:
                return base_epoch_ms
            if first_rel_ms is None:
                return base_epoch_ms + v
            return base_epoch_ms + max(0, v - first_rel_ms)

        data = {
            "name": self.name,
            "status": self.status,
            "stage": self.stage,
            "start": shift(self.start),
            "stop": shift(self.stop if self.stop is not None else self.start),
        }
        if self.children:
            data["steps"] = [c.to_allure(base_epoch_ms=base_epoch_ms, first_rel_ms=first_rel_ms) for c in self.children]
        return data


def build_step_tree(log_text: str) -> Tuple[List[StepNode], Optional[int], Optional[int]]:
    roots: List[StepNode] = []
    stack: List[StepNode] = []
    first_ts: Optional[int] = None
    last_ts: Optional[int] = None

    for raw in log_text.splitlines():
        m = LINE_RE.match(raw)
        if not m:
            continue
        ts = parse_hms_ms(m.group("time"))
        name = re.sub(r"\s+", " ", m.group("name").strip())
        state = m.group("state")
        if first_ts is None and ts is not None:
            first_ts = ts
        if state == "RUNNING":
            node = StepNode(name=name, start=ts)
            (stack[-1].children if stack else roots).append(node)
            stack.append(node)
        elif state in ("COMPLETED", "FAILED"):
            idx = None
            for i in range(len(stack) - 1, -1, -1):
                if stack[i].name == name and stack[i].stop is None:
                    idx = i
                    break
            if idx is None:
                node = StepNode(name=name, start=ts)
                node.stop = ts
                node.status = "passed" if state == "COMPLETED" else "failed"
                roots.append(node)
                if ts is not None:
                    last_ts = ts if last_ts is None else max(last_ts, ts)
            else:
                node = stack.pop(idx)
                node.stop = ts if ts is not None else node.start
                node.status = "passed" if state == "COMPLETED" else "failed"
                if ts is not None:
                    last_ts = ts if last_ts is None else max(last_ts, ts)

    while stack:
        node = stack.pop()
        node.stop = node.start
        node.status = "failed"
        if node.stop is not None:
            last_ts = node.stop if last_ts is None else max(last_ts, node.stop)

    return roots, first_ts, last_ts

# -------------------------------------------------------------------
# Allure results writer (epochized)
# -------------------------------------------------------------------

def _parse_bs_time_to_epoch_ms(s: Optional[str]) -> Optional[int]:
    """Parse BS timestamps like '2025-05-20 13:38:35 +0000' or '2025-04-08 07:17:34 UTC'."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S %Z"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
    return None


def result_from_tree(
    roots: List[StepNode],
    first_ms: Optional[int],
    last_ms: Optional[int],
    suite_name: str,
    test_name: str,
    raw_log_filename: str,
    *,
    extra_labels: Optional[List[dict]] = None,
    parameters: Optional[List[dict]] = None,
    links: Optional[List[dict]] = None,  # NEW: allow adding Allure links
    # If known, pass true start time from BS (epoch ms) for better dating
    bs_test_start_epoch_ms: Optional[int] = None,
    history_discriminator: Optional[str] = None,
) -> dict:
    test_uuid = str(uuid.uuid4())

    def any_failed(nodes: List[StepNode]) -> bool:
        for n in nodes:
            if n.status != "passed":
                return True
            if n.children and any_failed(n.children):
                return True
        return False

    status = "failed" if any_failed(roots) else "passed"

    # Choose a base epoch so that step times are correct calendar dates.
    if bs_test_start_epoch_ms is not None and first_ms is not None:
        base_epoch_ms = bs_test_start_epoch_ms - first_ms
    else:
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        duration = (last_ms or 0) - (first_ms or 0) if (first_ms is not None and last_ms is not None) else 0
        base_epoch_ms = now_ms - max(0, duration)

    # Compute epochized test start/stop
    start_ms = (base_epoch_ms + first_ms) if first_ms is not None else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    stop_ms = (base_epoch_ms + last_ms) if (last_ms is not None and first_ms is not None) else start_ms

    labels = [
        {"name": "suite", "value": suite_name},
        {"name": "framework", "value": "maestro"},
        {"name": "language", "value": "python"},
    ]
    if extra_labels:
        labels.extend(extra_labels)

    params = parameters or []

    # Make each matrix item its own history, so Allure won't collapse as "Retries".
    hist_seed = f"{suite_name}:{test_name}"
    if history_discriminator:
        hist_seed += f":{history_discriminator}"
    history_id = str(uuid.uuid5(uuid.NAMESPACE_URL, hist_seed))

    result = {
        "uuid": test_uuid,
        "historyId": history_id,
        "name": test_name,
        "fullName": f"{suite_name}: {test_name}",
        "status": status,
        "stage": "finished",
        "start": start_ms,
        "stop": stop_ms,
        "steps": [n.to_allure(base_epoch_ms=base_epoch_ms, first_rel_ms=first_ms) for n in roots],
        "attachments": [
            {"name": "_raw_maestro_log", "type": "text/plain", "source": raw_log_filename}
        ],
        "labels": labels,
        "parameters": params,
    }
    if links:
        result["links"] = links
    return result

# -------------------------------------------------------------------
# BrowserStack Maestro API helpers (v2, api-cloud host)
# -------------------------------------------------------------------

BS_API_BASE = "https://api-cloud.browserstack.com/app-automate/maestro/v2"


def bs_get_json(url_or_path: str, *, auth: tuple) -> dict:
    """GET JSON from a full URL or BS v2 path."""
    if url_or_path.startswith("http"):
        url = url_or_path
    else:
        url = f"{BS_API_BASE}/{url_or_path.lstrip('/')}"
    r = requests.get(url, auth=auth, headers={"User-Agent": "maestro-allure/1.1"})
    if r.status_code == 401:
        print("ERROR: BrowserStack returned 401 for API call. Check username/access key.", file=sys.stderr)
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if "application/json" not in ct:
        raise RuntimeError(f"Expected JSON from {url}, got content-type={ct!r}")
    return r.json()


def iter_tests_for_build(build_id: string, *, auth: tuple):
    """
    Yield test dictionaries with at least:
      id, name, device, os, os_version, session_id, maestro_log_url, bs_test_start_epoch_ms
    """
    build = bs_get_json(f"builds/{build_id}", auth=auth)
    devices = build.get("devices", []) or []

    for d in devices:
        device_name = d.get("device") or "unknown"
        os_name = (d.get("os") or "").lower() or "android"
        os_version = d.get("os_version") or "unknown"
        sessions = d.get("sessions", []) or []

        for s in sessions:
            sess_id = s.get("id")
            if not sess_id:
                continue

            sess = bs_get_json(f"builds/{build_id}/sessions/{sess_id}", auth=auth)
            bs_session_start_ms = _parse_bs_time_to_epoch_ms(sess.get("start_time"))

            troot = (sess.get("testcases") or {})
            groups = troot.get("data") or []
            for g in groups:
                for case in g.get("testcases", []):
                    test_id = case.get("id")
                    test_name = case.get("name") or f"Test {test_id or 'unknown'}"

                    log_url = case.get("maestro_log") or case.get("maestrologs")
                    if not log_url:
                        print(f"WARNING: No Maestro text log URL for test {test_id} in session {sess_id} on {device_name}. Skipping.", file=sys.stderr)
                        continue

                    yield {
                        "id": test_id,
                        "name": test_name,
                        "device": device_name,
                        "os": os_name,
                        "os_version": os_version,
                        "session_id": sess_id,
                        "build_id": build_id,
                        "maestro_log_url": log_url,
                        "bs_test_start_epoch_ms": bs_session_start_ms,
                    }

# -------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Convert Maestro raw log(s) to Allure results.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--url", help="HTTP(s) URL OR local path to a single Maestro log (text).")
    mode.add_argument("--build-id", help="BrowserStack Maestro Build ID to convert all tests from.")

    ap.add_argument("--out-dir", default="./allure-results", help="Directory to write Allure results.")
    ap.add_argument("--suite", default="Maestro / Android", help="Allure suite name.")
    ap.add_argument("--test", default="Maestro Scenario", help="Allure test name (single-log mode only).")

    ap.add_argument("--username", help="BrowserStack username (falls back to $BROWSERSTACK_USERNAME).")
    ap.add_argument("--access-key", help="BrowserStack access key (falls back to $BROWSERSTACK_ACCESS_KEY).")

    args = ap.parse_args()

    if requests is None:
        print("ERROR: 'requests' is required. Try: pip install requests", file=sys.stderr)
        sys.exit(2)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    username = args.username or os.getenv("BROWSERSTACK_USERNAME", "")
    access_key = args.access_key or os.getenv("BROWSERSTACK_ACCESS_KEY", "")
    auth = (username, access_key) if (username and access_key) else None

    total_tests = 0

    if args.url:
        # Single-log mode
        log_text = fetch_text(args.url, auth=auth)
        raw_name = "_raw_maestro_log.txt"
        (out_dir / raw_name).write_text(log_text, encoding="utf-8")

        roots, first_ms, last_ms = build_step_tree(log_text)

        result = result_from_tree(
            roots=roots,
            first_ms=first_ms,
            last_ms=last_ms,
            suite_name=args.suite,
            test_name=args.test,
            raw_log_filename=raw_name,
            parameters=[],  # none in single-log mode
            history_discriminator=None,
        )
        result_path = out_dir / f"{uuid.uuid4()}-result.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

        container = {
            "uuid": str(uuid.uuid4()),
            "name": args.suite,
            "children": [result["uuid"]],
            "befores": [],
            "afters": [],
            "links": [],
        }
        (out_dir / f"{uuid.uuid4()}-container.json").write_text(json.dumps(container, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

        def flatten(nodes: List[StepNode]) -> List[StepNode]:
            out = []
            for n in nodes:
                out.append(n)
                out.extend(flatten(n.children))
            return out

        flat = flatten(roots)
        passed_cnt = sum(1 for s in flat if s.status == "passed")
        failed_cnt = sum(1 for s in flat if s.status != "passed")
        dur_s = ((last_ms or 0) - (first_ms or 0)) / 1000.0 if (first_ms is not None and last_ms is not None) else 0.0
        print(f"Wrote Allure results to: {out_dir}")
        print(f"Test: {args.test} | Steps: {len(flat)} (passed: {passed_cnt}, failed: {failed_cnt}) | Duration: {dur_s:.3f}s")
        total_tests = 1

    elif args.build_id:
        if not auth:
            print("ERROR: --build-id requires BrowserStack credentials. Use --username/--access-key or set BROWSERSTACK_USERNAME/BROWSERSTACK_ACCESS_KEY.", file=sys.stderr)
            sys.exit(2)

        # Iterate all BS tests in the build (v2 API)
        children = []
        for t in iter_tests_for_build(args.build_id, auth=auth):
            total_tests += 1
            test_name = t["name"]
            raw_name = f"_raw_{t['id']}_maestro_log.txt"

            # Download the plain-text Maestro log for this test (requires auth)
            log_text = fetch_text(t["maestro_log_url"], auth=auth)
            (out_dir / raw_name).write_text(log_text, encoding="utf-8")

            roots, first_ms, last_ms = build_step_tree(log_text)

            # Labels (IDs removed)
            labels = [
                {"name": "host", "value": (t.get("device") or "unknown")},
                {"name": "thread", "value": (t.get("os") or "unknown")},
            ]

            # Parameters: device + os_version first; no bs_* here
            parameters = [
                {"name": "device", "value": t.get("device") or "unknown"},
                {"name": "os_version", "value": t.get("os_version") or "unknown"},
                {"name": "os", "value": t.get("os") or "unknown"},
            ]

            # Allure links for quick navigation to BS dashboard
            # (common App Automate pattern)
            build_url = f"https://app-automate.browserstack.com/dashboard/v2/builds/{t.get('build_id')}"
            session_url = f"{build_url}/sessions/{t.get('session_id')}"
            links = [
                {"name": "Browserstack session", "url": session_url, "type": "BrowserStack"},
            ]

            hist_disc = f"{t.get('device')}|{t.get('os')}|{t.get('os_version')}|{t.get('session_id')}"

            result = result_from_tree(
                roots=roots,
                first_ms=first_ms,
                last_ms=last_ms,
                suite_name=args.suite,
                test_name=test_name,
                raw_log_filename=raw_name,
                extra_labels=labels,
                parameters=parameters,
                links=links,
                bs_test_start_epoch_ms=t.get("bs_test_start_epoch_ms"),
                history_discriminator=hist_disc,
            )
            (out_dir / f"{uuid.uuid4()}-result.json").write_text(
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            children.append(result["uuid"])

        # One container for the suite
        container = {
            "uuid": str(uuid.uuid4()),
            "name": args.suite,
            "children": children,
            "befores": [],
            "afters": [],
            "links": [],
        }
        (out_dir / f"{uuid.uuid4()}-container.json").write_text(
            json.dumps(container, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        print(f"Wrote Allure results to: {out_dir}")
        print(f"Converted {total_tests} BrowserStack test(s) from build {args.build_id}.")

if __name__ == "__main__":
    main()
