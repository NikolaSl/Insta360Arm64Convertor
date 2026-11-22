struct Camera {
    float3 rows[3];
    float xi;
    float d[13];
    float2 focal, centre;
    float skew;
    float2 affine_x, affine_y, offset;
    float outer, inner;
    float2 size;
};

/* CAMERA_CONSTANTS */

Texture2D<float> lens0_y : register(t0);
Texture2D<float2> lens0_uv : register(t1);
Texture2D<float> lens1_y : register(t2);
Texture2D<float2> lens1_uv : register(t3);
RWTexture2D<float> output_y : register(u0);
RWTexture2D<float2> output_uv : register(u1);
SamplerState linear_clamp : register(s0);
cbuffer Orientation : register(b0) { float4 q; };
RWByteAddressBuffer planar : register(u2);

[numthreads(8, 8, 1)]
void fingerprint(uint3 id : SV_DispatchThreadID)
{
    output_y[id.xy] = lens0_y.Load(int3(id.xy * 480 + 240, 0));
}

float3 camera_sample(float3 ray, Camera cam)
{
    float z = dot(ray, cam.rows[2]);
    if (z < cam.outer) return 0.0;
    float2 m = float2(dot(ray, cam.rows[0]), dot(ray, cam.rows[1])) / (z + cam.xi);
    float r2 = dot(m, m);
    float radial = 1.0 + r2*(cam.d[0] + r2*(cam.d[1] + r2*(cam.d[2] + r2*(cam.d[3] + r2*cam.d[4]))));
    float2 tangent = float2(cam.d[5], cam.d[6]) + r2*float2(cam.d[7], cam.d[8]);
    float2 uv = m*radial + (r2 + 2.0*m*m)*tangent + 2.0*m.x*m.y*tangent.yx
              + r2*(float2(cam.d[9], cam.d[10]) + r2*float2(cam.d[11], cam.d[12]));
    uv = uv*cam.focal + cam.centre + float2(cam.skew*uv.y, 0.0);
    uv = float2(dot(cam.affine_x, uv), dot(cam.affine_y, uv)) + cam.offset;
    if (any(uv < -0.5) || any(uv >= cam.size - 0.5)) return 0.0;
    return float3(uv, clamp((z-cam.outer)/(cam.inner-cam.outer), 0.00001, 1.0));
}

float3 output_ray(float2 pixel)
{
    float2 angles = ((pixel + 0.5) / float2(OUTPUT_WIDTH, OUTPUT_HEIGHT) - 0.5)
                    * float2(6.28318530718, 3.14159265359);
    float sx, cx, sy, cy;
    sincos(angles.x, sx, cx);
    sincos(angles.y, sy, cy);
    float3 ray = float3(cy*sx, sy, cy*cx);
    float3 t = 2.0*cross(q.yzw, ray);
    return ray + q.x*t + cross(q.yzw, t);
}

float sample_luma(uint2 pixel)
{
    float3 ray = output_ray(float2(pixel));
    float3 a = camera_sample(ray, CAMERA0), b = camera_sample(ray, CAMERA1);
    float total = a.z + b.z;
    float luma = (64.0 * 64.0) / 65535.0;
    if (total > 0.0) {
        float ya = a.z > 0.0 ? lens0_y.SampleLevel(linear_clamp, (a.xy + 0.5)/CAMERA0.size, 0) : 0.0;
        float yb = b.z > 0.0 ? lens1_y.SampleLevel(linear_clamp, (b.xy + 0.5)/CAMERA1.size, 0) : 0.0;
        luma = (a.z*ya + b.z*yb)/total;
    }
    return luma;
}

float2 sample_chroma(uint2 pixel)
{
    float3 ray = output_ray(float2(pixel)*2.0 + float2(0.0, 0.5));
    float3 a = camera_sample(ray, CAMERA0);
    float3 b = camera_sample(ray, CAMERA1);
    float total = a.z + b.z;
    float2 chroma = (512.0 * 64.0) / 65535.0;
    if (total > 0.0) {
        // Left-sited and vertically centered P010 chroma.
        float2 ca = a.z > 0.0 ? lens0_uv.SampleLevel(linear_clamp,
            (a.xy + float2(1.0, 0.5))/CAMERA0.size, 0) : 0.0;
        float2 cb = b.z > 0.0 ? lens1_uv.SampleLevel(linear_clamp,
            (b.xy + float2(1.0, 0.5))/CAMERA1.size, 0) : 0.0;
        chroma = (a.z*ca + b.z*cb)/total;
    }
    return chroma;
}

[numthreads(8, 8, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    uint2 pixel = id.xy;
    if (pixel.x >= OUTPUT_WIDTH || pixel.y >= OUTPUT_HEIGHT) return;
    output_y[pixel] = sample_luma(pixel);
    if (pixel.x < OUTPUT_WIDTH/2 && pixel.y < OUTPUT_HEIGHT/2)
        output_uv[pixel] = sample_chroma(pixel);
}

uint pack_samples(float2 values)
{
    uint2 code = (uint2)clamp(round(values * (65535.0 / 64.0)), 0.0, 1023.0);
    return code.x | (code.y << 16);
}

[numthreads(8, 8, 1)]
void main10(uint3 id : SV_DispatchThreadID)
{
    uint2 pixel = id.xy * uint2(2, 1);
    if (pixel.x >= OUTPUT_WIDTH || pixel.y >= OUTPUT_HEIGHT) return;
    uint offset = (pixel.y * OUTPUT_WIDTH + pixel.x) * 2;
    planar.Store(offset, pack_samples(float2(sample_luma(pixel), sample_luma(pixel + uint2(1, 0)))));
    if (pixel.x < OUTPUT_WIDTH/2 && pixel.y < OUTPUT_HEIGHT/2) {
        float2 a = sample_chroma(pixel), b = sample_chroma(pixel + uint2(1, 0));
        uint y_bytes = OUTPUT_WIDTH * OUTPUT_HEIGHT * 2;
        uint c_offset = (pixel.y * (OUTPUT_WIDTH/2) + pixel.x) * 2;
        planar.Store(y_bytes + c_offset, pack_samples(float2(a.x, b.x)));
        planar.Store(y_bytes + y_bytes/4 + c_offset, pack_samples(float2(a.y, b.y)));
    }
}
