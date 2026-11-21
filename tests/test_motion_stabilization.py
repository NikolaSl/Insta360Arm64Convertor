import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from insta360_arm import rotation_commands
from motion_stabilization import (GRAVITY, IDENTITY, UP, estimate_rotations, inverse,
                                  product, rotate, scaled_axis, v360_angles)


class StabilizationTests(unittest.TestCase):
    def test_rotation_decomposition(self):
        for pitch in (23, 90, -90):
            axes = ((0, 1, 0), (1, 0, 0), (0, 0, 1))
            q = IDENTITY
            for axis, angle in zip(axes, (35, pitch, -12)):
                q = product(q, scaled_axis(axis, math.radians(angle)))
            recovered = IDENTITY
            for axis, angle in zip(axes, v360_angles(q)):
                recovered = product(recovered, scaled_axis(axis, math.radians(angle)))
            self.assertAlmostEqual(abs(sum(x*y for x, y in zip(q, recovered))), 1)

    def test_static_tilt_levels_gravity(self):
        physical_tilt = scaled_axis((1, 0, 0), math.radians(15))
        accel = rotate(inverse(physical_tilt), (0, -GRAVITY, 0))
        samples = [{"timestamp_ms": i, "gyro": (0, 0, 0), "accl": accel}
                   for i in range(-100, 301, 5)]
        result = list(estimate_rotations(samples, IDENTITY, 25, 5))
        self.assertEqual([i for i, _ in result], list(range(6)))
        for _, (yaw, pitch, roll) in result:
            self.assertAlmostEqual(yaw, 0)
            self.assertAlmostEqual(pitch, -15)
            self.assertAlmostEqual(roll, 0)

    def test_yaw_is_smoothed_and_imu_coverage_is_checked(self):
        samples = [{"timestamp_ms": i, "gyro": (0, 50 if i > 0 else 0, 0),
                    "accl": tuple(GRAVITY*v for v in UP)}
                   for i in range(-100, 501, 5)]
        yaw = list(estimate_rotations(samples, IDENTITY, 25, 10))[-1][1][0]
        self.assertGreater(yaw, -20)
        self.assertLess(yaw, -9)
        with self.assertRaisesRegex(ValueError, "ends before"):
            list(estimate_rotations(samples, IDENTITY, 25, 20))

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_vulkan_runtime_rotations_match_cpu(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("NumPy is required for pixel comparison")
        ffmpeg = shutil.which("ffmpeg")
        hardware = ["-init_hw_device", "vulkan=vk:0", "-filter_hw_device", "vk"]
        source = ["-f", "lavfi", "-i", "testsrc2=size=256x128:rate=4"]
        probe = subprocess.run([ffmpeg, "-v", "error", *hardware, *source, "-vf",
                                "hwupload,v360_vulkan=e:e,hwdownload,format=yuv420p",
                                "-frames:v", "1", "-f", "null", "-"], capture_output=True)
        if probe.returncode:
            self.skipTest("FFmpeg Vulkan projection is unavailable on this device")
        with tempfile.TemporaryDirectory() as directory:
            frames = []
            for gpu in (False, True):
                (Path(directory) / "motion.cmd").write_text(
                    rotation_commands(0, 28, 17, -24, vulkan=gpu)
                    + rotation_commands(0.2, -15, 0, 9, vulkan=gpu), encoding="ascii")
                graph = "sendcmd=f=motion.cmd," + (
                    "hwupload,v360_vulkan@stitch=e:e:rorder=rpy,hwdownload,format=yuv420p"
                    if gpu else "v360@stitch=e:e")
                output = subprocess.check_output(
                    [ffmpeg, "-v", "error", *(hardware if gpu else []), *source, "-vf", graph,
                     "-frames:v", "2", "-f", "rawvideo", "-pix_fmt", "gray", "-"], cwd=directory)
                frames.append(np.frombuffer(output, dtype=np.uint8).reshape(2, -1))
            for cpu, gpu in zip(*frames):
                self.assertGreater(np.corrcoef(cpu, gpu)[0, 1], 0.98)
