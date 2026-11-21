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
