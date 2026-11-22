"""Experimental dual-fisheye conversion using a native FFmpeg executable."""

import argparse
import csv
from fractions import Fraction
from itertools import repeat
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

from calibrated_stitch import camera_indices, prepare_maps
from fused_stitch import fused_graph, prepare_fused, supports_fused_input
from motion_stabilization import IDENTITY, estimate_quaternions, product, scaled_axis, unit, v360_angles
from native_stitch import native_executable, prepare_native, supports_native_input


def run(args, capture=False, cwd=None, final=True, is_ffmpeg=True):
    return subprocess.run(
        args, check=True, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE if capture else None, cwd=cwd,
        creationflags=(subprocess.CREATE_NO_WINDOW
                       if os.name == "nt" and Path(sys.executable).stem == "pythonw" else 0),
    ).stdout


def probe(exe, path):
    return json.loads(run([exe, "-v", "error", "-show_streams", "-show_format",
                           "-of", "json", str(path)], capture=True))


def machine(exe):
    with open(exe, "rb") as f:
        ident = f.read(20)
        if ident.startswith(b"\x7fELF"):
            if len(ident) < 20 or ident[5] not in (1, 2):
                raise ValueError("Invalid ELF header")
            order = "little" if ident[5] == 1 else "big"
            code = int.from_bytes(ident[18:20], order)
            if ident[4] == 2:
                return {183: "ARM64", 62: "x64"}.get(code, hex(code))
            return {40: "ARM", 3: "x86"}.get(code, hex(code))
        if not ident.startswith(b"MZ"):
            return "unknown"
        f.seek(60)
        offset, = struct.unpack("<I", f.read(4))
        f.seek(offset)
        if f.read(4) != b"PE\0\0":
            raise ValueError("Invalid PE signature")
        code, = struct.unpack("<H", f.read(2))
    return {0xAA64: "ARM64", 0x8664: "x64", 0x14C: "x86"}.get(code, hex(code))


def executable(name):
    location = shutil.which(name)
    if not location:
        raise ValueError(f"Cannot find {name}. Add FFmpeg to PATH or supply its full path.")
    return str(Path(location).resolve())


def rate(value):
    result = Fraction(value)
    if not 0 < result <= 1000:
        raise ValueError("FPS must be positive and at most 1000")
    return result


def depth(stream):
    fmt = stream.get("pix_fmt", "")
    if fmt in {"yuv420p", "yuvj420p", "nv12"}:
        return 8
    if fmt in {"yuv420p10le", "p010le"}:
        return 10
    raise ValueError(f"Unsupported source pixel format {fmt!r}. Expected 8- or 10-bit 4:2:0.")


def videos(info):
    return [s for s in info["streams"] if s["codec_type"] == "video"
            and not s.get("disposition", {}).get("attached_pic")]


def choose(info, index):
    candidates = videos(info)
    if index is None and len(candidates) == 1:
        return candidates[0]
    for stream in candidates:
        if stream["index"] == index:
            return stream
    raise ValueError(f"Select an absolute video stream index from {[s['index'] for s in candidates]}")


def crop(text, stream):
    if text is None:
        values = (stream["width"], stream["height"], 0, 0)
    else:
        values = tuple(map(int, text.split(":")))
    if len(values) != 4:
        raise ValueError("Crop must be SIZE:SIZE:X:Y")
    w, h, x, y = values
    if (w != h or w <= 0 or min(x, y) < 0 or any(n % 2 for n in values)
            or x + w > stream["width"] or y + h > stream["height"]):
        raise ValueError("Each lens requires an even, square crop inside its stream")
    return values


def rotation_commands(t, yaw, pitch, roll, vulkan=False, end=None):
    target = "v360_vulkan@stitch" if vulkan else "v360@stitch"
    commands = [] if vulkan else [f"{target} reset_rot -1"]
    # This Vulkan shader reads the matrix transposed. Reverse the order and
    # negate the angles so it samples the same directions as the CPU filter.
    if vulkan:
        yaw, pitch, roll = -yaw, -pitch, -roll
    commands.extend(f"{target} {axis} {angle:.9f}"
                    for axis, angle in zip(("yaw", "pitch", "roll"), (yaw, pitch, roll))
                    if vulkan or angle != 0)
    interval = f"{t:.9f}" if end is None else f"{t:.9f}-{end:.9f}"
    return f"{interval} {', '.join(commands)};\n"


def corrections(path):
    lines = []
    previous = -1
    with open(path, newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != ["time_s", "yaw_deg", "pitch_deg", "roll_deg"]:
            raise ValueError("Motion CSV header must be time_s,yaw_deg,pitch_deg,roll_deg")
        for row in reader:
            t, yaw, pitch, roll = (float(row[k]) for k in reader.fieldnames)
            if (not all(math.isfinite(v) for v in (t, yaw, pitch, roll))
                    or t < 0 or t <= previous or (not lines and t != 0)
                    or max(abs(yaw), abs(pitch), abs(roll)) > 180
                    or (lines and f"{t:.9f}" == f"{previous:.9f}")):
                raise ValueError("Motion requires finite angles in [-180,180] and increasing times from zero")
            lines.append((t, yaw, pitch, roll))
            previous = t
    if not lines:
        raise ValueError("Motion CSV is empty")
    return "".join(rotation_commands(*row, end=lines[i+1][0] if i+1 < len(lines) else None)
                   for i, row in enumerate(lines))


def automatic_hardware_bitrate(width, height, fps):
    """Choose an HEVC target for the output size and cadence."""
    presets = ((1920 * 960, 15), (3840 * 1920, 35), (5760 * 2880, 60),
               (6016 * 3008, 60), (7680 * 3840, 120))
    pixels = width * height
    low_pixels, low_rate = 0, 0
    for high_pixels, high_rate in presets:
        if pixels <= high_pixels:
            target = low_rate + (high_rate - low_rate) * (pixels - low_pixels) / (high_pixels - low_pixels)
            break
        low_pixels, low_rate = high_pixels, high_rate
    else:
        target = low_rate * pixels / low_pixels
    return f"{max(1, round(target * float(fps) / 30))}M"


def encoder_args(encoder, bits, bitrate, crf, preset):
    if bitrate and not re.fullmatch(r"[1-9][0-9]*[kKmM]?", bitrate):
        raise ValueError("Bitrate must be positive, optionally suffixed with k or M")
    if encoder == "mf":
        if bits != 8:
            raise ValueError("The FFmpeg Media Foundation path in this MVP supports only 8-bit NV12")
        if os.name != "nt":
            raise ValueError("Media Foundation is available on Windows. Select the libx265 encoder.")
        if not bitrate:
            raise ValueError("Media Foundation requires a resolved bitrate target")
        return ["-c:v", "hevc_mf", "-hw_encoding", "1", "-pix_fmt", "nv12", "-b:v", bitrate]
    args = ["-c:v", "libx265", "-preset", preset,
            "-pix_fmt", "yuv420p10le" if bits == 10 else "yuv420p"]
    return args + (["-b:v", bitrate] if bitrate else ["-crf", str(crf)])


def box(kind, payload):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def boxes(data):
    pos = 0
    while pos < len(data):
        if len(data) - pos < 8:
            raise ValueError("Truncated MP4 box")
        size, kind = struct.unpack_from(">I4s", data, pos)
        if not 8 <= size <= len(data) - pos:
            raise ValueError("Unsupported or damaged nested MP4 box")
        yield kind, data[pos + 8:pos + size]
        pos += size


def tag_spherical(path):
    """Insert Spherical Video V2 into FFmpeg's final moov; mdat offsets stay fixed."""
    zeros = bytes(4)
    spherical = box(b"sv3d", box(b"svhd", zeros + b"Insta360 ARM MVP\0") +
                    box(b"proj", box(b"prhd", bytes(16)) + box(b"equi", bytes(20))))
    count = 0

    def update(data, level=0):
        nonlocal count
        chain = (b"trak", b"mdia", b"minf", b"stbl", b"stsd")
        if level == len(chain):
            prefix = data[:8]
            entries = []
            for kind, payload in boxes(data[8:]):
                if kind in {b"hvc1", b"hev1"}:
                    if len(payload) < 78:
                        raise ValueError("Truncated HEVC visual sample entry")
                    children = b"".join(box(k, p) for k, p in boxes(payload[78:])
                                        if k not in {b"sv3d", b"st3d"})
                    payload = payload[:78] + children + box(b"st3d", bytes(5)) + spherical
                    count += 1
                entries.append(box(kind, payload))
            return prefix + b"".join(entries)
        return b"".join(box(k, update(p, level + 1) if k == chain[level] else p)
                        for k, p in boxes(data))

    end = path.stat().st_size
    with path.open("r+b") as f:
        while f.tell() < end:
            start = f.tell()
            size, kind = struct.unpack(">I4s", f.read(8))
            header = 8
            if size == 1:
                size, = struct.unpack(">Q", f.read(8))
                header = 16
            if not header <= size <= end - start:
                raise ValueError("Invalid top-level MP4 box")
            if kind == b"moov":
                if start + size != end:
                    raise ValueError("Expected FFmpeg's final moov atom")
                result = update(f.read(size - header))
                if count != 1:
                    raise ValueError("Expected one HEVC sample entry")
                f.seek(start)
                f.write(box(b"moov", result))
                f.truncate()
                return
            f.seek(start + size)
    raise ValueError("Missing MP4 moov atom")


def encode(ffmpeg, ffprobe, args, output, bits, width, height, motion=None, runner=run,
           prepare=None, render=None, frame_count=None):
    output = output.resolve()
    if output.suffix.lower() != ".mp4" or output.exists() or not output.parent.is_dir():
        raise ValueError("Output must be a new .mp4 in an existing directory")
    with tempfile.TemporaryDirectory(prefix=".insta360-", dir=output.parent) as temp:
        work = Path(temp)
        candidate = work / "output.mp4"
        if motion:
            (work / "motion.cmd").write_text(corrections(motion), encoding="ascii")
        started = time.perf_counter()
        if prepare:
            prepare(work)
        if render:
            render(work, candidate)
        else:
            runner([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "verbose", "-benchmark",
                    *args, "-tag:v", "hvc1", str(candidate)], cwd=work)
        tag_spherical(candidate)
        [stream] = videos(probe(ffprobe, candidate))
        spherical = any(s.get("projection") == "equirectangular"
                        for s in stream.get("side_data_list", []))
        if (stream.get("codec_name") != "hevc" or depth(stream) != bits
                or (stream["width"], stream["height"]) != (width, height) or not spherical):
            raise ValueError("Encoded video failed codec, dimensions, depth, or 360 metadata verification")
        if frame_count is not None and int(stream.get("nb_frames", 0)) != frame_count:
            raise ValueError("Native output frame count does not match the requested duration")
        # Publish atomically without replacing any output created by another process.
        if os.name == "nt":
            candidate.rename(output)
        else:
            os.link(candidate, output)
        elapsed = time.perf_counter() - started
        duration = float(stream.get("duration", 0))
        print(json.dumps({"output": str(output), "seconds": round(elapsed, 3),
                          "video_seconds": duration, "realtime_factor": duration / elapsed}))


COLOR_FIELDS = {"color_range": "color_range", "color_space": "colorspace",
                "color_transfer": "color_trc", "color_primaries": "color_primaries"}


def read_calibration(path, tool, motion):
    folder = Path(__file__).resolve().parent / "telemetry" / "target"
    name = "insta360-telemetry.exe" if os.name == "nt" else "insta360-telemetry"
    candidates = [folder / mode / name for mode in ("release", "debug")]
    tool = tool or next((str(p) for p in candidates if p.is_file()), name)
    mode = "--lens-imu" if motion else "--rig-only"
    data = json.loads(run([executable(tool), mode, str(path)], capture=True))
    if data["camera_model"] != "Insta360 X6" or not data["rig"]:
        raise ValueError("Calibrated stitching requires a supported X6 recording")
    return data


def stabilization_frame_count(stream, fps, seconds):
    count = int(stream["nb_frames"])
    if seconds is not None:
        count = min(count, math.ceil(seconds * fps))
    if count <= 0:
        raise ValueError("Automatic stabilization needs the source frame count")
    return count


def automatic_orientations(data, stream, fps, count, offset_ms):
    if not data["accurate_timestamps"] or rate(stream["r_frame_rate"]) != fps:
        raise ValueError("Automatic stabilization needs timestamped IMU and constant frame rate")
    projection = data["rig"]["projection"]
    lens0 = projection["kind"]["Rig"]["cameras"][0]["rotation"]
    lens_rotation = unit(tuple(lens0[k] for k in "wxyz"))
    # The parser's X6 lens-oriented IMU still uses its sideways optical basis.
    # Image feature motion on the X6 confirms (-z, y, x) before the lens residual.
    lens_to_rig = product(lens_rotation, scaled_axis((0, 1, 0), -math.pi/2))
    return estimate_quaternions(data.pop("imu"), lens_to_rig, fps, count - 1, offset_ms)


def automatic_commands(data, stream, fps, seconds, offset_ms, vulkan=False):
    count = stabilization_frame_count(stream, fps, seconds)
    # FFmpeg rounds timeline comparisons to microseconds. Place each command
    # between frame timestamps so rounding cannot delay it by a frame.
    return "".join(rotation_commands(float(max(0, frame - 0.25) / fps),
                                     *v360_angles(q), vulkan=vulkan,
                                     end=float((frame + 0.75) / fps) if frame + 1 < count else None)
                   for frame, q in automatic_orientations(data, stream, fps, count, offset_ms))


def opencl_ready(ffmpeg, device):
    try:
        probe = subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-init_hw_device", f"opencl=stitcher:{device}",
             "-filter_hw_device", "stitcher", "-f", "lavfi", "-i", "color=size=32x32",
             "-vf", "format=p010le,hwupload,hwdownload,format=p010le",
             "-frames:v", "1", "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


class NativeUnavailable(ValueError):
    """The native export could not start and may be retried using FFmpeg."""


def native_compatible(args, streams, fps, width, height, bits):
    native_encoder = ((args.encoder == "mf" and bits == 8)
                      or (args.encoder == "x265" and bits == 10 and width % 4 == 0))
    return (os.name == "nt" and args.calibrated and not args.motion and native_encoder
            and (args.fps is None or rate(args.fps) == fps)
            and args.decode_device == "cpu"
            and not (args.right or args.left_crop or args.right_crop or args.left_turns
                     or args.right_turns or args.swap)
            and supports_native_input(*streams, width, height))


def native_convert(args, ffmpeg, ffprobe, source, streams, fps, width, height, bits, runner):
    if not native_compatible(args, streams, fps, width, height, bits):
        raise ValueError("Native D3D11 needs calibrated X6 29.97 fps BT.709 tracks, source frame rate "
                         "and Windows 8-bit hardware HEVC or 10-bit x265. Use Auto for other settings.")
    software = bits == 10
    version_arg, expected_version = (("--main10-version", "insta360-main10-1") if software
                                     else ("--protocol-version", "insta360-d3d11-1"))
    try:
        tool = executable(args.native_tool or native_executable(bits) or
                          ("insta360_main10.exe" if software else "d3d11_surface_probe.exe"))
        if run([tool, version_arg], capture=True).strip() != expected_version:
            raise NativeUnavailable("Rebuild the native stitch executable for this converter version")
    except (OSError, subprocess.CalledProcessError) as error:
        raise NativeUnavailable(f"The native D3D11 executable could not start: {error}") from error
    left = streams[0]
    count = stabilization_frame_count(left, fps, args.seconds)
    target = args.bitrate or "0"
    digits = target.rstrip("kKmM")
    scale = {"": 1, "k": 1000, "m": 1000000}[target[len(digits):].lower()]
    bitrate = int(digits) * scale
    if bitrate > 0xffffffff:
        raise ValueError("Native bitrate exceeds the Media Foundation limit")

    def render(folder, candidate):
        calibration = read_calibration(source, args.telemetry_tool, args.auto_stabilize)
        poses = (automatic_orientations(calibration, left, fps, count, args.imu_offset_ms)
                 if args.auto_stabilize else enumerate(repeat(IDENTITY, count)))
        prepare_native(folder, ffmpeg, source, streams, calibration["rig"], width,
                       height, count, bitrate, poses)
        raw = folder / "native-video.mp4"
        try:
            command = [tool, "--stitch-main10" if software else "--stitch", str(source),
                       "native-stitch.hlsl", "native-poses.bin", raw.name]
            if software:
                command.extend((args.preset, str(args.crf)))
            runner(command, cwd=folder, final=False, is_ffmpeg=False)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            if not raw.exists():
                raise NativeUnavailable(f"Native D3D11 could not start: {error}") from error
            raise
        runner([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "verbose",
                "-i", "native-video.mp4", "-i", str(source), "-map", "0:v:0",
                "-map", "1:a:0?", "-t", str(float(count/fps)), "-c", "copy",
                "-tag:v", "hvc1", "-map_metadata", "-1", "-map_chapters", "-1",
                str(candidate)], cwd=folder)

    return encode(ffmpeg, ffprobe, [], Path(args.output), bits, width, height,
                  runner=runner, render=render, frame_count=count)


def calibrated_graph(rig, width, height, bits, color_range):
    indices = camera_indices(rig)
    pixel = "yuv444p10le" if bits == 10 else "yuv444p"
    inputs = []
    for name in ("lens0-x", "lens0-y", "lens1-x", "lens1-y", "mask444"):
        inputs += ["-f", "rawvideo", "-pixel_format", pixel if name == "mask444" else "gray16le",
                   "-video_size", f"{width}x{height}", "-i", f"{name}.raw"]
    source_range = f"scale=in_range={color_range}:out_range={color_range}," if color_range else ""
    graph = [
        f"[0:{index}]{source_range}format={pixel},setsar=1[cal{i}];"
        f"[cal{i}][{1+2*i}:v][{2+2*i}:v]remap[l{i}]"
        for i, index in enumerate(indices)]
    return inputs, graph, "[l0][l1][5:v]maskedmerge"


def convert(args, ffmpeg, ffprobe, runner=run, on_auto_bitrate=None):
    paths = [Path(args.input).resolve(strict=True)]
    if args.right:
        paths.append(Path(args.right).resolve(strict=True))
    infos = [probe(ffprobe, path) for path in paths]
    if len(paths) == 1 and len(videos(infos[0])) == 2:
        default_left, default_right = (v["index"] for v in videos(infos[0]))
    else:
        default_left = default_right = None
    left = choose(infos[0], args.left_stream if args.left_stream is not None else default_left)
    right = choose(infos[-1], args.right_stream if args.right_stream is not None else default_right)
    if len(paths) == 1 and left["index"] == right["index"]:
        raise ValueError("Input must contain two distinct lens tracks or use --right")
    left_crop, right_crop = crop(args.left_crop, left), crop(args.right_crop, right)
    source_bits = depth(left)
    if left_crop[0] != right_crop[0] or source_bits != depth(right):
        raise ValueError("The two crops must have the same size and bit depth")

    def color(stream, key):
        return stream.get(key) or ("pc" if key == "color_range" and stream["pix_fmt"] == "yuvj420p" else None)

    if any(color(left, k) != color(right, k) for k in COLOR_FIELDS):
        raise ValueError("Lens color declarations differ")
    start_times = [Fraction(s["start_time"]) for s in (left, right)]
    rates = [rate(s["avg_frame_rate"]) for s in (left, right)]
    if rates[0] != rates[1] or abs(start_times[0] - start_times[1]) >= 1 / (2 * rates[0]):
        raise ValueError("Lens rates or start times differ. Align the sources before converting.")
    if all("duration" in s for s in (left, right)):
        if abs(Fraction(left["duration"]) - Fraction(right["duration"])) > 1 / rates[0]:
            raise ValueError("Lens durations differ by more than one frame")
    if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0):
        raise ValueError("Preview duration must be positive")
    if args.auto_stabilize and not args.calibrated:
        raise ValueError("Automatic stabilization requires factory-calibrated X6 stitching")
    if args.stitch_device == "opencl" and (not args.calibrated or args.motion):
        raise ValueError("Fused OpenCL stitching requires factory calibration and no manual CSV")
    if args.stitch_device == "opencl" and args.decode_device != "cpu":
        raise ValueError("This FFmpeg path cannot combine Direct3D decoding with OpenCL stitching. "
                         "Select CPU decoding for OpenCL, or CPU stitching for Direct3D.")
    if args.decode_device != "cpu" and (os.name != "nt" or not args.calibrated):
        raise ValueError("Direct3D decoding is available for factory-calibrated X6 exports on Windows")
    stitch_device = args.stitch_device
    if not math.isfinite(args.imu_offset_ms):
        raise ValueError("IMU offset must be finite")
    w, h = args.size or (left_crop[0] * 2, left_crop[0])
    if h < 2 or h % 2 or w != 2 * h:
        raise ValueError("Output must be 2:1 with even dimensions")
    if not 180 <= args.fov <= 360:
        raise ValueError("FOV must be between 180 and 360 degrees")
    bits = args.bits or (8 if args.encoder == "mf" else source_bits)
    if args.encoder == "mf" and not args.bitrate:
        target = automatic_hardware_bitrate(w, h, rate(args.fps) if args.fps else rates[0])
        args = argparse.Namespace(**(vars(args) | {"bitrate": target}))
        if on_auto_bitrate:
            on_auto_bitrate(target)
        else:
            print(f"Automatic hardware bitrate target: {target[:-1]} Mb/s", file=sys.stderr)
    enc = encoder_args(args.encoder, bits, args.bitrate, args.crf, args.preset)
    try_native = (stitch_device == "auto" and (args.native_tool or native_executable(bits))
                  and native_compatible(args, (left, right), rates[0], w, h, bits))
    if stitch_device == "native" or try_native:
        try:
            return native_convert(args, ffmpeg, ffprobe, paths[0], (left, right),
                                  rates[0], w, h, bits, runner)
        except NativeUnavailable:
            if stitch_device == "native":
                raise
            print("Native Direct3D could not start. Trying the FFmpeg stitcher.", file=sys.stderr)
    if stitch_device == "auto":
        use_opencl = (os.name == "nt" and args.calibrated and not args.motion
                      and args.decode_device == "cpu" and supports_fused_input(left, right)
                      and opencl_ready(ffmpeg, args.opencl_device))
        stitch_device = "opencl" if use_opencl else "cpu"
    intermediate = "yuv420p10le" if source_bits == 10 else (
        "yuvj420p" if color(left, "color_range") == "pc" else "yuv420p")
    target = "nv12" if args.encoder == "mf" else ("yuv420p10le" if bits == 10 else "yuv420p")
    graph = []
    for i, (stream, geometry, turns) in enumerate(
            ((left, left_crop, args.left_turns), (right, right_crop, args.right_turns))):
        filters = [f"crop={':'.join(map(str, geometry))}", "setsar=1", f"format={intermediate}"]
        filters += {0: [], 1: ["transpose=clock"], 2: ["hflip", "vflip"],
                    3: ["transpose=cclock"]}[turns]
        graph.append(f"[{min(i, len(paths)-1)}:{stream['index']}]{','.join(filters)}[l{i}]")
    order = "[l1][l0]" if args.swap else "[l0][l1]"
    motion = "sendcmd=f=motion.cmd," if args.motion or args.auto_stabilize else ""
    cadence = f",fps={args.fps}" if args.fps else ""
    rng = color(left, "color_range")
    range_filter = f",scale=in_range={rng}:out_range={rng}" if rng in {"pc", "tv"} else ""
    cmd = []
    if args.decode_device != "cpu":
        download_pixel = "p010le" if source_bits == 10 else "nv12"
        # With threaded HEVC decoding the Qualcomm driver failed to begin
        # frames. A single submission thread per lens works on this device.
        cmd += ["-xerror", "-threads:v", "1", "-hwaccel", args.decode_device,
                "-hwaccel_output_format", download_pixel]
    for path in paths:
        cmd += ["-noautorotate", "-i", str(path)]
    prepare = None
    if args.calibrated:
        if args.right or args.left_crop or args.right_crop or args.left_turns or args.right_turns or args.swap:
            raise ValueError("Factory calibration requires both original tracks and their unmodified geometry")
        calibration = read_calibration(paths[0], args.telemetry_tool, args.auto_stabilize)
        rig = calibration["rig"]
    if stitch_device == "opencl":
        count = stabilization_frame_count(left, rates[0], args.seconds) if args.auto_stabilize else 1
        raw_inputs, graph, project, constants = fused_graph(rig, (left, right), w, h, count)
        cmd[:0] = ["-init_hw_device", f"opencl=stitcher:{args.opencl_device}",
                   "-filter_hw_device", "stitcher"]
        cmd += raw_inputs

        def prepare(folder):
            orientations = (automatic_orientations(calibration, left, rates[0], count,
                                                   args.imu_offset_ms) if args.auto_stabilize else None)
            prepare_fused(folder, constants, count, orientations)
    elif args.calibrated:
        extra_inputs, graph, project = calibrated_graph(rig, w, h, source_bits,
                                                       rng if rng in {"pc", "tv"} else None)
        cmd += extra_inputs
        vulkan = args.auto_stabilize and args.stabilization_device == "vulkan"
        if vulkan:
            cmd[:0] = ["-init_hw_device", "vulkan=stabilizer:0", "-filter_hw_device", "stabilizer"]
            upload = "yuv420p10le" if source_bits == 10 else "yuv420p"
            project += (f",{motion}format={upload},hwupload,"
                        f"v360_vulkan@stitch=equirect:equirect:rorder=rpy,hwdownload,format={upload}")
        elif motion:
            project += f",{motion}v360@stitch=equirect:equirect:interp={args.interp}"

        def prepare(folder):
            if args.auto_stabilize:
                commands = automatic_commands(calibration, left, rates[0], args.seconds,
                                              args.imu_offset_ms, vulkan)
                (folder / "motion.cmd").write_text(commands, encoding="ascii")
            prepare_maps(folder, rig, (left, right), w, h, source_bits)
    else:
        project = (f"{order}hstack=shortest=1,{motion}v360@stitch=dfisheye:equirect:"
                   f"w={w}:h={h}:ih_fov={args.fov}:iv_fov={args.fov}:interp={args.interp}")
    graph.append(f"{project}{cadence}{range_filter},format={target}[sphere]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "[sphere]", "-map", "0:a:0?",
            "-map_metadata", "-1", "-map_chapters", "-1", "-c:a", "copy",
            "-fps_mode", "passthrough", *enc]
    if args.seconds is not None:
        cmd += ["-t", str(args.seconds)]
    for field, flag in COLOR_FIELDS.items():
        value = color(left, field)
        if value and value not in {"unknown", "unspecified", "reserved"}:
            cmd += [f"-{flag}", value]
    encode(ffmpeg, ffprobe, cmd, Path(args.output), bits, w, h, args.motion, runner, prepare)


def size(value):
    w, h = map(int, value.lower().split("x"))
    return w, h


def build_parser(parser_class=argparse.ArgumentParser):
    parser = parser_class(description=__doc__)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--require-arm64", action="store_true")
    sub = parser.add_subparsers(dest="action", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("input")
    sub.add_parser("doctor")
    convert_parser = sub.add_parser("convert")
    convert_parser.add_argument("input")
    convert_parser.add_argument("output")
    convert_parser.add_argument("--right")
    for lens in ("left", "right"):
        convert_parser.add_argument(f"--{lens}-stream", type=int)
        convert_parser.add_argument(f"--{lens}-crop")
        convert_parser.add_argument(f"--{lens}-turns", type=int, choices=range(4), default=0)
    convert_parser.add_argument("--swap", action="store_true")
    convert_parser.add_argument("--fov", type=float, default=180)
    convert_parser.add_argument("--interp", choices=("near", "line", "cube"), default="line")
    convert_parser.add_argument("--fps", type=lambda x: str(rate(x)))
    stabilization = convert_parser.add_mutually_exclusive_group()
    stabilization.add_argument("--motion", type=Path)
    stabilization.add_argument("--auto-stabilize", action="store_true")
    convert_parser.add_argument("--imu-offset-ms", type=float, default=0.0)
    convert_parser.add_argument("--stabilization-device", choices=("cpu", "vulkan"), default="cpu")
    convert_parser.add_argument("--calibrated", action="store_true")
    convert_parser.add_argument("--stitch-device", choices=("auto", "cpu", "opencl", "native"), default="auto")
    convert_parser.add_argument("--native-tool", help="Path to the ARM64 Direct3D stitcher executable")
    convert_parser.add_argument("--decode-device", choices=("cpu", "d3d11va", "d3d12va"), default="cpu")
    convert_parser.add_argument("--opencl-device", default="1.0" if os.name == "nt" else "0.0")
    convert_parser.add_argument("--telemetry-tool", help="Path to the native telemetry executable")
    convert_parser.add_argument("--seconds", type=float, help="Limit output to a short preview")
    for command in (convert_parser, sub.add_parser("encode-test")):
        command.add_argument("--size", type=size)
        command.add_argument("--bits", type=int, choices=(8, 10))
        command.add_argument("--encoder", choices=("x265", "mf"), default="x265")
        command.add_argument("--bitrate")
        command.add_argument("--crf", type=int, choices=range(52), default=18)
        command.add_argument("--preset", choices=("ultrafast", "superfast", "veryfast", "faster",
                                                  "fast", "medium", "slow", "slower", "veryslow",
                                                  "placebo"), default="fast")
    test = sub.choices["encode-test"]
    test.add_argument("output")
    test.add_argument("--fps", type=lambda x: str(rate(x)), default="30")
    test.add_argument("--seconds", type=float, default=2)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        ffprobe = executable(args.ffprobe)
        if args.action == "inspect" and not args.require_arm64:
            print(json.dumps(probe(ffprobe, Path(args.input).resolve(strict=True)), indent=2))
            return
        ffmpeg = executable(args.ffmpeg)
        if args.require_arm64 and any(machine(e) != "ARM64" for e in (ffmpeg, ffprobe)):
            raise ValueError("Both FFmpeg tools must be native ARM64 executables")
        if args.action == "inspect":
            print(json.dumps(probe(ffprobe, Path(args.input).resolve(strict=True)), indent=2))
        elif args.action == "doctor":
            print(json.dumps({"host": platform.machine(), "python": platform.architecture()[0],
                              "ffmpeg": machine(ffmpeg), "ffprobe": machine(ffprobe)}, indent=2))
            run([ffmpeg, "-version"])
            encoder = "hevc_mf" if os.name == "nt" else "libx265"
            run([ffmpeg, "-hide_banner", "-h", f"encoder={encoder}"])
        elif args.action == "encode-test":
            w, h = args.size or (1920, 960)
            bits = args.bits or 8
            if w != 2*h or h < 2 or h % 2 or not 0 < args.seconds <= 300:
                raise ValueError("Test requires even 2:1 dimensions and 0 < seconds <= 300")
            bitrate = args.bitrate
            if args.encoder == "mf" and not bitrate:
                bitrate = automatic_hardware_bitrate(w, h, rate(args.fps))
                print(f"Automatic hardware bitrate target: {bitrate[:-1]} Mb/s", file=sys.stderr)
            enc = encoder_args(args.encoder, bits, bitrate, args.crf, args.preset)
            fmt = "nv12" if args.encoder == "mf" else ("yuv420p10le" if bits == 10 else "yuv420p")
            encode(ffmpeg, ffprobe, ["-f", "lavfi", "-i",
                   f"testsrc2=size={w}x{h}:rate={args.fps},format={fmt}", "-t", str(args.seconds),
                   *enc], Path(args.output), bits, w, h)
        else:
            convert(args, ffmpeg, ffprobe)
    except (OSError, ValueError, TypeError, KeyError, ZeroDivisionError, struct.error,
            subprocess.CalledProcessError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
