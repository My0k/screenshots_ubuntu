<div align="center">

# 📸 Screenshot Shelf

**Your latest screenshots, always at hand and ready to drag.**

GNOME already saves everything to `~/Pictures/Screenshots`, but attaching one means
opening the file manager and hunting for it. This removes that step.

<img src="docs/bandeja.png" alt="The shelf showing the six most recent screenshots as thumbnails" width="820">

<sub>🇪🇸 <a href="README.es.md">Léeme en español</a> · the app's UI is in Spanish</sub>

</div>

---

## Why I built this

I work with [Claude Code](https://claude.com/claude-code) in [Ghostty](https://ghostty.org),
and I kept hitting the same friction: I'd screenshot a piece of frontend I wanted to
show it — a broken layout, a component that looked off — and then spend more time
attaching the image than describing the problem. Open Files, find the folder, sort by
date, drag it across.

So I made a shelf that keeps the last few screenshots one drag away. Take the shot,
drag the thumbnail straight into the terminal, keep typing. It works with anything that
accepts a dropped file, but that's the loop it was built for.

## How it works

```
Print Screen  →  the shelf pops to the front  →  drag the thumbnail  →  drop it anywhere
```

1. **Take a screenshot as usual**: `Print Screen`, or the *Capture* button.
2. **The shelf comes forward by itself**, with the new screenshot already selected.
3. **Drag the thumbnail** into a terminal, browser, Slack, Telegram, GIMP, an email…

Selection is one at a time, so you always drag exactly the screenshot you just took.

<div align="center">
<img src="docs/vacio.png" alt="Empty state: no screenshots yet" width="700">
</div>

## Install

```bash
./install.sh              # launcher + Super+Shift+S shortcut
./install.sh --autostart  # also starts with your session and watches in the background
./install.sh --uninstall  # reverts everything
```

> **Requirements** · Ubuntu with GNOME (tested on 25.10 / GNOME 49, Wayland) and
> `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`, which ship preinstalled.
>
> Runs on the system Python (`/usr/bin/python3`). If you use pyenv it doesn't matter:
> the launcher already points at the right interpreter.

## Shortcuts

| Shortcut | Action |
|:--|:--|
| `Super+Shift+S` | Show/hide the shelf (global) |
| **Drag** | Drop the screenshot into another app |
| `Ctrl+N` | Take a screenshot |
| `Ctrl+C` | Copy the image to the clipboard |
| `Ctrl+Shift+C` | Copy the file path |
| `Enter` | Open in the image viewer |
| `F2` | Rename |
| `Delete` | Move to trash |
| `F5` | Refresh |
| `Esc` | Hide the shelf |

Right-clicking a thumbnail opens a menu with every action.

## Configuration

Through *Preferences* (`Ctrl+,`) or by editing `~/.config/screenshot-shelf/config.json`.

<div align="center">
<img src="docs/preferencias.png" alt="Preferences dialog: visible screenshots, thumbnail size and behaviour" width="820">
</div>

| Key | Default | What it does |
|:--|:--|:--|
| `max_items` | `6` | How many recent screenshots are shown |
| `thumb_size` | `200` | Thumbnail width in px |
| `auto_show` | `true` | Bring the shelf forward when a new screenshot appears |
| `hide_on_drop` | `false` | Hide the window after dragging something out |
| `close_hides` | `true` | Closing the window hides it and keeps watching |
| `watch_dirs` | `[]` | Fixed paths to watch (empty = autodetect) |

<details>
<summary><b>Why <code>auto_show</code> needs a trick on Wayland</b></summary>

<br>

Wayland downgrades focus requests to a "Window is ready" notification. To get around
that, the window is **remapped**: GNOME treats it as freshly opened and actually gives
it focus. This needs `focus-new-windows` set to `smart`, which is the default.

</details>

## What it does under the hood

- **Watches the folders with `GFileMonitor`**: anything that lands there shows up
  instantly, whether it came from Print Screen, Flameshot or an `scp`.
- **Autodetects the folders**: `~/Pictures/Screenshots`, its Spanish equivalent, and
  whatever you have configured in `org.gnome.gnome-screenshot`.
- **On drag** it offers `text/uri-list` + `GdkFileList` + the path as plain text, which
  is what browsers, file managers and GTK/Qt/Electron apps accept.
- **On copy** it also offers the full PNG as an image, so you can paste straight into
  editors or chats.
- **On delete** it moves to the trash (`GIO trash`), never deletes permanently.

<details>
<summary><b>The «Capture» button stopped working</b></summary>

<br>

On Wayland no app can grab the screen on its own: it has to go through the portal
(`org.freedesktop.portal.Screenshot`), which shows GNOME's capture UI and asks for
permission the first time. If you ever answered *No*, GNOME remembers. To make it ask
again:

```bash
gdbus call --session --dest org.freedesktop.impl.portal.PermissionStore \
  --object-path /org/freedesktop/impl/portal/PermissionStore \
  --method org.freedesktop.impl.portal.PermissionStore.DeletePermission \
  screenshot screenshot ""
```

The **Print Screen** key always works (GNOME Shell handles it) and the shelf picks up
the result either way.

</details>

<details>
<summary><b>Log noise</b></summary>

<br>

GTK 4.20 emits `gtk_adjustment_get_value: assertion failed` twice per frame while you
interact with the window. It's benign and doesn't come from this app (it uses no
adjustments), but it floods the journal, so that one message is filtered out. To see
everything unfiltered:

```bash
SHELF_VERBOSE=1 screenshot-shelf
```

</details>
