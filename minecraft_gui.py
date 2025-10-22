#!/usr/bin/env python3
"""
minecraft_cache_warmer_gui.py
Simple GUI that finds CurseForge and Prism instances and warms the OS file cache
for faster modded Minecraft startup. Works on Windows and macOS.

Run:
  python minecraft_cache_warmer_gui.py

Notes:
  This tool only reads files to nudge them into the OS page cache.
  It does not modify your files.
"""

import os
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import time
import shutil
import subprocess

# ------------- cache warm core -------------

DEFAULT_PATTERNS = [
    "*.jar", "*.zip", "*.json", "*.cfg", "*.toml", "*.ini",
    "*.mixins.json", "*.mcmeta", "*.png", "*.jpg", "*.ogg", "*.wav", "*.txt"
]

SKIP_DIRNAMES = {
    ".git", ".gradle", ".idea", "logs", "crash-reports", "screenshots", "shaderpacks"
}

def iter_files(root: Path, patterns):
    seen = set()
    for pattern in patterns:
        for p in root.rglob(pattern):
            try:
                if not p.is_file():
                    continue
                key = (p.resolve() if p.exists() else p)
                if key in seen:
                    continue
                seen.add(key)
                if any(part in SKIP_DIRNAMES for part in p.parts):
                    continue
                yield p
            except Exception:
                continue

def human(n):
    units = ["B","KB","MB","GB","TB","PB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.1f} {units[i]}"

def warm_file(path: Path, chunk_mb=16):
    size = path.stat().st_size
    total = 0
    with open(path, "rb", buffering=0) as f:
        chunk = max(1, int(chunk_mb * 1024 * 1024))
        while True:
            data = f.read(chunk)
            if not data:
                break
            total += len(data)
    return total

# ------------- instance discovery -------------

def probable_instance_dirs():
    out = []

    home = Path.home()
    platform = sys.platform

    candidates = set()

    if platform.startswith("win"):
        userprofile = Path(os.environ.get("USERPROFILE", str(home)))
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))

        # CurseForge paths
        candidates.add(userprofile / "Documents" / "CurseForge" / "Minecraft" / "Instances")
        candidates.add(userprofile / "CurseForge" / "Minecraft" / "Instances")
        # Old Twitch location
        candidates.add(userprofile / "Twitch" / "Minecraft" / "Instances")

        # PrismLauncher
        candidates.add(appdata / "PrismLauncher" / "instances")
        candidates.add(appdata / "MultiMC" / "instances")

        # Vanilla default
        candidates.add(appdata / ".minecraft")

    elif platform == "darwin":
        # macOS
        lib = home / "Library" / "Application Support"
        candidates.add(lib / "CurseForge" / "Minecraft" / "Instances")
        candidates.add(lib / "minecraft")  # vanilla
        candidates.add(lib / "PrismLauncher" / "instances")
        candidates.add(lib / "MultiMC" / "instances")
        # Some users keep CurseForge in Documents
        candidates.add(home / "Documents" / "CurseForge" / "Minecraft" / "Instances")

    else:
        # Linux and others
        home_cfg = home / ".local" / "share"
        candidates.add(home / ".minecraft")
        candidates.add(home_cfg / "PrismLauncher" / "instances")
        candidates.add(home_cfg / "MultiMC" / "instances")
        candidates.add(home / "PrismLauncher" / "instances")
        candidates.add(home / "MultiMC" / "instances")

    # Expand and filter existing
    for c in candidates:
        try:
            c = c.expanduser()
            if c.exists() and c.is_dir():
                out.append(c)
        except Exception:
            continue

    # Remove duplicates while preserving order
    seen = set()
    uniq = []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(rp)
    return uniq

def list_instances(root_dir: Path):
    # For CurseForge and Prism, each subfolder is an instance
    instances = []
    try:
        for child in sorted(root_dir.iterdir()):
            if child.is_dir():
                # Basic heuristic: must contain mods or config or resourcepacks
                has_content = any((child / sub).exists() for sub in ("mods", "config", "resourcepacks", ".minecraft"))
                if has_content:
                    instances.append(child)
    except Exception:
        pass
    return instances

# ------------- GUI -------------

class CacheWarmerGUI(tk.Tk):

    def _detect_curseforge(self):
        # Try common CurseForge app locations
        exe = None
        if sys.platform.startswith("win"):
            # Typical user install path
            candidates = [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "CurseForge" / "CurseForge.exe",
                Path(os.environ.get("PROGRAMFILES", "")) / "CurseForge" / "CurseForge.exe",
            ]
            for c in candidates:
                if c and c.exists():
                    exe = str(c)
                    break
        elif sys.platform == "darwin":
            candidates = [
                "/Applications/CurseForge.app/Contents/MacOS/CurseForge",
                str(Path.home() / "Applications" / "CurseForge.app" / "Contents" / "MacOS" / "CurseForge"),
            ]
            for c in candidates:
                if Path(c).exists():
                    exe = c
                    break
        else:
            # Linux: no official CurseForge. Some use Overwolf via wine or a flatpak
            exe = shutil.which("curseforge") or shutil.which("CurseForge")
        if exe:
            self._append_log(f"Detected CurseForge at {exe}")
            # We cannot pass an instance name to CurseForge directly. Populate launch box to just open the app.
            self.launch_cmd_var.set(f'"{exe}"')
        else:
            messagebox.showinfo("Not found", "CurseForge was not found. You can still warm instances and open the folder.")

    def _open_curseforge(self):
        cmd = self.launch_cmd_var.get().strip()
        if not cmd:
            self._detect_curseforge()
            cmd = self.launch_cmd_var.get().strip()
        if not cmd:
            self._append_log("No CurseForge app found to open")
            return
        self._append_log(f"Opening CurseForge: {cmd}")
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(cmd, shell=True)
            else:
                subprocess.Popen(cmd, shell=True)
        except Exception as e:
            self._append_log(f"Failed to open CurseForge: {e}")

    def _reveal_selected(self):
        sel_indices = self.instances_list.curselection()
        if not sel_indices:
            messagebox.showwarning("Nothing selected", "Select an instance first")
            return
        # Reveal the first selected instance
        path = self._instance_map[sel_indices[0]]
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(f'explorer "{path}"')
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                # Try xdg-open
                subprocess.Popen(["xdg-open", str(path)])
            self._append_log(f"Opened folder: {path}")
        except Exception as e:
            self._append_log(f"Failed to open folder: {e}")


    def _detect_prism(self):
        # Try to find prismlauncher on PATH or at common macOS location
        exe = shutil.which("prismlauncher")
        if not exe and sys.platform == "darwin":
            mac_path = "/Applications/PrismLauncher.app/Contents/MacOS/prismlauncher"
            if Path(mac_path).exists():
                exe = mac_path
        if exe:
            # Template uses {instance} placeholder
            self.launch_cmd_var.set(f'"{exe}" --launch "{{instance}}"')
            self._append_log(f"Detected PrismLauncher at {exe}")
        else:
            messagebox.showinfo("Not found", "PrismLauncher was not found. Enter a custom command. Use {instance} as a placeholder.")

    def _maybe_launch(self, instance_paths):
        if not self.launch_after_var.get():
            return
        cmd_tpl = self.launch_cmd_var.get().strip()
        if not cmd_tpl:
            self._append_log("Launch requested but no command template set")
            return

        # If single selection, try to substitute the instance name
        instance_name = None
        if len(instance_paths) == 1:
            instance_name = instance_paths[0].name
        else:
            # Many launchers cannot take multiple instances. Use the first.
            instance_name = instance_paths[0].name

        cmd = cmd_tpl.replace("{instance}", instance_name)
        self._append_log(f"Launching: {cmd}")
        try:
            # Start detached
            if sys.platform.startswith("win"):
                subprocess.Popen(cmd, shell=True, creationflags=0x00000008)  # CREATE_NO_WINDOW
            else:
                subprocess.Popen(cmd, shell=True)
        except Exception as e:
            self._append_log(f"Launch failed: {e}")

    def __init__(self):
        super().__init__()
        self.title("Minecraft Cache Warmer")
        self.geometry("840x560")

        self.detected_roots = probable_instance_dirs()

        self.limit_gb_var = tk.DoubleVar(value=8.0)
        self.dry_run_var = tk.BooleanVar(value=False)

        self._build()

        # Theme guard. Some Homebrew Python builds do not get Aqua. Pick a safe theme.
        try:
            style = ttk.Style()
            themes = style.theme_names()
            if "aqua" in themes:
                style.theme_use("aqua")
            elif "clam" in themes:
                style.theme_use("clam")
        except Exception:
            pass
        # Sanity banner so you can see the UI loaded
        banner = ttk.Label(self, text="Minecraft Cache Warmer ready", font=("Helvetica", 12, "bold"))
        banner.pack(pady=4)

        # populate roots and instances
        self._refresh_roots()

    def _build(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Detected Roots").grid(row=0, column=0, sticky="w")
        self.roots_combo = ttk.Combobox(top, state="readonly", width=90)
        self.roots_combo.grid(row=1, column=0, columnspan=3, sticky="we", pady=4)
        self.roots_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_instances())

        browse_btn = ttk.Button(top, text="Browse...", command=self._browse_root)
        browse_btn.grid(row=1, column=3, padx=6)

        mid = ttk.Frame(self, padding=(10,0,10,10))
        mid.pack(fill="both", expand=True)

        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="Instances").pack(anchor="w")
        self.instances_list = tk.Listbox(left, selectmode="extended")
        self.instances_list.pack(fill="both", expand=True, pady=4)

        right = ttk.Frame(mid)
        right.pack(side="right", fill="y")

        ttk.Label(right, text="Options").grid(row=0, column=0, sticky="w")
        ttk.Label(right, text="Read limit in GB").grid(row=1, column=0, sticky="w", pady=(6,0))
        self.limit_entry = ttk.Entry(right, textvariable=self.limit_gb_var, width=10)
        self.limit_entry.grid(row=1, column=1, sticky="w", pady=(6,0))

        self.dry_check = ttk.Checkbutton(right, text="Dry run only", variable=self.dry_run_var)
        self.dry_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6,0))

        ttk.Label(right, text="Launch after warm").grid(row=3, column=0, sticky="w", pady=(8,0))
        self.launch_after_var = tk.BooleanVar(value=False)
        self.launch_after = ttk.Checkbutton(right, variable=self.launch_after_var)
        self.launch_after.grid(row=3, column=1, sticky="w", pady=(8,0))

        ttk.Label(right, text="Launch command").grid(row=4, column=0, sticky="w")
        self.launch_cmd_var = tk.StringVar(value="")
        self.launch_cmd = ttk.Entry(right, textvariable=self.launch_cmd_var, width=42)
        self.launch_cmd.grid(row=4, column=1, sticky="we")

        self.detect_btn = ttk.Button(right, text="Detect Prism", command=self._detect_prism)
        self.detect_btn.grid(row=5, column=0, columnspan=2, sticky="we", pady=(6,0))

        self.detect_cf_btn = ttk.Button(right, text="Detect CurseForge", command=self._detect_curseforge)
        self.detect_cf_btn.grid(row=6, column=0, columnspan=2, sticky="we", pady=(6,0))

        self.open_cf_btn = ttk.Button(right, text="Open CurseForge", command=self._open_curseforge)
        self.open_cf_btn.grid(row=7, column=0, columnspan=2, sticky="we", pady=(6,0))

        self.reveal_btn = ttk.Button(right, text="Reveal Instance Folder", command=self._reveal_selected)
        self.reveal_btn.grid(row=8, column=0, columnspan=2, sticky="we", pady=(6,0))

        self.warm_btn = ttk.Button(right, text="Warm Selected", command=self._warm_selected)
        self.warm_btn.grid(row=9, column=0, columnspan=2, pady=(12,0), sticky="we")

        self.stop_btn = ttk.Button(right, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.grid(row=10, column=0, columnspan=2, pady=(6,0), sticky="we")

        logf = ttk.Frame(self, padding=(10,0,10,10))
        logf.pack(fill="both", expand=True)
        ttk.Label(logf, text="Log").pack(anchor="w")
        self.log = tk.Text(logf, height=12, wrap="none")
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

        self.progress = ttk.Progressbar(logf, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x")

        for col in range(4):
            top.grid_columnconfigure(col, weight=1)

    def _append_log(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.update_idletasks()

    def _refresh_roots(self):
        items = [str(p) for p in self.detected_roots]
        self.roots_combo["values"] = items
        if items:
            self.roots_combo.current(0)
            self._refresh_instances()

    def _browse_root(self):
        directory = filedialog.askdirectory(title="Select instances root or .minecraft folder")
        if not directory:
            return
        p = Path(directory).expanduser()
        if p.exists():
            self.detected_roots.insert(0, p)
            self._refresh_roots()

    def _refresh_instances(self):
        self.instances_list.delete(0, "end")
        sel = self.roots_combo.get()
        if not sel:
            return
        root = Path(sel)
        # If the selected root itself looks like an instance, show it
        candidates = []
        if any((root / sub).exists() for sub in ("mods", "config", "resourcepacks", ".minecraft")):
            candidates.append(root)
        # Also list subfolders that look like instances
        candidates.extend(list_instances(root))

        names = []
        for c in candidates:
            # Present simple names but store paths
            try:
                name = c.name
                if name.lower() in (".minecraft", "minecraft"):
                    name = f"{name}  {str(c)}"
                names.append((name, c))
            except Exception:
                continue

        # keep a simple mapping
        self._instance_map = {}
        for i, (name, path) in enumerate(names):
            self.instances_list.insert("end", name)
            self._instance_map[i] = path

        self._append_log(f"Found {len(names)} instance folder(s) under {root}")

    def _stop(self):
        if hasattr(self, "_stop_flag"):
            self._stop_flag = True

    def _warm_selected(self):
        sel_indices = self.instances_list.curselection()
        if not sel_indices:
            messagebox.showwarning("Nothing selected", "Select at least one instance")
            return

        limit_gb = self.limit_gb_var.get()
        dry = self.dry_run_var.get()

        targets = [self._instance_map[i] for i in sel_indices]

        self.warm_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._stop_flag = False

        def worker():
            try:
                patterns = DEFAULT_PATTERNS
                budget = int(limit_gb * 1024 * 1024 * 1024)
                warmed_total = 0
                t0 = time.time()

                total_files = 0
                temp_lists = {}
                for t in targets:
                    files = list(iter_files(t, patterns))
                    temp_lists[str(t)] = files
                    total_files += len(files)
                self.progress.configure(maximum=max(1, total_files))
                progressed = 0

                for t in targets:
                    files = temp_lists[str(t)]
                    if self._stop_flag:
                        break
                    self._append_log(f"Start {t}")
                    files = list(iter_files(t, patterns))

                    def weight(p: Path):
                        name = p.name.lower()
                        if name.endswith(".jar"):
                            return 0
                        if name.endswith(".zip"):
                            return 1
                        if name.endswith(".json") or name.endswith(".toml") or name.endswith(".cfg") or name.endswith(".ini"):
                            return 2
                        if name.endswith(".png") or name.endswith(".ogg") or name.endswith(".wav"):
                            return 3
                        return 4

                    files.sort(key=lambda p: (weight(p), -p.stat().st_size))

                    warmed = 0
                    for i, fpath in enumerate(files, 1):
                        self.progress["value"] = progressed
                        self.progress.update_idletasks()
                        if self._stop_flag:
                            break
                        size = fpath.stat().st_size
                        if dry:
                            self._append_log(f"[{i:5d}/{len(files)}] plan {fpath} {human(size)}")
                            continue
                        if warmed_total >= budget:
                            self._append_log(f"Hit limit {limit_gb} GB. Stopping.")
                            break
                        try:
                            rb = warm_file(fpath)
                            warmed += rb
                            warmed_total += rb
                            self._append_log(f"[{i:5d}] warmed {fpath} {human(rb)}  total {human(warmed_total)}")
                            progressed += 1
                        except Exception as e:
                            self._append_log(f"[{i:5d}] error {fpath}: {e}")
                        finally:
                            progressed += 1

                    self._append_log(f"Done {t} warmed {human(warmed)}")

                dt = time.time() - t0
                self._append_log(f"All done in {dt:.1f}s. Total warmed {human(warmed_total)}")
                self.progress["value"] = self.progress["maximum"]
                self._maybe_launch(targets)
            finally:
                self.warm_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")

        threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
    app = CacheWarmerGUI()
    app.mainloop()
