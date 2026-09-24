"""The hosted page is generated; it must not be hand-edited.

docs/index.html is derived from the local dashboard by scripts/build_hosted.py
so the two boards cannot drift apart. This test regenerates it and fails if the
committed copy differs, which is what stops someone (me, later) from patching
the generated file directly and having the fix silently lost on the next build.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOSTED = ROOT / "docs" / "index.html"


def test_hosted_page_is_up_to_date():
    before = HOSTED.read_text()
    subprocess.run([sys.executable, "scripts/build_hosted.py"],
                   cwd=ROOT, check=True, capture_output=True)
    after = HOSTED.read_text()
    assert before == after, (
        "docs/index.html is stale or was hand-edited. Run "
        "`python3 scripts/build_hosted.py` and commit the result."
    )


def test_hosted_page_has_no_interactive_controls():
    """A static page cannot accept a click, so a control there is a lie."""
    html = HOSTED.read_text()
    assert 'class="chk' not in html, "the mark-done button is not clickable on a static page"
    assert "data-act=" not in html, "row action buttons cannot work without a server"
    assert "/api/" not in html, "the hosted page must not reference the local API"


def test_hosted_page_keeps_the_board_layout():
    html = HOSTED.read_text()
    # Tiles are built in JS, so assert on the construction call, not markup.
    for marker in ('createElement("details")', 'className = "board"',
                   'id="donut"', 'id="bars"', 'id="up"', "donutSvg"):
        assert marker in html, f"missing {marker!r}"


def test_both_pages_share_the_same_past_rule():
    """The Due/Done split must mean the same thing in both dashboards."""
    rule = re.compile(r"const isPast = \(row, now\) =>(.*?);", re.S)
    local = rule.search((ROOT / "src/coursetrack/dashboard/static/index.html").read_text())
    hosted = rule.search(HOSTED.read_text())
    assert local and hosted
    assert local.group(1).strip() == hosted.group(1).strip()
