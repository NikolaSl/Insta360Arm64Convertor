import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from insta360_arm import automatic_hardware_bitrate, box, boxes, corrections, encoder_args, machine, probe, videos


class UnitTests(unittest.TestCase):
    def test_elf_architecture_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "native-tool"
            for machine_code, expected in ((183, "ARM64"), (62, "x64")):
                for order, tag in (("little", 1), ("big", 2)):
                    path.write_bytes(b"\x7fELF" + bytes((2, tag)) + bytes(12)
                                     + machine_code.to_bytes(2, order))
                    self.assertEqual(machine(path), expected)

    def test_mf_rejects_ten_bit(self):
        with self.assertRaisesRegex(ValueError, "8-bit"):
            encoder_args("mf", 10, "25M", 18, "fast")

    def test_automatic_bitrate_uses_output_size_and_cadence(self):
        self.assertEqual(automatic_hardware_bitrate(7680, 3840, 30), "120M")
        self.assertEqual(automatic_hardware_bitrate(6016, 3008, 30), "60M")
        self.assertEqual(automatic_hardware_bitrate(5760, 2880, 30), "60M")
        self.assertEqual(automatic_hardware_bitrate(5760, 2880, 60), "120M")
        self.assertEqual(automatic_hardware_bitrate(3840, 1920, 60), "70M")
        self.assertEqual(encoder_args("x265", 10, None, 18, "faster")[-2:], ["-crf", "18"])
        self.assertEqual(encoder_args("x265", 10, "40M", 18, "faster")[-2:], ["-b:v", "40M"])

    def test_boxes(self):
        self.assertEqual(list(boxes(box(b"abcd", b"abc"))), [(b"abcd", b"abc")])
        with self.assertRaises(ValueError):
            list(boxes(b"broken"))

    def test_motion(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "motion.csv"
            header = "time_s,yaw_deg,pitch_deg,roll_deg\n"
            path.write_text(header + "0,20,0,0\n1,0,0,0\n")
            command = corrections(path)
            self.assertIn("0.000000000-1.000000000 v360@stitch reset_rot -1", command)
            self.assertIn("v360@stitch reset_rot -1, v360@stitch yaw 20", command)
            path.write_text(header + "0,0,0,0\n0,0,0,0\n")
            with self.assertRaises(ValueError):
                corrections(path)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "FFmpeg and FFprobe are required on PATH")
class IntegrationTests(unittest.TestCase):
    def cli(self, *args):
        result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "insta360_arm.py"),
                                 *map(str, args)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def fixture(self, source, pix):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi",
                        "-i", f"testsrc2=size=96x96:rate=4,format={pix}",
                        "-f", "lavfi", "-i", f"color=blue:size=96x96:rate=4,format={pix}",
                        "-f", "lavfi", "-i", "sine=frequency=440",
                        "-map", "0:v", "-map", "1:v", "-map", "2:a", "-t", "1",
                        "-c:v", "libx265", "-preset", "ultrafast", "-c:a", "aac", str(source)],
                       check=True, capture_output=True)

    def test_tracks_defaults_audio_and_spherical_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / "test.insv", Path(temp) / "sphere.mp4"
            self.fixture(source.with_suffix(".mp4"), "yuv420p10le")
            source.with_suffix(".mp4").rename(source)
            self.cli("convert", source, output, "--preset", "ultrafast")
            result = probe("ffprobe", output)
            [video] = videos(result)
            self.assertEqual((video["width"], video["height"], video["pix_fmt"]),
                             (192, 96, "yuv420p10le"))
            self.assertEqual(video["avg_frame_rate"], "4/1")
            self.assertTrue(any(x.get("projection") == "equirectangular"
                                for x in video["side_data_list"]))
            self.assertIn("aac", [s["codec_name"] for s in result["streams"]])

    def test_pair_motion_changes_pixels_and_resets(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source, right, output = (folder / n for n in ("pair.mp4", "right.mp4", "sphere.mp4"))
            self.fixture(source, "yuv420p")
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:v:1",
                            "-c", "copy", str(right)], check=True)
            motion = folder / "motion.csv"
            motion.write_text("time_s,yaw_deg,pitch_deg,roll_deg\n0,0,0,0\n"
                              "0.25,90,30,20\n0.5,0,0,0\n")
            self.cli("convert", source, output, "--left-stream", 0, "--right", right,
                     "--motion", motion, "--size", "128x64", "--fps", "4", "--bitrate", "1M")
            # The blue hemisphere covers the right edge at rest. Compare decoded
            # patches rather than whole frames because the other lens is animated.
            data = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(output),
                                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
            def sample(frame, x, y):
                start = ((frame * 64 + y) * 128 + x) * 3
                return data[start:start + 3]
            candidates = [(x, y) for y in (16, 32, 48) for x in range(8, 125, 8)]
            self.assertTrue(any(sample(0, x, y)[2] > 220 and sample(0, x, y)[0] < 30
                                and sample(1, x, y)[2] < 180
                                and sample(2, x, y)[2] > 220 and sample(2, x, y)[0] < 30
                                for x, y in candidates))
