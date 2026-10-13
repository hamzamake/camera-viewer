#!/usr/bin/env python3
"""
Camera Recorder — RTSP multi-stream recorder with live preview.
Dependencies: pip install opencv-python pillow
Also requires: ffmpeg + ffprobe on PATH
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import subprocess
import os, json, queue
from datetime import datetime
from pathlib import Path

# Try importing cv2 / PIL — show friendly error if missing
try:
    import cv2
    from PIL import Image, ImageTk
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

# ─── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path.home() / ".camera_recorder_config.json"

DEFAULT_CONFIG = {
    "output_dir": str(Path.home() / "CameraRecordings"),
    "segment_minutes": 3,
    "cameras": [
        {"name": "Camera 1",
         "rtsp": "replace",
         "enabled": True},
        {"name": "Camera 2",
         "rtsp": "replace",
         "enabled": True},
    ],
}


def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ─── Recorder ──────────────────────────────────────────────────────────────────

def probe_stream(rtsp: str) -> dict:
    """Run ffprobe and return a dict with keys: has_video, has_audio, audio_info, raw."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-rtsp_transport", "tcp",
        "-print_format", "json",
        "-show_streams",
        rtsp,
    ]
    result = {"has_video": False, "has_audio": False, "audio_info": "", "raw": ""}
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=10)
        import json as _json
        data = _json.loads(out)
        for s in data.get("streams", []):
            ct = s.get("codec_type", "")
            if ct == "video":
                result["has_video"] = True
            elif ct == "audio":
                result["has_audio"] = True
                codec  = s.get("codec_name", "?")
                rate   = s.get("sample_rate", "?")
                ch     = s.get("channels", "?")
                result["audio_info"] = f"{codec.upper()}  {rate} Hz  {ch}ch"
        result["raw"] = out.decode(errors="replace")
    except subprocess.TimeoutExpired:
        result["raw"] = "Probe timed out"
    except subprocess.CalledProcessError as e:
        result["raw"] = str(e)
    except FileNotFoundError:
        result["raw"] = "ffprobe not found"
    return result


def _warm_up_stream(rtsp: str, seconds: float = 2.5) -> None:
    """Open the RTSP stream and discard frames for `seconds` seconds.

    This consumes the initial non-IDR frames so the very first real recording
    segment begins on a clean keyframe, eliminating the black-first-segment bug.
    """
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-fflags", "+igndts+discardcorrupt",
        "-i", rtsp,
        "-t", str(seconds),
        "-f", "null", "-",
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=seconds + 5)
    except Exception:
        pass   # best-effort; failure just means first segment might still have a short black


class CameraRecorder(threading.Thread):
    """Records one RTSP stream in rolling N-minute MP4 segments via ffmpeg.

    Black-first-segment fix:
      RTSP cameras emit a GOP (group of pictures) that starts with a keyframe
      only every N seconds (often 2-4 s). When ffmpeg connects mid-GOP the first
      frames it receives are P/B-frames that can't decode without the preceding
      keyframe → black video until the next keyframe.

      Two-layer defence:
        1. _warm_up_stream() opens the stream for ~2.5 s before the first real
           segment and throws all frames away. By the time we start recording the
           camera has already cycled past the leading non-IDR frames.
        2. Every segment uses -vf "select=eq(pict_type\\,I),setpts=N/FRAME_RATE/TB"
           for frame 0 — replaced here with the simpler and more reliable approach
           of re-encoding with libx264 + -force_key_frames so every output segment
           is guaranteed to open with an I-frame regardless of input timing.
    """

    def __init__(self, name, rtsp, output_dir, segment_minutes, status_cb):
        super().__init__(daemon=True)
        self.name_label   = name
        self.rtsp         = rtsp
        self.output_dir   = output_dir
        self.segment_secs = segment_minutes * 60
        self.status_cb    = status_cb
        self._stop        = threading.Event()
        self.proc         = None

    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            # Send 'q' over stdin — ffmpeg's clean-quit signal.
            # This lets ffmpeg finalize the MP4 container (write the moov atom)
            # so the interrupted segment is playable instead of black/corrupt.
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def run(self):
        cam_dir = os.path.join(self.output_dir, _safe(self.name_label))
        os.makedirs(cam_dir, exist_ok=True)

        first_segment = True

        while not self._stop.is_set():

            # ── Warm-up before the very first segment ──────────────────────────
            if first_segment:
                self.status_cb(self.name_label, "⏳ Connecting…", "")
                _warm_up_stream(self.rtsp, seconds=2.5)
                first_segment = False
                if self._stop.is_set():
                    break

            ts       = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            out_file = os.path.join(cam_dir, f"{_safe(self.name_label)}_{ts}.mp4")

            cmd = [
                "ffmpeg", "-loglevel", "error",
                "-rtsp_transport", "tcp",
                # Ignore broken DTS/PTS from cameras, fix timestamps on ingest
                "-fflags", "+igndts+discardcorrupt",
                "-use_wallclock_as_timestamps", "1",
                "-i", self.rtsp,
                "-t", str(self.segment_secs),
                # Re-encode video with a forced keyframe at t=0 so every
                # segment is independently seekable from the very first frame
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "23",
                "-x264opts", "keyint=60:min-keyint=60",
                "-force_key_frames", "expr:gte(t,0)",
                # Re-encode audio to AAC; camera may not have a mic — handled gracefully
                "-c:a", "aac",
                "-b:a", "128k",
                # If the stream has no audio track this flag prevents an error
                "-ignore_unknown",
                # frag_keyframe + empty_moov writes a valid container header
                # immediately, so the file is playable even if recording is
                # interrupted before the normal end (no black/unplayable file).
                # faststart is implied by the fragmented format.
                "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                "-y", out_file,
            ]

            self.status_cb(self.name_label, "🔴 Recording", out_file)
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.PIPE,
                                              stdin=subprocess.PIPE)
                _, err = self.proc.communicate()
                if self._stop.is_set():
                    break
                if self.proc.returncode not in (0, 255):
                    msg = err.decode(errors="replace").strip().split("\n")[-1]
                    self.status_cb(self.name_label, f"⚠ {msg[:70]}", "")
                    self._stop.wait(5)
            except FileNotFoundError:
                self.status_cb(self.name_label,
                               "❌ ffmpeg not found — install it and add to PATH", "")
                break
            except Exception as e:
                self.status_cb(self.name_label, f"❌ {e}", "")
                self._stop.wait(5)

        self.status_cb(self.name_label, "⏹ Stopped", "")


# ─── Live Preview thread ────────────────────────────────────────────────────────

class CameraPreview(threading.Thread):
    """Connects to RTSP via OpenCV and pushes BGR frames into a queue."""

    def __init__(self, rtsp, frame_queue, stop_event):
        super().__init__(daemon=True)
        self.rtsp  = rtsp
        self.q     = frame_queue
        self._stop = stop_event

    def run(self):
        cap = None
        while not self._stop.is_set():
            if cap is None or not cap.isOpened():
                cap = cv2.VideoCapture(self.rtsp, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if not cap.isOpened():
                    self._stop.wait(3)
                    continue

            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap = None
                self._stop.wait(2)
                continue

            # Keep queue size ≤1 so we always show the freshest frame
            while not self.q.empty():
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    break
            self.q.put(frame)
            self._stop.wait(0.033)  # ~30 fps

        if cap:
            cap.release()


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _safe(s):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)


# ─── Palette ───────────────────────────────────────────────────────────────────

DARK_BG    = "#0d0f14"
PANEL_BG   = "#161922"
ACCENT     = "#00e5ff"
ACCENT2    = "#ff4f7b"
TEXT       = "#e8eaf0"
SUBTEXT    = "#6b7280"
SUCCESS    = "#22c55e"
WARNING    = "#f59e0b"
DANGER     = "#ef4444"
BORDER     = "#252a35"
PREVIEW_BG = "#050608"


# ─── Main App ──────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg       = load_config()
        self.recorders : dict[str, CameraRecorder] = {}
        self.previews  : dict[str, dict]            = {}
        self.recording  = False

        self.title("Camera Recorder")
        self.geometry("1060x700")
        self.minsize(820, 560)
        self.configure(bg=DARK_BG)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not HAS_CV2:
            self.after(600, self._warn_cv2)

    # ─── Dependency warning ───────────────────────────────────────────────────

    def _warn_cv2(self):
        messagebox.showwarning(
            "Missing dependency",
            "Live preview requires opencv-python and Pillow.\n\n"
            "Install with:\n  pip install opencv-python pillow\n\n"
            "Recording still works without them."
        )

    # ─── Top-level UI ─────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header bar
        hdr = tk.Frame(self, bg=DARK_BG, pady=16)
        hdr.pack(fill="x", padx=24)

        tk.Label(hdr, text="◉  CAMERA RECORDER",
                 font=("Courier New", 14, "bold"),
                 fg=ACCENT, bg=DARK_BG).pack(side="left")

        self.record_btn = tk.Button(
            hdr, text="▶  START RECORDING",
            font=("Courier New", 10, "bold"),
            bg=ACCENT, fg=DARK_BG,
            activebackground="#00b8cc", activeforeground=DARK_BG,
            relief="flat", padx=18, pady=8, cursor="hand2",
            command=self._toggle_recording)
        self.record_btn.pack(side="right")

        # Tab style
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("C.TNotebook", background=DARK_BG, borderwidth=0)
        style.configure("C.TNotebook.Tab",
                        background=PANEL_BG, foreground=SUBTEXT,
                        font=("Courier New", 9, "bold"),
                        padding=[16, 8], borderwidth=0)
        style.map("C.TNotebook.Tab",
                  background=[("selected", BORDER)],
                  foreground=[("selected", ACCENT)])

        nb = ttk.Notebook(self, style="C.TNotebook")
        nb.pack(fill="both", expand=True, padx=24, pady=(0, 12))

        self._build_live_view(nb)
        self._build_settings(nb)

    # ─── Live View tab ────────────────────────────────────────────────────────

    def _build_live_view(self, nb):
        page = tk.Frame(nb, bg=DARK_BG)
        nb.add(page, text="  Live View  ")

        self.grid_frame = tk.Frame(page, bg=DARK_BG)
        self.grid_frame.pack(fill="both", expand=True, padx=12, pady=10)

        self._rebuild_grid()

        bar = tk.Frame(page, bg=PANEL_BG, pady=7)
        bar.pack(fill="x", side="bottom")
        self.status_bar = tk.Label(
            bar, text="Ready — press Start Recording to begin.",
            font=("Courier New", 8), fg=SUBTEXT, bg=PANEL_BG)
        self.status_bar.pack(padx=16)

    def _rebuild_grid(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.cam_tiles: dict[str, dict] = {}

        cams = [c for c in self.cfg["cameras"] if c.get("enabled", True)]
        if not cams:
            tk.Label(self.grid_frame, text="No cameras enabled.",
                     font=("Courier New", 11), fg=SUBTEXT,
                     bg=DARK_BG).pack(pady=60)
            return

        cols = 2 if len(cams) > 1 else 1
        PW   = 460 if cols == 1 else 430
        PH   = 310 if cols == 1 else 242

        for i, cam in enumerate(cams):
            r, c = divmod(i, cols)
            cell = tk.Frame(self.grid_frame, bg=PANEL_BG,
                            highlightbackground=BORDER, highlightthickness=1)
            cell.grid(row=r, column=c, padx=8, pady=8, sticky="nsew")
            self.grid_frame.columnconfigure(c, weight=1)
            self.grid_frame.rowconfigure(r, weight=1)

            # Title bar
            top = tk.Frame(cell, bg=BORDER, pady=6)
            top.pack(fill="x")

            dot = tk.Label(top, text="●", font=("Courier New", 8, "bold"),
                           fg=SUBTEXT, bg=BORDER)
            dot.pack(side="left", padx=(10, 4))

            tk.Label(top, text=cam["name"].upper(),
                     font=("Courier New", 9, "bold"),
                     fg=TEXT, bg=BORDER).pack(side="left")

            status_lbl = tk.Label(top, text="IDLE",
                                  font=("Courier New", 8, "bold"),
                                  fg=SUBTEXT, bg=BORDER)
            status_lbl.pack(side="right", padx=10)

            # Preview canvas
            canvas = tk.Canvas(cell, width=PW, height=PH,
                                bg=PREVIEW_BG, highlightthickness=0,
                                cursor="hand2")
            canvas.pack(fill="both", expand=True)

            canvas.create_text(
                PW // 2, PH // 2,
                text="▶  Click to start live preview",
                font=("Courier New", 10), fill=SUBTEXT,
                tags="placeholder")

            # Info bar
            bot = tk.Frame(cell, bg=BORDER, pady=5)
            bot.pack(fill="x")

            file_lbl = tk.Label(bot, text="",
                                font=("Courier New", 7), fg=SUBTEXT,
                                bg=BORDER, anchor="w")
            file_lbl.pack(side="left", fill="x", expand=True, padx=(10, 4))

            probe_btn = tk.Button(
                bot, text="🔍 Audio",
                font=("Courier New", 7, "bold"),
                bg=BORDER, fg=SUBTEXT,
                activebackground=PANEL_BG, activeforeground=ACCENT,
                relief="flat", padx=6, pady=0, cursor="hand2",
                command=lambda n=cam["name"], r=cam["rtsp"]: self._probe_audio(n, r))
            probe_btn.pack(side="right", padx=(0, 6))

            self.cam_tiles[cam["name"]] = {
                "canvas": canvas, "dot": dot,
                "status": status_lbl, "file": file_lbl,
                "photo": None, "cam": cam,
                "pw": PW, "ph": PH,
            }

            canvas.bind("<Button-1>",
                        lambda e, n=cam["name"]: self._toggle_preview(n))

    # ─── Audio probe ─────────────────────────────────────────────────────────

    def _probe_audio(self, cam_name, rtsp):
        """Run ffprobe in a thread and pop up the result."""
        self.status_bar.config(text=f"Probing {cam_name}…")

        def run():
            result = probe_stream(rtsp)
            self.after(0, self._show_probe_result, cam_name, result)

        threading.Thread(target=run, daemon=True).start()

    def _show_probe_result(self, cam_name, result):
        if result["has_audio"]:
            msg = (f"✅  {cam_name} HAS an audio track\n\n"
                   f"Codec / format:  {result['audio_info']}\n\n"
                   f"Audio will be included in recordings.")
        else:
            msg = (f"❌  {cam_name} has NO audio track\n\n"
                   f"The camera either has no microphone,\n"
                   f"or audio is disabled in the camera's web UI.\n\n"
                   f"Recordings will be video-only for this camera.")
        self.status_bar.config(text=f"Probe done — {cam_name}")
        messagebox.showinfo(f"Audio probe — {cam_name}", msg)

    # ─── Preview ──────────────────────────────────────────────────────────────

    def _toggle_preview(self, name):
        if name in self.previews:
            self._stop_preview(name)
        else:
            self._start_preview(name)

    def _start_preview(self, name):
        if not HAS_CV2:
            messagebox.showinfo(
                "Missing library",
                "Install opencv-python and Pillow for live preview:\n"
                "  pip install opencv-python pillow")
            return

        tile = self.cam_tiles.get(name)
        if not tile:
            return

        stop_ev = threading.Event()
        fq      = queue.Queue(maxsize=2)
        thread  = CameraPreview(tile["cam"]["rtsp"], fq, stop_ev)
        thread.start()

        self.previews[name] = {"thread": thread, "queue": fq, "stop": stop_ev}

        canvas = tile["canvas"]
        canvas.delete("placeholder")
        pw, ph = tile["pw"], tile["ph"]
        canvas.create_text(pw // 2, ph // 2,
                           text="Connecting…",
                           font=("Courier New", 9), fill=SUBTEXT,
                           tags="connecting")

        tile["dot"].config(fg=ACCENT)
        tile["status"].config(text="PREVIEW", fg=ACCENT)
        self._poll_preview(name)

    def _stop_preview(self, name):
        info = self.previews.pop(name, None)
        if info:
            info["stop"].set()

        tile = self.cam_tiles.get(name)
        if tile:
            canvas = tile["canvas"]
            canvas.delete("all")
            pw, ph = tile["pw"], tile["ph"]
            canvas.create_text(pw // 2, ph // 2,
                               text="▶  Click to start live preview",
                               font=("Courier New", 10), fill=SUBTEXT,
                               tags="placeholder")
            tile["dot"].config(fg=SUBTEXT)
            if name not in self.recorders:
                tile["status"].config(text="IDLE", fg=SUBTEXT)

    def _poll_preview(self, name):
        if name not in self.previews:
            return

        tile = self.cam_tiles.get(name)
        info = self.previews.get(name)
        if not tile or not info:
            return

        canvas = tile["canvas"]
        try:
            frame = info["queue"].get_nowait()
        except queue.Empty:
            frame = None

        if frame is not None:
            canvas.delete("connecting")
            cw = canvas.winfo_width()  or tile["pw"]
            ch = canvas.winfo_height() or tile["ph"]
            if cw > 10 and ch > 10:
                img   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img   = cv2.resize(img, (cw, ch), interpolation=cv2.INTER_LINEAR)
                photo = ImageTk.PhotoImage(Image.fromarray(img))
                canvas.delete("frame")
                canvas.create_image(0, 0, anchor="nw", image=photo, tags="frame")
                tile["photo"] = photo  # prevent GC

        self.after(33, self._poll_preview, name)

    # ─── Recording status (from threads) ─────────────────────────────────────

    def _rec_status(self, name, status, filepath):
        self.after(0, self._apply_rec_status, name, status, filepath)

    def _apply_rec_status(self, name, status, filepath):
        tile = self.cam_tiles.get(name)
        if tile:
            if "Recording" in status:
                color, dot_color = ACCENT2, ACCENT2
            elif "Error" in status or "not found" in status:
                color, dot_color = DANGER, DANGER
            elif "Stopped" in status:
                color = SUBTEXT
                dot_color = ACCENT if name in self.previews else SUBTEXT
            else:
                color, dot_color = WARNING, WARNING

            label = (status
                     .replace("🔴 ", "● ")
                     .replace("⏹ ", "■ ")
                     .replace("⚠ ", "! ")
                     .replace("❌ ", "✕ "))
            tile["status"].config(text=label.upper()[:20], fg=color)
            tile["dot"].config(fg=dot_color)
            if filepath:
                short = "…" + filepath[-65:] if len(filepath) > 65 else filepath
                tile["file"].config(text=short)

        ts = datetime.now().strftime("%H:%M:%S")
        self.status_bar.config(text=f"[{ts}]  {name}: {status}")

    # ─── Recording control ────────────────────────────────────────────────────

    def _toggle_recording(self):
        if not self.recording:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        out_dir = self.cfg["output_dir"]
        os.makedirs(out_dir, exist_ok=True)
        cams = [c for c in self.cfg["cameras"] if c.get("enabled", True)]
        if not cams:
            messagebox.showwarning("No cameras", "No enabled cameras found.")
            return
        for cam in cams:
            r = CameraRecorder(
                name=cam["name"], rtsp=cam["rtsp"],
                output_dir=out_dir,
                segment_minutes=self.cfg["segment_minutes"],
                status_cb=self._rec_status,
            )
            r.start()
            self.recorders[cam["name"]] = r
        self.recording = True
        self.record_btn.config(text="⏹  STOP RECORDING",
                               bg=DANGER, activebackground="#b91c1c")
        self.status_bar.config(text=f"Recording  →  {out_dir}")

    def _stop_recording(self):
        for r in self.recorders.values():
            r.stop()
        self.recorders.clear()
        self.recording = False
        self.record_btn.config(text="▶  START RECORDING",
                               bg=ACCENT, activebackground="#00b8cc")

    # ─── Settings tab ─────────────────────────────────────────────────────────

    def _build_settings(self, nb):
        page = tk.Frame(nb, bg=DARK_BG)
        nb.add(page, text="  Settings  ")

        # Scrollable canvas
        sc = tk.Canvas(page, bg=DARK_BG, highlightthickness=0)
        sb = ttk.Scrollbar(page, orient="vertical", command=sc.yview)
        inner = tk.Frame(sc, bg=DARK_BG)
        inner.bind("<Configure>",
                   lambda e: sc.configure(scrollregion=sc.bbox("all")))
        sc.create_window((0, 0), window=inner, anchor="nw")
        sc.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        sc.pack(side="left", fill="both", expand=True)

        # Output directory
        sec = self._section(inner, "Output Directory")
        row = tk.Frame(sec, bg=PANEL_BG)
        row.pack(fill="x")
        self.dir_var = tk.StringVar(value=self.cfg["output_dir"])
        tk.Entry(row, textvariable=self.dir_var,
                 font=("Courier New", 9),
                 bg=BORDER, fg=TEXT, insertbackground=ACCENT,
                 relief="flat", bd=0).pack(side="left", fill="x",
                                           expand=True, ipady=7, ipadx=8)
        tk.Button(row, text="Browse",
                  font=("Courier New", 9, "bold"),
                  bg=ACCENT, fg=DARK_BG,
                  activebackground="#00b8cc", activeforeground=DARK_BG,
                  relief="flat", padx=12, cursor="hand2",
                  command=self._browse_dir).pack(side="right",
                                                 padx=(8, 0), ipady=7)

        # Segment length
        sec2 = self._section(inner, "Segment Length")
        sr = tk.Frame(sec2, bg=PANEL_BG)
        sr.pack(fill="x")
        self.seg_var = tk.IntVar(value=self.cfg["segment_minutes"])
        for v, lbl in [(1, "1 min"), (3, "3 min"), (5, "5 min"), (10, "10 min")]:
            tk.Radiobutton(sr, text=lbl, variable=self.seg_var, value=v,
                           font=("Courier New", 9), fg=TEXT, bg=PANEL_BG,
                           selectcolor=BORDER,
                           activebackground=PANEL_BG, activeforeground=ACCENT,
                           cursor="hand2").pack(side="left", padx=(0, 16))

        # Cameras
        sec3 = self._section(inner, "Cameras")
        self.cam_rows = []
        for cam in self.cfg["cameras"]:
            self._add_cam_row(sec3, cam)
        tk.Button(sec3, text="＋  Add Camera",
                  font=("Courier New", 9, "bold"),
                  bg=PANEL_BG, fg=ACCENT,
                  activebackground=BORDER, activeforeground=ACCENT,
                  relief="flat", pady=8, cursor="hand2",
                  command=lambda: self._add_cam_row(sec3)).pack(anchor="w", pady=(10, 0))

        # Save
        tk.Button(inner, text="💾  Save Settings",
                  font=("Courier New", 10, "bold"),
                  bg=SUCCESS, fg=DARK_BG,
                  activebackground="#16a34a", activeforeground=DARK_BG,
                  relief="flat", padx=20, pady=10, cursor="hand2",
                  command=self._save_settings).pack(pady=24)

    def _section(self, parent, title):
        outer = tk.Frame(parent, bg=DARK_BG)
        outer.pack(fill="x", padx=24, pady=(16, 0))
        tk.Label(outer, text=title.upper(),
                 font=("Courier New", 8, "bold"),
                 fg=SUBTEXT, bg=DARK_BG).pack(anchor="w", pady=(0, 6))
        inner = tk.Frame(outer, bg=PANEL_BG, padx=16, pady=14,
                         highlightbackground=BORDER, highlightthickness=1)
        inner.pack(fill="x")
        return inner

    def _add_cam_row(self, parent, cam=None):
        if cam is None:
            cam = {"name": f"Camera {len(self.cam_rows)+1}",
                   "rtsp": "rtsp://", "enabled": True}
        rf       = tk.Frame(parent, bg=PANEL_BG)
        rf.pack(fill="x", pady=(0, 8))
        en_var   = tk.BooleanVar(value=cam.get("enabled", True))
        name_var = tk.StringVar(value=cam["name"])
        rtsp_var = tk.StringVar(value=cam["rtsp"])
        tk.Checkbutton(rf, variable=en_var, bg=PANEL_BG, fg=ACCENT,
                       activebackground=PANEL_BG, selectcolor=BORDER,
                       cursor="hand2").pack(side="left")
        tk.Entry(rf, textvariable=name_var, width=14,
                 font=("Courier New", 9),
                 bg=BORDER, fg=TEXT, insertbackground=ACCENT,
                 relief="flat", bd=0).pack(side="left", ipady=5,
                                           ipadx=6, padx=(4, 8))
        tk.Entry(rf, textvariable=rtsp_var,
                 font=("Courier New", 8),
                 bg=BORDER, fg=TEXT, insertbackground=ACCENT,
                 relief="flat", bd=0).pack(side="left", fill="x",
                                           expand=True, ipady=5, ipadx=6)
        entry = (name_var, rtsp_var, en_var)

        def remove(f=rf, e=entry):
            self.cam_rows = [x for x in self.cam_rows if x is not e]
            f.destroy()

        tk.Button(rf, text="✕", font=("Courier New", 9, "bold"),
                  bg=PANEL_BG, fg=DANGER,
                  activebackground=PANEL_BG, activeforeground="#ff7a7a",
                  relief="flat", cursor="hand2",
                  command=remove).pack(side="right", padx=(8, 0))
        self.cam_rows.append(entry)

    def _browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d:
            self.dir_var.set(d)

    def _save_settings(self):
        cameras = [{"name": n.get().strip(), "rtsp": r.get().strip(),
                    "enabled": e.get()}
                   for n, r, e in self.cam_rows]
        self.cfg["output_dir"]      = self.dir_var.get().strip()
        self.cfg["segment_minutes"] = self.seg_var.get()
        self.cfg["cameras"]         = cameras
        save_config(self.cfg)
        messagebox.showinfo("Saved",
                            "Settings saved.\n\nRestart the app to apply camera changes.")

    # ─── Close ────────────────────────────────────────────────────────────────

    def _on_close(self):
        if self.recording:
            if not messagebox.askyesno("Quit", "Recording is active — stop and quit?"):
                return
            self._stop_recording()
        for info in list(self.previews.values()):
            info["stop"].set()
        self.destroy()


# ─── Entry ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
