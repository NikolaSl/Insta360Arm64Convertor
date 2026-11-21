from pathlib import Path
import tempfile
import unittest

from calibrated_stitch import prepare_maps

try:
    import numpy as np
except ImportError:
    np = None


@unittest.skipIf(np is None, "NumPy is required for calibrated projection")
class CalibrationTests(unittest.TestCase):
    def test_back_to_back_lenses_cover_the_sphere_and_blend(self):
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
        rig = {
            "pictures": [{"VideoTrack": 0}, {"VideoTrack": 1}],
            "projection": {
                "kind": {"Rig": {"cameras": [lens(1, identity), lens(0, dict(w=0, x=0, y=1, z=0))]}},
                "pose": identity, "stereo": {"layout": "Mono"},
            },
        }
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
