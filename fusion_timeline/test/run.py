#!/usr/bin/env python3
"""
Run the timeline page's own code headlessly — the shared technique in
fusion_ui/jsc_harness.py (read its docstring for the why). Covers the tree model, which
is pure: component groups, ⌘G around rows, nesting, explode, rename carry, wrong-side
filing and the health roll-up. Everything that needs the add-in (edit, suppress,
rename in Fusion, the lock) is checked by hand in Fusion.

    python3 fusion_timeline/test/run.py
"""

import importlib.util
import io
import os
import sys

WINDOW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "jsc_harness", os.path.join(os.path.dirname(WINDOW), "fusion_ui", "jsc_harness.py"))
harness = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(harness)


if __name__ == '__main__':
    with io.open(os.path.join(WINDOW, 'test', 'fixture.json'), encoding='utf-8') as f:
        fixture = 'var FIXTURE = %s;' % f.read()
    sys.exit(harness.run(WINDOW, prelude=[fixture]))
