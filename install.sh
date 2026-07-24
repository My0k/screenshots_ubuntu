#!/usr/bin/env bash
# Instala la Bandeja de capturas: lanzador, atajo de teclado y (opcional) autoarranque.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PY="$HERE/screenshot_shelf.py"
BIN="$HOME/.local/bin/screenshot-shelf"
DESKTOP="$HOME/.local/share/applications/cl.myok.ScreenshotShelf.desktop"
AUTOSTART="$HOME/.config/autostart/cl.myok.ScreenshotShelf.desktop"
ACCEL="<Super><Shift>s"

usage() {
    cat <<EOF
Uso: ./install.sh [opciones]

  (sin opciones)   instala el lanzador + atajo $ACCEL
  --no-keybind     no registrar el atajo de teclado
  --autostart      arrancar la bandeja al iniciar sesión (vigila en segundo plano)
  --uninstall      quitar todo lo instalado
EOF
}

keybind() {  # $1 = install | remove
    /usr/bin/python3 - "$1" "$BIN" "$ACCEL" <<'PY'
import sys
from gi.repository import Gio

action, binary, accel = sys.argv[1], sys.argv[2], sys.argv[3]
BASE = "org.gnome.settings-daemon.plugins.media-keys"
PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
MINE = PATH + "screenshot-shelf/"

root = Gio.Settings.new(BASE)
bindings = list(root.get_strv("custom-keybindings"))

if action == "remove":
    if MINE in bindings:
        root.set_strv("custom-keybindings", [b for b in bindings if b != MINE])
        print("atajo eliminado")
    sys.exit(0)

if MINE not in bindings:
    bindings.append(MINE)
    root.set_strv("custom-keybindings", bindings)

entry = Gio.Settings.new_with_path(BASE + ".custom-keybinding", MINE)
entry.set_string("name", "Bandeja de capturas")
entry.set_string("command", f"{binary} --toggle")
entry.set_string("binding", accel)
print(f"atajo {accel} → {binary} --toggle")
PY
}

DO_KEYBIND=1
DO_AUTOSTART=0
case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    --uninstall)
        rm -f "$BIN" "$DESKTOP" "$AUTOSTART"
        keybind remove || true
        update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
        echo "✔ desinstalado (tu configuración queda en ~/.config/screenshot-shelf/)"
        exit 0 ;;
    --no-keybind) DO_KEYBIND=0 ;;
    --autostart)  DO_AUTOSTART=1 ;;
    "") ;;
    *) usage; exit 1 ;;
esac

[[ -f "$APP_PY" ]] || { echo "no encuentro $APP_PY"; exit 1; }
/usr/bin/python3 -c 'import gi; gi.require_version("Adw","1")' 2>/dev/null || {
    echo "Falta PyGObject/libadwaita. Instala con:"
    echo "  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
    exit 1
}

mkdir -p "$(dirname "$BIN")" "$(dirname "$DESKTOP")"
chmod +x "$APP_PY"
cat > "$BIN" <<EOF
#!/usr/bin/env bash
exec /usr/bin/python3 "$APP_PY" "\$@"
EOF
chmod +x "$BIN"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Bandeja de capturas
Comment=Ver y arrastrar las últimas capturas de pantalla
Exec=$BIN
Icon=applets-screenshooter
Terminal=false
Categories=Graphics;Utility;
Keywords=captura;screenshot;pantalla;shelf;
StartupNotify=true
StartupWMClass=cl.myok.ScreenshotShelf
Actions=capture;

[Desktop Action capture]
Name=Capturar ahora
Exec=$BIN --capture
EOF

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

if [[ $DO_KEYBIND -eq 1 ]]; then
    keybind install || echo "(no se pudo registrar el atajo; hazlo a mano en Configuración → Teclado)"
fi

if [[ $DO_AUTOSTART -eq 1 ]]; then
    mkdir -p "$(dirname "$AUTOSTART")"
    cp "$DESKTOP" "$AUTOSTART"
    echo "X-GNOME-Autostart-enabled=true" >> "$AUTOSTART"
    echo "✔ autoarranque activado"
fi

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "⚠ ~/.local/bin no está en tu PATH; agrégalo a ~/.bashrc" ;;
esac

echo "✔ instalado: ejecuta 'screenshot-shelf' o busca «Bandeja de capturas»"
