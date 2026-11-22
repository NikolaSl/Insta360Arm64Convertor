from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from calibrated_stitch import prepare_maps
from fused_stitch import fused_graph, prepare_fused
from motion_stabilization import IDENTITY

try:
    import numpy as np
except ImportError:
    np = None


def synthetic_rig():
    def lens(index, rotation):
        return {
            "picture": {"input": index, "rect": None},
            "intrinsics": {
                "model": {"Mei": {"xi": 2.0, "dist": {"RadtanPro": {"d": [0]*13}}}},
                "focal": [100, 100], "centre": [64, 64], "skew": 0,
            },
            "rotation": rotation, "fov_limit": 1.65,
            "calib_to_picture": {"a": [[1, 0], [0, 1]], "t": [0, 0]},
            "mirror": False, "masks": [], "image_circle": None,
        }

    identity = dict(w=1, x=0, y=0, z=0)
    return {
        "pictures": [{"VideoTrack": 0}, {"VideoTrack": 1}],
        "projection": {
            "kind": {"Rig": {"cameras": [lens(1, identity), lens(0, dict(w=0, x=0, y=1, z=0))]}},
            "pose": identity, "stereo": {"layout": "Mono"},
        },
    }


@unittest.skipIf(np is None, "NumPy is required for calibrated projection")
class CalibrationTests(unittest.TestCase):
    def test_back_to_back_lenses_cover_the_sphere_and_blend(self):
        rig = synthetic_rig()
        streams = [{"index": i, "width": 128, "height": 128} for i in (0, 1)]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            prepare_maps(folder, rig, streams, 256, 128, 10)
            x0 = np.fromfile(folder / "lens0-x.raw", dtype="<u2").reshape(128, 256)
            x1 = np.fromfile(folder / "lens1-x.raw", dtype="<u2").reshape(128, 256)
            mask = np.fromfile(folder / "mask444.raw", dtype="<u2").reshape(3, 128, 256)
            self.assertTrue(np.all((x0 != 65535) | (x1 != 65535)))
            self.assertEqual((x0[64, 128], x1[64, 0]), (64, 64))
            self.assertEqual((mask[0, 64, 128], mask[0, 64, 0]), (0, 1023))
            self.assertTrue(np.any((mask[0, 64] > 0) & (mask[0, 64] < 1023)))
            np.testing.assert_array_equal(mask[0], mask[1])
            np.testing.assert_array_equal(mask[0], mask[2])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_fused_projection_applies_frame_indexed_rotations(self):
        ffmpeg = shutil.which("ffmpeg")
        flags = ["-v", "error", "-init_hw_device",
                 "opencl=stitcher:1.0" if os.name == "nt" else "opencl=stitcher:0.0",
                 "-filter_hw_device", "stitcher"]
        probe = subprocess.run([ffmpeg, *flags, "-f", "lavfi", "-i", "color=size=32x32",
                                "-vf", "format=p010le,hwupload,hwdownload,format=p010le",
                                "-frames:v", "1", "-f", "null", "-"], capture_output=True)
        if probe.returncode:
            self.skipTest("OpenCL device unavailable")
        streams = [dict(index=i, width=128, height=128, pix_fmt="yuv420p10le",
                        color_range="tv", chroma_location="left", time_base="1/16384",
                        r_frame_rate="2/1", start_time="0.000000", nb_frames="2") for i in (0, 1)]
        inputs, graph, project, constants = fused_graph(synthetic_rig(), streams, 256, 128, 2)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            subprocess.run([
                ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                "color=red:size=128x128:rate=2,format=yuv420p10le",
                "-f", "lavfi", "-i", "color=blue:size=128x128:rate=2,format=yuv420p10le",
                "-map", "0:v", "-map", "1:v", "-t", "1",
                "-c:v", "libx265", "-preset", "ultrafast", "pair.mp4",
            ], check=True, cwd=folder, capture_output=True)
            prepare_fused(folder, constants, 2, ((0, IDENTITY), (1, (0, 0, 1, 0))))
            result = subprocess.run(
                [ffmpeg, *flags, "-loglevel", "info", "-i", "pair.mp4", *inputs,
                 "-filter_complex", ";".join([*graph, f"{project},showinfo[sphere]"]),
                 "-map", "[sphere]", "-frames:v", "2", "-fps_mode", "passthrough",
                 "-pix_fmt", "p010le", "-f", "rawvideo", "-"],
                cwd=folder, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            timestamps = [float(line.split(b"pts_time:")[1].split()[0])
                          for line in result.stderr.splitlines() if b"pts_time:" in line]
            self.assertEqual(timestamps[:2], [0, 0.5])
            frames = np.frombuffer(result.stdout, dtype="<u2").reshape(2, -1)
            centre = frames[:, 64*256+128] >> 6
            # The front lens is blue. A 180-degree turn shows the red rear lens.
            self.assertTrue(140 < centre[0] < 190, centre)
            self.assertTrue(280 < centre[1] < 355, centre)
