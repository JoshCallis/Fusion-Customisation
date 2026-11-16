"""
fusion_shared_state.py
──────────────────────
Tiny key/value store shared across Fusion 360 add-ins via a JSON file that
lives alongside this module (i.e. in the AddIns root directory).

USAGE IN ANY ADD-IN
───────────────────
At the top of your add-in, add the AddIns root to sys.path so Python can
find this module, then import it:

    import os, sys
    _ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ADDINS_DIR not in sys.path:
        sys.path.insert(0, _ADDINS_DIR)
    import fusion_shared_state as fss

Then read / write values anywhere:

    # Read
    visible = fss.get_value('sketches_visible', default=False)

    # Write
    fss.set_value('sketches_visible', True)

    # Delete a key
    fss.delete_key('sketches_visible')

    # Read the whole dict (avoid mutating it directly)
    state = fss.read_all()

KEYS CURRENTLY IN USE
─────────────────────
  'sketches_visible'  bool   Set by ToggleSketchVisibility.
                             When True, add-ins that switch the active
                             component should make the new component's
                             sketches visible.
"""

import json
import os

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'Shared State.json')


def read_all() -> dict:
    """Return the full state dict. Returns {} if the file is missing or corrupt."""
    try:
        with open(_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_all(state: dict) -> None:
    with open(_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


def get_value(key: str, default=None):
    """Return the value for *key*, or *default* if it is not present."""
    return read_all().get(key, default)


def set_value(key: str, value) -> None:
    """Write *value* for *key* and persist to disk."""
    state = read_all()
    state[key] = value
    _write_all(state)


def delete_key(key: str) -> None:
    """Remove *key* from the store if it exists."""
    state = read_all()
    if key in state:
        del state[key]
        _write_all(state)


def run(context):
    pass


def stop(context):
    pass
