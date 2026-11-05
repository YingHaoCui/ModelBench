from kv_experiment import sessions, compare


def test_shared_prefix_and_unique_prefix_start_before_context():
    rows = list(sessions(count=3, turns=2, repeat_ratio=1, context_chars=100))
    assert rows[0]["requests"][0]["messages"][0] == rows[1]["requests"][0]["messages"][0]
    assert len(rows[0]["requests"][1]["messages"]) == 4
    unique = list(sessions(count=3, repeat_ratio=0, context_chars=100))
    assert len({r["requests"][0]["messages"][0]["content"] for r in unique}) == 3
    assert rows == list(sessions(count=3, turns=2, repeat_ratio=1, context_chars=100))


def test_perf_gate_does_not_accept_hit_rate_as_latency_evidence():
    baseline = {"success": 100, "requests": 100, "failed": 0, "ttft_ms": {"p95": 100}, "successful_requests_per_s": 10}
    assert compare(baseline, {"weighted_cache_hit_ratio": 1}, high_repeat=True)["status"] == "insufficient_evidence"
    assert compare(baseline, baseline, high_repeat=True)["status"] == "pair_fail"
    assert compare(baseline, baseline, high_repeat=False)["status"] == "pair_pass"
