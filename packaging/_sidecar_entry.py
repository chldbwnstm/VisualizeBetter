"""PyInstaller entry for the [8-F] serve sidecar.

The bundled exe is the full `visualizebetter` CLI, so the Tauri shell launches it as
`visualizebetter serve --port … --data-dir … --no-open` — reusing the same serve the web
app and CLI use, with no separate code path.
"""

from visualizebetter.cli import main

if __name__ == "__main__":
    main()
