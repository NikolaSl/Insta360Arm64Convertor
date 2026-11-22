// Output rays are rotated into the camera rig, projected into each visible
// lens and blended before a single P010 sphere is downloaded.
typedef struct {
    float3 rows[3];
    float xi;
    float d[13];
    float2 focal, centre;
    float skew;
    float2 affine_x, affine_y, offset;
    float outer, inner;
    float2 size;
} Camera;

const sampler_t nearest = CLK_NORMALIZED_COORDS_FALSE | CLK_ADDRESS_CLAMP_TO_EDGE | CLK_FILTER_NEAREST;
const sampler_t linear = CLK_NORMALIZED_COORDS_FALSE | CLK_ADDRESS_CLAMP_TO_EDGE | CLK_FILTER_LINEAR;

float3 project(float3 ray, __constant const Camera *cam)
{
    float z = dot(ray, cam->rows[2]);
    if (z < cam->outer)
        return (float3)(0.0f);
    float2 m = (float2)(dot(ray, cam->rows[0]), dot(ray, cam->rows[1])) / (z + cam->xi);
    float r2 = dot(m, m);
    __constant const float *d = cam->d;
    float radial = 1.0f + r2*(d[0] + r2*(d[1] + r2*(d[2] + r2*(d[3] + r2*d[4]))));
    float2 tangent = (float2)(d[5] + d[7]*r2, d[6] + d[8]*r2);
    float2 uv = m*radial + (r2 + 2.0f*m*m)*tangent
                + 2.0f*m.x*m.y*tangent.yx + r2*((float2)(d[9], d[10]) + r2*(float2)(d[11], d[12]));
    uv = uv*cam->focal + cam->centre + (float2)(cam->skew * uv.y, 0.0f);
    uv = (float2)(dot(cam->affine_x, uv), dot(cam->affine_y, uv)) + cam->offset;
    if (any(uv < -0.5f) || any(uv >= cam->size - 0.5f))
        return (float3)(0.0f);
    return (float3)(uv, clamp((z - cam->outer)/(cam->inner - cam->outer), 0.00001f, 1.0f));
}

// Camera constants and output dimensions are generated from the validated rig.
/* CAMERA_CONSTANTS */

float4 sample_lens(read_only image2d_t image, float2 uv, bool chroma)
{
    // P010 chroma is left-sited and vertically centered. OpenCL's texture
    // coordinates place pixel centers at n+0.5.
    if (chroma)
        uv = (uv - (float2)(0.0f, 0.5f)) * 0.5f;
    return read_imagef(image, linear, uv + 0.5f);
}

__kernel void stitch(write_only image2d_t dst, uint frame,
                     read_only image2d_t lens0, read_only image2d_t lens1,
                     read_only image2d_t orientations)
{
    int2 pixel = (int2)(get_global_id(0), get_global_id(1));
    bool chroma = get_image_width(dst) != OUTPUT_WIDTH;
    float2 uv = convert_float2(pixel);
    if (chroma)
        uv = uv*2.0f + (float2)(0.0f, 0.5f);
    float2 angles = ((uv + 0.5f) / (float2)(OUTPUT_WIDTH, OUTPUT_HEIGHT) - 0.5f)
                    * (float2)(6.28318530718f, 3.14159265359f);
    float cx, cy;
    float sx = sincos(angles.x, &cx);
    float sy = sincos(angles.y, &cy);
    float3 ray = (float3)(cy*sx, sy, cy*cx);
    uint index = min(frame, ROTATIONS - 1u);
    int2 qpos = (int2)((index % 256u)*4u, index / 256u);
    float4 q = (float4)(read_imagef(orientations, nearest, qpos).x,
                        read_imagef(orientations, nearest, qpos + (int2)(1, 0)).x,
                        read_imagef(orientations, nearest, qpos + (int2)(2, 0)).x,
                        read_imagef(orientations, nearest, qpos + (int2)(3, 0)).x);
    q = normalize(q*2.0f - 1.0f);
    float3 t = 2.0f*cross(q.yzw, ray);
    ray += q.x*t + cross(q.yzw, t);
    float3 a = project(ray, &CAMERA0), b = project(ray, &CAMERA1);
    float4 color = (float4)(chroma ? 0.5f : 4096.0f/65535.0f);
    if (a.z > 0.0f && b.z > 0.0f)
        color = mix(sample_lens(lens0, a.xy, chroma), sample_lens(lens1, b.xy, chroma),
                    b.z/(a.z + b.z));
    else if (a.z > 0.0f)
        color = sample_lens(lens0, a.xy, chroma);
    else if (b.z > 0.0f)
        color = sample_lens(lens1, b.xy, chroma);
    write_imagef(dst, pixel, color);
}
