# Native Direct3D exports

The Windows ARM64 helpers provide native Direct3D stitching for compatible X6 footage. Auto tries the FFmpeg stitcher if the native helper cannot start before creating the video file. To select the native mode explicitly, use **Advanced → Calibrated stitch processor → Native Direct3D (preview)**.

## 8-bit hardware HEVC

- Media Foundation reads the original X6 file as an MP4 byte stream and decodes both lenses to P010 surfaces on the same D3D11 device.
- The converter identifies the lens order by comparing 64 luma samples from each decoder's first frame to the same points in the FFmpeg reference. It refuses ambiguous matches. The native tool then copies the lens surfaces to shader-readable P010 textures on the GPU and checks the two timestamps against the source cadence before each frame.
- The Adreno compute shader applies the factory Mei/RadtanPro projection, bilinear lens sampling and overlap blending, and writes the 8K sphere into the two planes of an NV12 texture.
- The Media Foundation sink receives an NV12 surface buffer that references the texture. After the initial fingerprint check, the per-frame path stays on the Direct3D device. The driver's internal transfers have not been measured. FFmpeg then copies the original AAC and packages the output MP4. The app verifies frame count and 360 metadata before publishing the file.

The FFmpeg device-interop tests found that the native Qualcomm OpenCL device can derive a context from D3D11, but FFmpeg failed to map the X6 P010 decoder frames directly. FFmpeg also could not derive Vulkan from D3D12, or map its Vulkan frames directly to D3D11 on this build. A separate FFmpeg test selected the Qualcomm hardware HEVC transform and successfully accepted a D3D11 NV12 texture. The native prototype stays on one Direct3D device to avoid the unsupported cross-API mappings.

## Build and test

Use the ARM64 Visual Studio Native Tools command prompt with the Windows SDK.

```bat
cl /nologo /std:c++17 /W4 /EHsc /O2 /Fo.tooling/d3d11_surface_probe.obj /Fe.tooling/d3d11_surface_probe.exe tools/d3d11_surface_probe.cpp d3d11.lib d3dcompiler.lib mf.lib mfplat.lib mfreadwrite.lib mfuuid.lib ole32.lib
.tooling\d3d11_surface_probe.exe
.tooling\d3d11_surface_probe.exe --decode clip.insv
python insta360_arm.py convert clip.insv native-preview.mp4 --calibrated --auto-stabilize --stitch-device native --encoder mf --bits 8 --bitrate 120M --seconds 60
```

The preparer requires native FFmpeg and FFprobe and the project's telemetry executable. Both native paths require two synchronized 3840×3840 10-bit BT.709 limited-range X6 tracks at 30000/1001 fps and retain the source frame rate. Choose **Auto** for other recording modes or manual correction CSVs. The native route temporarily holds both its video-only file and the final packaged MP4 on the destination volume. The GUI supports progress and cancelling before publication.

## 10-bit CPU x265 with shared GPU output

The Media Foundation decoder supplies P010 on D3D11. The helper copies the two
decoder surfaces into shareable GPU textures and opens them in D3D12 on the
same adapter. A shared GPU fence protects access to the lenses. The D3D12
shader combines factory projection, seam blending and orientation and writes
planar Y, U and V directly into cache-coherent CPU-readable memory. Two output
buffers let the GPU work on one frame while the application submits the
previous frame to x265. x265 still copies input into internal padded frames.

Build with the [BtbN Windows ARM64 GPL shared distribution](https://github.com/BtbN/FFmpeg-Builds/releases)
under `.tooling\ffmpeg-master-latest-winarm64-gpl-shared`. Use the ARM64
Visual Studio Native Tools prompt. Keep the output executable beside the DLLs
from the matching distribution. The helper is looked up by name and its
protocol version is checked before export.

```bat
set "AV=.tooling\ffmpeg-master-latest-winarm64-gpl-shared"
cl /nologo /std:c++17 /W4 /EHsc /O2 /DINSTA360_MAIN10 /external:W0 /external:I"%AV%\include" /Fo.tooling/insta360_main10.obj /Fe"%AV%\bin\insta360_main10.exe" tools/d3d11_surface_probe.cpp d3d11.lib d3d12.lib d3dcompiler.lib mf.lib mfplat.lib mfreadwrite.lib mfuuid.lib ole32.lib "%AV%\lib\avcodec.lib" "%AV%\lib\avformat.lib" "%AV%\lib\avutil.lib"
python insta360_arm.py convert clip.insv native-main10.mp4 --calibrated --auto-stabilize --stitch-device native --encoder x265 --bits 10 --preset fast --crf 18 --seconds 6
```

This option requires cache-coherent UMA and supports 2:1 output dimensions up
to 7680×3840. It uses the selected x265 preset and CRF or target bitrate.
The encoder and libavformat write timestamps for reordered HEVC frames.
The converter then copies the original AAC audio, adds spherical metadata,
and validates the result before publishing.

## Result on the Zenbook A14

The native ARM64 tool streamed 1,799 frames from the same 60-second X6 input used for the OpenCL benchmark. It exported 7680×3840 at 30000/1001 fps with stabilization and a 120 Mb/s HEVC target. The measured video bitrate was about 112 Mb/s. Native processing took 68.05 seconds. Separate measurements put pose preparation at 1.97 seconds and audio stream copy plus spherical MP4 packaging at 0.48 seconds, for about 70.5 seconds in total. The previously measured OpenCL path took 190.22 seconds at the same target settings. These are single runs.

The 60-second native video decoded without errors. FFprobe verified BT.709, 8-bit HEVC, the expected frame count, and AAC plus equirectangular metadata in the packaged preview. The Media Foundation sink reported a D3D11-aware asynchronous transform. It did not report the active encoder's CLSID, so this result establishes the texture handoff to Media Foundation and does not independently identify the active transform.

The integrated path verified lens order and exported the full 343.577-second X6 segment in 382.54 seconds with stabilization, 8K and a 120 Mb/s target. The output contained all 10,297 HEVC frames, the original 343.552-second AAC stream and spherical metadata. FFmpeg decoded samples from the beginning, middle and end without errors. This full-segment result was one run on one supported recording mode.
