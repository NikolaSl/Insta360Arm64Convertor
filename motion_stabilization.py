"""Estimate X6 sphere rotations from time-aligned gyroscope and acceleration."""

import math


UP = (0, -1, 0)
FORWARD = (0, 0, 1)
IDENTITY = (1, 0, 0, 0)
GRAVITY = 9.80665


def cross(a, b):
    x, y, z = a
    u, v, w = b
    return y*w-z*v, z*u-x*w, x*v-y*u


def product(a, b):
    w, x, y, z = a
    u, i, j, k = b
    return (w*u-x*i-y*j-z*k, w*i+x*u+y*k-z*j,
            w*j-x*k+y*u+z*i, w*k+x*j-y*i+z*u)


def inverse(q):
    w, x, y, z = q
    return w, -x, -y, -z


def unit(values):
    length = math.hypot(*values)
    return tuple(x / length for x in values)


def rotate(q, v):
    return product(product(q, (0, *v)), inverse(q))[1:]


def scaled_axis(vector, scale):
    length = math.hypot(*vector)
    if not length or not scale:
        return IDENTITY
    half = length * scale / 2
    factor = math.sin(half) / length
    return math.cos(half), *(v*factor for v in vector)


def level_rotation(acceleration):
    """Shortest rotation from measured up to world up."""
    a = unit(acceleration)
    if a[1] > 0.9999999:
        return 0, 1, 0, 0
    return unit((1 - a[1], *cross(a, UP)))


def v360_angles(q):
    """Decompose output-ray to input-ray rotation as Ry(yaw) Rx(pitch) Rz(roll)."""
    w, x, y, z = q
    sin_pitch = max(-1, min(1, 2*(w*x-y*z)))
    pitch = math.asin(sin_pitch)
    if abs(sin_pitch) > 1 - 1e-10:
        yaw = math.atan2(2*(w*y-x*z), 1-2*(y*y+z*z))
        roll = 0
    else:
        yaw = math.atan2(2*(x*z+w*y), 1-2*(x*x+y*y))
        roll = math.atan2(2*(x*y+w*z), 1-2*(x*x+z*z))
    return tuple(map(math.degrees, (yaw, pitch, roll)))


def estimate_rotations(samples, lens_to_rig, fps, last_frame, offset_ms=0):
    """Yield frame-indexed rotations. Positive offset samples the IMU later."""
    step_ms = 1000 / float(fps)
    start = offset_ms
    previous = None
    frame = 0
    attitude = IDENTITY
    heading = None
    for sample in samples:
        timestamp = sample["timestamp_ms"]
        if timestamp < start - 3000:
            continue
        gyro, accel = sample["gyro"], sample["accl"]
        if gyro is None or accel is None:
            continue
        if not all(math.isfinite(v) for v in (timestamp, *gyro, *accel)):
            raise ValueError("X6 IMU contains non-finite values")
        gyro, accel = rotate(lens_to_rig, gyro), rotate(lens_to_rig, accel)
        strength = math.hypot(*accel)
        gravity_weight = max(0, 1 - abs(strength - GRAVITY)/(0.2*GRAVITY))
        if previous is None:
            if timestamp > start:
                raise ValueError("X6 IMU starts after the first requested frame")
            if gravity_weight <= 0:
                continue
            attitude = level_rotation(accel)
            dt = 0
        else:
            dt = (timestamp - previous) / 1000
            if not 0 < dt <= 0.05:
                raise ValueError("X6 IMU timestamps have a discontinuity")
            attitude = product(attitude, scaled_axis(gyro, dt*math.pi/180))
            if gravity_weight:
                observed_up = rotate(attitude, tuple(v/strength for v in accel))
                correction = cross(observed_up, UP)
                attitude = product(scaled_axis(correction, dt*gravity_weight/1.5), attitude)
            attitude = unit(attitude)
        previous = timestamp
        x, _, z = rotate(attitude, FORWARD)
        if math.hypot(x, z) > 0.05:
            measured = math.atan2(x, z)
            if heading is None:
                heading = measured
            else:
                error = math.remainder(measured - heading, 2*math.pi)
                heading = math.remainder(heading - math.expm1(-dt/0.45)*error, 2*math.pi)
        if timestamp >= start + frame*step_ms:
            output_to_camera = product(inverse(attitude), scaled_axis((0, 1, 0), heading or 0))
            yield frame, v360_angles(output_to_camera)
            if frame == last_frame:
                return
            frame += 1
            if timestamp >= start + frame*step_ms:
                raise ValueError("X6 IMU cannot cover every video frame")
    raise ValueError("X6 IMU ends before the last requested frame")
