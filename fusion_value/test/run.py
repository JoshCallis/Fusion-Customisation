#!/usr/bin/env python3
"""
Run the value window's own page code, headlessly.

    python3 test/run.py                  # live parameters, from the params server
    python3 test/run.py params.json      # a saved /params snapshot

The shared technique is fusion_ui/jsc_harness.py — read its docstring for the why.
This window adds its own DOM shim (dom_shim.js here, a richer one than the generic
shim) and hands the cases both vocabularies plus a REAL parameter list, so the
autocomplete is asserted on against what a document actually contains. Because the
page's top-level `let`s end up in the same scope, cases.js can reach in and drive
updateAC() / acceptAC() directly with a chosen situation.
"""

import importlib.util
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOW = os.path.dirname(HERE)
_spec = importlib.util.spec_from_file_location(
    "jsc_harness", os.path.join(os.path.dirname(WINDOW), "fusion_ui", "jsc_harness.py"))
harness = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(harness)

PARAMS_URL = 'http://127.0.0.1:8731/params'


def load_params(source):
    if source is None:
        with urllib.request.urlopen(PARAMS_URL, timeout=3) as fh:
            return json.load(fh)['params']
    with open(source) as fh:
        data = json.load(fh)
    return data['params'] if isinstance(data, dict) else data


def main():
    params = load_params(sys.argv[1] if len(sys.argv) > 1 else None)
    return harness.run(
        WINDOW,
        shim=os.path.join(HERE, 'dom_shim.js'),
        prelude=[harness.vocab('VOCAB', 'variables.json'),
                 harness.vocab('PARAM_NAMING_VOCAB', 'param_naming.json'),
                 'var PARAMS = ' + json.dumps(params) + ';'],
        ok_note=' (%d parameters)' % len(params))


if __name__ == '__main__':
    sys.exit(main())
