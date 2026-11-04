"""Bounded, closed-loop streaming load generator. Python 3.11+."""
import argparse
import asyncio
import contextlib
import json
import os
import random
import statistics
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from workloads import PROFILES, advance, body_for, digest, initial_messages, load_replay


async def sse_events(content):
    """aiohttp line iteration reassembles TCP fragments; SSE events may span lines."""
    data = []
    async for raw in content:
        line = raw.decode("utf-8-sig").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield "\n".join(data)


def usage_metrics(usage):
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    prompt_details = usage.get("prompt_tokens_details")
    cached = prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
    source = "usage.prompt_tokens_details.cached_tokens" if cached is not None else None
    if cached is None and usage.get("prompt_cache_hit_tokens") is not None:
        cached = usage["prompt_cache_hit_tokens"]
        source = "usage.prompt_cache_hit_tokens"
    def valid(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    prompt = prompt if valid(prompt) else None
    completion = completion if valid(completion) else None
    cached = cached if valid(cached) and (prompt is None or cached <= prompt) else None
    miss = usage.get("prompt_cache_miss_tokens")
    miss = miss if valid(miss) else prompt - cached if prompt is not None and cached is not None else None
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    reasoning = details.get("reasoning_tokens")
    reasoning = reasoning if valid(reasoning) else None
    total = usage.get("total_tokens")
    total = total if valid(total) else prompt + completion if prompt is not None and completion is not None else None
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "uncached_tokens": miss, "reasoning_tokens": reasoning, "total_tokens": total,
            "cached_tokens": cached, "cache_metric_source": source if cached is not None else None,
            "cache_hit_ratio": cached / prompt if cached is not None and prompt else None}


async def request(client, url, body, headers, args, observed=None):
    start = time.perf_counter()
    result = {"ok": False, "status": None, "ttft_ms": None, "tpot_ms": None,
              "output_chunks": 0, "chunk_gap_mean_ms": None, "finish_reason": None,
              "backend": None, "server_request_id": None}
    if observed is not None:
        observed.update(result)
        result = observed
    result.update(headers_ms=None, generation_ms=None, decode_tokens_per_s=None,
                  first_text_ms=None, first_reasoning_ms=None, tool_calls_count=0,
                  text_chars=0, reasoning_chars=0, tool_argument_chars=0, sse_events=0)
    first = last = None
    gaps = []
    content, reasoning, calls = [], [], {}
    usage = {}
    done = False
    try:
        async with client.post(url, json=body, headers=headers, allow_redirects=False) as response:
            result["headers_ms"] = (time.perf_counter() - start) * 1000
            result["status"] = response.status
            result["backend"] = response.headers.get(args.backend_header) if args.backend_header else None
            result["server_request_id"] = response.headers.get("x-request-id")
            if response.status != 200:
                # Do not persist server error bodies: they can echo credentials or prompts.
                raise ValueError(f"http_{response.status}")
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                raise ValueError("expected_sse_content_type")
            async for event in sse_events(response.content):
                result["sse_events"] += 1
                if event.strip() == "[DONE]":
                    done = True
                    break
                obj = json.loads(event)
                if obj.get("error"):
                    raise ValueError("stream_error")
                if obj.get("usage"):
                    usage.update(obj["usage"])
                output = False
                for choice in obj.get("choices", []):
                    if choice.get("index", 0) != 0:
                        raise ValueError("multiple_choices_not_supported")
                    delta = choice.get("delta", {})
                    if choice.get("finish_reason"):
                        result["finish_reason"] = choice["finish_reason"]
                    for field, target in (("content", content), ("reasoning_content", reasoning)):
                        if delta.get(field):
                            target.append(delta[field])
                            kind = "text" if field == "content" else "reasoning"
                            result[f"{kind}_chars"] += len(delta[field])
                            if result[f"first_{kind}_ms"] is None:
                                result[f"first_{kind}_ms"] = (time.perf_counter() - start) * 1000
                            output = True
                    for part in delta.get("tool_calls", []):
                        call = calls.setdefault(part["index"], {"id": "", "type": "function",
                                                               "function": {"name": "", "arguments": ""}})
                        if part.get("id"):
                            call["id"] += part["id"]
                        for field in ("name", "arguments"):
                            value = part.get("function", {}).get(field, "")
                            call["function"][field] += value
                            if field == "arguments":
                                result["tool_argument_chars"] += len(value)
                            output |= bool(value)
                if output:
                    now = time.perf_counter()
                    if first is None:
                        first = now
                        result["ttft_ms"] = (first - start) * 1000
                    if last is not None:
                        gaps.append((now - last) * 1000)
                    last = now
                    result["output_chunks"] += 1
            if not done or not result["finish_reason"]:
                raise ValueError("incomplete_stream")
            if calls and any(not c["id"] or not c["function"]["name"] for c in calls.values()):
                raise ValueError("incomplete_tool_call")
            result["ok"] = True
    except asyncio.CancelledError:
        result["error"] = "cancelled"
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
        # Only locally defined codes; no arbitrary exception string in reports.
        result["error"] = str(exc) if isinstance(exc, ValueError) and str(exc) in {
            "expected_sse_content_type", "stream_error", "multiple_choices_not_supported",
            "incomplete_stream", "incomplete_tool_call", f"http_{result['status']}"
        } else type(exc).__name__
    result["e2e_ms"] = (time.perf_counter() - start) * 1000
    result.update(usage_metrics(usage))
    if first is not None:
        result["ttft_ms"] = (first - start) * 1000
        # Harness-style generation: first output through completed response,
        # distinct from last-output timing used by benchmark TPOT.
        result["generation_ms"] = max(0, result["e2e_ms"] - result["ttft_ms"])
    if gaps:
        result["chunk_gap_mean_ms"] = statistics.mean(gaps)
    tokens = result["completion_tokens"]
    if tokens is not None and result["generation_ms"] is not None and result["generation_ms"] > 0:
        result["decode_tokens_per_s"] = tokens * 1000 / result["generation_ms"]
    if first is not None and tokens is not None and tokens > 1 and result["output_chunks"] > 1:
        result["tpot_ms"] = (last - first) * 1000 / (tokens - 1)
    assistant = {"role": "assistant", "content": "".join(content) or None}
    if reasoning:
        assistant["reasoning_content"] = "".join(reasoning)
    if calls:
        assistant["tool_calls"] = [calls[k] for k in sorted(calls)]
    result["tool_calls_count"] = len(calls)
    result["metric_unavailable"] = {
        key: reason for key, reason in {
            "ttft_ms": "no_output_delta" if first is None else None,
            "tpot_ms": "missing_completion_usage" if tokens is None else "insufficient_output_samples" if result["tpot_ms"] is None else None,
            "cache_hit_ratio": "missing_cache_usage" if result["cached_tokens"] is None else "missing_or_zero_prompt_usage" if not result["prompt_tokens"] else None,
        }.items() if reason
    }
    return result, assistant


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "p99": None}
    def percentile(p):
        position = (len(values) - 1) * p
        left = int(position)
        return values[left] + (values[min(left + 1, len(values) - 1)] - values[left]) * (position - left)
    return {"count": len(values), "mean": statistics.mean(values),
            **{f"p{p}": percentile(p / 100) for p in (50, 95, 99)}}


def summarize(rows, elapsed):
    success = [r for r in rows if r["ok"]]
    cache = [r for r in success if r["cached_tokens"] is not None and r["prompt_tokens"] is not None]
    denominator = sum(r["prompt_tokens"] for r in cache)
    pairs = matches = 0
    previous = {}
    for r in rows:
        sid = r["session_id"]
        before = previous.get(sid)
        if before and r["ok"] and before["ok"] and r["backend"] and before["backend"]:
            pairs += 1
            matches += r["backend"] == before["backend"]
        previous[sid] = r
    timed = [r for r in success if r.get("generation_ms", 0) and r.get("completion_tokens") is not None]
    decode_ms = sum(r["generation_ms"] for r in timed)
    totals = {}
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "uncached_tokens", "reasoning_tokens", "total_tokens"):
        values = [r[key] for r in success if r.get(key) is not None]
        totals[key] = {"value": sum(values) if values else None, "requests": len(values)}
    return {"requests": len(rows), "success": len(success), "failed": len(rows) - len(success),
            "sessions": len({r["session_id"] for r in rows}),
            "token_totals": totals,
            "llm_time_ms": sum(r["e2e_ms"] for r in rows),
            "generation_time_ms": sum(r.get("generation_ms") or 0 for r in success),
            "weighted_decode_tokens_per_s": sum(r["completion_tokens"] for r in timed) * 1000 / decode_ms if decode_ms else None,
            "tool_calls": sum(r.get("tool_calls_count", 0) for r in success),
            "finish_reasons": dict(Counter(r.get("finish_reason") for r in success)),
            "observed_ttft_ms": distribution(r.get("ttft_ms") for r in rows),
            "metric_unavailable": dict(Counter(f"{key}:{reason}" for r in rows for key, reason in r.get("metric_unavailable", {}).items())),
            "errors": dict(Counter(r.get("error", "unknown") for r in rows if not r["ok"])),
            "elapsed_s": elapsed, "successful_requests_per_s": len(success) / elapsed if elapsed else 0,
            "reported_output_tokens_per_s": sum(r["completion_tokens"] or 0 for r in success) / elapsed if elapsed else 0,
            "output_usage_coverage": sum(r["completion_tokens"] is not None for r in success) / len(success) if success else 0,
            "cache_usage_coverage": len(cache) / len(success) if success else 0,
            "weighted_cache_hit_ratio": sum(r["cached_tokens"] for r in cache) / denominator if denominator else None,
            "backend_adjacent_pairs": pairs, "backend_same_ratio": matches / pairs if pairs else None,
            **{k: distribution(r.get(k) for r in success) for k in
               ("ttft_ms", "tpot_ms", "e2e_ms", "chunk_gap_mean_ms", "headers_ms",
                "generation_ms", "decode_tokens_per_s", "first_text_ms", "first_reasoning_ms")}}


def live_snapshot(rows, in_flight, elapsed, peak, lag):
    summary = summarize(rows, elapsed)
    active = list(in_flight.values())
    observed = distribution(r.get("ttft_ms") for r in [*rows, *active])
    summary.update(active=len(active), peak_in_flight=peak, observed_ttft_ms=observed,
                   updated_at=datetime.now(timezone.utc).isoformat(),
                   waiting_first_token=sum(r.get("ttft_ms") is None for r in active),
                   streaming=sum(r.get("ttft_ms") is not None for r in active),
                   event_loop_lag_ms=distribution(lag),
                   recent_requests=[{k: r.get(k) for k in (
                       "client_request_id", "session_index", "turn", "profile", "ok", "status",
                       "ttft_ms", "tpot_ms", "generation_ms", "decode_tokens_per_s", "prompt_tokens",
                       "completion_tokens", "cached_tokens", "error", "metric_unavailable")}
                       for r in rows[-20:]])
    return summary


async def run(args):
    replay = load_replay(args.replay)
    run_id = args.run_id or str(uuid.uuid4())
    directory = Path(args.output) / run_id
    directory.mkdir(parents=True, exist_ok=True)
    config = vars(args).copy()
    # URL may embed auth; never store endpoint or API key in artifacts.
    config.pop("base_url")
    (directory / "config.json").write_text(json.dumps({"run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(), "config": config}, indent=2), encoding="utf-8")
    if args.dry_run:
        profile = args.profiles[0]
        body = body_for(args, initial_messages(profile, 0, args), profile,
                        replay[0]["requests"][0] if replay else None)
        (directory / "sample-request.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
        print(f"Dry run (no network): {directory}")
        return 0
    rows, lag = [], []
    in_flight = {}
    active = peak = 0
    owner = asyncio.current_task()
    next_session = 0
    started = time.perf_counter()
    deadline = started + args.duration if args.duration else float("inf")
    headers = {"Accept": "text/event-stream"}
    key = os.environ.get(args.api_key_env)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    connector = aiohttp.TCPConnector(limit=args.concurrency, limit_per_host=args.concurrency,
                                     force_close=args.fresh_connections)
    timeout = aiohttp.ClientTimeout(total=args.timeout, sock_read=args.timeout)

    async def monitor():
        while True:
            before = time.perf_counter()
            await asyncio.sleep(0.5)
            lag.append(max(0, time.perf_counter() - before - 0.5) * 1000)
            if (directory / "stop").exists():
                owner.cancel()
                return
            # Check stop every 0.5s, aggregate every 2s to limit client overhead.
            if len(lag) % 4 != 1:
                continue
            progress = live_snapshot(rows, in_flight, time.perf_counter() - started, peak, lag)
            temporary = directory / "progress.tmp"
            temporary.write_text(json.dumps(progress), encoding="utf-8")
            temporary.replace(directory / "progress.json")
            if len(lag) % 20 == 1:
                print(f"requests={len(rows)} active={active} peak={peak}", flush=True)

    with (directory / "requests.jsonl").open("w", encoding="utf-8") as output:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False,
                                          read_bufsize=2**20) as client:
            async def worker(worker_id):
                nonlocal next_session, active, peak
                await asyncio.sleep(args.ramp_seconds * worker_id / args.concurrency)
                while next_session < args.sessions and time.perf_counter() < deadline and not (directory / "stop").exists():
                    index = next_session
                    next_session += 1
                    sid = str(uuid.uuid5(uuid.UUID(run_id), str(index)))
                    profile = args.profiles[index % len(args.profiles)]
                    messages = initial_messages(profile, index, args) if not replay else []
                    record = replay[index % len(replay)] if replay else None
                    turns = len(record["requests"]) if record else args.turns
                    rng = random.Random(args.seed + index)
                    for turn in range(turns):
                        if time.perf_counter() >= deadline:
                            break
                        build_start = time.perf_counter()
                        body = body_for(args, messages, profile, record["requests"][turn] if record else None)
                        request_headers = dict(headers)
                        if args.session_header:
                            request_headers[args.session_header] = sid if args.affinity == "stable" else str(uuid.uuid4())
                        prefix_hash = digest(json.dumps(body["messages"][0], sort_keys=True))
                        build_ms = (time.perf_counter() - build_start) * 1000
                        active += 1
                        peak = max(peak, active)
                        client_id = str(uuid.uuid4())
                        observation = {}
                        in_flight[client_id] = observation
                        try:
                            result, assistant = await request(client, args.base_url.rstrip("/") + "/chat/completions",
                                                               body, request_headers, args, observation)
                        finally:
                            active -= 1
                            in_flight.pop(client_id, None)
                        result.update(run_id=run_id, session_id=sid, session_index=index, turn=turn,
                                      profile="replay" if record else profile, prefix_hash=prefix_hash,
                                      build_ms=build_ms, client_request_id=client_id)
                        output.write(json.dumps(result) + "\n")
                        output.flush()
                        rows.append(result)
                        if result.get("error") == "cancelled":
                            raise asyncio.CancelledError
                        if not result["ok"]:
                            break  # No retries: do not conceal overload or replay a broken conversation.
                        if not record:
                            advance(messages, assistant, index, turn, args)
                        if turn + 1 < turns:
                            await asyncio.sleep(rng.uniform(0, args.think_seconds))
            monitoring = asyncio.create_task(monitor())
            interrupted = False
            try:
                async with asyncio.TaskGroup() as group:
                    for i in range(min(args.concurrency, args.sessions)):
                        group.create_task(worker(i))
            except asyncio.CancelledError:
                interrupted = True
            finally:
                monitoring.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitoring
                elapsed = time.perf_counter() - started
                summary = live_snapshot(rows, {}, elapsed, peak, lag)
                summary.update(run_id=run_id, interrupted=interrupted, peak_in_flight=peak,
                               event_loop_lag_ms=distribution(lag),
                               by_profile={p: summarize([r for r in rows if r["profile"] == p], elapsed)
                                           for p in sorted({r["profile"] for r in rows})},
                               by_turn={str(t): summarize([r for r in rows if r["turn"] == t], elapsed)
                                        for t in sorted({r["turn"] for r in rows})})
                (directory / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
                print(f"Report: {directory}\nrequests={len(rows)} success={summary['success']} peak={peak}")
    return 130 if interrupted else (1 if summary["failed"] or not rows else 0)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8000/v1", help="OpenAI API base including /v1")
    p.add_argument("--model", required=True)
    p.add_argument("--api-key-env", default="OPENAI_API_KEY")
    p.add_argument("--concurrency", type=int, default=32)
    p.add_argument("--sessions", type=int, default=128)
    p.add_argument("--turns", type=int, default=6)
    p.add_argument("--projects", type=int, default=32)
    p.add_argument("--profiles", nargs="+", choices=list(PROFILES), default=list(PROFILES))
    p.add_argument("--prefix-mode", choices=["shared", "project", "unique"], default="project")
    p.add_argument("--context-chars", type=int, default=48000)
    p.add_argument("--history-turns", type=int, default=2)
    p.add_argument("--tool-chars", type=int, default=8000)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--duration", type=float, default=0, help="Stop scheduling after seconds; drain active requests")
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("--ramp-seconds", type=float, default=10)
    p.add_argument("--think-seconds", type=float, default=0, help="Uniform delay in [0, value] between turns")
    p.add_argument("--session-header", default="", help="Use only the header supported by your router")
    p.add_argument("--backend-header", default="", help="Response header containing backend identity")
    p.add_argument("--affinity", choices=["stable", "rotate"], default="stable")
    p.add_argument("--fresh-connections", action="store_true")
    p.add_argument("--no-stream-usage", action="store_true")
    p.add_argument("--replay", help="JSONL sessions of full Chat Completions bodies")
    p.add_argument("--output", default="results")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--run-id", help=argparse.SUPPRESS)
    return p


def main():
    p = parser()
    args = p.parse_args()
    if args.run_id:
        try:
            args.run_id = str(uuid.UUID(args.run_id))
        except ValueError:
            p.error("run-id must be a UUID")
    if not 1 <= args.concurrency <= 2000:
        p.error("concurrency must be in [1, 2000]")
    for name in ("sessions", "turns", "projects", "context_chars", "tool_chars", "max_tokens", "timeout"):
        if getattr(args, name) <= 0:
            p.error(f"{name} must be positive")
    for name in ("history_turns", "duration", "ramp_seconds", "think_seconds"):
        if getattr(args, name) < 0:
            p.error(f"{name} cannot be negative")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
