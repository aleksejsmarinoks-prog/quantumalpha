#!/usr/bin/env python3
"""
QA Phase 7.2 — Atomic patch script for funding_arb.py and liquidity_vortex.py

Adds [EVAL_TICK] structured log marker at evaluation cycle end. Idempotent
(safe to re-run). AST-validated before write.

Usage from VPS:
    cd /home/qa/quantumalpha
    python -m bot.strategies._funding_arb_patch          # patch both files
    # OR specify which:
    python -m bot.strategies._funding_arb_patch funding_arb
    python -m bot.strategies._funding_arb_patch liquidity_vortex

What it does:
  For each target file:
    1. Locates the evaluate() / run_cycle() / tick() method
    2. Inserts a logger.info("[EVAL_TICK] strategy=<id> ...") line at the end
       of the method (before return)
    3. Validates with ast.parse() before writing
    4. Backs up original to <file>.bak.phase72_<timestamp>

If the script CAN'T safely auto-patch (method signature too varied),
prints a manual diff and exits non-zero. In that case, apply the diff
shown in PHASE7_2_INTEGRATION_PATCH.md manually.
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("qa.patch72")

ROOT = Path(__file__).resolve().parents[2]   # project root


# ---------------------------------------------------------------------------
# Patch configurations
# ---------------------------------------------------------------------------

PATCHES = {
    "funding_arb": {
        "file": "bot/strategies/funding_arb.py",
        "marker_check": "[EVAL_TICK] strategy=funding_arb_v1",
        # Search for typical cycle-end patterns
        "anchor_patterns": [
            re.compile(r"^(\s+)(return\s+(None|self\._.*|cycle_result|result))\s*$", re.M),
            re.compile(r"^(\s+)return\s*$", re.M),
        ],
        "marker_line": (
            'self.logger.info("[EVAL_TICK] strategy=funding_arb_v1 '
            'result=%s", _result_label)'
        ),
        "preamble_lines": [
            "# Phase 7.2 — telemetry marker",
            "_result_label = locals().get('cycle_result', 'unknown') "
            "if isinstance(locals().get('cycle_result'), str) else 'hold'",
        ],
    },
    "liquidity_vortex": {
        "file": "bot/strategies/liquidity_vortex.py",
        "marker_check": "[EVAL_TICK] strategy=liquidity_vortex_v1",
        "anchor_patterns": [
            re.compile(r"^(\s+)(return\s+(None|self\._.*|tick_result|verdict))\s*$", re.M),
            re.compile(r"^(\s+)return\s*$", re.M),
        ],
        "marker_line": (
            'self.logger.info("[EVAL_TICK] strategy=liquidity_vortex_v1 '
            'symbols=%s result=%s", ",".join(self.symbols) '
            'if hasattr(self, "symbols") else "?", _result_label)'
        ),
        "preamble_lines": [
            "# Phase 7.2 — telemetry marker",
            "_result_label = locals().get('verdict', 'hold') "
            "if isinstance(locals().get('verdict'), str) else 'hold'",
        ],
    },
}


def _already_patched(content: str, marker: str) -> bool:
    return marker in content


def _backup(path: Path) -> Path:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    bak = path.with_suffix(path.suffix + f".bak.phase72_{ts}")
    shutil.copy2(path, bak)
    log.info("  backup → %s", bak.name)
    return bak


def _validate_python(content: str, file_label: str) -> bool:
    try:
        ast.parse(content)
        return True
    except SyntaxError as e:
        log.error("  SYNTAX ERROR after patch (%s): %s", file_label, e)
        return False


def patch_file(key: str) -> bool:
    cfg = PATCHES[key]
    path = ROOT / str(cfg["file"])

    if not path.exists():
        log.error("  %s: file not found at %s", key, path)
        return False

    original = path.read_text(encoding="utf-8")

    if _already_patched(original, str(cfg["marker_check"])):
        log.info("  %s: already patched ✓ (skip)", key)
        return True

    # Find the LAST return statement in the file (last cycle/method conclusion)
    matches: List[re.Match] = []
    anchor_patterns = cfg["anchor_patterns"]
    assert isinstance(anchor_patterns, list)
    for pat in anchor_patterns:
        matches.extend(pat.finditer(original))
    if not matches:
        log.error("  %s: no return anchor found — manual patch needed", key)
        log.error("    See PHASE7_2_INTEGRATION_PATCH.md")
        return False

    # Pick the last occurrence — most likely the cycle-end return
    last = max(matches, key=lambda m: m.start())
    indent = last.group(1)
    insert_pos = last.start()

    block_lines = [f"{indent}{ln}" for ln in cfg["preamble_lines"]]
    block_lines.append(f"{indent}{cfg['marker_line']}")
    block = "\n".join(block_lines) + "\n"

    new_content = original[:insert_pos] + block + original[insert_pos:]

    if not _validate_python(new_content, key):
        log.error("  %s: NOT writing (would break parser)", key)
        return False

    _backup(path)
    path.write_text(new_content, encoding="utf-8")
    log.info("  %s: patched ✓", key)
    return True


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    targets = argv if argv else list(PATCHES.keys())

    log.info("=== QA Phase 7.2 — strategy log marker patcher ===")
    log.info("Project root: %s", ROOT)
    ok = True
    for key in targets:
        if key not in PATCHES:
            log.error("Unknown target: %s (valid: %s)",
                      key, ", ".join(PATCHES.keys()))
            ok = False
            continue
        log.info("Patching %s ...", key)
        if not patch_file(key):
            ok = False

    if ok:
        log.info("All patches applied. Run tests + restart qa-bot.service.")
        return 0
    log.error("Some patches failed — see messages above.")
    log.error("Fall back to manual diffs in PHASE7_2_INTEGRATION_PATCH.md")
    return 2


if __name__ == "__main__":
    sys.exit(main())
