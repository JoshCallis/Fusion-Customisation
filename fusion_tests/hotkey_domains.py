"""Offline exercise of the router's decisions. Stubs adsk so the pure logic —
domains, exclusivity, specificity, resolution — can be checked without Fusion."""
import sys, types, json, os, tempfile

adsk = types.ModuleType('adsk'); core = types.ModuleType('adsk.core')
class _H: pass
core.ApplicationCommandEventHandler = _H
core.CommandCreatedEventHandler = _H
class _App:
    @staticmethod
    def get():
        a = types.SimpleNamespace(); a.userInterface = types.SimpleNamespace()
        return a
core.Application = _App
adsk.core = core
sys.modules['adsk'] = adsk; sys.modules['adsk.core'] = core

sys.path.insert(0, '/Users/josh/Code/Scripts/fusion_ui')
import importlib.util
spec = importlib.util.spec_from_file_location('fusion_hotkeys', '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py')
hk = importlib.util.module_from_spec(spec); sys.modules['fusion_hotkeys'] = hk
spec.loader.exec_module(hk)

# isolate the bindings file
tmp = tempfile.mkdtemp(); hk.BINDINGS_JSON = os.path.join(tmp, 'bindings.json')
# ...and every file the LIVE add-in shares with remapd. /tmp/fusion_command is
# remapd's `command` axis: a route dispatched here once wrote 'JoshRoute_AltCtrl9'
# into it, and every idle-only key stopped until Fusion next published.
hk.COMMAND_STAMP = os.path.join(tmp, 'fusion_command')
hk.SKETCH_AXIS_STAMP = os.path.join(tmp, 'fusion_sketch')
hk.LOG = os.path.join(tmp, 'fusion_hotkeys.log')
json.dump({'bindings': {}}, open(hk.BINDINGS_JSON, 'w')); hk._bindings = None; hk._bind_mtime = 0

fired = []
def mk(name): return lambda: fired.append(name)

hk.declare('CycleOp',   'Extrude: Cycle Operation', when={'command': 'extrude'}, run=mk('cycle'),  register=False)
hk.declare('StartExtr', 'Extrude',                  when={'command': 'none'},    native=True,      register=False)
hk.declare('SketchThing','Sketch: Something',       when={'sketch': 'in', 'command': 'none'}, run=mk('sketch'), register=False)
hk.declare('Broad',     'Always',                   when={},                     run=mk('broad'),  register=False)

for cid in ('CycleOp', 'StartExtr', 'SketchThing'):
    hk.set_binding(cid, 's')

ok = True
def check(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print(('  ok  ' if good else '  FAIL') + '  %-52s %s' % (label, got if good else '%r != %r' % (got, want)))

print('\n1. Exclusivity is DERIVED, not declared')
check('extrude-running vs nothing-running',
      hk.exclusive({'command': 'extrude'}, {'command': 'none'}), (True, ['command']))
check('in-a-sketch vs extrude-running',
      hk.exclusive({'sketch': 'in', 'command': 'none'}, {'command': 'extrude'}), (True, ['command']))
check('in-a-sketch vs out-of-a-sketch',
      hk.exclusive({'sketch': 'in'}, {'sketch': 'out'}), (True, ['sketch']))
check('unconstrained vs anything is NOT exclusive',
      hk.exclusive({}, {'sketch': 'in'})[0], False)
check('alias: command=extrude covers SmartExtrudeCmd',
      'SmartExtrudeCmd' in hk.normalise({'command': 'extrude'})['command'], True)

print('\n2. One key, three claimants: resolution by live context')
check('extrude running            -> cycle the operation',
      hk.resolve('s', {'command': 'SmartExtrudeCmd', 'sketch': 'out'}).id, 'CycleOp')
check('idle in a sketch           -> the sketch action',
      hk.resolve('s', {'command': 'none', 'sketch': 'in'}).id, 'SketchThing')
check('idle, not in a sketch      -> start an extrude',
      hk.resolve('s', {'command': 'none', 'sketch': 'out'}).id, 'StartExtr')
check('some other command running -> nothing claims it',
      hk.resolve('s', {'command': 'FilletCmd', 'sketch': 'out'}), None)

print('\n3. Specificity breaks a genuine overlap')
# A narrow special case and a general fallback SHARE a key on purpose: they are not
# exclusive, so the more specific one wins wherever it applies.
hk.declare('NarrowSketch', 'Sketch: Narrow', when={'sketch': 'in'}, run=mk('narrow'), register=False)
hk.set_binding('Broad', 't'); hk.set_binding('NarrowSketch', 't')
check('narrow beats broad in the sketch',
      hk.resolve('t', {'command': 'none', 'sketch': 'in'}).id, 'NarrowSketch')
check('broad still gets it elsewhere',
      hk.resolve('t', {'command': 'none', 'sketch': 'out'}).id, 'Broad')

print('\n4. What the window is told when a key is asked for')
check('a fourth domain, exclusive with all three, is free to take s',
      hk.conflicts('s', {'command': 'FilletCmd'}), [])
check('the SAME domain as one already on s is not',
      [c['id'] for c in hk.conflicts('s', {'command': 'extrude'})], ['CycleOp'])
check('adding an unscoped action to s collides with all three',
      sorted(c['id'] for c in hk.conflicts('s', {})), ['CycleOp', 'SketchThing', 'StartExtr'])

print('\n5. Re-binding one action leaves the others alone')
before = hk.bindings()
hk.set_binding('CycleOp', 'shift+s')
after = hk.bindings()
check('only CycleOp moved', {k: v for k, v in after.items() if before.get(k) != v}, {'CycleOp': 'shift+s'})
check('s still resolves for the others',
      hk.resolve('s', {'command': 'none', 'sketch': 'in'}).id, 'SketchThing')

print('\n6. A per-command override merges over a declared `when`')
hk.declare('OverrideTest', 'Override Test', when={'command': 'extrude'}, run=mk('override'), register=False)
hk.set_binding('OverrideTest', 'u', when={'sketch': 'in'})
check('override ADDS an axis the declaration did not have',
      hk.when_of(hk.get('OverrideTest')), {'command': 'extrude', 'sketch': 'in'})
check('resolves under the MERGED domain, not just the declared one',
      hk.resolve('u', {'command': 'SmartExtrudeCmd', 'sketch': 'in'}).id, 'OverrideTest')
hk.set_binding('OverrideTest', 'u', when={'command': 'none'})
check('override REPLACES an axis the declaration already had',
      hk.when_of(hk.get('OverrideTest')), {'command': 'none'})
hk.set_binding('OverrideTest', '')   # clear the claim so it stops sharing 'u'/'s' below


def _write_raw(bindings_map, when_map=None, routes_map=None):
    """Write bindings.json directly, bypassing set_binding() — the shape a bound
    id Fusion's own keymap holds would arrive in, with no Action ever declared
    for it."""
    data = {'bindings': bindings_map, 'when': when_map or {}, 'routes': routes_map or {}}
    json.dump(data, open(hk.BINDINGS_JSON, 'w'))
    hk._bind_mtime = 0   # force _load_bindings() to notice the change


print('\n7. A bound id with no declared Action is adopted, and dropped when its claim goes')
raw = dict(hk.bindings())
raw['UnknownNativeCmd'] = 'ctrl+alt+z'
_write_raw(raw, {'UnknownNativeCmd': {'workspace': 'design'}})
hk._load_bindings()
adopted = hk.get('UnknownNativeCmd')
check('adopted as an Action', adopted is not None, True)
check('adopted is native', getattr(adopted, 'native', None), True)
check('adopted is flagged adopted', getattr(adopted, 'adopted', None), True)
check('adopted carries its override as `when`', hk.when_of(adopted), {'workspace': 'design'})
raw.pop('UnknownNativeCmd')
_write_raw(raw)
hk._load_bindings()
check('dropped once its claim is gone', hk.get('UnknownNativeCmd'), None)


print('\n8. set_binding() preserves `when` and `routes` it did not touch')
hk.ensure_route('shift+alt+r')
before_routes = dict(hk._routes)
hk.set_binding('CycleOp', 'ctrl+alt+q')
check('routes untouched by an unrelated set_binding', dict(hk._routes), before_routes)
hk.set_binding('SketchThing', 'ctrl+alt+w')
check('routes STILL untouched by a second unrelated set_binding', dict(hk._routes), before_routes)


print('\n9. Route dispatch: cancels itself, runs the resolved winner')
hk.declare('RouteNative', 'Route Native', when={'workspace': 'design'}, native=True, register=False)
hk.declare('RouteOurs',   'Route Ours',   when={'workspace': 'cam'},    run=mk('routeours'), register=False)
rid = hk.route_id('ctrl+alt+9')
hk._routes[rid] = 'ctrl+alt+9'
hk.set_binding('RouteNative', 'ctrl+alt+9')
hk.set_binding('RouteOurs', 'ctrl+alt+9')

executed = []
hk._execute = lambda cid: executed.append(cid)


class FakeArgs:
    def __init__(self, cid):
        self.commandId = cid
        self.isCanceled = False


_orig_sample = hk._sample
hk._ctx_time = 0
hk._sample = lambda axis: 'design' if axis == 'workspace' else _orig_sample(axis)
args = FakeArgs(rid)
hk._on_command_starting(args)
hk._sample = _orig_sample
check('route cancels itself', args.isCanceled, True)
check('native winner is executed, not invoked', executed, ['RouteNative'])

fired.clear(); executed.clear()
hk._ctx_time = 0   # bust the context cache so the new workspace is sampled
args = FakeArgs(rid)
_orig_sample = hk._sample
hk._sample = lambda axis: 'cam' if axis == 'workspace' else _orig_sample(axis)
hk._on_command_starting(args)
hk._sample = _orig_sample
check('route cancels itself (ours)', args.isCanceled, True)
check('our winner is invoked, not executed', fired, ['routeours'])
check('nothing executed for our own winner', executed, [])

fired.clear(); executed.clear()
hk._ctx_time = 0
args = FakeArgs(rid)
hk._sample = lambda axis: 'sim' if axis == 'workspace' else _orig_sample(axis)
hk._on_command_starting(args)
hk._sample = _orig_sample
check('route with no matching claimant still cancels', args.isCanceled, True)
check('nothing runs when nothing matches', fired + executed, [])


print('\n10. sync_from_keymap() leaves an adopted id\'s claim alone')
raw = dict(hk.bindings())
raw['AdoptedSync'] = 'ctrl+alt+8'
_write_raw(raw)
hk._load_bindings()
adopted = hk.get('AdoptedSync')
hk.sync_from_keymap([{'seq': 'ctrl+alt+7', 'commands': [{'id': 'AdoptedSync'}]}])
check('seq_hint is still updated', adopted.seq_hint, 'ctrl+alt+7')
check('the binding itself is untouched', hk.binding_of('AdoptedSync'), 'ctrl+alt+8')


print('\n11. route_id() agrees with remap/fusion.py\'s copy')
sys.path.insert(0, '/Users/josh/Code/Scripts/remap')
import fusion as remap_fusion  # noqa: E402
for chord in ('e', 'ctrl+alt+e', 'shift+alt+comma', 'cmd+.', 'ctrl+alt+shift+cmd+q',
              # Fusion's own spelling — what apply_plan actually sends /route
              'shift+control+d', 'alt+control+s', 'shift+command+alt+control+f',
              'option+e'):
    check('route_id(%r) matches remap/fusion.py' % chord,
          hk.route_id(chord), remap_fusion.route_id(chord))


print('\n12. Shared actions: one Cycle Direction, a provider per command')
hk._register_definition = lambda act: None          # no Fusion here
hk.provide('direction', 'ExtrudeT', {'command': 'extrude'}, mk('extrude-dir'))
hk.provide('direction', 'PatternT', {'command': 'SmartPatternCmd'}, mk('pattern-dir'))
hk.declare('IdleDirKey', 'Idle thing', when={'command': 'none'}, run=mk('idle'), register=False)
hk.set_binding('ExtrudeCycleDirectionCmd', 'alt+control+x'); hk.set_binding('IdleDirKey', 'alt+control+x')
shared = hk.get('ExtrudeCycleDirectionCmd')
check('its domain is the union of its providers',
      hk.normalise(hk.when_of(shared))['command'] >= {'SmartExtrudeCmd', 'SmartPatternCmd'}, True)
check('it can share a key with a command:none action',
      hk.exclusive(hk.when_of(shared), {'command': 'none'}), (True, ['command']))
check('env is left open by a mixed command list',
      hk._env_of({'command': ['extrude', 'SmartPatternCmd']}), None)
real_context = hk.context
check('it applies in Dialogs', hk.when_of(shared).get('env'), ['dialog'])
for cmd in ('SmartExtrudeCmd', 'SmartPatternCmd', 'ExtrudeCycleDirectionCmd'):
    check('%-24s is one of our dialogs' % cmd, hk._is_dialog(cmd), True)
check('a native command is not a dialog', hk._is_dialog('SketchDimension'), False)
for cmd, want in (('SmartExtrudeCmd', 'extrude-dir'), ('SmartPatternCmd', 'pattern-dir')):
    hk.context = lambda axes=None, force=False, c=cmd: {'command': c, 'sketch': 'out'}
    del fired[:]
    hk.resolve('alt+control+x', {'command': cmd, 'env': 'dialog'}).invoke()
    check('%-16s -> its own provider' % cmd, fired, [want])
# The editor sends back the whole domain it was shown, providers' commands and all;
# a stale list must never pin which dialogs the cycle works in.
hk.set_binding('ExtrudeCycleDirectionCmd', 'alt+control+x',
               {'env': ['dialog'], 'command': ['extrude']})
check('set_binding drops a shared action\'s command', hk._when_over.get('ExtrudeCycleDirectionCmd'),
      {'env': ['dialog']})
hk._when_over['ExtrudeCycleDirectionCmd'] = {'env': ['dialog'], 'command': ['extrude']}
check('a frozen command override loses to the providers',
      'SmartPatternCmd' in hk.normalise(hk.when_of(shared))['command'], True)
hk.set_binding('ExtrudeCycleDirectionCmd', 'alt+control+x', {})
check('idle -> the other claimant',
      hk.resolve('alt+control+x', {'command': 'none'}).id, 'IdleDirKey')
hk.context = real_context
hk.withdraw('PatternT')
check('withdraw drops the provider from the domain',
      'SmartPatternCmd' in hk.normalise(hk.when_of(hk.get('ExtrudeCycleDirectionCmd')))['command'], False)
hk.withdraw('ExtrudeT')
check('the last provider gone, the action goes', hk.get('ExtrudeCycleDirectionCmd'), None)
hk.provide('direction_back', 'ExtrudeT', {'command': 'extrude'}, mk('extrude-dir-back'))
back = hk.get('CycleDirectionBackCmd')
check('Cycle Direction Back is its own shared action', back is not None and back.owner, 'Shared')
check('it is one of our dialogs', hk._is_dialog('CycleDirectionBackCmd'), True)
hk.set_binding('CycleDirectionBackCmd', 'shift+alt+control+x')
hk.context = lambda axes=None, force=False: {'command': 'SmartExtrudeCmd', 'sketch': 'out'}
del fired[:]
hk.resolve('shift+alt+control+x', {'command': 'SmartExtrudeCmd', 'env': 'dialog'}).invoke()
check('the back key runs the back provider', fired, ['extrude-dir-back'])
hk.context = real_context
hk.set_binding('CycleDirectionBackCmd', '')
hk.withdraw('ExtrudeT')


print('\n11. an env override replaces a declared command that only spelled env')
# Browser declares every action `command: none` (= Sketch + Viewport). Ticking
# Dialogs used to merge under it and come back unticked.
hk.declare('BrowserLike', 'Browser thing', when={'command': 'none'}, native=True, register=False)
hk.declare('RealCmdScope', 'Scoped to a real id', when={'command': 'SketchDimension'},
           native=True, register=False)
hk.set_binding('BrowserLike', 'ctrl+alt+8', {'env': ['sketch', 'viewport', 'dialog']})
check('command:none gives way to env', hk.when_of(hk.get('BrowserLike')),
      {'env': ['sketch', 'viewport', 'dialog']})
check('it resolves in a dialog now',
      getattr(hk.resolve('ctrl+alt+8', {'command': 'SmartCombineCmd', 'env': 'dialog'}), 'id', None),
      'BrowserLike')
hk.set_binding('BrowserLike', 'ctrl+alt+8', {})
check('no override -> the declaration again', hk.when_of(hk.get('BrowserLike')), {'command': 'none'})
hk.set_binding('RealCmdScope', 'ctrl+alt+7', {'env': ['dialog']})
check('a command with no implied_by entry is kept',
      hk.when_of(hk.get('RealCmdScope')).get('command'), 'SketchDimension')
hk.set_binding('BrowserLike', ''); hk.set_binding('RealCmdScope', '')


print('\n%s\n' % ('ALL PASS' if ok else 'FAILURES ABOVE'))
sys.exit(0 if ok else 1)
