from pathlib import Path
from fractions import Fraction
import struct
import tempfile
import unittest
from unittest.mock import patch

from motion_stabilization import IDENTITY
from native_stitch import prepare_native, supports_native_input
from insta360_arm import build_parser, native_compatible


def lens(index):
    return {
        "index": index, "width": 3840, "height": 3840, "codec_name": "hevc",
        "pix_fmt": "yuv420p10le", "chroma_location": "left", "color_range": "tv",
        "color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709",
        "avg_frame_rate": "30000/1001", "r_frame_rate": "30000/1001",
        "start_time": "0.000000", "time_base": "1/30000", "nb_frames": "2",
    }


class NativeStitchTests(unittest.TestCase):
    def test_main10_uses_cpu_encoder_and_aligned_planar_output(self):
        args = build_parser().parse_args(["convert", "clip.insv", "out.mp4", "--calibrated",
                                          "--encoder", "x265", "--bits", "10"])
        streams = (lens(0), lens(1))
        fps = Fraction(30000, 1001)
        with patch("insta360_arm.os.name", "nt"):
            self.assertTrue(native_compatible(args, streams, fps, 7680, 3840, 10))
            self.assertFalse(native_compatible(args, streams, fps, 7680, 3840, 8))
            self.assertFalse(native_compatible(args, streams, fps, 7678, 3839, 10))
            args.motion = "rotations.csv"
            self.assertFalse(native_compatible(args, streams, fps, 7680, 3840, 10))
    def test_native_eligibility_rejects_unsupported_rate_and_color(self):
        left, right = lens(0), lens(1)
        self.assertTrue(supports_native_input(left, right, 5760, 2880))
        self.assertFalse(supports_native_input(left, right, 8192, 4096))
        self.assertFalse(supports_native_input(left, dict(right, avg_frame_rate="30/1"), 7680, 3840))
        self.assertFalse(supports_native_input(left, dict(right, color_transfer="smpte2084"),
                                               7680, 3840))

    def test_native_table_uses_factory_order_and_contains_every_pose(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            fingerprints = [bytes([11])*128, bytes([22])*128]
            with (patch("native_stitch.fused_graph", return_value=([], [], "", "#define ROTATIONS 2u")),
                  patch("native_stitch.camera_indices", return_value=[1, 0]),
                  patch("native_stitch.fingerprint", side_effect=lambda ff, source, i: fingerprints[i])):
                prepare_native(folder, "ffmpeg", Path("clip.insv"), (lens(0), lens(1)), {},
                               7680, 3840, 2, 120000000, enumerate((IDENTITY, IDENTITY)))
            payload = (folder / "native-poses.bin").read_bytes()
            self.assertEqual(struct.unpack_from("<4I", payload), (2, 120000000, 7680, 3840))
            self.assertEqual(payload[16:272], fingerprints[1] + fingerprints[0])
            self.assertEqual(struct.unpack_from("<8f", payload, 272), IDENTITY + IDENTITY)
