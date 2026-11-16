"""
+ Workspace Toggle
──────────────────
One assignable command that flips between the Design and Render workspaces, and
makes the Render workspace usable as a no-timeline modeling environment:

  • Toggle Design ⇄ Render            — assign a keyboard shortcut (works even
    with the ribbon hidden).
  • On cold startup, switch to Render as soon as the first document lands in
    Design (Fusion always starts in Design; activeWorkspace throws until the
    session exists, so the tick loop retries until the switch takes).
  • In Render, mirror every Design command into hidden panels so the "S" command
    search can find them (Extrude, Fillet, …). Re-applied on launch because
    these toolbar customizations do not survive a Fusion restart.
  • In Render, force ShadedWithVisibleEdgesOnly (+refresh) so body edges draw.
  • Keep the Rendering Gallery palette CLOSED whenever it appears (closed via the
    API, not moved off-screen — so it doesn't confuse yabai).
  • After a switch, fire Maximize Viewport's reapply event so hidden chrome
    (tabs/ribbon) doesn't flash back up.

The timeline only exists in Design, so switching there brings it up (the reason
to switch). Keep Maximize Viewport's "Hide Timeline" OFF so the Design timeline
stays visible.
"""

import adsk.core
import json
import threading
import time
import traceback

_app = None
_ui = None
_handlers = []
_running = False
_gallery_burst_until = 0.0   # close the gallery fast for a moment after entering Render
_startup_render_pending = False   # cold startup: flip Design→Render once a doc is live
_startup_render_deadline = 0.0

CMD_ID = 'JoshWorkspaceToggle'
CMD_NAME = 'Toggle Design/Render'
CMD_DESC = 'Switch between the Design and Render workspaces'
PANEL_ID = 'SolidScriptsAddinsPanel'

DESIGN = 'FusionSolidEnvironment'
RENDER = 'FusionRenderEnvironment'
GALLERY = 'RenderingGallery'
CMD_TAB = 'DesignCmdsTab'

EV_SWITCH = 'JoshWSToggleSwitch'
EV_TICK = 'JoshWSToggleTick'
MAXIMIZE_EVENT = 'MaximizeViewportReapply'


# ── Render setup helpers ───────────────────────────────────────────────────────

def _collect(controls, acc, seen):
    for c in controls:
        try:
            ot = c.objectType
            if ot == 'adsk::core::CommandControl':
                cd = c.commandDefinition
                if cd and cd.id not in seen:
                    seen.add(cd.id); acc.append(cd.id)
            elif ot == 'adsk::core::DropDownControl':
                _collect(c.controls, acc, seen)
        except Exception:
            pass


def _populate_render():
    """Mirror every Design command into hidden Render panels (idempotent)."""
    try:
        design = _ui.workspaces.itemById(DESIGN)
        render = _ui.workspaces.itemById(RENDER)
        if not design or not render:
            return
        acc = []; seen = set()
        for tab in design.toolbarTabs:
            for p in tab.toolbarPanels:
                try:
                    _collect(p.controls, acc, seen)
                except Exception:
                    pass
        if not acc:
            return
        tab = render.toolbarTabs.itemById(CMD_TAB) or render.toolbarTabs.add(CMD_TAB, 'DesignCmds')
        CHUNK = 40
        panels = []
        for i in range(0, len(acc), CHUNK):
            pid = 'DC_Panel%d' % (i // CHUNK)
            pp = tab.toolbarPanels.itemById(pid) or tab.toolbarPanels.add(pid, 'DC%d' % (i // CHUNK))
            panels.append(pp)
        for idx, cid in enumerate(acc):
            pp = panels[idx // CHUNK]
            try:
                if not pp.controls.itemById(cid):
                    cd = _ui.commandDefinitions.itemById(cid)
                    if cd:
                        pp.controls.addCommand(cd)
            except Exception:
                pass
    except Exception:
        pass


# NOTE: edges in Render now come from a binary patch to NsScene10.dylib
# (VisualStyleAttribute::DrawVisibleEdge forced true @0x5644a), which makes the
# NATIVE Render display draw edges without forcing the visual style. Forcing the
# visual style is what flipped Render into its broken, overexposed renderer, so we
# must NOT do it here — faces stay correctly exposed and edges still show.


def _close_gallery():
    try:
        p = _ui.palettes.itemById(GALLERY)
        if p and p.isVisible:
            # park it far off-screen first so any residual flash is unseen (Fusion
            # remembers the floating position); if Fusion clamps the value it lands
            # in the bottom-right corner, which is acceptable. Then close it.
            try:
                p.setPosition(8000, 8000)
            except Exception:
                pass
            p.isVisible = False
    except Exception:
        pass


def _setup_render():
    if not _ui.workspaces.itemById(RENDER).toolbarTabs.itemById(CMD_TAB):
        _populate_render()
    _close_gallery()


# ── event plumbing ─────────────────────────────────────────────────────────────

class WSActivatedHandler(adsk.core.WorkspaceEventHandler):
    def notify(self, args):
        global _gallery_burst_until
        try:
            if args.workspace.id == RENDER:
                _gallery_burst_until = time.monotonic() + 3.0  # catch the gallery fast
                _setup_render()
        except Exception:
            pass


class SwitchHandler(adsk.core.CustomEventHandler):
    """Deferred from the command execute so the workspace switch runs cleanly
    outside the command context."""
    def notify(self, args):
        try:
            target = DESIGN if _ui.activeWorkspace.id == RENDER else RENDER
            ws = _ui.workspaces.itemById(target)
            if ws:
                ws.activate()
            # re-hide chrome that the workspace switch popped back up
            try:
                _app.fireCustomEvent(MAXIMIZE_EVENT)
            except Exception:
                pass
        except Exception:
            pass


def _startup_switch_to_render():
    """Retry (from the tick) until the startup Design→Render switch takes.
    activeWorkspace throws until the session exists, and activate() can be a
    silent no-op while the first document is still loading."""
    global _startup_render_pending
    if time.monotonic() > _startup_render_deadline:
        _startup_render_pending = False
        return
    try:
        aw = _ui.activeWorkspace
    except Exception:
        return  # no session yet — retry next tick
    if not aw:
        return
    if aw.id == RENDER:
        _startup_render_pending = False
        return
    if aw.id != DESIGN:
        return  # some other environment is up; don't fight it, retry until deadline
    ws = _ui.workspaces.itemById(RENDER)
    if not ws:
        return
    ws.activate()
    if _ui.activeWorkspace.id == RENDER:
        _startup_render_pending = False
        try:
            _app.fireCustomEvent(MAXIMIZE_EVENT)
        except Exception:
            pass


class TickHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            if _startup_render_pending:
                _startup_switch_to_render()
            if _ui.activeWorkspace.id == RENDER:
                _close_gallery()
        except Exception:
            pass


def _worker():
    while _running:
        try:
            _app.fireCustomEvent(EV_TICK)
        except Exception:
            pass
        # poll fast right after entering Render so the gallery is killed almost
        # immediately; a bit faster while the startup switch is pending; idle at
        # 1 s otherwise
        if time.monotonic() < _gallery_burst_until:
            delay = 0.05
        elif _startup_render_pending:
            delay = 0.25
        else:
            delay = 1.0
        time.sleep(delay)


class ToggleExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            _app.fireCustomEvent(EV_SWITCH)
        except Exception:
            if _ui:
                _ui.messageBox(traceback.format_exc())


class ToggleCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        h = ToggleExecute()
        args.command.execute.add(h)
        _handlers.append(h)


# ── lifecycle ──────────────────────────────────────────────────────────────────

def _register_event(eid, handler):
    try:
        ev = _app.registerCustomEvent(eid)
    except Exception:
        _app.unregisterCustomEvent(eid)
        ev = _app.registerCustomEvent(eid)
    ev.add(handler)
    _handlers.append(handler)


def run(context):
    global _app, _ui, _running, _startup_render_pending, _startup_render_deadline
    _app = adsk.core.Application.get()
    _ui = _app.userInterface
    try:
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = _ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)
        on_created = ToggleCreated()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)
        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)

        _register_event(EV_SWITCH, SwitchHandler())
        _register_event(EV_TICK, TickHandler())

        wsa = WSActivatedHandler()
        _ui.workspaceActivated.add(wsa)
        _handlers.append(wsa)

        # mirror Design commands now (survives the no-persist restart)
        _populate_render()
        # activeWorkspace throws (InternalValidationError: pCurrentSession) when
        # run() fires before a session exists at cold startup; we're never in
        # Render that early anyway, and workspaceActivated will handle it later.
        try:
            in_render = _ui.activeWorkspace.id == RENDER
        except Exception:
            in_render = False
        if in_render:
            _setup_render()

        # Cold startup only (not add-in reloads): Fusion lands in Design, but we
        # want Render as the default no-timeline environment. The tick loop does
        # the actual switch once the session/first document exists.
        try:
            ctx = context if isinstance(context, dict) else json.loads(context)
            is_startup = bool(ctx.get('IsApplicationStartup'))
        except Exception:
            is_startup = False
        if is_startup:
            _startup_render_pending = True
            _startup_render_deadline = time.monotonic() + 120.0

        _running = True
        threading.Thread(target=_worker, daemon=True).start()
    except Exception:
        if _ui:
            _ui.messageBox('Workspace Toggle start failed:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _running
    try:
        _running = False
        _handlers.clear()
        for eid in (EV_SWITCH, EV_TICK):
            try:
                _app.unregisterCustomEvent(eid)
            except Exception:
                pass
        # leave the command definition so its shortcut binding survives a reload
    except Exception:
        if _ui:
            _ui.messageBox('Workspace Toggle stop failed:\n{}'.format(traceback.format_exc()))
