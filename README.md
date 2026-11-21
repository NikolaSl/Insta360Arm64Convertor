# Insta360 ARM64 Pipeline

Experimental Windows on Arm command-line converter for two synchronized, square-cropped fisheye views. It produces a mono 2:1 equirectangular HEVC MP4 with Spherical Video V2 metadata. It supports factory-calibrated X6 projection, overlap blending and automatic rotational stabilization using the camera's motion data.

## Setup

Use native ARM64 Python 3.11 or later and put matching ARM64 `ffmpeg.exe` and `ffprobe.exe` on PATH. The current BtbN Windows ARM64 GPL build provides a starting point for libx265. Pin and record the exact build for repeatable runs. [FFmpeg downloads](https://ffmpeg.org/download.html) · [BtbN releases](https://github.com/BtbN/FFmpeg-Builds/releases)

```powershell
python insta360_arm.py --require-arm64 doctor
python -m unittest discover -s tests -v
python insta360_arm.py --require-arm64 encode-test test.mp4 --size 1920x960 --bits 10
```

`doctor` inspects the PE machine type of both FFmpeg tools and prints the options exposed by the build. Seeing `hevc_mf` in that output only confirms the wrapper is compiled in.

## Windows desktop app

Double-click `Launch Insta360 Converter.cmd` or run `python insta360_gui.py`. Choose a source and MP4 destination, then select the CPU or Qualcomm hardware encoder. Resolution, frame rate, bit depth, target bitrate and preview length are available on the Convert tab. The bitrate slider and numeric field use Mb/s. Hardware encoding selects 8-bit and requires a target bitrate.

Use Advanced for separate stream indices, lens geometry, a rotation correction CSV, or the path to `ffmpeg.exe` if FFmpeg is not found. Keep the matching `ffprobe.exe` beside it. Apply stabilization is checked by default. It uses the built-in IMU on factory-calibrated X6 exports, or the rotation CSV if you supply one. Uncheck it to export without stabilization. Use Cancel to stop an export or Open output folder after verification.

## Factory-calibrated X6 stitch

The desktop app enables **Factory calibration and seam blending (X6)** by default for single-file INSV sources. It requires NumPy and the native telemetry executable. Build the executable as shown below or supply its location on the command line with `--telemetry-tool`.

```powershell
python -m pip install numpy
cargo build --locked --release --manifest-path telemetry/Cargo.toml
python insta360_arm.py --require-arm64 convert clip.insv calibrated.mp4 --calibrated --seconds 5
```

This path reads both lens models from the recording and feathers their overlap. It uses fixed CPU maps and nearest-neighbor lens sampling. It does not compensate for nearby parallax, per-lens exposure differences, or rolling shutter. At 8K, it needs several hundred MB of temporary map files alongside the destination and can take more memory and time than the ideal-lens mode. Uncheck the option to use the earlier projection or to use manual lens geometry settings.

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
| `--bitrate` or `--crf` | x265 defaults to CRF 18 and preset `fast`. An explicit bitrate takes precedence. MF requires an explicit bitrate. |
| `--left-crop`, `--right-crop` | Full frame. Use even `SIZE:SIZE:X:Y` values to fit the lens circles. |
| `--left-turns`, `--right-turns`, `--swap` | No changes. Set quarter-turns clockwise or swap the stacked views to match lens orientation. |
| `--fov`, `--interp` | Shared 180° FOV and bilinear interpolation. These are ideal-lens defaults. |
| `--motion` | No correction. Accepts the CSV described below. |

Audio from the first input is copied if present and must be MP4-compatible. The pipeline carries recognized color declarations from the lens streams. Dolby Vision metadata, camera data tracks, spatial audio signaling and I-Log color transforms are outside the MVP. An output is published only after HEVC, bit depth, size and spherical metadata are verified by FFprobe. Use a new output filename.

## Apply rotation corrections

For automatic X6 stabilization, add `--auto-stabilize` to a `--calibrated` export. The extractor reads the sensor samples in the first lens's coordinate frame. The estimator integrates gyro rotation, gently aligns up with gravity using the accelerometer, and smooths heading. It applies one correction per source frame before any output frame-rate change. It uses the parser's timestamps and supports `--imu-offset-ms` for a measured sync correction. A positive offset uses a later sensor sample for each frame. Keep the default zero unless you have measured an offset.

Automatic stabilization requires timestamped X6 IMU data and constant-rate footage. The desktop app defaults to Vulkan for the rotation pass, which was verified on the Zenbook's Adreno GPU with the bundled build. Choose `cpu` in Advanced if your GPU or FFmpeg build lacks Vulkan projection. The CLI defaults to CPU and accepts `--stabilization-device vulkan` to use the GPU. The CPU pass is expensive at 8K. Stabilization corrects camera orientation. Camera translation, rolling shutter and absolute heading drift remain outside the current estimator.

To use externally derived corrections instead, pass a CSV with this format:

```csv
time_s,yaw_deg,pitch_deg,roll_deg
0,0,0,0
0.033333,0.2,-0.1,0.5
```

Pass this file using `--motion rotations.csv`. Times are seconds from the normalized video timeline and must increase from zero. Angles are absolute corrections in FFmpeg v360 yaw, pitch, roll composition order. Each row takes effect at the first eligible frame and is held until the next row. Supply a synchronized per-frame correction sequence for smooth motion. The ideal-lens path combines rotation and projection in one sampling step. The calibrated path rotates the blended sphere in a second pass. Dynamic updates rebuild the CPU rotation map and can be much slower than a static stitch. These rotations address orientation changes. Camera translation, nearby parallax, motion blur and rolling shutter require other techniques.

### Extract camera motion and rig data

Build the optional telemetry tool with native ARM64 Rust and the ARM64 MSVC linker. The converter itself has no Rust dependency.

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

MF requests hardware encoding explicitly and errors if it cannot complete. There is no silent fallback. If it fails, use x265. The current upstream FFmpeg wrapper advertises NV12 but lacks P010 input. This project uses x265 for 10-bit and CPU processing for decoding and projection. [FFmpeg Media Foundation source](https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/libavcodec/mfenc.c)

See [research and validation](docs/research.md) and the [benchmark protocol](docs/benchmark.md).
