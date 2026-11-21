use std::{env, error::Error, fs::File, io, path::PathBuf, sync::Arc};
use telemetry_parser::{
    Input, InputOptions,
    util::{camera_rig, normalized_imu_interpolated},
};

fn main() -> Result<(), Box<dyn Error>> {
    let args: Vec<_> = env::args_os().skip(1).collect();
    let (path, rig_only, lens_axes) = match args.as_slice() {
        [path] => (PathBuf::from(path), false, false),
        [option, path] if option == "--rig-only" => (PathBuf::from(path), true, false),
        [option, path] if option == "--lens-imu" => (PathBuf::from(path), false, true),
        _ => return Err("Usage: insta360-telemetry [--rig-only | --lens-imu] INPUT.insv".into()),
    };

    let mut file = File::open(&path)?;
    let size = file.metadata()?.len();
    let input = Input::from_stream_with_options(
        &mut file,
        size,
        &path,
        |_| {},
        Arc::default(),
        InputOptions {
            dont_look_for_sidecar_files: true,
            ..Default::default()
        },
    )?;
    if input.camera_type() != "Insta360" {
        return Err("An original Insta360 file is required".into());
    }
    let mut result = serde_json::json!({
        "camera_model": input.camera_model(),
        "accurate_timestamps": input.has_accurate_timestamps(),
        "frame_readout_ms": input.frame_readout_time(),
        "rig": camera_rig(&input),
    });
    if !rig_only {
        // X6 records are rotated into lens 0's frame before the parser applies
        // its Gyroflow axis convention. Request the lens frame for the rig.
        let samples = normalized_imu_interpolated(&input, lens_axes.then(|| "XYZ".into()))?;
        if !samples.iter().any(|sample| sample.gyro.is_some())
            || !samples.iter().any(|sample| sample.accl.is_some())
        {
            return Err("No usable gyroscope and accelerometer pair was found".into());
        }
        result["imu"] = serde_json::to_value(samples)?;
    }
    serde_json::to_writer(io::stdout().lock(), &result)?;
    Ok(())
}
