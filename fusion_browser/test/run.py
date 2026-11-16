#!/usr/bin/env python3
"""
Run the component browser's own page code, headlessly — the shared technique in
fusion_ui/jsc_harness.py (read its docstring for the why). Only exercises what has no
live-Fusion dependency: suspend mode's guards, ⌘C's clipboard text, and the name-group
layer — which is pure reading of parameter names and so is the one piece of the tree
that CAN be checked without Fusion at all.

    python3 test/run.py
"""

import importlib.util
import os
import sys

WINDOW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "jsc_harness", os.path.join(os.path.dirname(WINDOW), "fusion_ui", "jsc_harness.py"))
harness = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(harness)


if __name__ == '__main__':
    sys.exit(harness.run(WINDOW, prelude=[harness.vocab('PARAM_NAMING_VOCAB', 'param_naming.json')]))
