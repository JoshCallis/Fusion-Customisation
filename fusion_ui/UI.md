# UI.md — how a page here looks and behaves

The rulebook for every page of the Control Room (`remap/`), and for any other window
that draws a list. The Control Room grew one request at a time, so each feature
used to land only on the page it was asked for, and matching the others meant doing
the same work again. That is what this file and the kit exist to stop.

**Where a new feature goes, in this order:**

1. **The kit** (`fusion_ui/list.js` + `list.css`). This covers anything a list does,
   and any look a row, heading, editor, button or menu has. Put it here and every
   page with that shape gets it at once.
2. **A spec option** on the kit, for something only some lists want (`nest`, `drag`,
   `markUnfiled`…). The page turns it on; the behaviour still lives in the kit.
3. **Tokens** (`theme.css`, or the window's own redefinitions of them) for a size,
   colour or spacing.
4. **The page's own code** comes last, and holds only what the page is *about*: its
   data, what its row shows, what its editor asks, what its verbs post.

Something unique to one page goes in that page. The moment a second page wants it,
it moves into the kit.

## The pieces

| file | what it is |
|---|---|
| `fusion_ui/theme.css` | Tokens: palette, the type scale (`--fs-*`, `--font`, `--font-mono`), spacing (`--gutter`, `--nest`), control heights (`--ctl-h`, `--field-h`, `--btn-h`), radii. Shared by every window. |
| `fusion_ui/list.css` | The kit's look: rows, group headings, cursor and pick, editor and button row, buttons, menus, cards, empty states. Tokens only. |
| `fusion_ui/list.js` | `ListKit`: the kit's behaviour. The API is documented in its header comment. |
| `remap/index.css` | The Control Room's own look: the light palette (it redefines tokens rather than restyling), window chrome, page header, fields, chips, and a short "one page's own" section. |
| `remap/index.js` | Pages. Each renderer calls `ListKit.draw(list, ListKit.state(pageId(page)), spec)`. |

Both files are served by `webwin.SHARED_ASSETS`. The Control Room's `server.py`
routes them from that table too, so a new shared file needs only a tag in the page.

## A list

**Row anatomy.** Name on the left (`.id`). Asides next to it (`.sub`). The value on
the right (`.emits`, or the row's own control). A row carries no buttons: what can
be *done* to it lives in the right-click menu.

**Groups.**
- Click a heading anywhere to open or close it. There is no twirl arrow and no
  count.
- Double-click a group's name to rename it (or right-click → Rename).
- Collapsed state is kept by the page when it has somewhere to store it
  (`onCollapse`, e.g. Fusion's `fusion_view.json`). Otherwise the kit keeps it per
  list, in localStorage.
- A search opens everything.
- Groups can nest (`nest: true`). They arrive flattened, with a `depth`, in
  reading order.
- A group with `own: false` belongs to the app or the data (Fusion's catalog
  groups, a Keys page's key). It can't be renamed, nested inside, or given a menu.
  `rename: false` is the middle case: ours to file into and nest in, but its name
  is the data's (a shortcuts page's By Modifier headings).

**Cursor and pick.** These are two different things with two different marks.
- **The cursor** (`.cur`, the hover shade — no side bar) is the keyboard's place. The nav keys
  move it, a click puts it there, and it is always scrolled into view.
- **The pick** (`.picked`, a fill) is what ⌘-click marked (the first ⌘-click also takes
  the cursor's row, as in Finder), or ⇧-click (the run, as drawn, from the last plain or ⌘ click — it replaces the pick). It is a pointing
  gesture and is never saved.
- A verb acts on the pick, or on the cursor if nothing is picked. A menu opened
  on a picked row acts on the whole pick.
- **Move To ▸, Cut, Paste** are the kit's, on every list that takes drops into groups
  (`drag.into` + `onDrop`) — the menu's way of doing what a drag does, through the same
  `onDrop`, so a page writes it once (a group with `cut: false` — By Modifier's headings —
  offers no Cut; `groupMenu` gets the right-click event, to read where on the heading it
  landed). A menu item may carry `items: [...]` for a
  submenu (hover or click). **A flyout never shuts on its way to being used**: another
  item takes over only once the pointer settles on it, not while it crosses items heading
  for the flyout (`menu_aim.js`), and leaving the menu shuts nothing. Every menu with a
  flyout goes through `MenuAim`, never a bare mouseleave/timer. `showMenu(e, items,
  {under: el})` hangs a menu as a dropdown below `el` instead of at the pointer. Cut (⌘X) dims what it names (`.cut`, dashed — not `.off`)
  until Paste (⌘V, or the menu), ⎋, a new Cut, or leaving the page. ⌘V with no cursor
  lands in the last clicked group, or at the top level on a spec with `pasteLoose`.
- **The colours** are the kit's too, listed in the right-click menu itself (no flyout), on any list whose spec has `color(itemIds, groupIds, c)`:
  Finder's seven tag colours (theme.css `--tag-*`) or None, on what the menu carries.
  A row or group with `color` wears it on its name, cursor and pick included. The page
  stores it (the shortcut pages: `<app>_view.json` By Group, `<app>_modview.json` By
  Modifier — two separate sets).
- **Names are capitalised** (`capitalise.js`); a field whose text must stay as typed
  (a command, an id, a search) says `data-nocaps="1"`.

**A dropdown's mark is a dot on the right, and it is earned.** House rule, for every
suggestion list and menu in these windows (`.ac-row.sel` / `.ac-mark` in
`fusion_value/index.css` is the implementation; nothing on the list kit draws one yet).
A row Enter would take is marked with a small accent dot at the far end of the row —
never a bar down its left edge, which reads as a rule on the whole list rather than as a
mark on one row. The dot is drawn on *every* row and shown on one, so nothing beside it
shifts as the mark moves. And a list that has merely *opened* marks nothing: the mark
says "this is the one", which is not true until an arrow key has said so. Enter still
takes the top row before that — the default is a convenience, and has stopped looking
like a choice.

**Dragging.** Drop marks are a *box* for "into" and a *line* for "before".
- An **ordered** list (`drag.order`) drops a row before a row, or a group before a
  group. Keys and Slots ▸ Settings ▸ Layouts work this way, because file order means
  something there.
- A **filed** list (`drag.into`) files a row into a group.
  - A group dropped on the lower half of a heading goes *inside* that group.
  - A group dropped on the top half goes *before* it.
  - The floor of the list is the top level.
  - With `drag.rows` its rows have an order too: a row drops *before* a row (and
    into that row's group, if it came from another), and on the lower half of a
    group's *last* row it drops below it, at the group's end. Control Room's shortcut
    pages work this way.
  - A **placed** list (`placed`, the shortcut pages) draws a subgroup among its
    parent's rows — at the top unless moved. A group dropped on a row's top half goes
    above that row, on its lower half below it; dropped into a heading it goes to the
    top.
- **Grid lists.** An item's `style` (a CSS string) goes on its row — By Modifier uses
  `grid-column` / `grid-row` to lay several apps' cells out as the columns of one
  grid. `bodyExtra(g, rows)` appends nodes to each group body drawn (the grid's lead
  column). `blocks: true` wraps each depth-0 group (heading, bodies, subgroups) in
  `div.list-block` — loose rows get `div.list-block.loose` — so a page can flow whole
  groups into columns; the kit does not do the flowing.
- A **mixed** list (`drag.mixed`, the timeline) is both at once: rows and groups
  drop before either, a heading's top half is "before" for any drag, and the floor
  of a `sequence` list is its end. `drag.can` lets the page refuse a spot — a refused
  spot shows no mark, so what you can't do never looks possible.
- A **slot** list (`drag.onto`, By Modifier) drops ONE row *onto* a row (a box,
  `onto` in `onDrop`) — the page swaps or replaces. A placeholder row is
  `drag/edit: false`: a target only (it may be `rename: true` — By Modifier's slot
  notes: `renameValue` is the field's starting text, `emptyOk` commits an empty name
  as "clear"). A placeholder's ⏎ / F falls to the page's `enter(it, node)`, which can
  open **`ListKit.suggestField(rowNode, { load, pick, placeholder })`** — a field in
  the name's place with matches under it (every word typed is a prefix of a word in
  the name, anywhere in it, never mid-word; most typed words matched whole first, then
  names starting with the text, then first-word, then shorter; up to 50; ↑/↓
  move, ⏎ or a click picks, ⎋ cancels). Any list can use it.
- Whatever is picked moves with the thing dragged. Dropping something before
  itself does nothing.

**Renaming in place** works the same way everywhere: double-click the name, choose
right-click → Rename, or press the Rename key.
- ⏎ commits, ⎋ cancels, and clicking away commits.
- A rename is refused while an editor is open, since whatever is half-typed there
  would be lost.
- Edit mode (hold ⌥) is separate: it renames the window's own *chrome* (page
  titles, descriptions, the section menu), not list items.

**The editor** opens under its row, never as a dialog. Only one is open at a time.
The button row is built by `ListKit.actions` and always has this order:

> message · extra · **destructive** · Cancel · **primary**

The destructive button is furthest from the primary, and the primary sits where
macOS puts the default button. Nothing else draws an `.actions` row by hand.

**Adding.** There is one "+ New …" button (`.addbtn`) at the end of the list.
- `add.draft` opens a new editor.
- `add.name` opens a row that asks for a name first. Master Shortcuts uses this,
  then opens the new item's editor on its key.
- `add.options` is several *kinds* of thing on the one button: clicking it offers
  a menu, and picking a kind opens the ordinary new-item editor with that kind's
  draft. A list never grows a second "+ New …". The New key takes the first kind.
  Slots ▸ Shortcuts adds a toggle this way.

**Empty.** When nothing exists, the list says "No X yet." When a filter or search
hides everything, it says "Nothing matches." It never just leaves a blank space.

## Keys that finish things

| key | does |
|---|---|
| ⏎ | Commits what is open: a menu closes, otherwise the open editor's primary button fires (that list's editor, not the first thing on the page shaped like one). With nothing open and the cursor on a row (focus not in a field), it is **nav-in** — opens the row, the same as F. |
| ⎋ | Backs out of the innermost thing: menu → armed capture field → rename → dropdown → section menu → editor or add row → Find → pick → cursor → the page's own search box (`clearFind()`, when it has text). Only when nothing is left does the window close. Inside `Remap.app` ⎋ arrives through `window.__escape()`, never as a keydown. |
| ⇥ | Stays inside the open editor and skips its buttons (⎋ and ⏎ already do their jobs). |
| ↑ ↓ ← → | Bare arrows, never while typing in a field: the cursor steps to the row drawn next to it in that direction (`ListKit.step`) — on screen, so in a grid of blocks (By Modifier) ← / → cross columns; a plain list's ↑ / ↓ are nav-up / nav-down. An open dropdown takes ↑ / ↓. Built in, not a Control Room command — other keys are bound TO the arrows. |

**The Control Room commands** (`remap/controlroom_keys.json`, each its own key or a
link to a Master Shortcut) are handled by one dispatcher in `index.js`, which passes
them to `ListKit.command`. They work the same on every page that has a list:

| command | on a list |
|---|---|
| nav-down / nav-up | Move the cursor. Ignored while an editor is open, so an unsaved draft is never thrown away. |
| nav-in | On a closed group: open it. On an open group: step to its first child. On a row: open its editor, or (with no editor) go into its field. |
| nav-out | Close the open editor. Otherwise close the group under the cursor, or step out to the parent group and close it. |
| deselect | Clear the pick and the cursor, and close the editor. |
| rename | Rename the thing under the cursor, or the one picked thing. With several items picked, the field opens on the last-edited one and its commit renames them all. |
| new-group | A new group in the group the cursor sits in, named straight away. |
| ungroup | Dissolve the picked groups (or the one under the cursor); what was in them moves up. Only when everything selected is a group — a row picked inside a group never ungroups it. |
| toggle-enabled | Enable or disable what is picked (Keys: the switch; Fusion/Chrome: Hide — groups too, via `toggleGroups`). |
| find | Focus the page's own search box. A page without one gets the kit's Find bar, which filters by name. |

If a page leaves a verb's handler out, the kit declines that command and the key
passes through. No page contains "not on this page" code.

## Fields

- A capture field (`captureField`) records the physical key.
  - It reports a chord **once, whole**: modifiers going down only *show*.
  - A bare modifier is reported when every key comes back up with nothing else
    pressed.
  - A page may save or redraw on change without cutting a chord in half.
- A key that can be a Master Shortcut is a `masterKeyField` with the 🔗 next to it.
  **An action that exists in `general.json` is never spelled as a key anywhere
  else.**
- When a field sits on a row (Control Room ▸ Shortcuts, Finder ▸ Shortcuts), the
  row has no editor: the key *is* the row.
- No field is prose: `ListKit.noSuggest` (autocomplete / autocorrect / spellcheck /
  writing suggestions off) is on every kit field, and Control Room sets it on any
  field that takes focus.
- An inline field (rename, a slot's search) puts its row back **before** it commits,
  so a commit that redraws cannot draw the field again; one whose node was redrawn
  away is not open (`inlineOpen`). A stale one used to silence every list key.

## Picking and pending

- ⇧-click picks the run as drawn; on a list drawn as blocks side by side
  (`blocks: true`), the on-screen rectangle between the two rows.
- A write drawn ahead of its answer carries the row class `pending` (a spinner after
  the name) until the page reads it back.
- A second right-click event from the same press is ignored by `showMenu`, and an
  empty menu never shuts an open one (the chip menu that flashed away).

## Messages

- **In an editor:** on its own message line (`ListKit.say`).
- **No editor:** a page note. `ListKit.attempt(slot, fn)` shows "setting…", then
  either the error or nothing, and reloads. `noteCard(slot)` draws it.
- **Something that happened elsewhere** (a key moved from its group chip):
  `ListKit.flash`; a standing one (true until it isn't): `ListKit.pinNotes`. Both
  share one stack at the bottom left — plain text, no box, newest on top, each with
  a ✕; an error stays until closed. Don't flash what the page already shows (a
  deleted row is its own confirmation).
- **Never silence.** A save that failed and said nothing is a save you will believe
  happened.
- **Waiting dims.** A panel that has sent something and stays up for the answer (a
  Fusion commit that has to build, a load) sets `busy` on `<body>` until it comes back
  — `theme.css` dims the `.fpanel`'s contents and stops their clicks. The answer, an
  error included, lifts it. FusionValue does this for every commit (`setCommitting`).
- **Nothing reports what is fine.** There are no "running" badges and no counts.
  Only a fault is worth interrupting for.

## Every page

- A header with a name, a one-line description, and a **?** for the long help
  (`setHelp`). Every page sets help.
- An index page (`kind: "index"`) names the files that hold its setting today.
- Colours, sizes and spacing come only from tokens. A literal is a value that can't
  follow the palette. Light mode redefines tokens and restyles nothing.

## Not on the kit yet

- **Fusion Parameters and Fusion Browser** each still have their own cursor,
  expand/collapse, ⏎/⎋ and rename (`fusion_params/index.js` `moveSelection`,
  `fusion_browser/index.js` `moveCursor` / `expandOrInto`). Moving them onto the kit
  is the next step. Both already get `list.js` from their servers.
- **Slots ▸ Sequence** is a read-only table (`.tbl`). It has nothing to select,
  open or change.
