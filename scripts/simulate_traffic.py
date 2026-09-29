#!/usr/bin/env python3
"""Simulate customer traffic against the distributed agents.

Each worker is an independent "customer": it sleeps a random interval, sends
one randomly chosen prompt to one triage agent, waits for the answer, and
repeats. The sleeps are drawn per worker, so requests arrive spread out rather
than in lockstep batches -- which is the point, since the traces this produces
are what the dashboards are read from.

Every request carries a generated W3C ``traceparent``, and the trace id is
printed with the response, so any line of output can be pasted straight into
Tempo.

Stdlib only: it targets the agents over HTTP and is meant to be runnable with
nothing installed.

    python scripts/simulate_traffic.py                    # both instances, forever
    python scripts/simulate_traffic.py --workers 3        # 3 customers per instance
    python scripts/simulate_traffic.py --iterations 5     # 5 requests per worker, then stop
    python scripts/simulate_traffic.py --target gpt-4.1   # one instance only
    python scripts/simulate_traffic.py --min-wait 5 --max-wait 10   # impatient customers
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

# The two deployments from two-model-stack/docker-compose.yml. Only the triage
# agents are addressed: hitting a specialist directly would skip the routing
# decision, which is the interesting part of the trace.
TARGETS: dict[str, str] = {
    "gpt-4.1": "http://localhost:8090",
    "gpt-4.1-mini": "http://localhost:8080",
}

# Fixed prompts, one picked at random per interaction. Deliberately a mix of
# billing, technical and ambiguous requests: routing only shows up as an
# interesting signal if some requests are genuinely hard to route.
PROMPTS: list[str] = [
    "I was double charged on account ACC-4471. Check my invoice and refund me 42.50 USD.",
    "What is the status of my invoice for ACC-9001?",
    "My device DEV-88 in us-east is offline. Check for outages and restart it.",
    "Is there an outage in eu-west? My connection has been dropping all morning.",
    "I paid my bill last week but account ACC-2210 still shows as unpaid. Can you look into it?",
    "Please refund 19.99 USD on ACC-3355, I was billed for a plan I cancelled.",
    "The router DEV-12 keeps rebooting itself. Can you restart it properly from your side?",
    "My internet is down and I also think I was overcharged this month on ACC-7788.",
    "Can you confirm whether my latest invoice for ACC-5150 has been paid?",
    "Device DEV-404 in ap-south has had no signal since yesterday. Please investigate.",
    "I want a refund of 120.00 USD on account ACC-6612 for the outage last week.",
    "Something is wrong with my service but I am not sure what. Account ACC-1001, device DEV-77.",
]


@dataclass(frozen=True)
class Result:
    ok: bool
    trace_id: str
    seconds: float
    answer: str


_print_lock = threading.Lock()
_stop = threading.Event()


def log(message: str) -> None:
    """Write one line, atomically -- workers all print to the same stdout."""
    with _print_lock:
        print(f"{datetime.now():%H:%M:%S} {message}", flush=True)


def new_traceparent() -> tuple[str, str]:
    """Build a W3C traceparent and return it alongside its trace id.

    Generating the id here rather than letting the agent create one is what
    makes a request findable: the id is printed locally before the trace even
    reaches the collector.
    """
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return trace_id, f"00-{trace_id}-{span_id}-01"


def send_prompt(base_url: str, prompt: str, timeout: float) -> Result:
    """Send one A2A ``SendMessage`` request and return the agent's answer."""
    trace_id, traceparent = new_traceparent()
    payload = {
        "jsonrpc": "2.0",
        "id": secrets.token_hex(4),
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": f"msg-{secrets.token_hex(6)}",
                "role": "ROLE_USER",
                "parts": [{"text": prompt}],
            }
        },
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            # Mandatory: the handler rejects the SDK's default 0.3 with
            # -32009 "A2A version not supported".
            "A2A-Version": "1.0",
            "traceparent": traceparent,
        },
        method="POST",
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        return Result(False, trace_id, time.monotonic() - started, f"HTTP {exc.code}: {exc.read()[:200]!r}")
    except Exception as exc:  # network error, timeout, malformed JSON
        return Result(False, trace_id, time.monotonic() - started, f"{type(exc).__name__}: {exc}")

    elapsed = time.monotonic() - started
    if "error" in body:
        return Result(False, trace_id, elapsed, f"JSON-RPC error: {body['error']}")
    return Result(True, trace_id, elapsed, extract_answer(body))


def extract_answer(body: dict) -> str:
    """Pull the agent's reply out of the A2A task history.

    The agents run non-streaming, so the answer is a single ROLE_AGENT message
    in ``result.task.history`` -- not in ``task.artifacts``.
    """
    history = body.get("result", {}).get("task", {}).get("history", [])
    texts = [
        part["text"]
        for message in history
        if message.get("role") == "ROLE_AGENT"
        for part in message.get("parts", [])
        if part.get("text")
    ]
    return " ".join(texts) if texts else json.dumps(body)[:300]


def worker(name: str, target: str, base_url: str, args: argparse.Namespace) -> None:
    """One independent customer: wait, ask, repeat."""
    counter = itertools.count(1) if args.iterations <= 0 else iter(range(1, args.iterations + 1))
    for turn in counter:
        # Each worker draws its own delay, so the arrival process of one
        # customer says nothing about the next one's.
        delay = random.uniform(args.min_wait, args.max_wait)
        log(f"[{name}] sleeping {delay:.1f}s before request #{turn}")
        if _stop.wait(delay):
            return

        prompt = random.choice(PROMPTS)
        log(f"[{name}] -> {target} :: {prompt}")
        result = send_prompt(base_url, prompt, args.timeout)
        status = "ok " if result.ok else "ERR"
        answer = " ".join(result.answer.split())
        if len(answer) > 220:
            answer = answer[:220] + "..."
        log(f"[{name}] <- {status} {result.seconds:5.1f}s trace={result.trace_id} :: {answer}")

        if _stop.is_set():
            return
    log(f"[{name}] finished {args.iterations} requests")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send randomly timed, randomly chosen prompts to the distributed agents.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--target",
        action="append",
        choices=sorted(TARGETS),
        help="instance to drive; repeatable. Default: every instance.",
    )
    parser.add_argument("--workers", type=int, default=1, help="concurrent customers per instance")
    parser.add_argument("--min-wait", type=float, default=30.0, help="minimum think time, seconds")
    parser.add_argument("--max-wait", type=float, default=60.0, help="maximum think time, seconds")
    parser.add_argument("--iterations", type=int, default=0, help="requests per worker; 0 runs until Ctrl-C")
    parser.add_argument("--timeout", type=float, default=180.0, help="per-request timeout, seconds")
    parser.add_argument("--seed", type=int, default=None, help="seed the RNG for a reproducible run")
    args = parser.parse_args(argv)

    if args.min_wait < 0 or args.max_wait < args.min_wait:
        parser.error("--min-wait must be >= 0 and <= --max-wait")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.seed is not None:
        random.seed(args.seed)

    targets = args.target or sorted(TARGETS)
    threads = [
        threading.Thread(
            target=worker,
            args=(f"{target}#{index}", target, TARGETS[target], args),
            name=f"{target}#{index}",
            daemon=True,
        )
        for target in targets
        for index in range(1, args.workers + 1)
    ]

    plan = ", ".join(f"{target} ({TARGETS[target]})" for target in targets)
    log(f"starting {len(threads)} worker(s) against {plan}")
    log(f"think time {args.min_wait:.0f}-{args.max_wait:.0f}s, {len(PROMPTS)} prompts, Ctrl-C to stop")

    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            time.sleep(0.2)
    except KeyboardInterrupt:
        # Workers wait on the event rather than sleeping, so this cuts a
        # pending think time short instead of waiting out the full minute.
        _stop.set()
        log("stopping; waiting for in-flight requests")
        for thread in threads:
            thread.join(timeout=args.timeout)
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
