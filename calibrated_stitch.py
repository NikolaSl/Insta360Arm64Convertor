"""Static X6 Mei/RadtanPro projection and overlap maps for FFmpeg."""

from contextlib import ExitStack
import math
import shutil


def camera_indices(rig):
    cameras = rig["projection"]["kind"]["Rig"]["cameras"]
    return [rig["pictures"][c["picture"]["input"]]["VideoTrack"] for c in cameras]


def camera_maps(camera, ray, width, height, np):
    intrinsics = camera["intrinsics"]
    model = intrinsics["model"]["Mei"]
    d = model["dist"]["RadtanPro"]["d"]
    rotation = camera["rotation"]
    w, x, y, z = (rotation[k] for k in "wxyz")
    # The rig stores camera-to-rig. Project by applying its inverse.
    matrix = (
        (1 - 2*(y*y + z*z), 2*(x*y + z*w), 2*(x*z - y*w)),
        (2*(x*y - z*w), 1 - 2*(x*x + z*z), 2*(y*z + x*w)),
        (2*(x*z + y*w), 2*(y*z - x*w), 1 - 2*(x*x + y*y)),
    )
    a, b, c = (sum(component * weight for component, weight in zip(ray, row)) for row in matrix)
    denom = c + model["xi"]
    with np.errstate(divide="ignore", invalid="ignore"):
        mx, my = a / denom, b / denom
        r2 = mx*mx + my*my
        radial = 1 + r2*(d[0] + r2*(d[1] + r2*(d[2] + r2*(d[3] + r2*d[4]))))
        t1, t2 = d[5] + d[7]*r2, d[6] + d[8]*r2
        xd = mx*radial + (r2 + 2*mx*mx)*t1 + 2*mx*my*t2 + r2*(d[9] + d[11]*r2)
        yd = my*radial + (r2 + 2*my*my)*t2 + 2*mx*my*t1 + r2*(d[10] + d[12]*r2)
        fx, fy = intrinsics["focal"]
        cx, cy = intrinsics["centre"]
        u, v = fx*xd + intrinsics["skew"]*yd + cx, fy*yd + cy
        affine = camera["calib_to_picture"]
        ((a0, a1), (a2, a3)), (t0, t1) = affine["a"], affine["t"]
        px, py = np.rint(a0*u + a1*v + t0), np.rint(a2*u + a3*v + t1)
    limit = math.cos(camera["fov_limit"])
    valid = ((c >= limit) & (denom > 0) & (px >= 0) & (px < width) & (py >= 0) & (py < height))
    # Fade over the outer five degrees of valid coverage.
    inner = math.cos(camera["fov_limit"] - math.radians(5))
    weight = np.where(valid, np.clip((c - limit)/(inner - limit), 0.00001, 1), 0)
    return np.where(valid, px, 65535), np.where(valid, py, 65535), weight


def prepare_maps(folder, rig, streams, width, height, bits):
    try:
        import numpy as np
    except ImportError as error:
        raise ValueError("Calibrated stitching requires NumPy in this Python installation") from error
    projection = rig["projection"]
    cameras = projection["kind"]["Rig"]["cameras"]
    if (len(cameras) != 2 or projection["stereo"]["layout"] != "Mono" or
            projection["pose"] != {"w": 1.0, "x": 0.0, "y": 0.0, "z": 0.0} or
            any(c["picture"]["rect"] or c["mirror"] or c["masks"] or c["image_circle"]
                for c in cameras)):
        raise ValueError("Unsupported X6 rig geometry")
    indices = camera_indices(rig)
    if set(indices) != {s["index"] for s in streams}:
        raise ValueError("Factory calibration does not match the selected tracks")
    by_index = {s["index"]: s for s in streams}
    if max(width, height, *(s["width"] for s in streams), *(s["height"] for s in streams)) >= 65535:
        raise ValueError("Calibration maps require dimensions below 65535")
    theta = ((np.arange(width, dtype=np.float32) + 0.5) / width - 0.5) * (2*math.pi)
    sin_theta, cos_theta = np.sin(theta), np.cos(theta)
    names = ["lens0-x.raw", "lens0-y.raw", "lens1-x.raw", "lens1-y.raw", "blend.raw"]
    dtype = np.uint8 if bits == 8 else np.dtype("<u2")
    # Generate stripes so the floating-point intermediate arrays stay small at 8K.
    with ExitStack() as stack:
        files = [stack.enter_context((folder / name).open("wb")) for name in names]
        for top in range(0, height, 64):
            phi = ((np.arange(top, min(top+64, height), dtype=np.float32) + 0.5)
                   / height - 0.5)[:, None] * math.pi
            ray = np.cos(phi)*sin_theta, np.sin(phi), np.cos(phi)*cos_theta
            weights = []
            for i, (camera, index) in enumerate(zip(cameras, indices)):
                stream = by_index[index]
                x, y, weight = camera_maps(camera, ray, stream["width"], stream["height"], np)
                x.astype("<u2").tofile(files[2*i])
                y.astype("<u2").tofile(files[2*i+1])
                weights.append(weight)
            total = weights[0] + weights[1]
            mask = np.divide(weights[1], total, out=np.zeros_like(total), where=total > 0)
            np.rint(mask * ((1 << bits) - 1)).astype(dtype).tofile(files[4])
    # maskedmerge needs the same mask in every 4:4:4 plane.
    with (folder / names[-1]).open("rb") as source, (folder / "mask444.raw").open("wb") as target:
        for _ in range(3):
            source.seek(0)
            shutil.copyfileobj(source, target)
    (folder / names[-1]).unlink()
