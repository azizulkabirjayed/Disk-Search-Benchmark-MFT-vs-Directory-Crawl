#!/usr/bin/env python3
"""
benchmark_app.py - Disk Search Benchmark: MFT vs Directory Crawl

Compares two ways of enumerating every file on all fixed drives:

  * Directory Crawl - os.walk() from each drive root (normal Win32 listing APIs).
  * MFT Parse       - opens the raw volume (\\\\.\\C:) with CreateFileW via ctypes,
                      locates the Master File Table from the NTFS boot sector,
                      follows the $MFT data runs and reads it in 64 KB blocks with
                      ReadFile, parsing each 1,024-byte FILE record header.

Single file, standard library only. Windows only; run as Administrator.
"""

import os
import sys
import time
import queue
import struct
import ctypes
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

IS_WINDOWS = sys.platform == "win32"
BLOCK_SIZE = 64 * 1024          # bytes per ReadFile call
UI_UPDATE_INTERVAL = 0.05       # seconds between progress messages from workers


# --------------------------------------------------------------------------- #
#  Win32 API bindings (ctypes)                                                 #
# --------------------------------------------------------------------------- #
if IS_WINDOWS:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_BEGIN = 0
    MEM_COMMIT = 0x1000
    MEM_RESERVE = 0x2000
    MEM_RELEASE = 0x8000
    PAGE_READWRITE = 0x04
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL

    kernel32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE, ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
    ]
    kernel32.SetFilePointerEx.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.VirtualAlloc.argtypes = [
        wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD,
    ]
    kernel32.VirtualAlloc.restype = wintypes.LPVOID

    kernel32.VirtualFree.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
    kernel32.VirtualFree.restype = wintypes.BOOL

    shell32.IsUserAnAdmin.argtypes = []
    shell32.IsUserAnAdmin.restype = wintypes.BOOL

    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE


def is_admin():
    if not IS_WINDOWS:
        return False
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """Re-run this script elevated through the UAC 'runas' verb."""
    script = os.path.abspath(sys.argv[0])
    params = subprocess.list2cmdline([script] + sys.argv[1:])
    rc = shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    return (rc or 0) > 32


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def list_fixed_drives():
    """Letters of every fixed drive (internal/external HDD or SSD), e.g. ['C', 'J']."""
    if not IS_WINDOWS:
        return []
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    return [chr(65 + i) for i in range(26)
            if mask & (1 << i) and k32.GetDriveTypeW("%s:\\" % chr(65 + i)) == 3]


def apply_dark_title_bar(root):
    """Ask Windows 10/11 to draw a dark title bar for this Tk window."""
    if not IS_WINDOWS:
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (newer / older builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


class RawVolume:
    """Read-only handle to a raw NTFS volume with a page-aligned read buffer."""

    def __init__(self, drive_letter):
        self.path = "\\\\.\\%s:" % drive_letter.rstrip(":\\/").upper()
        handle = kernel32.CreateFileW(
            self.path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, 0, None,
        )
        if not handle or handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        # Raw volume I/O must be sector-aligned; VirtualAlloc gives page alignment.
        self.buf = kernel32.VirtualAlloc(None, BLOCK_SIZE, MEM_COMMIT | MEM_RESERVE,
                                         PAGE_READWRITE)
        if not self.buf:
            err = ctypes.get_last_error()
            kernel32.CloseHandle(self.handle)
            raise ctypes.WinError(err)

    def read_at(self, offset, size):
        if size > BLOCK_SIZE:
            raise ValueError("read size exceeds buffer")
        if not kernel32.SetFilePointerEx(self.handle, offset, None, FILE_BEGIN):
            raise ctypes.WinError(ctypes.get_last_error())
        got = wintypes.DWORD(0)
        if not kernel32.ReadFile(self.handle, self.buf, size, ctypes.byref(got), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(self.buf, got.value)

    def close(self):
        if getattr(self, "buf", None):
            kernel32.VirtualFree(self.buf, 0, MEM_RELEASE)
            self.buf = None
        if getattr(self, "handle", None):
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------------- #
#  NTFS parsing helpers                                                        #
# --------------------------------------------------------------------------- #
def parse_boot_sector(data):
    if data[3:11] != b"NTFS    ":
        raise ValueError("The volume is not formatted as NTFS (boot sector OEM ID mismatch).")
    bytes_per_sector = struct.unpack_from("<H", data, 0x0B)[0]
    spc_raw = data[0x0D]
    sectors_per_cluster = spc_raw if spc_raw <= 0x80 else 1 << (256 - spc_raw)
    cluster_size = bytes_per_sector * sectors_per_cluster
    mft_lcn = struct.unpack_from("<q", data, 0x30)[0]
    cpr = struct.unpack_from("<b", data, 0x40)[0]
    record_size = cpr * cluster_size if cpr > 0 else 1 << (-cpr)
    return {
        "bytes_per_sector": bytes_per_sector,
        "cluster_size": cluster_size,
        "mft_lcn": mft_lcn,
        "mft_offset": mft_lcn * cluster_size,
        "record_size": record_size,
    }


def apply_fixups(record):
    """Restore the last two bytes of every sector using the update sequence array."""
    rec = bytearray(record)
    usa_off, usa_cnt = struct.unpack_from("<HH", rec, 4)
    if usa_cnt < 2:
        return rec
    stride = len(rec) // (usa_cnt - 1)
    usn = rec[usa_off:usa_off + 2]
    for i in range(1, usa_cnt):
        end = i * stride - 2
        if end + 2 > len(rec):
            break
        if rec[end:end + 2] != usn:
            raise ValueError("MFT record fixup mismatch (torn or corrupt record).")
        rec[end:end + 2] = rec[usa_off + 2 * i:usa_off + 2 * i + 2]
    return rec


def parse_data_runs(rec, attr_off):
    runs_off = struct.unpack_from("<H", rec, attr_off + 0x20)[0]
    pos = attr_off + runs_off
    lcn = 0
    runs = []
    while pos < len(rec):
        header = rec[pos]
        if header == 0:
            break
        len_sz, off_sz = header & 0x0F, header >> 4
        pos += 1
        length = int.from_bytes(rec[pos:pos + len_sz], "little")
        pos += len_sz
        if off_sz:
            lcn += int.from_bytes(rec[pos:pos + off_sz], "little", signed=True)
            pos += off_sz
            runs.append((lcn, length))
        else:
            runs.append((None, length))      # sparse run
    return runs


def find_mft_data_runs(record0):
    """Return (runs, real_size) of the unnamed non-resident $DATA attribute in $MFT."""
    if record0[:4] != b"FILE":
        raise ValueError("MFT record 0 does not start with 'FILE' magic.")
    rec = apply_fixups(record0)
    off = struct.unpack_from("<H", rec, 0x14)[0]
    while off + 16 <= len(rec):
        atype, alen = struct.unpack_from("<II", rec, off)
        if atype == 0xFFFFFFFF or alen == 0:
            break
        if atype == 0x80 and rec[off + 8] == 1 and rec[off + 9] == 0:
            real_size = struct.unpack_from("<q", rec, off + 0x30)[0]
            return parse_data_runs(rec, off), real_size
        off += alen
    raise ValueError("Could not find the $DATA attribute of $MFT.")


# --------------------------------------------------------------------------- #
#  Benchmarks (run in a worker thread)                                         #
# --------------------------------------------------------------------------- #
def crawl_benchmark(path, cancel, post):
    files = dirs = errors = 0

    def on_error(_exc):
        nonlocal errors
        errors += 1

    t0 = time.perf_counter()
    last = t0
    cancelled = False
    for root, dnames, fnames in os.walk(path, onerror=on_error):
        dirs += len(dnames)
        files += len(fnames)
        if cancel.is_set():
            cancelled = True
            break
        now = time.perf_counter()
        if now - last >= UI_UPDATE_INTERVAL:
            last = now
            post({"elapsed": now - t0, "files": files, "dirs": dirs,
                  "errors": errors, "current": root})
    elapsed = time.perf_counter() - t0
    return {"elapsed": elapsed, "files": files, "dirs": dirs, "errors": errors,
            "items": files + dirs, "cancelled": cancelled, "scope": path}


def mft_benchmark(drive, cancel, post):
    t0 = time.perf_counter()
    with RawVolume(drive) as vol:
        info = parse_boot_sector(vol.read_at(0, 4096))
        rs, bps, cs = info["record_size"], info["bytes_per_sector"], info["cluster_size"]
        first_read = ((max(rs, bps) + bps - 1) // bps) * bps
        record0 = vol.read_at(info["mft_offset"], first_read)[:rs]
        runs, real_size = find_mft_data_runs(record0)

        covered = sum(length for _, length in runs) * cs
        partial = covered < real_size          # $MFT split via $ATTRIBUTE_LIST
        total = min(real_size, covered) // rs

        processed = valid = in_use = files = dirs = ext = bad = 0
        bytes_read = 0
        zero6 = b"\x00" * 6
        last = t0
        cancelled = False

        for lcn, clusters in runs:
            if processed >= total or cancelled:
                break
            run_bytes = clusters * cs
            if lcn is None:                    # sparse: nothing on disk
                processed = min(total, processed + run_bytes // rs)
                continue
            start = lcn * cs
            pos = 0
            while pos < run_bytes and processed < total:
                chunk = min(BLOCK_SIZE, run_bytes - pos)
                data = vol.read_at(start + pos, chunk)
                if not data:
                    break
                bytes_read += len(data)
                n = min(len(data) // rs, total - processed)
                for base in range(0, n * rs, rs):
                    magic = data[base:base + 4]
                    if magic == b"FILE":
                        valid += 1
                        flags = data[base + 0x16]
                        if flags & 0x01:                       # record in use
                            if data[base + 0x20:base + 0x26] != zero6:
                                ext += 1                       # extension record
                            elif flags & 0x02:
                                dirs += 1
                            else:
                                files += 1
                            in_use += 1
                    elif magic == b"BAAD":
                        bad += 1
                processed += n
                pos += chunk

                if cancel.is_set():
                    cancelled = True
                    break
                now = time.perf_counter()
                if now - last >= UI_UPDATE_INTERVAL:
                    last = now
                    post({"elapsed": now - t0, "processed": processed, "total": total,
                          "files": files, "dirs": dirs, "ext": ext,
                          "bytes_read": bytes_read})

    elapsed = time.perf_counter() - t0
    return {"elapsed": elapsed, "processed": processed, "total": total,
            "valid": valid, "in_use": in_use, "files": files, "dirs": dirs,
            "ext": ext, "bad": bad, "bytes_read": bytes_read,
            "items": files + dirs, "cancelled": cancelled, "partial": partial,
            "record_size": rs, "cluster_size": cs, "mft_size": real_size,
            "extents": len(runs), "scope": "%s:\\ (entire volume)" % drive}


# --------------------------------------------------------------------------- #
#  Formatting helpers                                                          #
# --------------------------------------------------------------------------- #
def fmt_int(n):
    return "{:,}".format(int(n))


def fmt_time(s):
    return "%.0f ms" % (s * 1000) if s < 1 else "%.2f s" % s


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def fmt_rate(items, seconds):
    return fmt_int(items / seconds) + " /s" if seconds > 0 else "—"


def shorten(text, width=58):
    return text if len(text) <= width else "…" + text[-(width - 1):]


# --------------------------------------------------------------------------- #
#  GUI                                                                         #
# --------------------------------------------------------------------------- #
class BenchmarkApp:
    C = {
        "bg": "#0a0a0c", "card": "#121216", "row": "#16161b", "row_alt": "#111115",
        "border": "#24242b", "text": "#e8e8ec", "muted": "#8b8b96", "dim": "#5d5d68",
        "trough": "#1c1c22", "crawl": "#60a5fa", "mft": "#34d399",
        "accent": "#6366f1", "accent_hover": "#7477f4", "accent_press": "#4f46e5",
        "stop": "#dc2626", "stop_hover": "#ef4444", "stop_press": "#b91c1c",
    }
    ROWS = [("time", "Time"), ("items", "Items found"), ("files", "Files"),
            ("dirs", "Folders"), ("rate", "Items / second"),
            ("speed", "Relative speed"), ("scope", "Drives scanned")]
    MFT_WEIGHT, CRAWL_WEIGHT = 1.0, 3.0     # share of the combined progress bar

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.running = False
        self.started = 0.0
        self.target = 0.0          # progress the worker reported (0..1)
        self.shown = 0.0           # progress currently drawn (eases toward target)
        self.drives = list_fixed_drives()

        root.title("Disk Search Benchmark: MFT vs Directory Crawl")
        root.geometry("780x540")
        root.minsize(660, 480)
        root.configure(bg=self.C["bg"])
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._setup_style()
        self._build_ui()
        apply_dark_title_bar(root)
        root.after(16, self._animate)
        root.after(40, self._pump_queue)
        root.after(250, self._check_environment)

    # ---------------- style ---------------- #
    def _setup_style(self):
        c = self.C
        s = self.style = ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=c["bg"], foreground=c["text"], font=("Segoe UI", 10),
                    bordercolor=c["border"], focuscolor=c["bg"])
        s.configure("App.TFrame", background=c["bg"])
        s.configure("Card.TFrame", background=c["card"])
        s.configure("Border.TFrame", background=c["border"])
        s.configure("Status.TLabel", background=c["bg"], foreground=c["text"],
                    font=("Segoe UI Semibold", 11))
        s.configure("Hint.TLabel", background=c["bg"], foreground=c["muted"], font=("Segoe UI", 9))
        s.configure("Pct.TLabel", background=c["bg"], foreground=c["muted"],
                    font=("Segoe UI Semibold", 10))
        s.configure("Head.TLabel", background=c["card"])
        s.configure("HeadCrawl.TLabel", background=c["card"], foreground=c["crawl"],
                    font=("Segoe UI Semibold", 11))
        s.configure("HeadMft.TLabel", background=c["card"], foreground=c["mft"],
                    font=("Segoe UI Semibold", 11))
        for i, bg in enumerate((c["row"], c["row_alt"])):
            s.configure("Key%d.TLabel" % i, background=bg, foreground=c["muted"])
            s.configure("Val%d.TLabel" % i, background=bg, foreground=c["text"],
                        font=("Segoe UI Semibold", 11))
        s.configure("Combined.Horizontal.TProgressbar", troughcolor=c["trough"],
                    bordercolor=c["trough"], background=c["accent"],
                    lightcolor=c["accent"], darkcolor=c["accent"], thickness=10)
        self._button_style("Run.TButton", c["accent"], c["accent_hover"], c["accent_press"])
        self._button_style("Stop.TButton", c["stop"], c["stop_hover"], c["stop_press"])

    def _button_style(self, name, base, hover, press):
        s = self.style
        s.configure(name, background=base, foreground="#ffffff", bordercolor=base,
                    lightcolor=base, darkcolor=base, focuscolor=base, focusthickness=0,
                    font=("Segoe UI Semibold", 11), padding=(30, 10), relief="flat")
        states = [("disabled", "#26262d"), ("pressed", press), ("active", hover)]
        s.map(name, background=states, bordercolor=states, lightcolor=states,
              darkcolor=states, focuscolor=states, foreground=[("disabled", self.C["dim"])])

    # ---------------- layout ---------------- #
    def _build_ui(self):
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(24, 22))
        outer.pack(fill="both", expand=True)

        # Run button + status
        top = ttk.Frame(outer, style="App.TFrame")
        top.pack(fill="x")
        self.btn = ttk.Button(top, text="Run", style="Run.TButton", width=8,
                              command=self._toggle, cursor="hand2")
        self.btn.pack(side="left")
        info = ttk.Frame(top, style="App.TFrame")
        info.pack(side="left", fill="x", expand=True, padx=(18, 0))
        self.status_var = tk.StringVar(value="Ready")
        self.detail_var = tk.StringVar(value=self._drives_text())
        ttk.Label(info, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w")
        ttk.Label(info, textvariable=self.detail_var, style="Hint.TLabel").pack(
            anchor="w", pady=(2, 0))

        # Combined progress bar
        prog = ttk.Frame(outer, style="App.TFrame")
        prog.pack(fill="x", pady=(20, 0))
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, style="Combined.Horizontal.TProgressbar",
                                   mode="determinate", maximum=1000)
        self.bar.grid(row=0, column=0, sticky="ew")
        self.pct_var = tk.StringVar(value="0%")
        ttk.Label(prog, textvariable=self.pct_var, style="Pct.TLabel", width=5,
                  anchor="e").grid(row=0, column=1, padx=(14, 0))

        # Comparison table
        border = ttk.Frame(outer, style="Border.TFrame", padding=1)
        border.pack(fill="both", expand=True, pady=(22, 0))
        table = ttk.Frame(border, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        for col, weight in ((0, 4), (1, 3), (2, 3)):
            table.columnconfigure(col, weight=weight, uniform="cols")
        heads = (("", "Head.TLabel"), ("Directory Crawl", "HeadCrawl.TLabel"),
                 ("MFT Parse", "HeadMft.TLabel"))
        for col, (text, style) in enumerate(heads):
            ttk.Label(table, text=text, style=style, padding=(18, 14)).grid(
                row=0, column=col, sticky="nsew")
        ttk.Frame(table, style="Border.TFrame", height=1).grid(
            row=1, column=0, columnspan=3, sticky="ew")
        self.cells = {}
        for i, (key, label) in enumerate(self.ROWS):
            z, r = i % 2, i + 2
            ttk.Label(table, text=label, style="Key%d.TLabel" % z, padding=(18, 10)).grid(
                row=r, column=0, sticky="nsew")
            for col, method in ((1, "crawl"), (2, "mft")):
                var = tk.StringVar(value="—")
                ttk.Label(table, textvariable=var, style="Val%d.TLabel" % z,
                          padding=(18, 10)).grid(row=r, column=col, sticky="nsew")
                self.cells[(method, key)] = var
            table.rowconfigure(r, weight=1)

    def _drives_text(self):
        if not self.drives:
            return "No fixed drives detected"
        return "Scans every fixed drive: " + "   ".join(d + ":\\" for d in self.drives)

    # ---------------- environment ---------------- #
    def _check_environment(self):
        if not IS_WINDOWS:
            messagebox.showerror("Unsupported platform",
                                 "This benchmark uses the Win32 API and NTFS, so it only "
                                 "runs on Windows.")
            return
        if not is_admin():
            if messagebox.askyesno(
                    "Administrator privileges required",
                    "Parsing the MFT means opening each drive's raw volume (e.g. \\\\.\\C:), "
                    "which Windows only allows for administrators.\n\n"
                    "Relaunch this app as Administrator now?", icon="warning"):
                if relaunch_as_admin():
                    self.root.destroy()
                    return
                messagebox.showerror("Elevation failed",
                                     "Could not relaunch with Administrator privileges "
                                     "(the UAC prompt may have been declined).")

    # ---------------- run control ---------------- #
    def _toggle(self):
        if self.running:
            self.cancel.set()
            self.btn.configure(state="disabled")
            self.status_var.set("Stopping…")
        else:
            self._start()

    def _start(self):
        if not IS_WINDOWS:
            messagebox.showerror("Unsupported platform", "This benchmark requires Windows.")
            return
        if not is_admin():
            messagebox.showerror(
                "Administrator privileges required",
                "The MFT test reads raw volumes (\\\\.\\C:), which needs Administrator "
                "rights.\n\nClose the app and start it with 'Run as administrator'.")
            return
        self.drives = list_fixed_drives()
        if not self.drives:
            messagebox.showerror("No drives", "No fixed drives were found to scan.")
            return
        for var in self.cells.values():
            var.set("—")
        self.target = self.shown = 0.0
        self.cancel.clear()
        self.running = True
        self.started = time.perf_counter()
        self.btn.configure(text="Stop", style="Stop.TButton", state="normal")
        self.status_var.set("Starting…")
        self.detail_var.set(self._drives_text())
        self.worker = threading.Thread(target=self._worker, args=(list(self.drives),),
                                       daemon=True)
        self.worker.start()

    def _worker(self, drives):
        """MFT first (fast, and its item counts give the crawl an accurate progress
        estimate), then os.walk() over every drive root."""
        put = self.q.put
        total_w = len(drives) * (self.MFT_WEIGHT + self.CRAWL_WEIGHT)
        done_w = 0.0
        agg = {"mft": {"elapsed": 0.0, "files": 0, "dirs": 0, "drives": [], "skipped": []},
               "crawl": {"elapsed": 0.0, "files": 0, "dirs": 0, "drives": []}}
        expected, problems = {}, []

        def live(method, p):
            a = agg[method]
            return {"elapsed": a["elapsed"] + p["elapsed"],
                    "files": a["files"] + p["files"], "dirs": a["dirs"] + p["dirs"]}

        def add(method, r, d):
            a = agg[method]
            a["elapsed"] += r["elapsed"]
            a["files"] += r["files"]
            a["dirs"] += r["dirs"]
            a["drives"].append(d)

        for d in drives:                                   # phase 1: MFT
            if self.cancel.is_set():
                break
            put(("phase", "mft", "Parsing the MFT of %s:\\" % d))

            def on_mft(p, base=done_w):
                frac = p["processed"] / max(1, p["total"])
                put(("progress", (base + frac * self.MFT_WEIGHT) / total_w, "mft",
                     live("mft", p), None))
            try:
                r = mft_benchmark(d, self.cancel, on_mft)
                add("mft", r, d)
                expected[d] = r["items"]
            except ValueError:                             # not NTFS
                agg["mft"]["skipped"].append(d)
            except Exception as exc:
                agg["mft"]["skipped"].append(d)
                problems.append("%s:  MFT parse failed — %s" % (d, exc))
            done_w += self.MFT_WEIGHT
            if not self.cancel.is_set():
                put(("progress", done_w / total_w, "mft", dict(agg["mft"]), None))

        for d in drives:                                   # phase 2: os.walk
            if self.cancel.is_set():
                break
            put(("phase", "crawl", "Crawling %s:\\ with os.walk()" % d))

            def on_crawl(p, base=done_w, exp=expected.get(d)):
                n = p["files"] + p["dirs"]
                frac = min(0.99, n / exp) if exp else n / (n + 50000.0)
                put(("progress", (base + frac * self.CRAWL_WEIGHT) / total_w, "crawl",
                     live("crawl", p), p["current"]))
            try:
                add("crawl", crawl_benchmark(d + ":\\", self.cancel, on_crawl), d)
            except Exception as exc:
                problems.append("%s:  directory crawl failed — %s" % (d, exc))
            done_w += self.CRAWL_WEIGHT
            if not self.cancel.is_set():
                put(("progress", done_w / total_w, "crawl", dict(agg["crawl"]), None))

        put(("finished", agg, self.cancel.is_set(), problems))

    # ---------------- queue → UI ---------------- #
    def _pump_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "phase":
                    self._on_phase(msg[1], msg[2])
                elif kind == "progress":
                    _, frac, method, data, current = msg
                    self.target = max(self.target, min(1.0, frac))
                    self._set_live(method, data)
                    if current:
                        self.detail_var.set(shorten(current, 90))
                elif kind == "finished":
                    self._finish(*msg[1:])
        except queue.Empty:
            pass
        self.root.after(40, self._pump_queue)

    def _on_phase(self, method, text):
        color = self.C[method]
        self.style.configure("Combined.Horizontal.TProgressbar", background=color,
                             lightcolor=color, darkcolor=color)
        self.status_var.set(text)
        if method == "mft":
            self.detail_var.set("Raw volume read · 64 KB blocks · 1 KB FILE records")

    def _animate(self):
        """Ease the bar toward the reported progress for smooth motion (~60 fps)."""
        diff = self.target - self.shown
        if diff:
            self.shown = self.target if abs(diff) < 0.0005 else self.shown + diff * 0.15
            self.bar["value"] = self.shown * 1000
            self.pct_var.set("%d%%" % round(self.shown * 100))
        self.root.after(16, self._animate)

    def _set_live(self, method, m):
        items = m["files"] + m["dirs"]
        cell = self.cells
        cell[(method, "time")].set(fmt_time(m["elapsed"]))
        cell[(method, "items")].set(fmt_int(items))
        cell[(method, "files")].set(fmt_int(m["files"]))
        cell[(method, "dirs")].set(fmt_int(m["dirs"]))
        cell[(method, "rate")].set(fmt_rate(items, m["elapsed"]))

    def _finish(self, agg, cancelled, problems):
        self.running = False
        self.btn.configure(text="Run", style="Run.TButton", state="normal")
        c, m = agg["crawl"], agg["mft"]
        for key, a in (("crawl", c), ("mft", m)):
            if a["drives"]:
                self._set_live(key, a)
        self.cells[("crawl", "scope")].set(", ".join(d + ":" for d in c["drives"]) or "—")
        mft_scope = ", ".join(d + ":" for d in m["drives"]) or "—"
        if m["skipped"]:
            mft_scope += "  (skipped " + ", ".join(d + ":" for d in m["skipped"]) + ")"
        self.cells[("mft", "scope")].set(mft_scope)

        c_rate = (c["files"] + c["dirs"]) / c["elapsed"] if c["elapsed"] > 0 else 0
        m_rate = (m["files"] + m["dirs"]) / m["elapsed"] if m["elapsed"] > 0 else 0
        verdict = ""
        if c_rate and m_rate:
            self.cells[("crawl", "speed")].set("1.0×")
            self.cells[("mft", "speed")].set("%.1f×" % (m_rate / c_rate))
            if m_rate >= c_rate:
                verdict = "MFT parse enumerated items %.1f× faster than os.walk()" % (
                    m_rate / c_rate)
            else:
                verdict = "os.walk() enumerated items %.1f× faster than MFT parse" % (
                    c_rate / m_rate)

        total = fmt_time(time.perf_counter() - self.started)
        if cancelled:
            self.status_var.set("Stopped after %s" % total)
            self.detail_var.set("Partial results shown below")
        else:
            self.target = 1.0
            self.status_var.set("Finished in %s" % total)
            self.detail_var.set(verdict or self._drives_text())
        if problems:
            messagebox.showwarning("Some drives could not be scanned", "\n".join(problems))

    def _on_close(self):
        self.cancel.set()
        self.root.destroy()


def main():
    if IS_WINDOWS:
        enable_dpi_awareness()
    root = tk.Tk()
    BenchmarkApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
