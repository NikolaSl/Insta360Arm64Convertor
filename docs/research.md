# Research and validation

## Container and decoding

Insta360 documents INSV for the X6's 360° mode, HEVC video, AAC audio and several capture modes up to 7680×3840 at 50 fps. Published capture resolution does not establish how each mode packages lens images. [X6 shooting specifications](https://onlinemanual.insta360.com/x6/en-us/specs/shooting-specs) · [X6 product specifications](https://store.insta360.com/product/x6)

One original recording exposed two 3840×3840 video tracks at 30000/1001 fps, each with 10-bit 4:2:0 HEVC, limited-range BT.709, zero start time and the same 343.577-second duration. AAC audio was in the same file. Short excerpts converted through both encoder paths. A decoded preview showed clear seam and nadir artifacts with the default geometry. Check other shooting modes independently.

## Gyro, accelerometer and stabilization

Gyroflow and Adrian Eddy's telemetry-parser offer a starting point. The Insta360 parser looks for proprietary records near the file end, including protobuf camera metadata, raw or scaled motion, time alignment and lens offsets. The pinned revision handles the X6-style `offset_v6` calibration and describes a 13-term lens model with an additional ξ coefficient. Coverage varies by model, firmware and parser revision. [Pinned Insta360 parser source](https://github.com/AdrianEddy/telemetry-parser/tree/7ab3b3d81b0def1ad849d3870470e2cb5bf89321/src/insta360) · [Gyroflow source](https://github.com/gyroflow/gyroflow) · [X5 firmware issue](https://github.com/AdrianEddy/telemetry-parser/issues/53)

On the original X6 file used for the conversion smoke test, the ARM64 telemetry tool recovered 348,877 entries containing both gyro and acceleration, reported a 14.56 ms readout time, and returned a camera rig. The extracted timestamps ranged from −6.385 to 344.342 seconds, extending beyond both ends of the video. Those are parser results. Frame synchronization and orientation still need a controlled motion test before generating corrections from them.

For integration, pin a parser revision and compare first-frame time, sample intervals, units and axis signs on stationary and single-axis rotation clips. Estimate gyro bias on the stationary segment. Fuse gyro with gravity from the accelerometer, reducing accelerometer weight during strong non-gravitational acceleration. Fit the camera-to-IMU orientation and video-to-IMU time offset, smooth orientation in quaternion space, and sample the relative camera-to-smoothed orientation at frame timestamps. Convert to the projector's rotation convention and compare against known rotations before applying to field footage. Gyroflow is a reference for telemetry and orientation algorithms. Its ordinary reframing workflow does not establish a calibrated dual-lens full-sphere result.

## Projection and seam

FFmpeg v360 uses an idealized dual-fisheye mapping. The MVP exposes crops, turns, lens order and a shared FOV to test input geometry. A production X6 profile needs per-lens optical centers, projection coefficients, relative rotations, overlap masks, exposure matching, and blending. Use a distant patterned scene for correspondence fitting, a level horizon for global orientation, and close objects for parallax evaluation. Compare with a matching Insta360 Studio export and repeat with different camera modes and lens guards. The vendor C++ SDK is a possible reference path. Check X6 coverage, licensing, and binary architecture for the specific download before treating it as a deployment dependency. [FFmpeg v360 docs](https://ffmpeg.org/ffmpeg-filters.html#v360) · [FFmpeg v360 source](https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/libavfilter/vf_v360.c) · [Insta360 Desktop Media SDK](https://github.com/Insta360Develop/Desktop-MediaSDK-Cpp)

The writer adds Spherical Video V2 to the HEVC visual sample entry in the final MP4 movie atom. It leaves media offsets intact and verifies recognition with FFprobe. Check the intended 360 player separately because player support varies. [Spherical Video V2 specification](https://github.com/google/spatial-media/blob/master/docs/spherical-video-v2-rfc.md)

## Windows ARM64 path

ASUS lists an X2E-88-100 configuration of the Zenbook A14. Qualcomm's product brief advertises dual 8K UHD HEVC encode at 30 fps for this part. That does not characterize the driver's exposed modes for 7680×3840, higher X6 frame rates, 10-bit or concurrent lens decoding. FFmpeg's MF path is an 8-bit encode experiment. D3D11VA decode and a Vulkan/compute projection path are separate future experiments. Profile CPU projection and copies before choosing a GPU implementation. [ASUS specifications](https://www.asus.com/laptops/for-home/zenbook/asus-zenbook-a14-ux3407/techspec/) · [Qualcomm product brief](https://www.qualcomm.com/content/dam/qcomm-martech/dm-assets/documents/Snapdragon-X2-Elite-Product-Brief.pdf)
