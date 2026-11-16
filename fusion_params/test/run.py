#!/usr/bin/env python3
"""
Run the parameter editor's own page code, headlessly — the shared technique in
fusion_ui/jsc_harness.py (read its docstring for the why): dom_shim.js, the shared
engines index.html loads, index.js verbatim, the naming vocabulary, then cases.js.

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
