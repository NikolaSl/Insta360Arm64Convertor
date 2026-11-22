# Insta360 ARM64 Pipeline

Experimental desktop and command-line converter for two synchronized, square-cropped fisheye views, built for Windows on Arm with a portable Linux path. It produces a mono 2:1 equirectangular HEVC MP4 with Spherical Video V2 metadata. It supports factory-calibrated X6 projection, overlap blending and automatic rotational stabilization using the camera's motion data.

## Windows setup

Use native ARM64 Python 3.11 or later and put matching ARM64 `ffmpeg.exe` and `ffprobe.exe` on PATH. The current BtbN Windows ARM64 GPL build provides a starting point for libx265. Pin and record the exact build for repeatable runs. [FFmpeg downloads](https://ffmpeg.org/download.html) · [BtbN releases](https://github.com/BtbN/FFmpeg-Builds/releases)

```powershell
python insta360_arm.py --require-arm64 doctor
python -m unittest discover -s tests -v
python insta360_arm.py --require-arm64 encode-test test.mp4 --size 1920x960 --bits 10
```

`doctor` inspects the PE machine type of both FFmpeg tools and prints the options exposed by the build. Seeing `hevc_mf` in that output only confirms the wrapper is compiled in.

## Linux setup

Use native Python 3.11 or later, NumPy, a matching FFmpeg/FFprobe build with libx265 and the v360, remap and maskedmerge filters, and stable Rust with support for the 2024 edition. Tk is required for the desktop app. On Debian or Ubuntu, the system packages below provide the Python/Tk setup and an FFmpeg starting point. [FFmpeg downloads](https://ffmpeg.org/download.html) · [BtbN Linux x64 and ARM64 builds](https://github.com/BtbN/FFmpeg-Builds/releases)

```bash
sudo apt install python3 python3-tk python3-venv ffmpeg xdg-utils build-essential
python3 -m venv .venv
source .venv/bin/activate
python -m pip install numpy
cargo build --locked --release --manifest-path telemetry/Cargo.toml
python insta360_arm.py doctor
python -m unittest discover -s tests -v
python insta360_gui.py
```

On Linux ARM64, pass `--require-arm64` to verify native FFmpeg and FFprobe executables. On Linux x86-64, omit that flag. The GUI opens the output folder using `xdg-open`. Save to a filesystem that supports hard links, such as ext4. The telemetry executable is `telemetry/target/release/insta360-telemetry`.

Linux uses libx265 for both 8-bit and 10-bit HEVC and defaults to CPU stabilization. Vulkan stabilization can be selected under Advanced if the FFmpeg build includes `v360_vulkan` and your GPU driver supports it. Linux hardware HEVC encoding is outside the current implementation. Media Foundation encoding remains a Windows feature.

## Desktop app

Double-click `Launch Insta360 Converter.cmd` or run `python insta360_gui.py`. Choose a source and MP4 destination. The Windows desktop app defaults to Qualcomm hardware HEVC at 8-bit. Choose the slower CPU encoder to preserve 10-bit color. Both modes default to automatic bitrate control. The hardware mode chooses a target from output resolution and frame rate and displays it during export. CPU encoding uses x265 CRF 18 with the `faster` preset by default. Check **Set target bitrate** to enter a manual target in Mb/s for either encoder.

Use Advanced for separate stream indices, lens geometry, a rotation correction CSV, or the path to FFmpeg if it is not found. Keep the matching FFprobe beside it. Apply stabilization is checked by default. It uses the built-in IMU on factory-calibrated X6 exports, or the rotation CSV if you supply one. Uncheck it to export without stabilization. Use Cancel to stop an export or Open output folder after verification.

## Factory-calibrated X6 stitch

The desktop app enables **Factory calibration and seam blending (X6)** by default for single-file INSV sources. It requires NumPy and the native telemetry executable. Build the executable as shown below or supply its location on the command line with `--telemetry-tool`.

```powershell
python -m pip install numpy
cargo build --locked --release --manifest-path telemetry/Cargo.toml
python insta360_arm.py --require-arm64 convert clip.insv calibrated.mp4 --calibrated --seconds 5
```

This path reads both lens models from the recording and feathers their overlap. It uses fixed CPU maps and nearest-neighbor lens sampling. It does not compensate for nearby parallax, per-lens exposure differences, or rolling shutter. At 8K, it needs several hundred MB of temporary map files alongside the destination and can take more memory and time than the ideal-lens mode. Uncheck the option to use the earlier projection or to use manual lens geometry settings.

For lower-resolution output, select the desired sphere size. The converter projects the original lens images directly to that size. Resizing the lens images beforehand changes their pixel geometry and requires a corresponding calibration transform. A higher-resolution render followed by a high-quality downscale can reduce aliasing, at greater processing cost.

### Experimental hardware paths

**Lens video decoding** under Advanced can use Direct3D 11 or 12 for calibrated Windows exports. It uses one decoder submission thread per lens and stops on a decode error. Software frames are then passed to the calibrated stitcher.

**Calibrated stitch processor** defaults to **Auto (fastest available)** on Windows. For compatible X6 exports, Auto can use the native Direct3D helpers for 8-bit hardware HEVC and 10-bit x265. The Main 10 helper uses GPU decoding and stitching, then passes the shared planar output directly to CPU x265. The FFmpeg path selects OpenCL for compatible 10-bit X6 input when using CPU lens decoding and the device passes a P010 check. Otherwise it uses CPU stitching. OpenCL combines projection, seam blending and automatic rotation in one GPU pass and also works with stabilization off. Select **CPU** to override the default or use a manual correction CSV. The Zenbook test used OpenCLOn12 device 1.0 and the Adreno X2-90 GPU. Linux defaults to CPU stitching. See the [native build instructions](docs/native-d3d11.md).

```powershell
python insta360_arm.py --require-arm64 convert clip.insv fused.mp4 --calibrated --auto-stabilize --stitch-device opencl --encoder mf --bits 8 --bitrate 120M --seconds 5
```

Use `--opencl-device PLATFORM.DEVICE` to select a different OpenCL device. Inspect FFmpeg's verbose device listing to confirm which device runs the shader.

**Native Direct3D 11 (preview)** is a Windows ARM64 path for the tested X6 29.97 fps recording mode and 8-bit hardware HEVC. It keeps the frame pipeline on the same GPU device and verifies lens order before starting the export. [Build, usage and measurements](docs/native-d3d11.md)

## Inspect and convert

```powershell
python insta360_arm.py --require-arm64 inspect clip.insv
python insta360_arm.py --require-arm64 convert clip.insv preview.mp4 --size 1920x960 --seconds 5
python insta360_arm.py --require-arm64 convert left.insv sphere.mp4 --right right.insv --size 5760x2880 --fps 30000/1001 --bits 10 --bitrate 75M
```

Use the first command before selecting a workflow. The converter accepts two explicitly selected video tracks or two files with one lens track each. A single file with exactly two video tracks is assumed to contain a lens pair in stream order. If you see a proxy, preview or other video track, select the absolute indices using `--left-stream` and `--right-stream`. Both inputs must come from the same capture with matching cadence and alignment. The converter checks available start and duration information, which cannot detect drift or prove synchronization. A prepacked dual-fisheye track is outside this interface.

| Option | Default and behavior |
| --- | --- |
| `--size WxH` | Twice the square crop width by the crop height. Set the known capture-mode dimensions explicitly if the source is cropped. |
| `--fps` | Keep filtered source cadence. Supply a positive rate or rational to resample by dropping or repeating frames. |
| `--bits 8/10` | Keep source 4:2:0 depth. An explicit 10-bit output cannot recover precision missing from an 8-bit source. |
| `--bitrate` or `--crf` | Omit bitrate to use x265 CRF 18 or a resolution- and frame-rate-based target for Media Foundation. The CLI x265 preset defaults to `fast`. An explicit bitrate takes precedence. |
| `--left-crop`, `--right-crop` | Full frame. Use even `SIZE:SIZE:X:Y` values to fit the lens circles. |
| `--left-turns`, `--right-turns`, `--swap` | No changes. Set quarter-turns clockwise or swap the stacked views to match lens orientation. |
| `--fov`, `--interp` | Shared 180° FOV and bilinear interpolation. These are ideal-lens defaults. |
| `--motion` | No correction. Accepts the CSV described below. |

Audio from the first input is copied if present and must be MP4-compatible. The pipeline carries recognized color declarations from the lens streams. Dolby Vision metadata, camera data tracks, spatial audio signaling and I-Log color transforms are outside the MVP. An output is published only after HEVC, bit depth, size and spherical metadata are verified by FFprobe. Use a new output filename.

## Apply rotation corrections

For automatic X6 stabilization, add `--auto-stabilize` to a `--calibrated` export. The extractor reads the sensor samples in the first lens's coordinate frame. The estimator integrates gyro rotation, gently aligns up with gravity using the accelerometer, and smooths heading. It applies one correction per source frame before any output frame-rate change. It uses the parser's timestamps and supports `--imu-offset-ms` for a measured sync correction. A positive offset uses a later sensor sample for each frame. Keep the default zero unless you have measured an offset.

Automatic stabilization requires timestamped X6 IMU data and constant-rate footage. The Windows desktop app defaults to Vulkan for the rotation pass, which was verified on the Zenbook's Adreno GPU. Choose `cpu` in Advanced if your GPU or FFmpeg build lacks Vulkan projection. The Linux desktop app and the CLI default to CPU. The CLI accepts `--stabilization-device vulkan` to use the GPU. The CPU pass is expensive at 8K. Stabilization corrects camera orientation. Camera translation, rolling shutter and absolute heading drift remain outside the current estimator.

To use externally derived corrections instead, pass a CSV with this format:

```csv
time_s,yaw_deg,pitch_deg,roll_deg
0,0,0,0
0.033333,0.2,-0.1,0.5
```

Pass this file using `--motion rotations.csv`. Times are seconds from the normalized video timeline and must increase from zero. Angles are absolute corrections in FFmpeg v360 yaw, pitch, roll composition order. Each row takes effect at the first eligible frame and is held until the next row. Supply a synchronized per-frame correction sequence for smooth motion. The ideal-lens path combines rotation and projection in one sampling step. The calibrated path rotates the blended sphere in a second pass. Dynamic updates rebuild the CPU rotation map and can be much slower than a static stitch. These rotations address orientation changes. Camera translation, nearby parallax, motion blur and rolling shutter require other techniques.

### Extract camera motion and rig data

On Windows, build the optional telemetry tool with native ARM64 Rust and the ARM64 MSVC linker. On Linux, use the native Rust target and system linker. The converter itself has no Rust dependency.

```powershell
cargo build --locked --manifest-path telemetry/Cargo.toml
.\telemetry\target\debug\insta360-telemetry.exe clip.insv > motion-and-rig.json
```

The tool reads the original file and writes JSON with interpolated IMU samples, timestamp information and the parsed camera rig when available. The pinned parser normalizes gyroscope values to degrees per second and accelerometer values to m/s² where those units are known. Preserve the raw JSON timeline until the video offset and axis directions are checked. The JSON is an input to developing a motion estimator. The converter's CSV expects the final rotation corrections.

## Try Media Foundation

```powershell
python insta360_arm.py --require-arm64 encode-test mf-4k.mp4 --encoder mf --bits 8 --size 3840x1920 --fps 30 --bitrate 35M
python insta360_arm.py --require-arm64 convert clip.insv mf-preview.mp4 --encoder mf --bits 8 --size 3840x1920 --bitrate 35M
```

MF requests hardware encoding explicitly and errors if it cannot complete. If it fails, use x265. The current upstream FFmpeg wrapper advertises NV12 but lacks P010 input. This project uses x265 for 10-bit encoding. [FFmpeg Media Foundation source](https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/libavcodec/mfenc.c)

See [research and validation](docs/research.md) and the [benchmark protocol](docs/benchmark.md).
