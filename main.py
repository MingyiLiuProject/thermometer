# -*- coding: utf-8 -*-
"""
Mi Thermometer 2 (LYWSD03MMC) — Integrated Collector + GUI
-----------------------------------------------------------
- BLE collector (Bleak) + Tk GUI in one process.
- Two devices, append-only logs per location:
    logs/Lab_inside.csv
    logs/Lab_outside.csv
- Timestamps: real local time with timezone (ISO 8601).
- GUI: big numeric Temp/Humidity + rolling charts.
- X-axis auto-format:
    ≤ 1 day   -> HH:MM:SS
    > 1 day   -> MM-DD HH:MM
    > 5 days  -> YYYY-MM-DD
- X-axis labels rotated/right-aligned; extra bottom margin to prevent clipping.
"""

import asyncio
import csv
import os
import threading
import datetime as dt
from datetime import timedelta
import glob
import tkinter as tk
from tkinter import ttk
from collections import deque
from typing import Dict, Optional, List, Tuple

from bleak import BleakScanner, BleakClient, BLEDevice
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import matplotlib.dates as mdates

# ========================= Collector Configuration =========================

# (Recommended) bind your two device MAC addresses (stabilizes matching)
KNOWN_ADDRS = {
    "A4:C1:38:DD:AC:A7",  # Lab outside
    "A4:C1:38:B9:74:3C",  # Lab inside
}
KNOWN_ADDRS = {a.replace("-", ":").upper() for a in KNOWN_ADDRS if a}

# Map MAC -> friendly display/log name
DISPLAY_NAMES: Dict[str, str] = {
    "A4:C1:38:B9:74:3C": "Lab inside",
    "A4:C1:38:DD:AC:A7": "Lab outside",
}

TARGET_NAME_KEYWORDS = ["LYWSD03MMC", "MJ_HT_V1", "Xiaomi"]
TARGET_COUNT = 2
UUID_TH_NOTIFY = "ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6"

LOG_DIR = "logs"
RECONNECT_DELAY_SECONDS = 5.0

# ========================= Collector Helpers =========================

def battery_percent_from_mv(mv: int) -> int:
    v = mv / 1000.0
    pct = int(round((v - 2.1) / (3.1 - 2.1) * 100))
    return max(0, min(100, pct))

def decode_thv(payload: bytes):
    if len(payload) < 5:
        return None
    t_c = int.from_bytes(payload[0:2], "little", signed=False) / 100.0
    rh = payload[2]
    mv = int.from_bytes(payload[3:5], "little", signed=False)
    return t_c, rh, mv, battery_percent_from_mv(mv)

def match_target(name: Optional[str], address: str) -> bool:
    addr_ok = address.replace("-", ":").upper() in KNOWN_ADDRS if KNOWN_ADDRS else False
    name_ok = bool(name) and any(kw in name for kw in TARGET_NAME_KEYWORDS)
    return addr_ok or name_ok

async def discover_targets(timeout: float = 60.0, want: int = TARGET_COUNT) -> List[BLEDevice]:
    found: Dict[str, BLEDevice] = {}

    def cb(d, adv):
        addr = (d.address or "").replace("-", ":").upper()
        if match_target(d.name, addr):
            found[addr] = d

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    try:
        for _ in range(int(timeout * 10)):
            if len(found) >= want:
                break
            await asyncio.sleep(0.1)
    finally:
        await scanner.stop()

    devices = list(found.values())
    print(f"Discovered {len(devices)} target(s):")
    for d in devices:
        print(f"  - {d.address}  {d.name}")
    return devices[:want]

def _friendly_csv_path_for_addr(addr_colon: str) -> str:
    name = DISPLAY_NAMES.get(addr_colon, addr_colon).replace(" ", "_")
    return os.path.join(LOG_DIR, f"{name}.csv")

async def device_task(ble_device: BLEDevice, stop_event: asyncio.Event):
    """Connect one device, subscribe, and keep appending to friendly log file."""
    addr_colon = ble_device.address.replace("-", ":").upper()
    csv_path = _friendly_csv_path_for_addr(addr_colon)

    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(csv_path):
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp_iso", "temperature_c", "humidity_pct", "voltage_mv", "battery_pct"])

    friendly = DISPLAY_NAMES.get(addr_colon, addr_colon)

    while not stop_event.is_set():
        try:
            print(f"[ {friendly} ] Connecting ...")
            async with BleakClient(ble_device, timeout=30.0) as client:
                if not client.is_connected:
                    raise RuntimeError("connected flag false")
                print(f"[ {friendly} ] Connected. Subscribing...")

                await asyncio.sleep(1.0)
                services = client.services
                ok = any(
                    ch.uuid.lower() == UUID_TH_NOTIFY and ("notify" in ch.properties or "indicate" in ch.properties)
                    for s in services for ch in s.characteristics
                )
                if not ok:
                    raise RuntimeError(f"notify characteristic not found: {UUID_TH_NOTIFY}")

                def handler(_sender, data: bytearray):
                    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
                    parsed = decode_thv(data)
                    if not parsed:
                        print(f"[ {friendly} ] [{now_iso}] raw={data.hex()} (len={len(data)})")
                        return
                    t_c, rh, mv, pct = parsed
                    try:
                        with open(csv_path, "a", newline="", encoding="utf-8") as f:
                            w = csv.writer(f)
                            w.writerow([now_iso, f"{t_c:.2f}", str(int(rh)), str(mv), str(pct)])
                    except Exception as e:
                        print(f"[ {friendly} ] write csv failed: {e}")
                    print(f"[ {friendly} ] [{now_iso}] T={t_c:.2f} °C, RH={int(rh):3d} %, V={mv/1000:.3f} V (~{pct} %)")

                await client.start_notify(UUID_TH_NOTIFY, handler)
                print(f"[ {friendly} ] Listening...")

                while not stop_event.is_set():
                    await asyncio.sleep(1.0)

                try:
                    await client.stop_notify(UUID_TH_NOTIFY)
                except Exception:
                    pass
                print(f"[ {friendly} ] Stopped by request.")
                return
        except Exception as e:
            print(f"[ {friendly} ] error: {e}")
            if stop_event.is_set():
                break
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

async def collector_main(stop_event: asyncio.Event):
    devices = await discover_targets(timeout=30.0, want=TARGET_COUNT)
    if not devices:
        print("No target device found. Get closer, tap the device button, close Mi Home / other apps, then retry.")
        return
    tasks = [asyncio.create_task(device_task(d, stop_event)) for d in devices]
    print("\n=== Collector started ===")
    print("Log dir:", os.path.abspath(LOG_DIR))
    print("Files: Lab_inside.csv, Lab_outside.csv (append-only)\n")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass

# ========================= GUI (reads CSV and plots) =========================

FILE_PATTERN = os.path.join(LOG_DIR, "*.csv")
GUI_REFRESH_MS = 2000          # change this to adjust GUI refresh interval (ms)
ROLLING_POINTS = 600           # number of points kept in chart

def list_device_files() -> Dict[str, str]:
    files = sorted(glob.glob(FILE_PATTERN))
    return {os.path.splitext(os.path.basename(f))[0]: f for f in files}

def pick_two(files_map: Dict[str, str]) -> Dict[str, str]:
    preferred = ["Lab_inside", "Lab_outside"]
    chosen: Dict[str, str] = {}
    for p in preferred:
        if p in files_map and len(chosen) < 2:
            chosen[p] = files_map[p]
    if len(chosen) < 2:
        for k, v in files_map.items():
            if k not in chosen:
                chosen[k] = v
            if len(chosen) >= 2:
                break
    return chosen

def read_last(path: str) -> Optional[dict]:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    last = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.reader(f):
                if row: last = row
    except Exception:
        return None
    if not last or last[0] == "timestamp_iso":
        return None
    try:
        return {
            "timestamp_iso": last[0],
            "temperature_c": float(last[1]),
            "humidity_pct": float(last[2]),
            "voltage_mv": float(last[3]),
            "battery_pct": float(last[4]),
        }
    except Exception:
        return None

def read_tail(path: str, max_rows: int = ROLLING_POINTS) -> List[Tuple[dt.datetime, float, float]]:
    out: deque = deque(maxlen=max_rows)
    try:
        with open(path, "r", encoding="utf-8") as f:
            r = csv.reader(f)
            header = next(r, None)
            for row in r:
                if len(row) < 5:
                    continue
                try:
                    tstamp = dt.datetime.fromisoformat(row[0])  # aware datetime with tz
                    temp = float(row[1])
                    hum = float(row[2])
                except Exception:
                    continue
                out.append((tstamp, temp, hum))
    except Exception:
        pass
    return list(out)

class DevicePanel:
    def __init__(self, parent: tk.Frame, title: str, csv_path: str):
        self.csv_path = csv_path
        self.wrap = tk.Frame(parent, bg="white", highlightbackground="#000", highlightthickness=1)

        head = tk.Frame(self.wrap, bg="white"); head.pack(fill=tk.X, padx=10, pady=(8, 0))
        tk.Label(head, text=title, bg="white", fg="black",
                 font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)
        self.meta = tk.Label(head, text="", bg="white", fg="#333", font=("Consolas", 10))
        self.meta.pack(side=tk.RIGHT)

        nums = tk.Frame(self.wrap, bg="white", highlightbackground="#000", highlightthickness=1)
        nums.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        left = tk.Frame(nums, bg="white"); left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        right = tk.Frame(nums, bg="white"); right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Bigger, centered numbers; split halves
        tk.Label(left, text="Temperature (°C)", bg="white", fg="black",
                 font=("Segoe UI", 14, "bold")).pack(anchor="center", pady=(5, 0))
        self.temp_val = tk.Label(left, text="--.-", bg="white", fg="black",
                                 font=("Segoe UI", 100, "bold"))
        self.temp_val.pack(anchor="center", expand=True)

        tk.Label(right, text="Humidity (%)", bg="white", fg="black",
                 font=("Segoe UI", 14, "bold")).pack(anchor="center", pady=(5, 0))
        self.humi_val = tk.Label(right, text="--", bg="white", fg="black",
                                 font=("Segoe UI", 100, "bold"))
        self.humi_val.pack(anchor="center", expand=True)

        chart = tk.Frame(self.wrap, bg="white", highlightbackground="#000", highlightthickness=1)
        chart.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # Slightly taller figure to avoid cramped labels
        self.fig = Figure(figsize=(5, 3.2), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Local Time")
        self.ax.set_ylabel("Temp / Humidity")
        self.ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.5)
        self.temp_line, = self.ax.plot([], [], label="Temp (°C)")
        self.humi_line, = self.ax.plot([], [], label="Humidity (%)")
        self.ax.legend(loc="upper left")

        # Default formatter; will be refined dynamically each refresh
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        self.ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=5))

        self.canvas = FigureCanvasTkAgg(self.fig, master=chart)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Reserve extra bottom margin to prevent clipping
        self.fig.subplots_adjust(bottom=0.28)

    def grid(self, **kwargs):
        self.wrap.grid(**kwargs)

    def _set_xaxis_formatter(self, xs: List[dt.datetime]):
        """Pick X-axis formatter based on total time span in data."""
        if not xs:
            return
        xs_sorted = sorted(xs)
        span = (xs_sorted[-1] - xs_sorted[0]).total_seconds()
        one_day = 24 * 3600
        five_days = 5 * one_day

        if span > five_days:
            fmt = mdates.DateFormatter("%Y-%m-%d")       # date only
        elif span > one_day:
            fmt = mdates.DateFormatter("%m-%d %H:%M")    # date + hour
        else:
            fmt = mdates.DateFormatter("%H:%M:%S")       # time only

        self.ax.xaxis.set_major_formatter(fmt)
        self.ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=5))

        # Rotate & right-align to avoid overlap/cutoff
        for label in self.ax.get_xticklabels():
            label.set_rotation(30)
            label.set_ha("right")

    def refresh(self):
        last = read_last(self.csv_path)
        if last:
            self.temp_val.config(text=f"{last['temperature_c']:.1f}")
            self.humi_val.config(text=f"{int(last['humidity_pct']):d}")
            self.meta.config(
                text=f"{last['timestamp_iso']}  |  {last['battery_pct']:.0f}%  |  {last['voltage_mv']/1000:.3f} V"
            )

        data = read_tail(self.csv_path, max_rows=ROLLING_POINTS)
        if not data:
            return

        xs = [t for (t, _, __) in data]   # tz-aware datetimes
        temps = [v for (_, v, __) in data]
        hums = [v for (_, __, v) in data]

        self.temp_line.set_data(xs, temps)
        self.humi_line.set_data(xs, hums)

        # Dynamic X-axis format + label rotation/alignment
        self._set_xaxis_formatter(xs)

        # Recompute limits and draw
        self.ax.relim()
        self.ax.autoscale_view()
        # Extra auto layout + margin to keep labels visible
        self.fig.tight_layout()
        self.fig.subplots_adjust(bottom=0.28)
        self.canvas.draw_idle()

class App:
    def __init__(self, root: tk.Tk, stop_event: asyncio.Event):
        self.stop_event = stop_event

        root.title("LYWSD03MMC – Lab inside & Lab outside (Collector + GUI)")
        root.geometry("1400x720")
        root.configure(bg="white")

        top = tk.Frame(root, bg="white"); top.pack(fill=tk.X)
        tk.Label(top, text=f"Log dir: {os.path.abspath(LOG_DIR)}",
                 bg="white", fg="black").pack(side=tk.LEFT, padx=10, pady=8)
        ttk.Button(top, text="Rescan CSV", command=self.reload).pack(side=tk.RIGHT, padx=10)

        self.body = tk.Frame(root, bg="white"); self.body.pack(fill=tk.BOTH, expand=True)
        self.panels: Dict[str, DevicePanel] = {}
        self.reload()
        self._tick()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self.stop_event.set()
        root.destroy()

    def reload(self):
        files = list_device_files()
        chosen = pick_two(files)
        # Remove panels for missing files
        for k in list(self.panels.keys()):
            if k not in chosen:
                self.panels[k].wrap.destroy()
                del self.panels[k]
        # Create / layout two panels (prefer Lab_inside/Outside order)
        ordered_titles = ["Lab_inside", "Lab_outside"]
        keys = [k for k in ordered_titles if k in chosen] + [k for k in chosen if k not in ordered_titles]
        col = 0
        for title_key in keys[:2]:
            if title_key not in self.panels:
                display_title = title_key.replace("_", " ")
                p = DevicePanel(self.body, display_title, chosen[title_key])
                self.panels[title_key] = p
                p.grid(row=0, column=col, sticky="nsew", padx=10, pady=10)
            col += 1
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_columnconfigure(1, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

    def _tick(self):
        for p in self.panels.values():
            try:
                p.refresh()
            except Exception:
                pass
        root.after(GUI_REFRESH_MS, self._tick)

# ========================= Boot: run collector in background thread =========================

def run_collector_in_background(stop_event: asyncio.Event):
    def _runner():
        asyncio.run(collector_main(stop_event))
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return t

if __name__ == "__main__":
    os.makedirs(LOG_DIR, exist_ok=True)

    STOP = asyncio.Event()
    run_collector_in_background(STOP)

    root = tk.Tk()
    App(root, STOP)
    root.mainloop()
