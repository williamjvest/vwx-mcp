import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PATH = ROOT / "tools" / "benchmark_latency.py"
SPEC = importlib.util.spec_from_file_location("vwx_latency_benchmark", PATH)
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class LatencyBenchmarkTests(unittest.TestCase):
    def test_nearest_rank_percentile(self):
        self.assertEqual(BENCH.percentile([5, 1, 4, 2, 3], 0.95), 5)
        self.assertEqual(BENCH.percentile([5, 1, 4, 2, 3], 0.50), 3)
        with self.assertRaises(ValueError):
            BENCH.percentile([1, 2, 3], 1.1)

    def test_regression_uses_absolute_noise_floor(self):
        baseline = {"paths": {"file_ipc": {"cases": {
            "ping": {"median_ms": 100, "p95_ms": 200}
        }}}}
        current = {"paths": {"file_ipc": {"cases": {
            "ping": {"median_ms": 190, "p95_ms": 240}
        }}}}
        self.assertEqual(BENCH.regressions(current, baseline, 1.35), [])

    def test_post_restart_vectorworks_band_is_not_a_regression(self):
        baseline = {"paths": {"file_ipc": {"cases": {
            "ping": {"median_ms": 146, "p95_ms": 166}
        }}}}
        current = {"paths": {"file_ipc": {"cases": {
            "ping": {"median_ms": 240, "p95_ms": 280}
        }}}}
        self.assertEqual(BENCH.regressions(current, baseline, 1.35), [])

    def test_regression_flags_slow_case(self):
        baseline = {"cases": {"ping": {"median_ms": 160, "p95_ms": 250}}}
        current = {"cases": {"ping": {"median_ms": 300, "p95_ms": 600}}}
        failures = BENCH.regressions(current, baseline, 1.35)
        self.assertEqual(len(failures), 2)

    def test_regression_supports_mixed_paths(self):
        baseline = {"paths": {
            "file_ipc": {"cases": {"ping": {"median_ms": 100, "p95_ms": 200}}},
            "mcp_http": {"cases": {"ping": {"median_ms": 110, "p95_ms": 220}}},
        }}
        current = {"paths": {
            "mcp_http": {"cases": {"ping": {"median_ms": 200, "p95_ms": 290}}}
        }}
        self.assertEqual(BENCH.regressions(current, baseline, 1.35), [])

    def test_regression_flags_toolset_drift_when_toolsets_match(self):
        # Drift is only flagged when both snapshots name the same toolset.
        baseline = {"paths": {"mcp_http": {
            "toolset": "mechanical", "tool_count": 78,
            "cases": {"ping": {"median_ms": 100, "p95_ms": 200}},
        }}}
        current = {"paths": {"mcp_http": {
            "toolset": "mechanical", "tool_count": 79,
            "cases": {"ping": {"median_ms": 100, "p95_ms": 200}},
        }}}
        self.assertEqual(len(BENCH.regressions(current, baseline, 1.35)), 1)

    def test_regression_ignores_toolset_drift_when_toolset_differs(self):
        # A baseline recorded under one preset must not fail a run under another.
        baseline = {"paths": {"mcp_http": {
            "toolset": "mechanical", "tool_count": 78,
            "cases": {"ping": {"median_ms": 100, "p95_ms": 200}},
        }}}
        current = {"paths": {"mcp_http": {
            "toolset": "interior", "tool_count": 115,
            "cases": {"ping": {"median_ms": 100, "p95_ms": 200}},
        }}}
        self.assertEqual(BENCH.regressions(current, baseline, 1.35), [])

    def test_regression_ignores_toolset_drift_when_baseline_has_no_toolset(self):
        # Old baselines (pre-toolset field) must not permanently fail.
        baseline = {"paths": {"mcp_http": {
            "tool_count": 56,
            "cases": {"ping": {"median_ms": 100, "p95_ms": 200}},
        }}}
        current = {"paths": {"mcp_http": {
            "toolset": "mechanical", "tool_count": 78,
            "cases": {"ping": {"median_ms": 100, "p95_ms": 200}},
        }}}
        self.assertEqual(BENCH.regressions(current, baseline, 1.35), [])
