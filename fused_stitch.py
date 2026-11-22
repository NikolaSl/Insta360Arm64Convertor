"""Build inputs for the opt-in fused P010 OpenCL projection."""

from array import array
from fractions import Fraction
import math
from pathlib import Path
import sys

from calibrated_stitch import camera_matrix, validate_rig
from motion_stabilization import IDENTITY


def _number(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Non-finite value in factory calibration")
    return f"{value:.10e}f"


def _list(values):
    return ", ".join(map(_number, values))


def _vector(values):
    return f"(float{len(values)})({_list(values)})"


def _camera(camera, stream, name):
    optics = camera["intrinsics"]
    model = optics["model"]["Mei"]
    affine = camera["calib_to_picture"]
    fov = camera["fov_limit"]
    rows = ", ".join(_vector(row) for row in camera_matrix(camera))
    distortion = _list(model["dist"]["RadtanPro"]["d"])
    return (
        f"__constant Camera {name} = {{ {{ {rows} }}, {_number(model['xi'])}, "
        f"{{ {distortion} }}, {_vector(optics['focal'])}, {_vector(optics['centre'])}, "
        f"{_number(optics['skew'])}, {_vector(affine['a'][0])}, {_vector(affine['a'][1])}, "
        f"{_vector(affine['t'])}, {_number(math.cos(fov))}, "
        f"{_number(math.cos(fov-math.radians(5)))}, "
        f"{_vector((stream['width'], stream['height']))} }};\n"
    )


def supports_fused_input(left, right):
    return (
        all(stream.get("pix_fmt") == "yuv420p10le"
            and stream.get("chroma_location") == "left" and stream.get("color_range") == "tv"
            for stream in (left, right))
        and all(left.get(key) and left[key] == right.get(key)
                for key in ("time_base", "start_time", "nb_frames", "r_frame_rate"))
    )


def fused_graph(rig, streams, width, height, count):
    cameras, indices = validate_rig(rig, streams)
    by_index = {stream["index"]: stream for stream in streams}
    if not supports_fused_input(*streams):
        raise ValueError("Fused OpenCL stitching requires synchronized, limited-range 10-bit X6 tracks with left chroma")
    constants = f"#define OUTPUT_WIDTH {width}\n#define OUTPUT_HEIGHT {height}\n#define ROTATIONS {count}u\n"
    for i, (camera, index) in enumerate(zip(cameras, indices)):
        constants += _camera(camera, by_index[index], f"CAMERA{i}")
    table_height = (count + 255)//256
    time_base = str(Fraction(streams[0]["time_base"]))
    frame_rate = str(Fraction(streams[0]["r_frame_rate"]))
    inputs = ["-f", "rawvideo", "-pixel_format", "yuv444p16le",
              "-video_size", f"1024x{table_height}", "-framerate", frame_rate,
              "-i", "orientation.raw"]
    graph = [f"[0:{index}]format=p010le,hwupload[g{i}]" for i, index in enumerate(indices)]
    # program_opencl copies lens PTS but leaves the output link in the first
    # input's time base. Keep the static table on that same clock.
    graph.append(f"[1:v]settb={time_base},hwupload[orientation]")
    project = ("[g0][g1][orientation]program_opencl=source=stitch.cl:kernel=stitch:"
               f"inputs=3:size={width}x{height},hwdownload,format=p010le,setsar=1")
    return inputs, graph, project, constants


def prepare_fused(folder, constants, count, orientations=None):
    if orientations is None:
        orientations = ((0, IDENTITY),)
    size = ((count + 255)//256)*1024
    values = array("H", [32768])*size
    frames = 0
    for index, quaternion in orientations:
        if index != frames or index >= count:
            raise ValueError("Unexpected frame index in stabilization table")
        values[4*index:4*index+4] = array("H", (
            round((max(-1, min(1, x)) + 1)*32767.5) for x in quaternion))
        frames += 1
    if frames != count:
        raise ValueError("Incomplete stabilization table")
    if sys.byteorder == "big":
        values.byteswap()
    with (folder / "orientation.raw").open("wb") as target:
        # FFmpeg supplies the plane at the current output index from every input.
        for _ in range(3):
            values.tofile(target)
    template = Path(__file__).with_name("fused_stitch.cl").read_text(encoding="ascii")
    (folder / "stitch.cl").write_text(template.replace("/* CAMERA_CONSTANTS */", constants),
                                     encoding="ascii")
