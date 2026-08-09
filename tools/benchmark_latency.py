#!/usr/bin/env python3
"""Measure live Vectorworks latency and catch transport regressions.

``file_ipc`` isolates job creation -> watcher -> VW main-thread dispatch ->
result write. ``mcp_http`` adds FastMCP, JSON-RPC, and streamable HTTP by
launching a temporary local server. Both benchmark cases are source-safe reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import statistics
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "benchmarks" / "vectorworks-latency-baseline.json"


def plugin_dir() -> Path:
    configured = os.environ.get("VWX_PLUGIN_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Vectorworks/2026/Plug-ins/VWX-MCP"
    appdata = os.environ.get("APPDATA")
    if appdata:
        for name in ("VWX-MCP", "VW-MCP"):
            candidate = Path(appdata) / "Nemetschek/Vectorworks/2026/Plug-Ins" / name
            if candidate.is_dir():
                return candidate
    raise RuntimeError("Vectorworks plugin directory not found; set VWX_PLUGIN_DIR")


def percentile(values: list[float], quantile: float) -> float:
    """Nearest-rank percentile, deterministic for short benchmark samples."""
    if not values:
        raise ValueError("values must not be empty")
    if not 0.0 <= quantile <= 1.0:
        raise ValueError(f"quantile must be in [0.0, 1.0], got {quantile}")
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def summarize(values: list[float]) -> dict[str, float | int]:
    return {
        "runs": len(values),
        "median_ms": round(statistics.median(values), 1),
        "p95_ms": round(percentile(values, 0.95), 1),
        "min_ms": round(min(values), 1),
        "max_ms": round(max(values), 1),
    }


class FileIPC:
    def __init__(self, base: Path, timeout: float = 10.0, poll: float = 0.005):
        self.jobs = base / "ipc/jobs"
        self.results = base / "ipc/results"
        self.timeout = timeout
        self.poll = poll
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)

    def call(self, command: str, params: dict[str, Any] | None = None) -> tuple[float, Any]:
        cid = uuid.uuid4().hex[:12]
        job = {
            "type": command,
            "params": params or {},
            "_cid": cid,
            "ts": time.time(),
        }
        target = self.jobs / f"{int(time.time() * 1000):013d}-{cid}.json"
        temp = target.with_suffix(".json.tmp")
        started = time.perf_counter()
        temp.write_text(json.dumps(job), encoding="utf-8")
        temp.replace(target)
        result_path = self.results / f"{cid}.json"
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if result_path.exists():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    time.sleep(self.poll)
                    continue
                result_path.unlink(missing_ok=True)
                elapsed = (time.perf_counter() - started) * 1000
                if isinstance(result, dict) and result.get("error"):
                    raise RuntimeError(f"{command} failed: {result['error']}")
                return elapsed, result
            time.sleep(self.poll)
        target.unlink(missing_ok=True)
        raise TimeoutError(f"{command} did not complete within {self.timeout:.1f}s")


CASES: dict[str, tuple[str, dict[str, Any]]] = {
    "ping": ("ping", {}),
    # Representative non-trivial read with a larger response than ping.
    "document_info": ("get_document_info", {}),
}


def run_file_suite(ipc: FileIPC, runs: int, warmup: int) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for case, (command, params) in CASES.items():
        for _ in range(warmup):
            ipc.call(command, params)
        samples = [ipc.call(command, params)[0] for _ in range(runs)]
        results[case] = summarize(samples)
    return {"cases": results}


def wait_for_port(host: str, port: int, process: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"temporary MCP server exited early:\n{output}")
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"temporary MCP server did not listen on {host}:{port}")


@contextmanager
def mcp_http_server(base: Path, port: int, timeout: float):
    host = "127.0.0.1"
    env = os.environ.copy()
    env.update({
        "MCP_TRANSPORT": "streamable-http",
        "FASTMCP_HOST": host,
        "FASTMCP_PORT": str(port),
        "VWX_TRANSPORT": "file",
        "VWX_PLUGIN_DIR": str(base),
        "VWX_TOOLSET": "mechanical",
    })
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "mcp-server" / "vwx_mcp_server.py")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_for_port(host, port, process, timeout)
        yield f"http://{host}:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


async def run_mcp_suite(url: str, runs: int, warmup: int) -> dict[str, Any]:
    try:
        from fastmcp import Client
    except ImportError as exc:
        raise RuntimeError(
            "mcp_http mode requires FastMCP; run with .venv/bin/python"
        ) from exc

    results: dict[str, Any] = {}
    async with Client(url) as client:
        tools = await client.list_tools()
        for case, (command, params) in CASES.items():
            samples: list[float] = []
            for index in range(warmup + runs):
                started = time.perf_counter()
                result = await client.call_tool(command, params)
                elapsed = (time.perf_counter() - started) * 1000
                if getattr(result, "is_error", False):
                    raise RuntimeError(f"MCP tool {command} failed: {result}")
                if index >= warmup:
                    samples.append(elapsed)
            results[case] = summarize(samples)
    return {"tool_count": len(tools), "cases": results}


def add_overhead(result: dict[str, Any]) -> None:
    paths = result.get("paths", {})
    direct = paths.get("file_ipc", {}).get("cases", {})
    mcp = paths.get("mcp_http", {}).get("cases", {})
    if not direct or not mcp:
        return
    result["mcp_overhead_ms"] = {
        case: {
            metric: round(float(mcp[case][metric]) - float(direct[case][metric]), 1)
            for metric in ("median_ms", "p95_ms")
        }
        for case in direct.keys() & mcp.keys()
    }


def regressions(current: dict[str, Any], baseline: dict[str, Any], ratio: float) -> list[str]:
    failures: list[str] = []
    # AX menu dispatch has two observed stable bands across VW process restarts:
    # ~145 ms hot and ~230 ms after a fresh process. Floors cover that real
    # process-level variance; the baseline ratio still catches drift above it.
    floors = {"median_ms": 250.0, "p95_ms": 300.0}
    current_paths = current.get("paths") or {"file_ipc": {"cases": current["cases"]}}
    baseline_paths = baseline.get("paths") or {"file_ipc": {"cases": baseline["cases"]}}
    for path, current_path in current_paths.items():
        baseline_path = baseline_paths.get(path)
        if not baseline_path:
            failures.append(f"{path}: missing from baseline")
            continue
        if "tool_count" in current_path and "tool_count" in baseline_path:
            if current_path["tool_count"] != baseline_path["tool_count"]:
                failures.append(
                    f"{path} tool_count {current_path['tool_count']} != "
                    f"baseline {baseline_path['tool_count']}"
                )
        for case, now in current_path["cases"].items():
            before = baseline_path.get("cases", {}).get(case)
            if not before:
                failures.append(f"{path}/{case}: missing from baseline")
                continue
            for metric, floor in floors.items():
                limit = max(floor, float(before[metric]) * ratio)
                if float(now[metric]) > limit:
                    failures.append(
                        f"{path}/{case} {metric} {now[metric]:.1f}ms > {limit:.1f}ms "
                        f"(baseline {before[metric]:.1f}ms)"
                    )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--mode", choices=("ipc", "mcp", "both"), default="both")
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--record-baseline", action="store_true")
    parser.add_argument("--no-check", action="store_true")
    parser.add_argument("--regression-ratio", type=float, default=1.35)
    args = parser.parse_args()
    if args.runs < 2 or args.warmup < 0:
        parser.error("--runs must be >= 2 and --warmup must be >= 0")

    base = plugin_dir()
    paths: dict[str, Any] = {}
    if args.mode in ("ipc", "both"):
        paths["file_ipc"] = run_file_suite(
            FileIPC(base, timeout=args.timeout), args.runs, args.warmup
        )
    if args.mode in ("mcp", "both"):
        with mcp_http_server(base, args.port, args.timeout) as url:
            paths["mcp_http"] = asyncio.run(
                run_mcp_suite(url, args.runs, args.warmup)
            )
    result = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "host": platform.node(),
        "platform": platform.platform(),
        "plugin_dir": str(base),
        "paths": paths,
    }
    add_overhead(result)
    print(json.dumps(result, indent=2))

    if args.record_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Recorded baseline: {args.baseline}")
        return 0
    if args.no_check:
        return 0
    if not args.baseline.exists():
        print(f"No baseline at {args.baseline}; run with --record-baseline", file=sys.stderr)
        return 2
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    failures = regressions(result, baseline, args.regression_ratio)
    if failures:
        print("Latency regression detected:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Latency check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
