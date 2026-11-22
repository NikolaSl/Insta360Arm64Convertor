# Benchmark protocol

1. Record the machine model, Windows and graphics driver versions, power mode, plugged-in state, FFmpeg version and PE machine checks. Use local SSD storage. Record each source's duration, capture mode, firmware, bit depth, track layout and file hashes.
2. Run `encode-test` on both encoders at the same 8-bit size, rate, duration and target bitrate. Start at 3840×1920 and 30 fps. Test 6016×3008 and 7680×3840 at 30 fps, then higher rates separately. Record failures and the full FFmpeg logs. A synthetic pattern tests acceptance and throughput under a controlled workload. Real camera texture and noise change encoder load.
3. Compare end-to-end `convert` with identical input, size, rate, bit depth, interpolation and target bitrate. Run a static and a per-frame-rotation condition. Use separate new filenames. Add a 10-bit x265 condition and a matching Studio export where possible.
4. Separate the cost of the stages using FFmpeg benchmarks. Decode both input streams to the null muxer, then repeat with the converter's hstack and v360 filter graph mapped to null. Compare with encode-only and full conversion. Stage results can overlap in a pipelined run and should not be added to predict total time.
5. Warm up once and time three runs per condition, recording median and range. Save elapsed seconds, output video seconds, their ratio, CPU utilization, Video Encode engine activity, peak memory and output bytes. Observe Windows Task Manager's Video Encode graph or capture GPU-engine activity with Windows Performance Recorder. Record the selected MFT if exposed in the verbose log.
6. Verify playback and sample decoded frames throughout. Inspect the seam, poles, horizon, nearby objects, color range, motion, and audio sync. Use comparable visual quality when interpreting encoder speeds. A camera export is a reference for geometry and stabilization, since its tone and exposure processing may differ.

The converter reports elapsed wall time for FFmpeg, tagging and output verification. A realtime factor of 2 means a 60-second clip took 30 seconds. At equal settings a sustained full conversion result is the relevant figure for estimating longer exports.

## Initial Zenbook A14 results

Single short runs on Snapdragon X2E-88-100, Windows 11 26H1 build 28000.3501, native FFmpeg/FFprobe N-129166-gae9f4540cb-20261121. Driver version, power mode and engine utilization were not recorded. FFmpeg logged selection of `QCOM Hardware Encoder - HEVC` with forced hardware encoding and wrote verified 8-bit HEVC in each MF run.

| Input and encoder | Output | Video seconds | Wall seconds | Realtime factor |
| --- | --- | ---: | ---: | ---: |
| Synthetic encode-only, MF, 60 Mb/s | 1920×960 at 30 fps | 2.000 | 0.789 | 2.54× |
| Synthetic encode-only, MF, 60 Mb/s | 3840×1920 at 30 fps | 2.000 | 1.187 | 1.68× |
| Synthetic encode-only, MF, 60 Mb/s | 7680×3840 at 30 fps | 2.000 | 2.057 | 0.97× |
| Original X6, CPU projection + MF, 35 Mb/s | 3840×1920 at 30000/1001 fps, 8-bit | 2.002 | 2.412 | 0.83× |
| Original X6, CPU projection + MF, 80 Mb/s | 7680×3840 at 30000/1001 fps, 8-bit | 2.002 | 3.457 | 0.58× |
| Original X6, CPU projection + x265 ultrafast, CRF 18 | 7680×3840 at 30000/1001 fps, 10-bit | 1.001 | 5.166 | 0.19× |

The real-video rows used static ideal-lens geometry and the same recording. Estimating sustained speed or comparing encoder efficiency requires a longer like-for-like test. FFmpeg reported about 3.8 GiB, 5.2 GiB and 10.6 GiB peak working set for the three camera runs in table order.

## Factory-calibrated stitching smoke test

On a two-second excerpt of the same X6 source, 7680×3840 at 30000/1001 fps with 8-bit Qualcomm HEVC and a 120 Mb/s target, the ideal-lens path took 2.98 seconds. The calibrated path took 6.49 seconds, including map generation. FFmpeg peak working sets were approximately 5.2 and 6.8 GiB respectively. The calibrated path builds fixed maps once per export, so the short test overstates the per-frame cost of map generation. A sustained comparison still needs longer runs.

## Automatic stabilization smoke test

At the same 8K settings, a two-second stabilized export using Adreno Vulkan rotation and Qualcomm hardware encoding took 8.76 seconds, including map generation. Logs identified both hardware paths and the resulting 60-frame MP4 decoded successfully. The lenses are still decoded and stitched on the CPU.

On an eight-second 960×480 excerpt with the same calibrated projection, median tracked displacement across nine background patches over four-frame intervals was 0.80 pixels with stabilization off, 0.53 pixels with CPU stabilization and 0.50 pixels with Vulkan stabilization. This is one footage comparison, with no independent timing reference. It does not establish performance for rapid motion, walking or other capture modes.

## Sustained 8K processor comparison

The same first 60 seconds of an original X6 segment were exported at 7680×3840, 30000/1001 fps, with automatic stabilization and 8-bit Qualcomm HEVC at a 120 Mb/s target. Each output had 1,799 frames and equirectangular MP4 metadata. These are single sequential runs.

| Decode | Projection, blend and rotation | Wall time | FFmpeg peak working set |
| --- | --- | ---: | ---: |
| CPU | CPU projection and blend, Adreno Vulkan rotation | 211.09 s | 7.05 GiB |
| D3D11VA | CPU projection and blend, Adreno Vulkan rotation | 205.03 s | 6.68 GiB |
| CPU | Fused Adreno OpenCLOn12 projection, blend and rotation | 190.22 s | 5.53 GiB |

The fused route reduced wall time by about 10% and aggregate FFmpeg CPU time by about 48% compared with the reference. It samples the lenses bilinearly in P010 and does not allocate the CPU map files, so its output is not expected to match the reference pixel-for-pixel. The tested OpenCLOn12 route did not negotiate a combined filter graph with Direct3D decoding. That combination stays unavailable.

Separate two-second decoder tests passed for both lens tracks in D3D11VA and D3D12VA when using one decoder submission thread per lens. The default threaded D3D11VA attempt exhibited frame submission errors. In two-second exports using the reference stitcher, both Direct3D decoder paths produced byte-identical MP4s to CPU decoding. The earlier unbounded rotation-command intervals generated excessive logs on longer clips. The sustained comparison uses the bounded intervals on both reference-stitch runs.

The [native Direct3D 11 path](native-d3d11.md) later processed the same 60-second 8K excerpt in 68.05 seconds, with approximately 2.45 seconds for separate pose preparation and final packaging. The integrated path exported the full 343.58-second segment in 382.54 seconds, including preparation, verification and packaging.

## Direct3D 12 shared-memory probe

On the Zenbook A14, the Qualcomm Adreno X2-90 adapter reported both `UMA` and
`CacheCoherentUMA`. A compute shader wrote an 88,473,600-byte buffer (the size
of a 7680×3840 10-bit 4:2:0 frame) directly into a CPU-readable write-back L0
heap. The CPU waited on a fence and verified every word. The direct path
issued no GPU buffer copy.

Across four runs, dispatch through fence and map averaged 3.02–5.14 ms for
the shared buffer and 5.57–9.67 ms for dispatch into a default buffer followed
by a copy to a readback buffer. Each run alternated the two paths and averaged
20 measurements per path after one warm-up. CPU verification was outside the
timing. The test used a synthetic write shader, with no stitch, color
conversion or encode in the measured section.

Build from the ARM64 Visual Studio Native Tools prompt:

```bat
cl /nologo /std:c++17 /W4 /EHsc /O2 /Fo.tooling/d3d12_uma_probe.obj /Fe.tooling/d3d12_uma_probe.exe tools/d3d12_uma_probe.cpp d3d12.lib d3d11.lib dxgi.lib d3dcompiler.lib
.tooling\d3d12_uma_probe.exe
```

As a separate baseline, the current stabilized 8K OpenCL to 10-bit x265
workflow (preset `fast`) exported 60 X6 frames in 18.75 seconds.

### Main 10 encoder handoff

The optional x265 build generates six synthetic 7680×3840 frames per path
at 30000/1001 fps. The shader writes right-aligned 10-bit Y, U and V planes
and the application passes the mapped plane pointers and strides to x265.
The direct path skips the D3D12 copy to a readback buffer. x265's internal
copy into padded encoder frames remains enabled so the application can
reuse the shared buffer after submission.

Both HEVC streams were byte-for-byte identical. FFprobe confirmed six
Main 10 `yuv420p10le` frames and FFmpeg decoded the complete direct stream
without errors. In one paired run, GPU dispatch through fence and map took
5.15 ms directly and 9.67 ms with the staged copy, excluding x265 submission.
This confirms the encoder handoff in isolation. For the camera pipeline see
the [native Direct3D Main 10 exporter](native-d3d11.md).

Extract [x265 4.2](https://download.videolan.org/pub/videolan/x265/x265_4.2.tar.gz)
under `.tooling` and run from an ARM64 Visual Studio Native Tools prompt
with CMake and Ninja available. The probe uses an assembly-disabled build
and a single x265 frame thread and is intended for correctness verification.

```bat
cmake -S .tooling/x265_4.2/source -B .tooling/x265-arm64-main10 -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_TRY_COMPILE_CONFIGURATION=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DHIGH_BIT_DEPTH=ON -DENABLE_ASSEMBLY=OFF -DENABLE_SHARED=OFF -DENABLE_CLI=OFF
cmake --build .tooling/x265-arm64-main10 --parallel
cl /nologo /std:c++17 /W4 /EHsc /O2 /MD /DUMA_WITH_X265 /I.tooling/x265_4.2/source /I.tooling/x265-arm64-main10 /Fo.tooling/d3d12_uma_x265.obj /Fe.tooling/d3d12_uma_x265.exe tools/d3d12_uma_probe.cpp .tooling/x265-arm64-main10/x265-static.lib d3d12.lib d3d11.lib dxgi.lib d3dcompiler.lib advapi32.lib
.tooling\d3d12_uma_x265.exe .tooling\uma-x265-staged.hevc .tooling\uma-x265-direct.hevc
fc /b .tooling\uma-x265-staged.hevc .tooling\uma-x265-direct.hevc
ffmpeg -v error -xerror -i .tooling\uma-x265-direct.hevc -f null -
```

### Main 10 camera export on Zenbook A14

The first 180 frames of one original X6 clip were exported at 7680×3840,
30000/1001 fps, Main 10, x265 `fast`, CRF 18, with factory calibration and
IMU stabilization. Both paths used x265's automatic frame threading (four
frame threads in these runs). Times include preparation, encoding, audio
packaging and MP4 validation.

| Path | Wall time | Output duration |
| --- | ---: | ---: |
| FFmpeg CPU decode, OpenCL projection, readback and conversion, x265 | 56.596 s | 6.006 s |
| Media Foundation decode, D3D12 projection to shared planar memory, x265 | 47.823 s | 6.006 s |

The native path reduced wall time by 15.5% in this short comparison. FFprobe
counted 180 frames with BT.709 limited range, AAC audio and equirectangular
metadata. FFmpeg decoded the complete native export without errors. The
first 30 frames averaged 47.72 dB PSNR against the OpenCL export. These are
single runs on one clip, and the difference includes
the change of decoder and shader in addition to the direct memory handoff.

With the same native Main 10 excerpt and CRF 18, x265 `faster` took
36.003 seconds in a separate single run, versus 47.823 seconds for `fast`.
The GUI now defaults to `faster` when the CPU encoder is selected. Preset
changes can affect compression efficiency at a given quality or target rate.
