# Bandeja de capturas (Screenshot Shelf)

Mejora el flujo de capturas de Ubuntu/GNOME: una ventana que muestra **las últimas 6
capturas** en vivo y desde la cual puedes **arrastrarlas (drag & drop) a cualquier app**
— navegador, Slack, Telegram, GIMP, un correo, Archivos, etc.

GNOME ya guarda las capturas en `~/Pictures/Screenshots`, pero para adjuntarlas hay que
abrir el explorador de archivos y buscarlas. Esto elimina ese paso.

## Instalación

```bash
./install.sh              # lanzador + atajo Super+Shift+S para mostrar/ocultar
./install.sh --autostart  # además, arranca con la sesión y vigila en segundo plano
./install.sh --uninstall  # revierte todo
```

Requisitos: Ubuntu con GNOME (probado en 25.10 / GNOME 49, Wayland) y
`python3-gi gir1.2-gtk-4.0 gir1.2-adw-1` (vienen preinstalados en Ubuntu).

Se ejecuta con el Python del sistema (`/usr/bin/python3`); si usas pyenv, no importa:
el lanzador ya apunta al intérprete correcto.

## Uso

1. Captura como siempre: **Impr Pant** (o el botón *Capturar* de la bandeja).
2. La bandeja salta al frente sola, con la captura nueva ya seleccionada.
3. **Arrastra la miniatura** hacia donde la necesites y suéltala.

La selección es de a una: siempre se arrastra exactamente la captura que tomaste.

### Atajos

| Atajo | Acción |
|---|---|
| `Super+Shift+S` | Mostrar/ocultar la bandeja (global) |
| `Ctrl+N` | Capturar pantalla |
| Arrastrar | Soltar la captura en otra app |
| `Ctrl+C` | Copiar la imagen al portapapeles |
| `Ctrl+Shift+C` | Copiar la ruta del archivo |
| `Enter` | Abrir en el visor |
| `F2` | Renombrar |
| `Supr` | Mover a la papelera |
| `F5` | Actualizar |
| `Esc` | Ocultar la bandeja |

Clic derecho sobre una miniatura abre el menú con todas las acciones.

## Qué hace por debajo

- Vigila las carpetas de capturas con `GFileMonitor`: lo que aparezca ahí (venga de
  Impr Pant, de Flameshot o de un `scp`) se muestra al instante.
- Las carpetas se autodetectan: `~/Pictures/Screenshots`, su equivalente en español y
  lo que tengas configurado en `org.gnome.gnome-screenshot`.
- Al arrastrar entrega `text/uri-list` + `GdkFileList` + la ruta como texto, que es lo
  que aceptan navegadores, gestores de archivos y apps GTK/Qt/Electron.
- Al copiar entrega además el **PNG completo** como imagen, para pegar directo en
  editores o chats.
- Borrar manda a la papelera (`GIO trash`), no borra de forma permanente.

## Configuración

`~/.config/screenshot-shelf/config.json` (o el diálogo *Preferencias*, `Ctrl+,`):

| Clave | Por defecto | Qué hace |
|---|---|---|
| `max_items` | `6` | Cuántas capturas recientes se muestran |
| `thumb_size` | `200` | Ancho de las miniaturas en px |
| `auto_show` | `true` | Traer la bandeja al frente al detectar una captura nueva. Para saltarse la degradación a «La ventana está lista» de Wayland, la ventana se remapea: GNOME la trata como recién abierta y le da el foco (`focus-new-windows` debe estar en `smart`, que es el valor por defecto) |
| `hide_on_drop` | `false` | Esconder la ventana tras arrastrar algo fuera |
| `close_hides` | `true` | Cerrar la ventana la esconde y sigue vigilando |
| `watch_dirs` | `[]` | Rutas fijas a vigilar (vacío = autodetectar) |

## Notas sobre el botón «Capturar»

En Wayland ninguna app puede capturar la pantalla por su cuenta: hay que pasar por el
portal (`org.freedesktop.portal.Screenshot`), que muestra la UI de captura de GNOME y
pide permiso la primera vez. Si alguna vez respondiste *No*, GNOME lo recuerda y el
botón deja de funcionar. Para que vuelva a preguntar:

```bash
gdbus call --session --dest org.freedesktop.impl.portal.PermissionStore \
  --object-path /org/freedesktop/impl/portal/PermissionStore \
  --method org.freedesktop.impl.portal.PermissionStore.DeletePermission \
  screenshot screenshot ""
```

La tecla **Impr Pant** siempre funciona (la maneja GNOME Shell) y la bandeja recoge el
resultado igual.

## Ruido en el log

GTK 4.20 emite `gtk_adjustment_get_value: assertion failed` dos veces por frame mientras
se interactúa con la ventana. Es benigno y no viene de esta app (no usa adjustments), pero
llena el journal, así que se filtra ese mensaje puntual. Para ver todo sin filtrar:

```bash
SHELF_VERBOSE=1 screenshot-shelf
```
