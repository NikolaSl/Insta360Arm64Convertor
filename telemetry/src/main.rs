use std::{env, error::Error, fs::File, io, path::PathBuf, sync::Arc};
use telemetry_parser::{
    Input, InputOptions,
    util::{camera_rig, normalized_imu_interpolated},
};

fn main() -> Result<(), Box<dyn Error>> {
    let mut args = env::args_os().skip(1);
    let path = PathBuf::from(args.next().ok_or("Usage: insta360-telemetry INPUT.insv")?);
    if args.next().is_some() {
        return Err("Usage: insta360-telemetry INPUT.insv".into());
    }

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
    let samples = normalized_imu_interpolated(&input, None)?;
    if !samples.iter().any(|sample| sample.gyro.is_some())
        || !samples.iter().any(|sample| sample.accl.is_some())
    {
        return Err("No usable gyroscope and accelerometer pair was found".into());
    }

    let result = serde_json::json!({
        "camera_model": input.camera_model(),
        "accurate_timestamps": input.has_accurate_timestamps(),
        "frame_readout_ms": input.frame_readout_time(),
        "rig": camera_rig(&input),
        "imu": samples,
    });
    serde_json::to_writer(io::stdout().lock(), &result)?;
    Ok(())
}
