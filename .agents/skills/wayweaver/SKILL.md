---
name: wayweaver
description: Control local or remote desktops, browsers, shells, Windows RDP hosts, and Android devices through Wayweaver MCP or its CLI.
---

# Wayweaver

Use Wayweaver when a task needs browser, desktop GUI, shell, VNC, RDP, X11, or Android interaction. It routes each capability to the best configured backend and can execute a full action sequence locally without an agent round-trip between steps.

## Install and configure

```bash
uv sync
cp targets.example.toml targets.toml
```

Edit `targets.toml`; do not commit credentials. Values support `${NAME}` and `${NAME:-default}` environment expansion. Set the config path explicitly when it is not `~/.config/wayweaver/targets.toml`:

```bash
export WAYWEAVER_CONFIG=/absolute/path/to/targets.toml
```

The reference Docker VM already contains the Linux runtime. For other targets, inspect and explicitly install the package-owned, versioned runtime through a named shell transport:

```bash
uv run wayweaver runtime doctor TARGET --platform linux --transport ssh
uv run wayweaver runtime inspect TARGET --platform linux
uv run wayweaver runtime install TARGET --platform linux --transport ssh
```

Probe targets before acting:

```bash
uv run wayweaver targets
```

Each target may combine adapters. Routing is operation- and capability-based:

- CDP: browser navigation, tabs, DOM text, CSS-targeted interaction, page capture, and page input.
- Local: execute shell operations and back native desktop adapters directly on the controller host.
- SSH: execute shell operations and transport desktop commands to remote targets.
- AT-SPI: Linux accessible elements, roles, state, actions, focus, text, and values.
- UIA: native Windows accessibility from the signed-in interactive session.
- X11 through any shell-capable transport: application catalog, window/workspace/clipboard semantics, desktop capture, and input.
- Wayland through any shell-capable transport: Sway IPC, KDE/KWin through `kdotool`, or the bundled GNOME Shell D-Bus bridge; capture and input remain capability-gated by live tools.
- VNC: protocol-native desktop screenshots and input fallback.
- RDP: Windows framebuffer and input through optional `aardwolf`.
- ADB: Android UIAutomator elements plus screenshot, input, shell, and optional `scrcpy`.

Run `wayweaver operations TARGET` before acting. It lists only currently routable canonical operations and the selected adapter. Add `--raw` only when semantic and visual operations are insufficient; it reveals raw operations for installed, available tools.

Keep capture and input on one coherent surface. A VNC screenshot must be acted on through VNC; X11 screenshots must use X11 input; an RDP framebuffer must use the same RDP session. Cross-backend fallback is valid only when coordinate spaces are known to match.

## MCP

Run the stdio server:

```bash
uv run wayweaver-mcp
```

Example client registration:

```json
{
  "mcpServers": {
    "wayweaver": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/repository", "run", "wayweaver-mcp"],
      "env": {
        "WAYWEAVER_CONFIG": "/absolute/path/to/repository/targets.toml"
      }
    }
  }
}
```

The MCP surface is deliberately small:

- `wayweaver_targets`: probe adapters, capabilities, and availability.
- `wayweaver_operations`: discover operations routable on a target; raw hatches are hidden by default.
- `wayweaver_do`: perform one canonical operation.
- `wayweaver_run`: execute an ordered canonical operation sequence locally.
- `wayweaver_capture`: return an MCP image, optionally constrained by native region or window.
- `wayweaver_observe`: return screen dimensions, screenshot path, window metadata, and optional OCR text plus structured word boxes and click points.
- `wayweaver_raw`: execute one adapter-specific operation previously returned by `wayweaver_operations(include_raw=true)`.

Text you need exactly comes from `clipboard.copy`, not from OCR; see "Reading text out of an application" below.

## CLI

```bash
uv run wayweaver targets
uv run wayweaver operations desktop
uv run wayweaver do desktop application.list
uv run wayweaver do desktop window.list
uv run wayweaver do desktop element.find '{"selector":{"name":"Save","role":"button"}}'
uv run wayweaver do desktop browser.navigate '{"url":"https://example.com"}'
uv run wayweaver capture desktop /tmp/current.png --params '{"window":"Terminal"}'
uv run wayweaver raw desktop x11 xprop '{"args":["-root"]}'
```

Use the hierarchy in this order: browser semantics through CDP; shell through SSH; accessible controls through AT-SPI; desktop applications/windows/workspaces/clipboard through X11 or Wayland; screenshot/OCR locators; coordinate input; an explicitly discovered raw operation.

## Batched operations

Use `wayweaver_run` or the CLI `run` command when the next operations are known. Canonical names are backend-neutral; the controller resolves every step against live adapter availability:

```bash
uv run wayweaver run desktop '{
  "steps": [
    {"application.open": "Xfce Terminal"},
    {"window.wait": {"selector": {"title": "Terminal"}, "timeout_ms": 3000}},
    {"window.assert": {"selector": {"title": "Terminal"}, "expect": {"active": true}}},
    {"keyboard.type": "whoami"},
    {"keyboard.press": "ENTER"},
    {"screen.observe": {"ocr": true}}
  ],
  "observe_after": true
}'
```

Operation families:

- `application.list`, `application.open`
- `window.list`, `window.wait`, `window.assert`, `window.focus`, `window.close`, `window.move`, `window.resize`, `window.minimize`, `window.maximize`, `window.fullscreen`, `window.restore`
- `workspace.list`, `workspace.switch`
- `clipboard.read`, `clipboard.write`
- `element.list`, `element.find`, `element.assert`, `element.wait`, `element.activate`, `element.focus`, `element.read`, `element.set_value`
- `browser.navigate`, `tab.list`, `browser.read`, `browser.click`, `browser.type`
- `pointer.move`, `pointer.click`, `pointer.drag`, `pointer.scroll`
- `keyboard.type`, `keyboard.press`, `keyboard.chord`
- `shell.execute`, `viewer.open`, `screen.observe`, `time.sleep`
- `recording.start`, `recording.status`, `recording.stop`, `recording.cancel`, `recording.capture`


The desktop is shared with a human. Semantic operations re-resolve applications, windows, and accessible elements at execution time. X11 reads the live pointer before every trajectory; CDP, VNC, RDP, and Wayland re-anchor with an absolute pointer event before moving. Before a destructive segment, use `element.assert` or `element.wait`; before coordinate input, use `window.assert`, `element.find`, or `screen.observe`. `screen.observe` returns signed `surface_id` and `observation_id` tokens that work across CLI processes, expire, reject older observations, and bind to the current graphical session when the adapter can identify it. UIA waits subscribe to native Windows accessibility events. AT-SPI and Android UIAutomator use bounded fresh-tree polling because remote providers and snapshot-only hierarchies do not offer a reliable cross-process event stream.

The runner stops at the first failure. It returns `failed_step`, the failed step, typed error, completed results, and a fresh observation by default. Correct the remaining sequence from that observation; do not replay completed non-idempotent steps.

A step may be made conditional with `when`, which compares a saved result against a value and skips the step when they differ. The reference is the same dotted path `$ref` uses, so a step can act on what an earlier one observed:

```json
[
  {"id": "state", "operation": "element.find", "params": {"selector": {"name": "Save"}}, "save_as": "save"},
  {"operation": "element.activate", "params": {"selector": {"name": "Save"}},
   "when": {"ref": "save.data.states.0", "equals": "enabled"}}
]
```

A skipped step is reported with `"skipped": true` rather than silently omitted.

A step whose failure is itself an answer takes `optional: true`. Dismissing a dialog that may not be there is the ordinary way a task begins, and without this the whole sequence ends on the first absent dialog. An optional step that fails is recorded with `"ok": false` and the sequence continues, so a later step can branch on whether it succeeded:

```json
[
  {"operation": "element.activate", "params": {"selector": {"name": "Close"}},
   "optional": true, "save_as": "dismissed"},
  {"operation": "window.focus", "params": {"selector": {"title": "Editor"}},
   "when": {"ref": "dismissed.ok", "equals": true}}
]
```

**Batch as much as you know.** The runner is the single largest thing you control about how long a task takes, and not because it executes faster. Measured on the same login flow, six separate calls and one sequence took the same wall clock -- 570ms against 545ms -- but the sequence is one round trip instead of six. Published measurements of computer-use agents put planning at 53-75% of total wall clock and screenshots plus action execution together at 1-3%, so the cost of a step is dominated by deciding to take it, not by taking it. Five round trips saved is five planning calls saved; the 25ms is noise. Hand over every action you can already predict, and come back only where the next move genuinely depends on what you see.

Explicit steps may add bounded workflow controls: `id`, `retry` (`max_attempts` up to 10, `backoff_ms`, and optional `on_codes`), `repeat` (up to 100), `save_as`, and `when`. Retries run only for errors whose contract says `retryable: true`; `on_codes` narrows that set. A parameter object containing only `{"$ref":"saved.path"}` resolves a prior saved response before validation. Internal saved values remain complete for references, while the returned `saved` object is bounded by `saved_output_limit` (32 KiB by default).

```json
{"steps":[
  {"id":"observe","operation":"screen.observe","params":{},"save_as":"observation"},
  {"id":"move","operation":"pointer.move","params":{
    "point":{"x":640,"y":450},
    "space":"surface",
    "surface_id":{"$ref":"observation.data.surface.id"},
    "observation_id":{"$ref":"observation.data.observation_id"}
  },"retry":{"max_attempts":2,"backoff_ms":250}}
]}
```

OCR fallback selectors accept `text`, `contains`, `fuzzy`, `similarity`, `nth`, and `region`; use `timeout_ms` and `interval_ms` for bounded retries. A region is `{"x": X, "y": Y, "width": W, "height": H}` in the selected surface coordinates. Pass a `region` whenever you know roughly where the control is: recognizing a crop costs roughly a tenth of recognizing the whole screen, and a regional locator stays regional. Set `full_screen_fallback` to `true` to also search the rest of the screen when the region misses; surfaces that cannot crop at all, such as VNC and RDP, widen on their own. `screen.observe` with `{"ocr": true}` returns both plain `ocr` text and `ocr_items`; each item contains the recognized word, confidence, bounding box, and center point in the signed observation's coordinate space. `element.find` returns the merged match box and point, while `element.activate` clicks that point and returns `"clicked": true`. Prefer native `element.*` operations whenever the control is accessible.

```json
{"element.activate":{"selector":{"name":"Terminal Emulator","role":"menu item","exact":true}}}
```

For transitions that must be confirmed, add opt-in postconditions. `surface_changed` compares captures from the same adapter, while `text_disappeared` retries OCR until the label is absent or the bounded timeout expires:

```json
{"element.activate":{
  "selector":{"text":"Finish","exact":true},
  "verify":{
    "surface_changed":true,
    "text_disappeared":"New Project",
    "timeout_ms":3000,
    "interval_ms":250
  }
}}
```

## Reading text out of an application

OCR is a locator, not a transcription tool. To get exact text out of a widget — a table, a decompiler pane, a log view, a long field truncated on screen — select it and use `clipboard.copy`, which returns the text verbatim:

```json
{"clipboard.copy": {"select_all": true}}
```

It poisons the clipboard with a sentinel before issuing the copy, so a chord that never reached the focused widget raises instead of silently handing back whatever the clipboard already held. Pass `restore: true` to leave the user's clipboard as it was. Click the widget first: the copy applies to whatever holds focus, and in table-style widgets `select_all` grabs every row as TSV, which is usually what you want.

Reach for OCR only when no selectable text exists. Recognizing dense monospace columns — hex dumps, byte-per-line listings — is unreliable and will silently drop and invent characters.

## Driving menus and dialogs

Prefer keyboard paths over clicking menu items. Accelerators and mnemonics land on the right target the first time, while a submenu entry is a coordinate guess that fails silently when the menu shifts. Where an application documents a shortcut for an action, use `keyboard.press` or `keyboard.chord`; use menu clicks only when there is no shortcut.

## Confirming that typing arrived

Keystrokes go wherever focus happens to be, so a click that missed its target types into nothing and every layer still reports the keystrokes as delivered. OCR makes this easy to hit: it matches the *word* you asked for, which may be body prose rather than the control that word labels. Pass `verify: true` to `keyboard.type` on any target with an element layer, and the result names what received the text:

```json
{"keyboard.type": {"text": "hunter2", "verify": true}}
```

`element.focused` answers the same question on its own — use it to check what holds focus before committing to a destructive keystroke.

## Composing information and input layers

Information and input do not have to come from the same adapter. VNC and RDP carry pixels, pointer and keyboard but no element tree; AT-SPI and UIA carry the tree but cannot drive real input. `element.point` resolves a selector through the richest layer available and returns a point any pointer adapter can act on, naming the layer that answered:

```json
{"element.point": {"selector": {"name": "Save", "role": "push button"}, "allow_fallback": false}}
```

The result carries `tier` (`semantic` or `visual`), `source` (the adapter that resolved it), and the `surface` the point was validated against. Feed `point` straight into `pointer.click`. Because layers can disagree about the desktop they describe, a point that falls outside the acting surface is refused rather than clicked.

Give `element.point` a `timeout_ms` when the element may not exist yet — right after a navigation, the form you are resolving legitimately is not there for a moment — and a `scroll` budget when it may sit below the fold; resolution scrolls toward the element and waits for its reported position to stop moving before answering:

```json
{"element.point": {"selector": {"name": "Username", "role": "entry", "scroll": 6}, "timeout_ms": 15000, "allow_fallback": false}}
```

A resolved point is still a prediction: the page can reflow between resolving and the click landing, and a click that misses reports success while focusing nothing. For a click that the next steps depend on, verify where focus actually went and re-resolve on a miss:

```json
[
  {"operation": "element.point", "params": {"selector": {"name": "Username", "role": "entry", "scroll": 6}}, "save_as": "field"},
  {"operation": "pointer.click", "params": {"point": {"$ref": "field.data.point"}, "space": "screen"}},
  {"operation": "element.focused", "params": {"application": "Chrome"}}
]
```

Scope `element.focused` to the application when you can — an unscoped focus query walks every application on the desktop, which costs seconds when a large toolkit is registered against ~30ms scoped.

Semantic operations fall back to the visual path on their own, which is roughly ten times slower and far easier to mislead. The switch is only reported afterwards in `backend.fallback`, and the OCR failure then replaces the real diagnostic — `text not found` where the truth was `accessible element not found`. Pass `allow_fallback: false` on any `element.*` call that should be semantic or nothing:

```json
{"element.find": {"selector": {"name": "Username", "role": "entry"}, "allow_fallback": false}}
```

Wait for a dialog to go away with `expect: {"absent": true}` rather than assuming a confirmed action committed:

```json
{"window.wait": {"selector": {"title": "Create New Folder"}, "expect": {"absent": true}, "timeout_ms": 5000}}
```

A window existing is not the same as a window being ready. `window.wait` returns as soon as the window is mapped, which on a GTK dialog is roughly 250ms before its entry accepts keystrokes — type immediately and the characters vanish with no error anywhere. Follow `window.wait` with an `element.wait` for the widget you are about to drive, and after confirming a dialog, wait for it to disappear before the next step rather than assuming it committed.

Never pad a sequence with `time.sleep` to let something load. Locators already poll: give them `timeout_ms` and let them return the moment the target exists, and wait for an outcome by locating the text that proves it rather than sleeping for a guessed duration. On a measured registration flow, fixed sleeps were 71% of the wall clock, and replacing them with these waits took the same task from 16.8s to 3.7s. Reach for `time.sleep` only for a deliberate pause with no observable signal.

OCR only sees the viewport, so a locator finds nothing that sits below the fold. Give text locators a `scroll` budget to page down until the target appears; the match reports how many scrolls it took:

```json
{"element.activate": {"selector": {"text": "Username", "scroll": 8, "window": "active"}}}
```

Text locators match anywhere in the captured surface, including the address bar, window titles, page headings, and adverts that happen to share the word. Narrow with `window`, `region`, or `nth`, and set `strict` to refuse an ambiguous match rather than clicking whichever the recognizer ordered first.

Text locators search the active window by default. Screen-wide recognition recovers only 36-91% of the text on a busy desktop, and it matches taskbar entries and other applications' titles as readily as the control you meant; a dialog's buttons that a full-screen pass cannot see at all read at 94% confidence inside the dialog's own rectangle. If the active window does not hold the target the search widens automatically, so nothing becomes unreachable. Pass `window: "screen"` to search the whole surface from the start.

Semantic lookups run through a persistent helper on the target. Accessibility clients initialize by marshalling every application's tree, so a helper started per call pays that repeatedly — seconds when a large toolkit is running. The session pays it once and is rebuilt automatically if it dies; set `session = false` on the adapter to force one-shot calls.

Role names are toolkit vocabulary rather than a shared one: the same widget is `push button` to GTK and Chrome but `button` to the Java accessibility bridge. Selectors compare roles normalized and treat known synonyms as one role, so either spelling resolves the same widget; unrelated roles still do not match.

Name an application exactly, including case. Several processes can share a name fragment — a file manager and its daemon both answer to `thunar` — and an ambiguous name is refused with the candidates listed rather than resolved to whichever registered first. An exact name also returns immediately instead of interrogating every other application, which matters when one of them is a slow toolkit.

Scope semantic lookups the same way with `application`. Walking every application is both slow and ambiguous: a bare `File` menu resolved to the file manager in 301ms while the same selector scoped to `application: "Ghidra"` returned Ghidra's own menu.

OCR does not see isolated one- and two-character labels at all. A tree entry named `ls`, drag handles labelled `A` and `B`, and icon-only controls such as `×` or `+` return zero matches rather than a wrong match, and no page-segmentation mode recovers them. Target these through `element.*` semantics, or by coordinates read from a region capture; do not expect a text locator to find them.

Pointer actions default to a human-like eased path, which costs roughly a second per click. Pass `duration_ms: 0` when realism does not matter: a click drops from about 1300ms to 175ms, and the endpoint is identical.

Small controls need exact coordinates. Panel splitters, resize handles, and scrollbars are often only a few pixels wide, so a target read off a screenshot by eye can miss by two pixels and do nothing at all. When a `pointer.drag` or click appears to have no effect, re-measure the target from a fresh region capture before assuming the operation is broken.

Confirm state changes rather than assuming them. A click that misses reports success, so guard consequential steps with `element.assert`, `element.wait`, a fresh `screen.observe`, or an `element.activate` `verify` block.

X11 targets with `wayweaver-x11-record` expose cross-process recordings for clicks, drags, vertical scrolling, keyboard input, and accessible-element activation. Use the lifecycle API when a human needs time to interact:

```bash
uv run wayweaver do desktop recording.start '{}'
uv run wayweaver do desktop recording.status '{"recording_id":"<id>"}'
uv run wayweaver do desktop recording.stop \
  '{"recording_id":"<id>","infer_elements":true}'
```

Use `recording.cancel` to discard a recording. `recording.capture` remains the bounded one-call form:

```bash
uv run wayweaver do desktop recording.capture \
  '{"duration_ms":5000,"infer_elements":true}'
```

`shell.execute` always returns `exit_code` and `success`. Set `check:true` to convert a disallowed exit code into `ACTION_FAILED`; customize success with `allowed_exit_codes`.

Raw access is explicit and discoverable:

```bash
uv run wayweaver operations desktop --raw
uv run wayweaver raw desktop x11 wmctrl '{"args":["-l"]}'
```

Do not guess a raw adapter command or use raw access merely because it is familiar.

## Platform-native semantics

The semantic stack is layered rather than pretending every desktop exposes one API:

1. CDP for browser DOM and browser input.
2. AT-SPI on Linux, UIA on Windows, and UIAutomator on Android for controls and state.
3. X11 EWMH or a compositor-specific Wayland API for windows and workspaces.
4. VNC/RDP framebuffer input, then screenshot/OCR, only when native metadata is unavailable.

Use `element.assert` for an immediate precondition and `element.wait` for appearance or a state transition. Put identity fields under `selector`, state requirements under `expect`, and all public durations in integer milliseconds:

```json
{"element.wait":{"selector":{"name":"Save","role":"button"},"expect":{"state":"enabled","value":true},"timeout_ms":5000}}
```

### GNOME Wayland

Install the packaged extension through the graphical user's local or SSH transport, then enable it and log out and back in before probing:

```bash
uv run wayweaver runtime install gnome --platform gnome --transport local
gnome-extensions enable wayweaver@wayweaver.local
```

Configure a Wayland adapter with `compositor = "gnome"`. The extension exports window/workspace metadata and actions only on the user's session D-Bus; it opens no network listener. `gnome-screenshot` supplies capture when installed, while `wtype`/`ydotool` and `wl-clipboard` independently gate input and clipboard capabilities.

```toml
[targets.gnome]
prefer = ["wayland", "atspi", "local"]

[targets.gnome.local]
kind = "local"

[targets.gnome.wayland]
transport = "local"
display = "wayland-0"
compositor = "gnome"
```

### KDE Wayland

Install `kdotool` in the graphical session and set `compositor = "kde"`. Wayweaver uses KWin's D-Bus scripting interface through `kdotool` for window identity, geometry, state, actions, and virtual desktops. Install Spectacle for capture; input and clipboard remain separately capability-gated.

```toml
[targets.kde.wayland]
transport = "local"
display = "wayland-0"
compositor = "kde"
```

### Windows UI Automation

Run Wayweaver as the signed-in desktop user, install the packaged UIA runtime with `wayweaver runtime install windows --platform windows --transport local`, and enable the `local` plus `uia` example in `targets.example.toml`. UIA exposes roles, names, automation IDs, state, bounds, native invoke/toggle/select patterns, focus, text, and writable values. Windows service/OpenSSH sessions are isolated from the signed-in desktop; use RDP only as the framebuffer/input fallback for a remote machine unless an agent runs inside that interactive session.

### Android UIAutomator

The ADB adapter probes `uiautomator` and advertises `element.*` only when the device provides it. Match by `name`, `text`, `resource_id`/`id`, `class`, `role`, or `package`; actions re-dump the hierarchy immediately before tapping or setting text. UIAutomator has no persistent cross-device event stream, so waits refresh bounded XML snapshots.

## Native Linux host

Use the `local` transport to control the Linux desktop on which Wayweaver itself runs. Run Wayweaver from the repository root, install `maim`, `xdotool`, `wmctrl`, `xclip`, `x11-utils`, and `python3-pyatspi`, then enable the host example in `targets.example.toml`:

```toml
[targets.host]
prefer = ["x11", "atspi", "local"]

[targets.host.local]
kind = "local"
cwd = "."

[targets.host.x11]
transport = "local"
display = "${DISPLAY:-:0}"

[targets.host.atspi]
transport = "local"
display = "${DISPLAY:-:0}"
```

The controller process must run as the graphical user and have access to that session's `DISPLAY`, Xauthority, and D-Bus. Install its runtime first with `wayweaver runtime install host --platform linux --transport local`. If the graphical environment is unavailable, the local target still exposes `shell.execute`, while desktop operations return an explicit availability error. For a remote machine, replace the local table with an SSH table and set `transport = "ssh"` on X11, Wayland, and AT-SPI.

Direct host control is equivalent to controlling an unlocked desktop. Prefer a dedicated user/session when isolation matters; VNC remains the pixel fallback rather than the primary native API.


## Bundled Ubuntu desktop

Start the repository VM with Compose:

```bash
docker compose up -d --build
docker compose ps
```

The web desktop is:

```text
http://${BIND_ADDR:-127.0.0.1}:${DESKTOP_PORT:-6080}/vnc.html?autoconnect=1&resize=remote
```

SSH uses `${SSH_PORT:-2222}`, native VNC uses `${VNC_PORT:-5901}`, and CDP uses `${CDP_PORT:-9222}`. Interactive SSH logins set `DISPLAY=:1`, so graphical programs launched with SSH appear in the shared desktop.

`/home/vm` is the swappable desktop identity. `VM_HOME` defaults to `./homes/default`; the separate `ssh-host-keys` volume preserves the VM's SSH host identity across container recreation. Change `VM_HOME` and recreate the service to switch desktop identities:

```bash
docker compose up -d --force-recreate
```

Keep `BIND_ADDR=127.0.0.1` unless remote access is intentional and protected. SSH/VNC credentials come from `.env`; never print or commit that file. CDP grants full browser control.

The image installs the same package-owned Linux runtime assets used for non-Docker targets. Docker owns only the OS dependencies, desktop session, browser, and SSH/VNC/CDP access topology.
