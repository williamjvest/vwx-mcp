# Tool coverage — historical live sweep + current automated checks

The table below records the original 222-case live Vectorworks 2026 sweep. It is
historical evidence, **not** a claim that every current command is live-tested:
the server now exposes 291 typed MCP tools backed by 368 public plugin verbs.
Current pull-request coverage is the automated `tests/` suite; new or changed
`vs.*` behavior still requires a focused live bridge readback before deployment.

## Historical live-sweep totals

| status | count | meaning |
|---|---|---|
| ok | 164 | executed, valid result |
| handled_error | 56 | intentional bad-input test → clean error dict (no crash, no dialog) |
| dialog_skip | 0 | opens a modal VW dialog (no headless `vs` path) — excluded from automated runs |
| FAIL | 2 | real bug at sweep time — **all fixed since** (see below) |

## By phase

| phase | ok | handled_error | other |
|---|---|---|---|
| P1_getters | 25 | 4 | 0 |
| P2_create | 30 | 3 | 0 |
| P3_query | 17 | 2 | 0 |
| P4_mutate | 61 | 18 | 2 |
| P5_wall_components | 8 | 2 | 0 |
| P6_worksheet | 3 | 0 | 0 |
| P7_viewport | 3 | 6 | 0 |
| P8_export | 7 | 1 | 0 |
| P9_misc | 6 | 17 | 0 |
| P10_cleanup | 4 | 3 | 0 |

## Bugs found + fixed

The sweep is a regression harness: each FAIL became a fix in `commands.py`. API renames are catalogued in [AGENTS.md](../AGENTS.md#vw2026-api-renames-function-does-not-exist-under-the-old-name). Highlights:

- `create_wall` **hard-crashed VW** (poking undocumented prefs) → rewritten with documented wall APIs.
- 3D extrudes, dimensions, class visibility, IFC props, worksheet cells, save, symbol-from-objects, mirror, layer rename — all called functions that don't exist under VW2026 names; corrected.
- Every fix re-verified by re-running its phase in the background.

## Current verification

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m py_compile vwx-plugin/*.py mcp-server/*.py
```

The latency harness additionally verifies the selected `VWX_TOOLSET` and live
MCP/file-IPC path when Vectorworks is running. Focused mutation tests must use a
named disposable document and verify state after the call.

## Re-running the historical sweep

The original scratchpad runner was not retained, so this exact 222-case sweep is
not currently reproducible. Do not cite it as current coverage. A replacement
must live under `tools/` with a named disposable-document guard before this
section can again claim a full live sweep.
