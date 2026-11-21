"""Windows desktop interface for the Insta360 ARM64 converter."""

import argparse
from pathlib import Path
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import insta360_arm as pipeline


ROOT = Path(__file__).resolve().parent
VIDEO_FILES = [("Camera and video files", "*.insv *.mp4 *.mov *.mkv"), ("All files", "*.*")]
SIZES = {"Match source": "", "8K · 7680 × 3840": "7680x3840",
         "6K · 6016 × 3008": "6016x3008", "4K · 3840 × 1920": "3840x1920",
         "2K · 1920 × 960": "1920x960", "Custom": ""}


def find_ffmpeg():
    local = sorted(ROOT.glob(".tooling/*/bin/ffmpeg.exe"))
    return (str(local[0]) if local else shutil.which("ffmpeg")) or ""


class Cancelled(Exception):
    pass


class GuiArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


class Conversion:
    def __init__(self, events):
        self.events = events
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.finalizing = False

    def cancel(self):
        with self.lock:
            if self.finalizing:
                return False
            self.cancelled.set()
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
            return True

    def run_ffmpeg(self, cmd, cwd):
        with tempfile.TemporaryFile(mode="w+b") as log:
            with self.lock:
                if self.cancelled.is_set():
                    raise Cancelled()
                self.process = subprocess.Popen(
                    [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]],
                    cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                process = self.process
            with process.stdout:
                for line in process.stdout:
                    if line.startswith("out_time_us="):
                        _, value = line.strip().split("=", 1)
                        if value.lstrip("-").isdigit():
                            self.events.put(("progress", max(0, int(value) / 1_000_000)))
            code = process.wait()
            with self.lock:
                self.process = None
                if self.cancelled.is_set():
                    raise Cancelled()
                self.finalizing = True
            if code:
                log.seek(max(0, log.tell() - 12000))
                details = log.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"FFmpeg exited with code {code}.\n\n{details.strip()}")
            self.events.put(("finalizing", None))

    def work(self, args, ffmpeg, ffprobe):
        try:
            info = pipeline.probe(ffprobe, Path(args.input).resolve(strict=True))
            duration = float(info.get("format", {}).get("duration") or 0)
            if args.seconds:
                duration = min(duration, args.seconds) if duration else args.seconds
            self.events.put(("duration", duration))
            pipeline.convert(args, ffmpeg, ffprobe, self.run_ffmpeg)
        except Cancelled:
            self.events.put(("cancelled", None))
        except Exception as error:
            event = "cancelled" if self.cancelled.is_set() else "error"
            self.events.put((event, str(error)))
        else:
            self.events.put(("complete", Path(args.output).resolve()))


class App:
    def __init__(self, root):
        self.root = root
        root.title("Insta360 ARM64 Converter")
        root.geometry("800x680")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        style.configure("Title.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("Hint.TLabel", foreground="#515c69")
        self.events = queue.Queue()
        self.job = None
        self.closing = False
        self.duration = 0
        self.last_output = None
        self.status = tk.StringVar(value="Select a source video to get started.")
        self.fields = {key: tk.StringVar(value=value) for key, value in {
            "input": "", "right": "", "output": "", "motion": "", "ffmpeg": find_ffmpeg(),
            "encoder": "x265", "bits": "Match source", "size": "Match source", "custom_size": "",
            "fps": "Match source", "bitrate": "35", "seconds": "5", "preset": "fast",
            "left_stream": "", "right_stream": "", "fov": "180",
            "left_crop": "", "right_crop": "", "left_turns": "0", "right_turns": "0",
            "interp": "line", "crf": "18", "imu_offset_ms": "0", "stabilization_device": "vulkan",
        }.items()}
        self.preview = tk.BooleanVar()
        self.fixed_bitrate = tk.BooleanVar()
        self.two_files = tk.BooleanVar()
        self.swap = tk.BooleanVar()
        self.stabilize = tk.BooleanVar(value=True)
        self.calibrated = tk.BooleanVar(value=True)
        self.bitrate_scale = tk.DoubleVar(value=35)
        self.syncing_bitrate = False
        self.options = {}
        self.make_layout()
        self.refresh_controls()
        root.update_idletasks()
        root.minsize(730, max(610, root.winfo_reqheight()))
        root.after(100, self.poll)

    def panel(self, parent, title):
        result = ttk.LabelFrame(parent, text=title, padding=(12, 8))
        result.columnconfigure(1, weight=1)
        return result

    def field(self, frame, row, title, key, chooser=None, choices=None):
        ttk.Label(frame, text=title).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
        if choices:
            control = ttk.Combobox(frame, textvariable=self.fields[key], values=choices,
                                   state="readonly", width=18)
        else:
            control = ttk.Entry(frame, textvariable=self.fields[key])
        control.grid(row=row, column=1, sticky="ew", pady=4)
        self.options[key] = control
        if chooser:
            button = ttk.Button(frame, text="Browse…", command=chooser)
            button.grid(row=row, column=2, padx=(8, 0), pady=4)
            self.options[key + "_browse"] = button
        return control

    def make_layout(self):
        page = ttk.Frame(self.root, padding=(20, 16))
        page.grid(sticky="nsew")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)
        ttk.Label(page, text="Insta360 ARM64 Converter", style="Title.TLabel").grid(sticky="w")
        ttk.Label(page, text="Export a 360° HEVC MP4 from your camera footage.",
                  style="Hint.TLabel").grid(sticky="w", pady=(3, 12))
        self.tabs = ttk.Notebook(page)
        self.tabs.grid(sticky="nsew")
        basic = ttk.Frame(self.tabs, padding=12)
        advanced = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(basic, text="Convert")
        self.tabs.add(advanced, text="Advanced")
        basic.columnconfigure(0, weight=1)
        advanced.columnconfigure(0, weight=1)
        files = self.panel(basic, "Files")
        files.grid(sticky="ew", pady=(0, 8))
        self.field(files, 0, "Source video", "input", self.choose_source)
        ttk.Checkbutton(files, text="Second lens is in a separate file", variable=self.two_files,
                        command=self.refresh_controls).grid(row=1, column=1, sticky="w")
        self.field(files, 2, "Second lens", "right", lambda: self.browse("right", VIDEO_FILES))
        self.field(files, 3, "Save MP4 to", "output", self.choose_output)
        export = self.panel(basic, "Export")
        export.grid(sticky="ew")
        for col, (label, value) in enumerate((("Software encoder · CPU", "x265"),
                                             ("Qualcomm hardware · 8-bit", "mf")), 1):
            ttk.Radiobutton(export, text=label, value=value, variable=self.fields["encoder"],
                            command=self.refresh_controls).grid(row=0, column=col, sticky="w", pady=4)
        self.field(export, 1, "Bit depth", "bits", choices=("Match source", "8-bit", "10-bit"))
        res = self.field(export, 2, "Resolution", "size", choices=tuple(SIZES))
        res.bind("<<ComboboxSelected>>", lambda _: self.refresh_controls())
        self.options["custom_size"] = ttk.Entry(export, textvariable=self.fields["custom_size"], width=18)
        self.options["custom_size"].grid(row=2, column=2, sticky="ew", padx=(8, 0))
        fps = self.field(export, 3, "Frame rate", "fps",
                         choices=("Match source", "24", "25", "30000/1001", "30", "48", "50", "60"))
        fps.configure(state="normal")
        self.options["bitrate_check"] = ttk.Checkbutton(
            export, text="Set target bitrate", variable=self.fixed_bitrate, command=self.refresh_controls)
        self.options["bitrate_check"].grid(row=4, column=0, sticky="w", pady=4)
        self.hardware_bitrate_label = ttk.Label(export, text="Target bitrate")
        self.hardware_bitrate_label.grid(row=4, column=0, sticky="w", pady=4)
        self.options["slider"] = ttk.Scale(export, from_=1, to=360, variable=self.bitrate_scale,
                                           command=self.slider_changed)
        self.options["slider"].grid(row=4, column=1, sticky="ew", padx=(0, 12))
        bitrate = ttk.Frame(export)
        bitrate.grid(row=4, column=2, sticky="w")
        self.options["bitrate"] = ttk.Entry(bitrate, textvariable=self.fields["bitrate"], width=7)
        self.options["bitrate"].pack(side="left")
        ttk.Label(bitrate, text="Mb/s").pack(side="left", padx=6)
        self.fields["bitrate"].trace_add("write", self.bitrate_changed)
        ttk.Checkbutton(export, text="Preview only", variable=self.preview,
                        command=self.refresh_controls).grid(row=5, column=0, sticky="w", pady=4)
        preview = ttk.Frame(export)
        preview.grid(row=5, column=1, sticky="w")
        self.options["seconds"] = ttk.Entry(preview, textvariable=self.fields["seconds"], width=7)
        self.options["seconds"].pack(side="left")
        ttk.Label(preview, text="seconds").pack(side="left", padx=6)
        ttk.Checkbutton(export, text="Apply stabilization", variable=self.stabilize,
                        command=self.refresh_controls).grid(row=6, column=0, sticky="w", pady=4)
        self.motion_hint = ttk.Label(export, style="Hint.TLabel", wraplength=460)
        self.motion_hint.grid(row=6, column=1, columnspan=2, sticky="w")
        self.fields["motion"].trace_add("write", lambda *_: self.refresh_controls())
        self.fields["input"].trace_add("write", lambda *_: self.refresh_controls())
        self.encoder_hint = ttk.Label(export, style="Hint.TLabel", wraplength=650)
        self.encoder_hint.grid(row=7, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Checkbutton(export, text="Factory calibration and seam blending (X6)",
                        variable=self.calibrated, command=self.refresh_controls).grid(row=8, column=0, columnspan=3,
                                                       sticky="w", pady=4)

        setup = self.panel(advanced, "Tools and stabilization")
        setup.grid(sticky="ew", pady=(0, 8))
        self.field(setup, 0, "FFmpeg executable", "ffmpeg",
                   lambda: self.browse("ffmpeg", [("FFmpeg", "ffmpeg.exe"), ("All files", "*.*")]))
        self.field(setup, 1, "Override with rotation CSV", "motion",
                   lambda: self.browse("motion", [("CSV files", "*.csv")]))
        self.field(setup, 2, "IMU timing offset (ms)", "imu_offset_ms")
        self.field(setup, 3, "Stabilization processor", "stabilization_device",
                   choices=("vulkan", "cpu"))
        geometry = ttk.Frame(advanced)
        geometry.grid(sticky="ew")
        geometry.columnconfigure((0, 1), weight=1, uniform="lens")
        for index, (side, label) in enumerate((("left", "First lens"), ("right", "Second lens"))):
            lens = self.panel(geometry, label)
            lens.grid(row=0, column=index, sticky="ew", padx=(0, 6) if index == 0 else (6, 0))
            self.field(lens, 0, "Stream index", side + "_stream")
            self.field(lens, 1, "Crop (size:size:x:y)", side + "_crop")
            self.field(lens, 2, "Quarter-turns", side + "_turns", choices=("0", "1", "2", "3"))
        lenses = self.panel(advanced, "Projection and CPU encoder")
        lenses.grid(sticky="ew", pady=(8, 0))
        self.field(lenses, 0, "Lens field of view (°)", "fov")
        self.field(lenses, 1, "Interpolation", "interp", choices=("near", "line", "cube"))
        self.field(lenses, 2, "CPU preset", "preset",
                   choices=("ultrafast", "superfast", "veryfast", "faster", "fast",
                            "medium", "slow", "slower", "veryslow"))
        self.field(lenses, 3, "CPU quality (CRF)", "crf")
        ttk.Checkbutton(lenses, text="Swap lens order", variable=self.swap).grid(
            row=4, column=1, sticky="w", pady=4)

        self.progress = ttk.Progressbar(page, maximum=100)
        self.progress.grid(sticky="ew", pady=(12, 5))
        ttk.Label(page, textvariable=self.status, wraplength=680).grid(sticky="w")
        actions = ttk.Frame(page)
        actions.grid(sticky="ew", pady=(8, 0))
        self.start_button = ttk.Button(actions, text="Convert video", command=self.start)
        self.start_button.pack(side="right")
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=8)
        self.open_button = ttk.Button(actions, text="Open output folder", command=self.open_folder,
                                      state="disabled")
        self.open_button.pack(side="left")

    def browse(self, key, types):
        current = self.fields[key].get().strip()
        selected = filedialog.askopenfilename(parent=self.root, filetypes=types,
                                             initialdir=str(Path(current).parent) if current else None)
        if selected:
            self.fields[key].set(selected)
        return selected

    def choose_source(self):
        path = self.browse("input", VIDEO_FILES)
        if path:
            src = Path(path)
            name = src.with_name(src.stem + "_360.mp4")
            number = 2
            while name.exists():
                name = src.with_name(f"{src.stem}_360_{number}.mp4")
                number += 1
            self.fields["output"].set(str(name))

    def choose_output(self):
        current = self.fields["output"].get().strip()
        path = filedialog.asksaveasfilename(
            parent=self.root, defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")],
            initialdir=str(Path(current).parent) if current else None,
            initialfile=Path(current).name if current else "video_360.mp4")
        if path:
            self.fields["output"].set(path)

    def slider_changed(self, value):
        if not self.syncing_bitrate:
            self.fields["bitrate"].set(str(round(float(value))))

    def bitrate_changed(self, *_):
        try:
            value = int(self.fields["bitrate"].get())
        except ValueError:
            return
        self.syncing_bitrate = True
        self.bitrate_scale.set(max(1, min(360, value)))
        self.syncing_bitrate = False

    def use_factory_calibration(self):
        return (self.calibrated.get() and not self.two_files.get()
                and Path(self.fields["input"].get().strip()).suffix.lower() == ".insv")

    def refresh_controls(self):
        hardware = self.fields["encoder"].get() == "mf"
        was_hardware = getattr(self, "hardware", False)
        if hardware:
            if not was_hardware:
                self.cpu_bits = self.fields["bits"].get()
            self.fields["bits"].set("8-bit")
        elif was_hardware:
            self.fields["bits"].set(self.cpu_bits)
        self.hardware = hardware
        if hardware:
            self.options["bitrate_check"].grid_remove()
            self.hardware_bitrate_label.grid()
        else:
            self.hardware_bitrate_label.grid_remove()
            self.options["bitrate_check"].grid()
        fixed = hardware or self.fixed_bitrate.get()
        csv_selected = bool(self.fields["motion"].get().strip())
        imu_enabled = self.stabilize.get() and self.use_factory_calibration() and not csv_selected
        for key, enabled in {"right": self.two_files.get(), "right_browse": self.two_files.get(),
                             "custom_size": self.fields["size"].get() == "Custom",
                             "seconds": self.preview.get(),
                             "slider": fixed, "bitrate": fixed,
                             "motion": self.stabilize.get(), "motion_browse": self.stabilize.get(),
                             "imu_offset_ms": imu_enabled,
                             "crf": not fixed, "preset": not hardware}.items():
            self.options[key].configure(state="normal" if enabled else "disabled")
        if not self.stabilize.get():
            motion_status = "Off for the next export."
        elif csv_selected:
            motion_status = "Will use the correction CSV selected in Advanced."
        elif imu_enabled:
            motion_status = "Uses X6 motion data. CPU stabilization can be slow at 8K."
        else:
            motion_status = "Uses X6 motion data with factory calibration, or a correction CSV."
        self.motion_hint.configure(text=motion_status)
        self.options["bits"].configure(state="disabled" if hardware else "readonly")
        self.options["stabilization_device"].configure(state="readonly" if imu_enabled else "disabled")
        self.encoder_hint.configure(text=(
            "Hardware encoding outputs 8-bit. A 10-bit source will lose color precision."
            if hardware else "CPU encoding can preserve 10-bit color. The lens projection is experimental."))

    def arguments(self):
        values = {key: variable.get().strip() for key, variable in self.fields.items()}
        if not values["input"] or not Path(values["input"]).is_file():
            raise ValueError("Select an existing source video.")
        if not values["output"] or Path(values["output"]).suffix.lower() != ".mp4":
            raise ValueError("Choose an MP4 destination.")
        output = Path(values["output"])
        if output.exists() or not output.resolve().parent.is_dir():
            raise ValueError("Choose a new output filename in an existing folder.")
        path = Path(values["ffmpeg"])
        if not path.is_file():
            raise ValueError("Select ffmpeg.exe under Advanced.")
        ffmpeg = pipeline.executable(str(path.resolve()))
        ffprobe = pipeline.executable(str(path.resolve().with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")))
        if os.name == "nt" and any(pipeline.machine(p) != "ARM64" for p in (ffmpeg, ffprobe)):
            raise ValueError("Select a Windows ARM64 build of FFmpeg and FFprobe.")
        command = ["convert", values["input"], values["output"], "--encoder", values["encoder"]]
        optional = {k: values[k] for k in ("left_stream", "right_stream", "left_crop",
                                          "right_crop", "left_turns", "right_turns", "fov", "interp")}
        if self.stabilize.get() and values["motion"]:
            optional["motion"] = values["motion"]
        elif self.stabilize.get() and self.use_factory_calibration():
            command.append("--auto-stabilize")
            optional["imu_offset_ms"] = values["imu_offset_ms"]
            optional["stabilization_device"] = values["stabilization_device"]
        if values["encoder"] == "x265":
            optional["preset"] = values["preset"]
            if not self.fixed_bitrate.get():
                optional["crf"] = values["crf"]
        if self.two_files.get():
            if not values["right"] or not Path(values["right"]).is_file():
                raise ValueError("Select an existing second-lens video.")
            optional["right"] = values["right"]
        if values["size"] == "Custom" and not values["custom_size"]:
            raise ValueError("Enter a custom resolution such as 3840x1920.")
        optional["size"] = values["custom_size"] if values["size"] == "Custom" else SIZES[values["size"]]
        if values["fps"] != "Match source":
            optional["fps"] = values["fps"]
        if values["bits"] != "Match source":
            optional["bits"] = values["bits"].split("-")[0]
        if self.fixed_bitrate.get() or values["encoder"] == "mf":
            if not values["bitrate"].isdecimal() or int(values["bitrate"]) < 1:
                raise ValueError("Enter a positive target bitrate in whole Mb/s.")
            optional["bitrate"] = values["bitrate"] + "M"
        if self.preview.get():
            if not values["seconds"]:
                raise ValueError("Enter a preview duration in seconds.")
            optional["seconds"] = values["seconds"]
        for key, value in optional.items():
            if value:
                command.extend(("--" + key.replace("_", "-"), value))
        if self.swap.get():
            command.append("--swap")
        if self.use_factory_calibration():
            command.append("--calibrated")
        return pipeline.build_parser(GuiArgumentParser).parse_args(command), ffmpeg, ffprobe

    def start(self):
        try:
            args = self.arguments()
        except (OSError, ValueError) as error:
            messagebox.showerror("Check export settings", str(error), parent=self.root)
            return
        self.job = Conversion(self.events)
        self.duration = 0
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(15)
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status.set("Reading source video…")
        threading.Thread(target=self.job.work, args=args, daemon=True).start()

    def cancel(self):
        if self.job and self.job.cancel():
            self.status.set("Cancelling…")
            self.cancel_button.configure(state="disabled")

    def close(self):
        if self.job:
            if messagebox.askyesno("Close converter?", "Cancel the running export and close?",
                                   parent=self.root):
                self.closing = True
                self.cancel()
        else:
            self.root.destroy()

    def open_folder(self):
        if self.last_output:
            try:
                os.startfile(str(self.last_output.parent))
            except OSError as error:
                messagebox.showerror("Could not open folder", str(error), parent=self.root)

    def poll(self):
        while True:
            try:
                event, value = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "duration":
                self.duration = value
                if value:
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                self.status.set("Converting video…")
            elif event == "progress":
                if self.duration:
                    percent = min(99, value * 100 / self.duration)
                    self.progress.configure(value=percent)
                    self.status.set(f"Converting video… {percent:.0f}%")
                else:
                    self.status.set(f"Converting video… {value:.1f} seconds encoded")
            elif event == "finalizing":
                self.status.set("Verifying the MP4…")
                self.cancel_button.configure(state="disabled")
            else:
                self.job = None
                self.progress.stop()
                self.progress.configure(mode="determinate", value=100 if event == "complete" else 0)
                self.start_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                if self.closing:
                    self.root.destroy()
                    return
                if event == "complete":
                    self.last_output = value
                    self.open_button.configure(state="normal")
                    self.status.set(f"Saved {value.name}")
                elif event == "cancelled":
                    self.status.set("Conversion cancelled.")
                else:
                    self.status.set("Conversion failed.")
                    self.show_failure(value)
        self.root.after(100, self.poll)

    def show_failure(self, details):
        window = tk.Toplevel(self.root)
        window.title("Conversion failed")
        window.geometry("700x370")
        window.transient(self.root)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        ttk.Label(window, text="Conversion failed. You can copy the details below.").grid(
            row=0, column=0, sticky="w", padx=12, pady=10)
        body = tk.Text(window, wrap="word", padx=8, pady=8)
        body.grid(row=1, column=0, sticky="nsew", padx=(12, 0))
        thumb = ttk.Scrollbar(window, command=body.yview)
        thumb.grid(row=1, column=1, sticky="ns", padx=(0, 12))
        body.configure(yscrollcommand=thumb.set)
        body.insert("1.0", details)
        body.configure(state="disabled")
        ttk.Button(window, text="Close", command=window.destroy).grid(
            row=2, column=0, sticky="e", pady=10)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
