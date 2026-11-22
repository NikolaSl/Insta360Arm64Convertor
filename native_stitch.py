"""Prepare the D3D11 stitcher and verify its lens order against FFmpeg."""

from fractions import Fraction
from pathlib import Path
import os
import re
import shutil
import struct
import subprocess

from calibrated_stitch import camera_indices
from fused_stitch import fused_graph, supports_fused_input


ROOT = Path(__file__).resolve().parent
FPS = Fraction(30000, 1001)


def native_executable(bits=8):
    name = "insta360_main10.exe" if bits == 10 else "d3d11_surface_probe.exe"
    tooling = ROOT / ".tooling"
    for path in (tooling / name, ROOT / "tools" / name,
                 tooling / "ffmpeg-master-latest-winarm64-gpl-shared" / "bin" / name):
        if path.is_file():
            return str(path)
    return shutil.which(name)


def supports_native_input(left, right, width, height):
    return (supports_fused_input(left, right) and 0 < width <= 7680 and 0 < height <= 3840
            and all((s["width"], s["height"], s.get("codec_name")) == (3840, 3840, "hevc")
                    and Fraction(s["avg_frame_rate"]) == FPS
                    and Fraction(s["r_frame_rate"]) == FPS
                    and Fraction(s["start_time"]) == 0
                    and all(s.get(key) == "bt709" for key in (
                        "color_space", "color_transfer", "color_primaries"))
                    for s in (left, right)))


def fingerprint(ffmpeg, source, index):
    result = subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-i", str(source), "-map", f"0:{index}",
         "-vf", "extractplanes=y", "-frames:v", "1", "-pix_fmt", "gray10le",
         "-f", "rawvideo", "-"],
        capture_output=True, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if len(result.stdout) != 3840*3840*2:
        raise ValueError("Could not verify the first decoded lens frame")
    return b"".join(result.stdout[i:i+2] for y in range(8) for x in range(8)
                    for i in (2*((y*480+240)*3840 + x*480+240),))


def prepare_native(folder, ffmpeg, source, streams, rig, width, height, count, bitrate, poses):
    *_, constants = fused_graph(rig, streams, width, height, count)
    constants = constants.replace("__constant Camera", "static const Camera")
    constants = re.sub(r"\(float([23])\)\(", r"float\1(", constants)
    shader = (ROOT / "tools" / "d3d11_stitch.hlsl").read_text(encoding="ascii")
    (folder / "native-stitch.hlsl").write_text(
        shader.replace("/* CAMERA_CONSTANTS */", constants), encoding="ascii")
    expected = b"".join(fingerprint(ffmpeg, source, index) for index in camera_indices(rig))
    with (folder / "native-poses.bin").open("wb") as output:
        output.write(struct.pack("<4I", count, bitrate, width, height) + expected)
        written = 0
        for index, quaternion in poses:
            if index != written or written >= count:
                raise ValueError("Unexpected frame index in native pose table")
            output.write(struct.pack("<4f", *quaternion))
            written += 1
        if written != count:
            raise ValueError("Incomplete native pose table")
