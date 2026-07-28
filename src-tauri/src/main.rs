// [8-F] VisualizeBetter desktop shell — a thin Tauri v2 window over the web app.
//
// ★ [8-D] the single graph owner is `visualizebetter serve`. This shell owns no graph: it
// spawns serve as a PyInstaller sidecar, waits for serve's [8-D] discovery
// port-file, and points the webview at http://127.0.0.1:PORT — where serve serves
// the very same React SPA the browser web app loads, over the same WS/HTTP. So the
// frontend is reused unchanged; the shell only manages the window and the sidecar
// lifecycle (spawn on start, graceful kill on exit — no orphan process).

// No console window on a Windows release build.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Deserialize;
use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

/// The subset of serve's [8-D] port-file the shell needs to reach it.
#[derive(Deserialize)]
struct ServeInfo {
    port: u16,
    #[serde(default)]
    token: Option<String>,
}

/// Keeps the sidecar handle so it can be killed on exit ([8-F]: no orphan sidecar).
struct Sidecar(Mutex<Option<CommandChild>>);

/// Match Python's `default_data_dir()`: %LOCALAPPDATA%/visualizebetter on Windows,
/// ~/.visualizebetter elsewhere — the shared location for the [8-D] port-file.
fn data_dir() -> PathBuf {
    #[cfg(windows)]
    if let Ok(base) = std::env::var("LOCALAPPDATA") {
        return PathBuf::from(base).join("visualizebetter");
    }
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_default();
    PathBuf::from(home).join(".visualizebetter")
}

/// Grab a free loopback port for the sidecar so two shells never collide.
fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8765)
}

/// Poll serve's [8-D] port-file until it appears and parses — that is serve's
/// readiness signal (it writes the file only once it is up).
fn wait_for_serve(port_file: &Path, timeout: Duration) -> Option<ServeInfo> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if let Ok(text) = fs::read_to_string(port_file) {
            if let Ok(info) = serde_json::from_str::<ServeInfo>(&text) {
                return Some(info);
            }
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    None
}

fn main() {
    let dir = data_dir();
    let _ = fs::create_dir_all(&dir);
    let port = free_port();
    let port_file = dir.join("serve.json");
    // A stale file from a previous run must not read as this run's readiness.
    let _ = fs::remove_file(&port_file);

    tauri::Builder::default()
        // A second launch focuses the running window instead of racing a rival
        // sidecar over the same data dir ([8-F]).
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .manage(Sidecar(Mutex::new(None)))
        .setup(move |app| {
            // [8-D] spawn serve — the one graph owner. externalBin resolves the
            // per-OS binary `binaries/visualizebetter-<target-triple>`.
            let (_rx, child) = app
                .shell()
                .sidecar("visualizebetter")?
                .args([
                    "serve",
                    "--no-open",
                    "--port",
                    &port.to_string(),
                    "--data-dir",
                    &dir.to_string_lossy(),
                ])
                .spawn()?;
            app.state::<Sidecar>().0.lock().unwrap().replace(child);

            // Wait for serve, then point the window at it. Off the main thread so
            // the UI paints the loading page while serve starts.
            let handle = app.handle().clone();
            let port_file = port_file.clone();
            std::thread::spawn(move || {
                if let Some(info) = wait_for_serve(&port_file, Duration::from_secs(40)) {
                    let mut url = format!("http://127.0.0.1:{}/", info.port);
                    if let Some(token) = info.token {
                        // Loopback needs no token; if the sidecar ran with one, the
                        // webview carries it ([11], serve's /live accepts ?token=).
                        url.push_str(&format!("?token={token}"));
                    }
                    if let (Some(w), Ok(parsed)) =
                        (handle.get_webview_window("main"), url.parse())
                    {
                        let _ = w.navigate(parsed);
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the VisualizeBetter shell")
        .run(|app, event| {
            if let RunEvent::ExitRequested { .. } = event {
                // [8-F] graceful shutdown: no orphan sidecar. PyInstaller's onefile
                // exe is a bootloader that runs the real serve as a *child*, so
                // kill.() (the bootloader pid alone) would leave that child behind —
                // kill the whole process tree instead.
                if let Some(child) = app.state::<Sidecar>().0.lock().unwrap().take() {
                    let pid = child.pid();
                    #[cfg(windows)]
                    {
                        let _ = std::process::Command::new("taskkill")
                            .args(["/F", "/T", "/PID", &pid.to_string()])
                            .status();
                    }
                    #[cfg(not(windows))]
                    {
                        // Best-effort tree kill on Unix; falls back to the direct kill.
                        let _ = std::process::Command::new("pkill")
                            .args(["-TERM", "-P", &pid.to_string()])
                            .status();
                        let _ = child.kill();
                    }
                }
            }
        });
}
