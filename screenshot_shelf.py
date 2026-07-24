#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
Screenshot Shelf — bandeja de capturas para Ubuntu / GNOME (Wayland).

Muestra las últimas N capturas de pantalla en una rejilla viva y permite
arrastrarlas (drag & drop) hacia cualquier aplicación: navegador, Slack,
Telegram, GIMP, un correo, el gestor de archivos, etc.

Requiere el python del sistema (/usr/bin/python3), que trae PyGObject.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk  # noqa: E402

APP_ID = "cl.myok.ScreenshotShelf"
APP_NAME = "Bandeja de capturas"
CONFIG_PATH = Path(GLib.get_user_config_dir()) / "screenshot-shelf" / "config.json"
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".bmp"}

DEFAULTS = {
    "watch_dirs": [],       # vacío = autodetectar
    "max_items": 6,         # cuántas capturas se muestran
    "thumb_size": 200,      # ancho del thumbnail en px
    "auto_show": True,      # traer la ventana al frente al detectar una captura nueva
    "hide_on_drop": False,  # ocultar la ventana después de arrastrar algo fuera
    "close_hides": True,    # cerrar la ventana la esconde en vez de salir
}

CSS = """
.shelf-card {
    background: var(--card-bg-color, @card_bg_color);
    border-radius: 12px;
    border: 1px solid alpha(currentColor, .10);
    transition: border-color 180ms ease, background 180ms ease;
}
.shelf-card:hover { border-color: alpha(currentColor, .28); }
flowboxchild:selected .shelf-card {
    border-color: @accent_bg_color;
    box-shadow: inset 0 0 0 1px @accent_bg_color;
}
.shelf-thumb {
    border-radius: 11px 11px 0 0;
    background: alpha(currentColor, .06);
}
.shelf-caption { font-size: .82em; padding: 5px 8px 6px 8px; }
.shelf-new { border-color: @accent_bg_color; }
flowboxchild { padding: 4px; border-radius: 14px; }
flowboxchild:selected { background: transparent; }
.shelf-overlay-bar {
    background: alpha(black, .55);
    border-radius: 9px;
    padding: 3px;
    margin: 6px;
}
.shelf-overlay-bar button { color: white; min-width: 26px; min-height: 26px; padding: 0; }
.shelf-overlay-bar button:hover { background: alpha(white, .22); }
.shelf-badge {
    background: alpha(black, .55);
    color: white;
    border-radius: 7px;
    padding: 1px 7px;
    margin: 6px;
    font-size: .78em;
}
"""


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self) -> None:
        self.data = dict(DEFAULTS)
        try:
            self.data.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass

    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save()

    def save(self) -> None:
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
        except Exception as exc:
            print(f"[shelf] no se pudo guardar la config: {exc}", file=sys.stderr)


def pictures_dir() -> Path:
    special = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
    return Path(special) if special else Path.home() / "Pictures"


def autodetect_dirs() -> list[Path]:
    """Carpetas donde GNOME suele dejar las capturas, en cualquier idioma."""
    pics = pictures_dir()
    found: list[Path] = []

    # Lo que diga gnome-screenshot, si está configurado.
    try:
        src = Gio.SettingsSchemaSource.get_default()
        if src and src.lookup("org.gnome.gnome-screenshot", True):
            custom = Gio.Settings.new("org.gnome.gnome-screenshot").get_string(
                "auto-save-directory"
            )
            if custom:
                path = Path(GLib.filename_from_uri(custom)[0] if "://" in custom else custom)
                if path.is_dir():
                    found.append(path)
    except Exception:
        pass

    for name in ("Screenshots", "Capturas de pantalla", "Capturas"):
        candidate = pics / name
        if candidate.is_dir():
            found.append(candidate)

    if not found:
        fallback = pics / "Screenshots"
        fallback.mkdir(parents=True, exist_ok=True)
        found.append(fallback)

    # dedup conservando orden
    return list(dict.fromkeys(p.resolve() for p in found))


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #
class Shot:
    __slots__ = ("path", "mtime", "size")

    def __init__(self, path: Path, mtime: float, size: int) -> None:
        self.path = path
        self.mtime = mtime
        self.size = size

    @property
    def uri(self) -> str:
        return GLib.filename_to_uri(str(self.path), None)

    def when(self) -> str:
        delta = time.time() - self.mtime
        if delta < 60:
            return "hace instantes"
        if delta < 3600:
            return f"hace {int(delta // 60)} min"
        stamp = time.localtime(self.mtime)
        if time.strftime("%F", stamp) == time.strftime("%F"):
            return time.strftime("hoy %H:%M", stamp)
        if delta < 7 * 86400:
            return time.strftime("%a %H:%M", stamp)
        return time.strftime("%d %b %H:%M", stamp)


class Library(GObject.Object):
    """Escanea las carpetas vigiladas y avisa cuando cambian."""

    __gsignals__ = {
        # lista de Shot, lista de rutas nuevas (str)
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
    }

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.monitors: list[Gio.FileMonitor] = []
        self.dirs: list[Path] = []
        self.known: set[str] = set()
        self._pending = 0
        self._first_scan = True

    def start(self) -> None:
        configured = [Path(p).expanduser() for p in (self.config["watch_dirs"] or [])]
        self.dirs = [p for p in configured if p.is_dir()] or autodetect_dirs()

        for monitor in self.monitors:
            monitor.cancel()
        self.monitors = []
        for directory in self.dirs:
            try:
                monitor = Gio.File.new_for_path(str(directory)).monitor_directory(
                    Gio.FileMonitorFlags.WATCH_MOVES, None
                )
                monitor.set_rate_limit(200)
                monitor.connect("changed", self._on_dir_changed)
                self.monitors.append(monitor)
            except GLib.Error as exc:
                print(f"[shelf] no se pudo vigilar {directory}: {exc}", file=sys.stderr)
        self.rescan()

    def _on_dir_changed(self, _monitor, _f, _other, event) -> None:
        interesting = (
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.MOVED_OUT,
            Gio.FileMonitorEvent.RENAMED,
            Gio.FileMonitorEvent.ATTRIBUTE_CHANGED,
        )
        if event not in interesting:
            return
        # Debounce: las capturas se escriben en varios pasos.
        self._pending += 1
        GLib.timeout_add(350, self._debounced)

    def _debounced(self) -> bool:
        self._pending -= 1
        if self._pending <= 0:
            self._pending = 0
            self.rescan()
        return GLib.SOURCE_REMOVE

    def rescan(self) -> None:
        shots: list[Shot] = []
        for directory in self.dirs:
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                if Path(entry.name).suffix.lower() not in EXTENSIONS:
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                if stat.st_size == 0:
                    continue  # todavía se está escribiendo
                shots.append(Shot(Path(entry.path), stat.st_mtime, stat.st_size))

        shots.sort(key=lambda s: s.mtime, reverse=True)
        shots = shots[: max(1, int(self.config["max_items"]))]

        paths = {str(s.path) for s in shots}
        fresh = [] if self._first_scan else sorted(paths - self.known)
        self._first_scan = False
        self.known = paths
        self.emit("changed", shots, fresh)


# --------------------------------------------------------------------------- #
# Thumbnails
# --------------------------------------------------------------------------- #
def texture_from_pixbuf(pixbuf: GdkPixbuf.Pixbuf) -> Gdk.Texture:
    """GdkPixbuf → GdkTexture sin usar la API deprecada new_for_pixbuf()."""
    fmt = Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(
        pixbuf.get_width(),
        pixbuf.get_height(),
        fmt,
        GLib.Bytes.new(pixbuf.get_pixels()),
        pixbuf.get_rowstride(),
    )


class ThumbCache:
    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="thumb")
        self.cache: dict[tuple[str, int, int], Gdk.Texture] = {}

    def get(self, shot: Shot, size: int, callback) -> None:
        key = (str(shot.path), int(shot.mtime), size)
        cached = self.cache.get(key)
        if cached is not None:
            callback(cached)
            return

        def work() -> None:
            texture = None
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(shot.path), size * 2, size * 2, True
                )
                texture = texture_from_pixbuf(pixbuf)
            except Exception:
                texture = None
            GLib.idle_add(deliver, texture, priority=GLib.PRIORITY_DEFAULT_IDLE)

        def deliver(texture) -> bool:
            if texture is not None:
                self.cache[key] = texture
                if len(self.cache) > 200:
                    for stale in list(self.cache)[:60]:
                        self.cache.pop(stale, None)
            callback(texture)
            return GLib.SOURCE_REMOVE

        self.pool.submit(work)


# --------------------------------------------------------------------------- #
# Tarjeta de captura
# --------------------------------------------------------------------------- #
class ShotCard(Gtk.FlowBoxChild):
    def __init__(self, shot: Shot, window: "ShelfWindow") -> None:
        super().__init__()
        self.shot = shot
        self.window = window
        size = int(window.config["thumb_size"])

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("shelf-card")
        card.set_overflow(Gtk.Overflow.HIDDEN)
        self.card = card
        self.set_child(card)

        self.picture = Gtk.Picture(
            content_fit=Gtk.ContentFit.COVER,
            can_shrink=True,
            hexpand=True,
        )
        self.picture.add_css_class("shelf-thumb")
        self.picture.set_size_request(size, int(size * 0.62))

        overlay = Gtk.Overlay(child=self.picture)

        # Barra flotante con acciones rápidas (aparece al pasar el mouse).
        bar = Gtk.Box(spacing=2, halign=Gtk.Align.END, valign=Gtk.Align.START)
        bar.add_css_class("shelf-overlay-bar")
        for icon, tip, handler in (
            ("edit-copy-symbolic", "Copiar al portapapeles", self._copy),
            ("image-x-generic-symbolic", "Abrir", self._open),
            ("user-trash-symbolic", "Mover a la papelera", self._trash),
        ):
            button = Gtk.Button(icon_name=icon, tooltip_text=tip, has_frame=False)
            button.connect("clicked", handler)
            bar.append(button)
        self.revealer = Gtk.Revealer(
            child=bar,
            transition_type=Gtk.RevealerTransitionType.CROSSFADE,
            halign=Gtk.Align.END,
            valign=Gtk.Align.START,
        )
        overlay.add_overlay(self.revealer)

        self.badge = Gtk.Label(label="", halign=Gtk.Align.START, valign=Gtk.Align.START)
        self.badge.add_css_class("shelf-badge")
        self.badge.set_visible(False)
        overlay.add_overlay(self.badge)

        card.append(overlay)

        caption = Gtk.Label(
            label=shot.when(),
            xalign=0,
            ellipsize=3,  # Pango.EllipsizeMode.END
            tooltip_text=f"{shot.path.name}\n{shot.path.parent}",
        )
        caption.add_css_class("shelf-caption")
        caption.add_css_class("dim-label")
        card.append(caption)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self.revealer.set_reveal_child(True))
        motion.connect("leave", lambda *_: self.revealer.set_reveal_child(False))
        self.add_controller(motion)

        # --- Drag & drop hacia afuera -------------------------------------
        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        drag.connect("prepare", self._on_drag_prepare)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_right_click)
        self.add_controller(click)

        window.thumbs.get(shot, size, self._set_texture)

    # -- helpers ---------------------------------------------------------
    def _set_texture(self, texture) -> None:
        if texture is None:
            self.picture.set_paintable(None)
            self.picture.set_child_visible(True)
            self.badge.set_text("¿ilegible?")
            self.badge.set_visible(True)
            return
        self.picture.set_paintable(texture)
        self.badge.set_text(f"{texture.get_width()}×{texture.get_height()}")
        self.badge.set_visible(True)

    def mark_new(self) -> None:
        self.card.add_css_class("shelf-new")
        GLib.timeout_add_seconds(6, lambda: (self.card.remove_css_class("shelf-new"), False)[1])

    # -- acciones --------------------------------------------------------
    def _copy(self, *_a) -> None:
        self.window.copy_shots([self.shot])

    def _open(self, *_a) -> None:
        self.window.open_shots([self.shot])

    def _trash(self, *_a) -> None:
        self.window.trash_shots([self.shot])

    def _on_right_click(self, gesture, _n, x, y) -> None:
        self.window.select_only(self)
        self.window.show_context_menu(self, x, y)

    # -- drag ------------------------------------------------------------
    def _on_drag_prepare(self, _source, _x, _y):
        # Selección única: siempre se arrastra exactamente esta captura.
        self.window.select_only(self)
        self.window.dragging = [self.shot]
        return build_content_provider([self.shot], include_texture=False)

    def _on_drag_begin(self, source, drag) -> None:
        paintable = self.picture.get_paintable()
        if paintable is not None:
            width = 128
            height = max(1, int(paintable.get_intrinsic_height() * width /
                                max(1, paintable.get_intrinsic_width())))
            source.set_icon(paintable, width // 2, height // 2)
        self.window.toast(f"Arrastrando {self.shot.path.name}…", 1)

    def _on_drag_end(self, _source, _drag, delete) -> None:
        if self.window.config["hide_on_drop"]:
            self.window.set_visible(False)


def build_content_provider(shots: list[Shot], include_texture: bool) -> Gdk.ContentProvider:
    """text/uri-list + GdkFileList (+ la imagen misma, para el portapapeles)."""
    providers: list[Gdk.ContentProvider] = []

    files = [Gio.File.new_for_path(str(s.path)) for s in shots]
    value = GObject.Value(Gdk.FileList, Gdk.FileList.new_from_list(files))
    providers.append(Gdk.ContentProvider.new_for_value(value))

    uris = "".join(f"{s.uri}\r\n" for s in shots).encode()
    providers.append(Gdk.ContentProvider.new_for_bytes("text/uri-list", GLib.Bytes.new(uris)))

    if include_texture and len(shots) == 1:
        try:
            texture = Gdk.Texture.new_from_filename(str(shots[0].path))
            providers.insert(0, Gdk.ContentProvider.new_for_value(GObject.Value(Gdk.Texture, texture)))
        except GLib.Error:
            pass

    text = "\n".join(str(s.path) for s in shots)
    providers.append(Gdk.ContentProvider.new_for_value(GObject.Value(str, text)))

    return Gdk.ContentProvider.new_union(providers)


# --------------------------------------------------------------------------- #
# Ventana
# --------------------------------------------------------------------------- #
class ShelfWindow(Adw.ApplicationWindow):
    def __init__(self, app: "ShelfApp") -> None:
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.config = app.config
        self.thumbs = ThumbCache()
        self.library = app.library
        self.cards: list[ShotCard] = []
        self.dragging: list[Shot] = []

        self.set_default_size(700, 500)
        self.set_size_request(420, 320)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        capture = Gtk.Button(child=Adw.ButtonContent(
            icon_name="camera-photo-symbolic", label="Capturar"))
        capture.set_tooltip_text("Abrir la herramienta de captura de GNOME (Impr Pant)")
        capture.add_css_class("suggested-action")
        capture.connect("clicked", lambda *_: self.capture())
        header.pack_start(capture)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Actualizar (F5)")
        refresh.connect("clicked", lambda *_: self.library.rescan())
        header.pack_start(refresh)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Abrir carpeta de capturas", "win.open-folder")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Preferencias", "win.preferences")
        section.append("Atajos de teclado", "win.shortcuts")
        section.append("Salir", "app.quit")
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Menú"))

        self.title_widget = Adw.WindowTitle(title=APP_NAME, subtitle="")
        header.set_title_widget(self.title_widget)
        toolbar.add_top_bar(header)

        # --- contenido ----------------------------------------------------
        self.flowbox = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.SINGLE,
            activate_on_single_click=False,
            homogeneous=True,
            column_spacing=6,
            row_spacing=6,
            min_children_per_line=1,
            max_children_per_line=12,
            valign=Gtk.Align.START,
        )
        self.flowbox.set_margin_top(10)
        self.flowbox.set_margin_bottom(10)
        self.flowbox.set_margin_start(10)
        self.flowbox.set_margin_end(10)
        self.flowbox.connect("child-activated", lambda _b, child: self.open_shots([child.shot]))
        self.flowbox.connect("selected-children-changed", lambda *_: self.update_actionbar())

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=self.flowbox,
            vexpand=True,
        )

        self.empty = Adw.StatusPage(
            icon_name="camera-photo-symbolic",
            title="Sin capturas todavía",
            description="Presiona Impr Pant o el botón «Capturar». "
                        "Las capturas nuevas aparecerán aquí al instante.",
        )

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(scroller, "grid")
        self.stack.add_named(self.empty, "empty")

        self.toaster = Adw.ToastOverlay(child=self.stack)
        toolbar.set_content(self.toaster)

        # --- barra de selección -------------------------------------------
        self.actionbar = Gtk.ActionBar(revealed=False)
        self.selection_label = Gtk.Label(label="", ellipsize=3, max_width_chars=36)
        self.actionbar.pack_start(self.selection_label)
        for icon, label, tip, handler in (
            ("edit-copy-symbolic", "Copiar", "Copiar al portapapeles (Ctrl+C)",
             lambda *_: self.copy_shots(self.selected_shots())),
            ("document-open-symbolic", "Abrir", "Abrir (Enter)",
             lambda *_: self.open_shots(self.selected_shots())),
            ("folder-symbolic", "Mostrar", "Mostrar en Archivos",
             lambda *_: self.reveal_shots(self.selected_shots())),
        ):
            button = Gtk.Button(child=Adw.ButtonContent(icon_name=icon, label=label),
                                tooltip_text=tip)
            button.connect("clicked", handler)
            self.actionbar.pack_end(button)
        trash = Gtk.Button(child=Adw.ButtonContent(icon_name="user-trash-symbolic",
                                                   label="Papelera"),
                           tooltip_text="Mover a la papelera (Supr)")
        trash.add_css_class("destructive-action")
        trash.connect("clicked", lambda *_: self.trash_shots(self.selected_shots()))
        self.actionbar.pack_end(trash)
        toolbar.add_bottom_bar(self.actionbar)

        self.set_content(toolbar)

        # --- menú contextual ----------------------------------------------
        self.context_menu = Gtk.PopoverMenu.new_from_model(self._build_context_model())
        self.context_menu.set_has_arrow(False)
        self.context_menu.set_halign(Gtk.Align.START)

        self._install_actions()
        self.connect("close-request", self._on_close)
        self.library.connect("changed", self.on_library_changed)

    # -- acciones / atajos -----------------------------------------------
    def _build_context_model(self) -> Gio.Menu:
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Abrir", "win.open")
        section.append("Copiar imagen", "win.copy")
        section.append("Copiar ruta", "win.copy-path")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Mostrar en Archivos", "win.reveal")
        section.append("Renombrar…", "win.rename")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Mover a la papelera", "win.trash")
        menu.append_section(None, section)
        return menu

    def _install_actions(self) -> None:
        specs = {
            "open": lambda *_: self.open_shots(self.selected_shots()),
            "copy": lambda *_: self.copy_shots(self.selected_shots()),
            "copy-path": lambda *_: self.copy_paths(self.selected_shots()),
            "reveal": lambda *_: self.reveal_shots(self.selected_shots()),
            "rename": lambda *_: self.rename_shot(),
            "trash": lambda *_: self.trash_shots(self.selected_shots()),
            "open-folder": lambda *_: self.open_folder(),
            "preferences": lambda *_: self.show_preferences(),
            "shortcuts": lambda *_: self.show_shortcuts(),
            "refresh": lambda *_: self.library.rescan(),
            "capture": lambda *_: self.capture(),
            "hide": lambda *_: self.set_visible(False),
        }
        for name, callback in specs.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        app = self.get_application()
        for accel, action in (
            ("<Ctrl>c", "win.copy"),
            ("<Ctrl><Shift>c", "win.copy-path"),
            ("Delete", "win.trash"),
            ("Return", "win.open"),
            ("F2", "win.rename"),
            ("F5", "win.refresh"),
            ("<Ctrl>n", "win.capture"),
            ("<Ctrl>o", "win.open-folder"),
            ("<Ctrl>comma", "win.preferences"),
            ("Escape", "win.hide"),
            ("<Ctrl>q", "app.quit"),
        ):
            app.set_accels_for_action(action, [accel])

    def _on_close(self, *_a) -> bool:
        if self.config["close_hides"]:
            self.set_visible(False)
            return True  # no destruir: seguimos vigilando en segundo plano
        return False

    # -- render -----------------------------------------------------------
    def on_library_changed(self, _library, shots: list[Shot], fresh: list[str]) -> None:
        selected = {str(s.path) for s in self.selected_shots()}
        self.flowbox.remove_all()
        self.cards = []

        for shot in shots:
            card = ShotCard(shot, self)
            self.flowbox.append(card)
            self.cards.append(card)
            if str(shot.path) in fresh:
                card.mark_new()
            elif str(shot.path) in selected:
                self.flowbox.select_child(card)

        self.stack.set_visible_child_name("grid" if shots else "empty")
        total = len(shots)
        where = ", ".join(d.name for d in self.library.dirs)
        self.title_widget.set_subtitle(
            f"{total} captura{'s' if total != 1 else ''} · {where}" if total else where
        )
        self.update_actionbar()

        if fresh and self.config["auto_show"]:
            newest = next((c for c in self.cards if str(c.shot.path) == fresh[-1]), None)
            if newest is not None:
                self.select_only(newest)
            self.raise_to_front()

    def raise_to_front(self) -> None:
        """Traer la ventana al frente de verdad, también en Wayland.

        Un present() desde una app en segundo plano no roba el foco: GNOME lo
        degrada a «La ventana está lista» en la bandeja. Remapear la superficie
        hace que el compositor la trate como ventana recién abierta y sí la
        levante (política focus-new-windows). Si ya está enfocada, no tocamos
        nada para no provocar un parpadeo innecesario.
        """
        if self.is_active():
            return
        if self.get_visible():
            self.set_visible(False)
            GLib.timeout_add(60, self._present_now)
        else:
            self._present_now()

    def _present_now(self) -> bool:
        self.present()
        return GLib.SOURCE_REMOVE

    def update_actionbar(self) -> None:
        shots = self.selected_shots()
        self.actionbar.set_revealed(bool(shots))
        self.selection_label.set_text(shots[0].path.name if shots else "")

    def selected_shots(self) -> list[Shot]:
        return [child.shot for child in self.flowbox.get_selected_children()]

    def select_only(self, card: ShotCard) -> None:
        self.flowbox.unselect_all()
        self.flowbox.select_child(card)

    def show_context_menu(self, card: ShotCard, x: float, y: float) -> None:
        self.context_menu.unparent()
        self.context_menu.set_parent(card)
        self.context_menu.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
        self.context_menu.popup()

    def toast(self, text: str, timeout: int = 3) -> None:
        self.toaster.add_toast(Adw.Toast(title=text, timeout=timeout))

    # -- operaciones ------------------------------------------------------
    def copy_shots(self, shots: list[Shot]) -> None:
        if not shots:
            return
        self.get_clipboard().set_content(build_content_provider(shots, include_texture=True))
        self.toast("Imagen copiada al portapapeles" if len(shots) == 1
                   else f"{len(shots)} archivos copiados")

    def copy_paths(self, shots: list[Shot]) -> None:
        if not shots:
            return
        self.get_clipboard().set("\n".join(str(s.path) for s in shots))
        self.toast("Ruta copiada")

    def open_shots(self, shots: list[Shot]) -> None:
        for shot in shots[:5]:
            launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(shot.path)))
            launcher.launch(self, None, None)

    def reveal_shots(self, shots: list[Shot]) -> None:
        if not shots:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(shots[0].path)))
        launcher.open_containing_folder(self, None, None)

    def open_folder(self) -> None:
        if not self.library.dirs:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(self.library.dirs[0])))
        launcher.launch(self, None, None)

    def trash_shots(self, shots: list[Shot]) -> None:
        if not shots:
            return
        failed = 0
        for shot in shots:
            try:
                Gio.File.new_for_path(str(shot.path)).trash(None)
            except GLib.Error:
                failed += 1
        self.library.rescan()
        if failed:
            self.toast(f"No se pudieron borrar {failed} archivo(s)")
        else:
            self.toast(f"{len(shots)} captura(s) a la papelera")

    def rename_shot(self) -> None:
        shots = self.selected_shots()
        if len(shots) != 1:
            self.toast("Selecciona una sola captura para renombrar")
            return
        shot = shots[0]

        entry = Gtk.Entry(text=shot.path.stem, activates_default=True)
        dialog = Adw.AlertDialog(heading="Renombrar captura",
                                 body=f"Extensión: {shot.path.suffix}")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("ok", "Renombrar")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response) -> None:
            if response != "ok":
                return
            name = entry.get_text().strip()
            if not name:
                return
            target = shot.path.with_name(name + shot.path.suffix)
            try:
                shot.path.rename(target)
                self.toast(f"Renombrada a {target.name}")
            except OSError as exc:
                self.toast(f"No se pudo renombrar: {exc}")
            self.library.rescan()

        dialog.connect("response", on_response)
        dialog.present(self)

    # -- captura ----------------------------------------------------------
    def capture(self) -> None:
        """Abre la UI de captura de GNOME vía el portal; guarda en la carpeta vigilada."""
        was_visible = self.get_visible()
        self.set_visible(False)

        def restore() -> bool:
            if was_visible:
                self.present()
            return GLib.SOURCE_REMOVE

        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            restore()
            self.toast(f"Sin bus de sesión: {exc}")
            return

        token = f"shelf{int(time.time() * 1000) % 1000000}"
        sender = bus.get_unique_name()[1:].replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

        def on_response(_conn, _sender, _path, _iface, _signal, params) -> None:
            bus.signal_unsubscribe(subscription)
            code, results = params.unpack()
            GLib.idle_add(restore)
            if code != 0:
                if code == 2:
                    GLib.idle_add(self._capture_fallback)
                return
            uri = results.get("uri")
            if uri:
                GLib.idle_add(self._ingest_capture, uri)

        subscription = bus.signal_subscribe(
            "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request",
            "Response", handle, None, Gio.DBusSignalFlags.NONE, on_response,
        )

        try:
            bus.call_sync(
                "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Screenshot", "Screenshot",
                GLib.Variant("(sa{sv})", ("", {
                    "handle_token": GLib.Variant("s", token),
                    "interactive": GLib.Variant("b", True),
                })),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error:
            bus.signal_unsubscribe(subscription)
            restore()
            self._capture_fallback()

    def _capture_fallback(self) -> bool:
        """Si el portal falla, probamos la API del Shell; si no, avisamos."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync(
                "org.gnome.Shell", "/org/gnome/Shell/Screenshot",
                "org.gnome.Shell.Screenshot", "InteractiveScreenshot",
                None, None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error:
            self.toast("Usa la tecla Impr Pant para capturar", 4)
        return GLib.SOURCE_REMOVE

    def _ingest_capture(self, uri: str) -> bool:
        """El portal deja el PNG en un temporal: lo movemos a la carpeta vigilada."""
        try:
            source = Path(GLib.filename_from_uri(uri)[0])
        except GLib.Error:
            return GLib.SOURCE_REMOVE
        if any(source.parent == d for d in self.library.dirs):
            self.library.rescan()
            return GLib.SOURCE_REMOVE
        target_dir = self.library.dirs[0]
        stamp = time.strftime("%Y-%m-%d %H-%M-%S")
        target = target_dir / f"Screenshot From {stamp}{source.suffix or '.png'}"
        index = 2
        while target.exists():
            target = target_dir / f"Screenshot From {stamp}-{index}{source.suffix or '.png'}"
            index += 1
        try:
            shutil.copy2(source, target)
            self.toast(f"Guardada en {target_dir.name}/")
        except OSError as exc:
            self.toast(f"No se pudo guardar: {exc}")
        self.library.rescan()
        return GLib.SOURCE_REMOVE

    # -- preferencias -----------------------------------------------------
    def show_preferences(self) -> None:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Bandeja")

        spin = Adw.SpinRow.new_with_range(4, 100, 1)
        spin.set_title("Capturas visibles")
        spin.set_subtitle("Cuántas de las más recientes se muestran")
        spin.set_value(int(self.config["max_items"]))
        spin.connect("notify::value", lambda row, _p: self._set_max_items(int(row.get_value())))
        group.add(spin)

        size = Adw.SpinRow.new_with_range(120, 400, 10)
        size.set_title("Tamaño de miniatura")
        size.set_value(int(self.config["thumb_size"]))
        size.connect("notify::value", lambda row, _p: self._set_thumb_size(int(row.get_value())))
        group.add(size)
        page.add(group)

        group = Adw.PreferencesGroup(title="Comportamiento")
        for key, title, subtitle in (
            ("auto_show", "Aparecer al capturar",
             "Traer la bandeja al frente cuando llega una captura nueva"),
            ("hide_on_drop", "Ocultar al arrastrar",
             "Esconder la ventana después de soltar una captura en otra app"),
            ("close_hides", "Cerrar la esconde",
             "Seguir vigilando en segundo plano al cerrar la ventana"),
        ):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=bool(self.config[key]))
            row.connect("notify::active",
                        lambda r, _p, k=key: self.config.__setitem__(k, r.get_active()))
            group.add(row)
        page.add(group)

        group = Adw.PreferencesGroup(
            title="Carpetas vigiladas",
            description="Se detectan solas. Edita "
                        f"{CONFIG_PATH} → \"watch_dirs\" para fijarlas a mano.",
        )
        for directory in self.library.dirs:
            group.add(Adw.ActionRow(title=directory.name, subtitle=str(directory.parent)))
        page.add(group)

        dialog = Adw.PreferencesDialog()
        dialog.add(page)
        dialog.present(self)

    def _set_max_items(self, value: int) -> None:
        if value != int(self.config["max_items"]):
            self.config["max_items"] = value
            self.library.rescan()

    def _set_thumb_size(self, value: int) -> None:
        if value != int(self.config["thumb_size"]):
            self.config["thumb_size"] = value
            self.library.rescan()

    def show_shortcuts(self) -> None:
        rows = [
            ("Ctrl+N", "Capturar pantalla"),
            ("F5", "Actualizar la bandeja"),
            ("Arrastrar", "Soltar la captura en cualquier app"),
            ("Ctrl+C", "Copiar imagen al portapapeles"),
            ("Ctrl+Shift+C", "Copiar la ruta del archivo"),
            ("Enter", "Abrir en el visor"),
            ("F2", "Renombrar"),
            ("Supr", "Mover a la papelera"),
            ("Esc", "Ocultar la bandeja"),
        ]
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for accel, description in rows:
            line = Gtk.Box(spacing=12)
            key = Gtk.Label(label=accel, xalign=1, width_chars=14)
            key.add_css_class("monospace")
            key.add_css_class("dim-label")
            line.append(key)
            line.append(Gtk.Label(label=description, xalign=0, hexpand=True))
            box.append(line)
        dialog = Adw.AlertDialog(heading="Atajos de teclado")
        dialog.set_extra_child(box)
        dialog.add_response("close", "Cerrar")
        dialog.present(self)


# --------------------------------------------------------------------------- #
# Aplicación
# --------------------------------------------------------------------------- #
class ShelfApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.config = Config()
        self.library = Library(self.config)
        self.window: ShelfWindow | None = None
        self.add_main_option("toggle", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Mostrar u ocultar la bandeja", None)
        self.add_main_option("capture", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Capturar y mostrar la bandeja", None)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.library.start()

    def do_command_line(self, command_line) -> int:
        options = command_line.get_options_dict().end().unpack()
        self.ensure_window()
        if options.get("toggle") and self.window.get_visible():
            self.window.set_visible(False)
        else:
            self.window.raise_to_front()
            if options.get("capture"):
                GLib.timeout_add(150, lambda: (self.window.capture(), False)[1])
        return 0

    def do_activate(self) -> None:
        self.ensure_window()
        self.window.present()

    def ensure_window(self) -> None:
        if self.window is None:
            self.window = ShelfWindow(self)
            self.library.rescan()


def quiet_known_gtk_noise() -> None:
    """Silencia una assertion benigna de GTK 4.20.

    Al interactuar con la ventana, GTK emite dos
    «gtk_adjustment_get_value: assertion 'GTK_IS_ADJUSTMENT' failed» por frame
    (~120 líneas/segundo en el journal). Es aguas arriba: esta app no toca
    ningún GtkAdjustment. No pude reproducirla sin interacción real del puntero,
    así que la filtro por mensaje exacto en vez de esconderla toda.
    SHELF_VERBOSE=1 deja pasar absolutamente todo.
    """
    if os.environ.get("SHELF_VERBOSE"):
        return

    def handler(domain, level, message, _data) -> None:
        if "gtk_adjustment_get_value" in message:
            return
        GLib.log_default_handler(domain, level, message, None)

    GLib.log_set_handler(
        "Gtk",
        GLib.LogLevelFlags.LEVEL_CRITICAL
        | GLib.LogLevelFlags.FLAG_FATAL
        | GLib.LogLevelFlags.FLAG_RECURSION,
        handler,
        None,
    )


def main() -> int:
    quiet_known_gtk_noise()
    Adw.init()
    return ShelfApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
