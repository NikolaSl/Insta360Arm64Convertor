// Test the texture and shader-view requirements for a D3D11 stitcher.
#include <d3d11_3.h>
#include <d3d10.h>
#include <d3dcompiler.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <mftransform.h>
#include <wrl/client.h>

#include <cstdint>
#include <cstdio>
#include <cwchar>
#include <cstdlib>
#include <array>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <memory>
#include <string>
#include <utility>
#include <vector>

using Microsoft::WRL::ComPtr;

static bool report(const char* label, HRESULT result)
{
    std::printf("%-44s 0x%08lx\n", label, static_cast<unsigned long>(result));
    return SUCCEEDED(result);
}

#ifdef INSTA360_MAIN10
#include "d3d12_main10.h"
#endif

static HRESULT texture(ID3D11Device3* device, UINT width, UINT height, DXGI_FORMAT format,
                       UINT bind, ID3D11Texture2D** result,
                       const D3D11_SUBRESOURCE_DATA* data = nullptr, UINT misc = 0)
{
    D3D11_TEXTURE2D_DESC desc{};
    desc.Width = width;
    desc.Height = height;
    desc.MipLevels = desc.ArraySize = desc.SampleDesc.Count = 1;
    desc.Format = format;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = bind;
    desc.MiscFlags = misc;
    return device->CreateTexture2D(&desc, data, result);
}

static HRESULT source_view(ID3D11Device3* device, ID3D11Texture2D* input,
                           UINT plane, ID3D11ShaderResourceView1** view)
{
    D3D11_SHADER_RESOURCE_VIEW_DESC1 desc{};
    desc.Format = plane ? DXGI_FORMAT_R16G16_UNORM : DXGI_FORMAT_R16_UNORM;
    desc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    desc.Texture2D.MipLevels = 1;
    desc.Texture2D.PlaneSlice = plane;
    return device->CreateShaderResourceView1(input, &desc, view);
}

static HRESULT output_view(ID3D11Device3* device, ID3D11Texture2D* output,
                           UINT plane, ID3D11UnorderedAccessView1** view)
{
    D3D11_UNORDERED_ACCESS_VIEW_DESC1 desc{};
    desc.Format = plane ? DXGI_FORMAT_R8G8_UNORM : DXGI_FORMAT_R8_UNORM;
    desc.ViewDimension = D3D11_UAV_DIMENSION_TEXTURE2D;
    desc.Texture2D.PlaneSlice = plane;
    return device->CreateUnorderedAccessView1(output, &desc, view);
}

static HRESULT video_type(UINT width, UINT height, const GUID& subtype, UINT bitrate,
                          IMFMediaType** result)
{
    ComPtr<IMFMediaType> type;
    HRESULT hr = MFCreateMediaType(&type);
    if (SUCCEEDED(hr)) hr = type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    if (SUCCEEDED(hr)) hr = type->SetGUID(MF_MT_SUBTYPE, subtype);
    if (SUCCEEDED(hr)) hr = MFSetAttributeSize(type.Get(), MF_MT_FRAME_SIZE, width, height);
    if (SUCCEEDED(hr)) hr = MFSetAttributeRatio(type.Get(), MF_MT_FRAME_RATE, 30000, 1001);
    if (SUCCEEDED(hr)) hr = MFSetAttributeRatio(type.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    if (SUCCEEDED(hr)) hr = type->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    if (SUCCEEDED(hr)) hr = type->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235);
    if (SUCCEEDED(hr)) hr = type->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT709);
    if (SUCCEEDED(hr)) hr = type->SetUINT32(MF_MT_VIDEO_PRIMARIES, MFVideoPrimaries_BT709);
    if (SUCCEEDED(hr)) hr = type->SetUINT32(MF_MT_TRANSFER_FUNCTION, MFVideoTransFunc_709);
    if (SUCCEEDED(hr) && subtype == MFVideoFormat_HEVC)
        hr = type->SetUINT32(MF_MT_AVG_BITRATE, bitrate);
    if (SUCCEEDED(hr)) *result = type.Detach();
    return hr;
}

static bool open_encoder(const wchar_t* path, ID3D11Device3* device, UINT width, UINT height,
                         UINT bitrate, ComPtr<IMFSinkWriter>& writer, DWORD& stream)
{
    UINT token{};
    ComPtr<IMFDXGIDeviceManager> manager;
    ComPtr<IMFAttributes> attributes;
    if (!report("Create MF DXGI manager", MFCreateDXGIDeviceManager(&token, &manager))
        || !report("Set encoder D3D device", manager->ResetDevice(device, token))
        || !report("Create sink writer attributes", MFCreateAttributes(&attributes, 4))
        || !report("Enable hardware MFT", attributes->SetUINT32(MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS, TRUE))
        || !report("Require D3D for sink", attributes->SetUINT32(MF_READWRITE_D3D_OPTIONAL, FALSE))
        || !report("Set sink DXGI manager", attributes->SetUnknown(MF_SINK_WRITER_D3D_MANAGER, manager.Get())))
        return false;

    ComPtr<IMFMediaType> output, input;
    if (!report("Create HEVC MP4 sink writer", MFCreateSinkWriterFromURL(path, nullptr, attributes.Get(), &writer))
        || !report("Configure HEVC output", video_type(width, height, MFVideoFormat_HEVC, bitrate, &output))
        || !report("Add HEVC stream", writer->AddStream(output.Get(), &stream))
        || !report("Configure NV12 input", video_type(width, height, MFVideoFormat_NV12, bitrate, &input))
        || !report("Set NV12 input", writer->SetInputMediaType(stream, input.Get(), nullptr))
        || !report("Begin HEVC encoding", writer->BeginWriting()))
        return false;

    ComPtr<IMFSinkWriterEx> extended;
    if (SUCCEEDED(writer.As(&extended))) {
        for (DWORD i = 0; ; ++i) {
            GUID category{}, clsid{};
            ComPtr<IMFTransform> transform;
            ComPtr<IMFAttributes> info;
            if (FAILED(extended->GetTransformForStream(stream, i, &category, &transform))) break;
            if (FAILED(transform->GetAttributes(&info))) continue;
            UINT32 aware{}, asynchronous{};
            info->GetUINT32(MF_SA_D3D11_AWARE, &aware);
            info->GetUINT32(MF_TRANSFORM_ASYNC, &asynchronous);
            std::printf("Sink transform %lu: D3D11 aware=%u, asynchronous=%u\n",
                        static_cast<unsigned long>(i), aware, asynchronous);
            if (SUCCEEDED(info->GetGUID(MFT_TRANSFORM_CLSID_Attribute, &clsid))) {
                wchar_t text[39]{};
                StringFromGUID2(clsid, text, 39);
                std::printf("Sink transform %lu: %ls\n", static_cast<unsigned long>(i), text);
            }
        }
    }

    return true;
}

static bool write_texture(IMFSinkWriter* writer, DWORD stream, ID3D11Texture2D* texture,
                          UINT frame)
{
    ComPtr<IMFMediaBuffer> buffer;
    ComPtr<IMFSample> sample;
    DWORD length{};
    const LONGLONG start = static_cast<LONGLONG>(frame) * 10000000 * 1001 / 30000;
    const LONGLONG end = (static_cast<LONGLONG>(frame) + 1) * 10000000 * 1001 / 30000;
    if (FAILED(MFCreateDXGISurfaceBuffer(__uuidof(ID3D11Texture2D), texture, 0, FALSE, &buffer))
        || FAILED(buffer->GetMaxLength(&length)) || FAILED(buffer->SetCurrentLength(length))
        || FAILED(MFCreateSample(&sample)) || FAILED(sample->AddBuffer(buffer.Get()))
        || FAILED(sample->SetSampleTime(start)) || FAILED(sample->SetSampleDuration(end - start)))
        return false;
    const HRESULT hr = writer->WriteSample(stream, sample.Get());
    return SUCCEEDED(hr) || report("Submit GPU-written NV12 sample", hr);
}

static bool encode_pattern(const wchar_t* path, ID3D11Device3* device, ID3D11Texture2D* pattern)
{
    ComPtr<IMFSinkWriter> writer;
    DWORD stream{};
    if (!open_encoder(path, device, 7680, 3840, 120000000, writer, stream)) return false;
    for (UINT frame = 0; frame < 15; ++frame) {
        if (!write_texture(writer.Get(), stream, pattern, frame)) return false;
    }
    return report("Finalize HEVC export", writer->Finalize());
}

struct LensViews {
    ComPtr<ID3D11Texture2D> texture;
    ComPtr<ID3D11ShaderResourceView1> y, uv;
    UINT misc = 0;
};

static bool read_lens(IMFSourceReader* reader, DWORD track, ID3D11Device3* device,
                      ID3D11DeviceContext* context, LensViews& lens, LONGLONG& time)
{
    ComPtr<IMFSample> sample;
    for (unsigned attempt = 0; attempt < 16 && !sample; ++attempt) {
        DWORD flags{};
        HRESULT hr = reader->ReadSample(track, 0, nullptr, &flags, &time, &sample);
        if (FAILED(hr)) return report("Read lens sample", hr);
        if (flags & MF_SOURCE_READERF_ENDOFSTREAM) return false;
    }
    ComPtr<IMFMediaBuffer> buffer;
    ComPtr<IMFDXGIBuffer> dxgi;
    ComPtr<ID3D11Texture2D> decoded;
    UINT subresource{};
    if (!sample || FAILED(sample->GetBufferByIndex(0, &buffer)) ||
        FAILED(buffer.As(&dxgi))
        || FAILED(dxgi->GetResource(IID_PPV_ARGS(&decoded)))
        || FAILED(dxgi->GetSubresourceIndex(&subresource))) return false;
    D3D11_TEXTURE2D_DESC desc{};
    decoded->GetDesc(&desc);
    ComPtr<ID3D11Device> owner;
    decoded->GetDevice(&owner);
    if (desc.Format != DXGI_FORMAT_P010 || desc.Width != 3840 || desc.Height != 3840
        || owner.Get() != static_cast<ID3D11Device*>(device)) return false;
    if (!lens.texture) {
        std::printf("Lens %lu: P010 %ux%u, array=%u, subresource=%u, pts=%lld\n",
                    static_cast<unsigned long>(track), desc.Width, desc.Height,
                    desc.ArraySize, subresource, time);
        if (!report("Allocate GPU lens copy", texture(device, desc.Width, desc.Height,
                DXGI_FORMAT_P010, D3D11_BIND_SHADER_RESOURCE, &lens.texture, nullptr, lens.misc))
            || !report("Create decoded-lens luma view", source_view(device, lens.texture.Get(), 0, &lens.y))
            || !report("Create decoded-lens chroma view", source_view(device, lens.texture.Get(), 1, &lens.uv)))
            return false;
    }
    context->CopySubresourceRegion(lens.texture.Get(), 0, 0, 0, 0,
                                   decoded.Get(), subresource, nullptr);
    return true;
}

using Fingerprint = std::array<uint16_t, 64>;

static bool lens_fingerprint(ID3D11Device3* device, ID3D11DeviceContext* context,
                             ID3D11ComputeShader* shader, const LensViews& lens,
                             Fingerprint& values)
{
    ComPtr<ID3D11Texture2D> pixels, staging;
    ComPtr<ID3D11UnorderedAccessView> output;
    if (FAILED(texture(device, 8, 8, DXGI_FORMAT_R16_UNORM, D3D11_BIND_UNORDERED_ACCESS, &pixels))
        || FAILED(device->CreateUnorderedAccessView(pixels.Get(), nullptr, &output))) return false;
    ID3D11ShaderResourceView* input = lens.y.Get();
    context->CSSetShader(shader, nullptr, 0);
    context->CSSetShaderResources(0, 1, &input);
    context->CSSetUnorderedAccessViews(0, 1, output.GetAddressOf(), nullptr);
    context->Dispatch(1, 1, 1);
    input = nullptr;
    ID3D11UnorderedAccessView* none = nullptr;
    context->CSSetShaderResources(0, 1, &input);
    context->CSSetUnorderedAccessViews(0, 1, &none, nullptr);
    D3D11_TEXTURE2D_DESC desc{};
    pixels->GetDesc(&desc);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    if (FAILED(device->CreateTexture2D(&desc, nullptr, &staging))) return false;
    context->CopyResource(staging.Get(), pixels.Get());
    D3D11_MAPPED_SUBRESOURCE map{};
    if (FAILED(context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &map))) return false;
    for (size_t y = 0; y < 8; ++y) {
        auto row = reinterpret_cast<const uint16_t*>(
            static_cast<const uint8_t*>(map.pData) + y*map.RowPitch);
        for (size_t x = 0; x < 8; ++x) values[y*8+x] = row[x] >> 6;
    }
    context->Unmap(staging.Get(), 0);
    return true;
}

static int distance(const Fingerprint& a, const Fingerprint& b)
{
    int result = 0;
    for (size_t i = 0; i < a.size(); ++i) result += std::abs(int(a[i])-int(b[i]));
    return result;
}

static bool decode_textures(const wchar_t* path, ID3D11Device3* device,
                            ID3D11DeviceContext* context, const wchar_t* shader_path = nullptr,
                            const wchar_t* poses_path = nullptr, const wchar_t* output = nullptr,
                            const wchar_t* preset = nullptr, const wchar_t* crf = nullptr)
try {
#ifndef INSTA360_MAIN10
    if (preset || crf) return false;
#endif
    ComPtr<IMFByteStream> file;
    ComPtr<IMFSourceResolver> resolver;
    ComPtr<IUnknown> resolved;
    ComPtr<IMFMediaSource> source;
    MF_OBJECT_TYPE kind{};
    if (!report("Open original camera byte stream", MFCreateFile(
            MF_ACCESSMODE_READ, MF_OPENMODE_FAIL_IF_NOT_EXIST, MF_FILEFLAGS_NONE, path, &file))
        || !report("Create MF source resolver", MFCreateSourceResolver(&resolver))
        || !report("Resolve camera stream as MP4", resolver->CreateObjectFromByteStream(
            file.Get(), L"camera.mp4", MF_RESOLUTION_MEDIASOURCE, nullptr, &kind, &resolved))
        || !report("Get media source", resolved.As(&source))) return false;

    UINT token{};
    ComPtr<IMFDXGIDeviceManager> manager;
    ComPtr<IMFAttributes> attributes;
    ComPtr<IMFSourceReader> reader;
    if (!report("Create decoder DXGI manager", MFCreateDXGIDeviceManager(&token, &manager))
        || !report("Set decoder D3D device", manager->ResetDevice(device, token))
        || FAILED(MFCreateAttributes(&attributes, 3))
        || FAILED(attributes->SetUINT32(MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS, TRUE))
        || FAILED(attributes->SetUINT32(MF_READWRITE_D3D_OPTIONAL, FALSE))
        || FAILED(attributes->SetUnknown(MF_SOURCE_READER_D3D_MANAGER, manager.Get()))
        || !report("Create D3D-backed reader", MFCreateSourceReaderFromMediaSource(
            source.Get(), attributes.Get(), &reader))
        || FAILED(reader->SetStreamSelection(static_cast<DWORD>(MF_SOURCE_READER_ALL_STREAMS), FALSE))) return false;

    std::vector<DWORD> lenses;
    ComPtr<IMFMediaType> requested;
    if (FAILED(MFCreateMediaType(&requested)) ||
        FAILED(requested->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video)) ||
        FAILED(requested->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_P010))) return false;
    for (DWORD track = 0; ; ++track) {
        ComPtr<IMFMediaType> original;
        GUID type{};
        if (FAILED(reader->GetNativeMediaType(track, 0, &original))) break;
        if (FAILED(original->GetGUID(MF_MT_MAJOR_TYPE, &type)) || type != MFMediaType_Video) continue;
        if (!report("Select native video track", reader->SetStreamSelection(track, TRUE))
            || !report("Request decoded P010 texture", reader->SetCurrentMediaType(
                track, nullptr, requested.Get()))) return false;
        lenses.push_back(track);
    }
    if (lenses.size() != 2) return false;

    LensViews views[2];
    if (preset) {
        for (auto& view : views)
            view.misc = D3D11_RESOURCE_MISC_SHARED_NTHANDLE | D3D11_RESOURCE_MISC_SHARED;
    }
    if (!shader_path) {
        LONGLONG times[2]{};
        const bool read = read_lens(reader.Get(), lenses[0], device, context, views[0], times[0])
            && read_lens(reader.Get(), lenses[1], device, context, views[1], times[1]);
        context->Flush();
        return read && times[0] == times[1] && SUCCEEDED(device->GetDeviceRemovedReason());
    }

    std::ifstream shader_file(std::filesystem::path(shader_path), std::ios::binary);
    const std::string shader((std::istreambuf_iterator<char>(shader_file)), {});
    if (shader.empty()) return false;
    ComPtr<ID3DBlob> bytecode, errors;
    HRESULT hr = D3DCompile(shader.data(), shader.size(), nullptr, nullptr, nullptr, "main",
                            "cs_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &bytecode, &errors);
    if (!report("Compile calibrated D3D11 stitch", hr)) {
        if (errors) std::fputs(static_cast<const char*>(errors->GetBufferPointer()), stderr);
        return false;
    }
    ComPtr<ID3D11ComputeShader> compute;
    ComPtr<ID3D11SamplerState> sampler;
    ComPtr<ID3D11Buffer> orientation;
    D3D11_SAMPLER_DESC sampler_desc{};
    sampler_desc.Filter = D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT;
    sampler_desc.AddressU = sampler_desc.AddressV = sampler_desc.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler_desc.ComparisonFunc = D3D11_COMPARISON_NEVER;
    D3D11_BUFFER_DESC buffer_desc{};
    buffer_desc.ByteWidth = 16;
    buffer_desc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    buffer_desc.Usage = D3D11_USAGE_DEFAULT;
    if (!report("Create calibrated stitch shader", device->CreateComputeShader(
            bytecode->GetBufferPointer(), bytecode->GetBufferSize(), nullptr, &compute))
        || FAILED(device->CreateSamplerState(&sampler_desc, &sampler))
        || FAILED(device->CreateBuffer(&buffer_desc, nullptr, &orientation))) return false;
    context->CSSetShader(compute.Get(), nullptr, 0);
    context->CSSetSamplers(0, 1, sampler.GetAddressOf());
    context->CSSetConstantBuffers(0, 1, orientation.GetAddressOf());
    std::ifstream poses(std::filesystem::path(poses_path), std::ios::binary);
    std::array<uint32_t,4> config{};
    std::array<Fingerprint,2> expected{}, actual{};
    if (!poses.read(reinterpret_cast<char*>(config.data()), sizeof(config))
        || !poses.read(reinterpret_cast<char*>(expected.data()), sizeof(expected))
        || !config[0] || (!config[1] && !preset) || !config[2] || !config[3])
        return false;
    const UINT width = config[2], height = config[3];
    ComPtr<ID3DBlob> fingerprint_code;
    ComPtr<ID3D11ComputeShader> fingerprint_shader;
    if (FAILED(D3DCompile(shader.data(), shader.size(), nullptr, nullptr, nullptr, "fingerprint",
                          "cs_5_0", 0, 0, &fingerprint_code, nullptr))
        || FAILED(device->CreateComputeShader(fingerprint_code->GetBufferPointer(),
            fingerprint_code->GetBufferSize(), nullptr, &fingerprint_shader))) return false;
    LONGLONG times[2]{};
    if (!read_lens(reader.Get(), lenses[0], device, context, views[0], times[0])
        || !read_lens(reader.Get(), lenses[1], device, context, views[1], times[1])
        || times[0] != 0 || times[1] != 0
        || !lens_fingerprint(device, context, fingerprint_shader.Get(), views[0], actual[0])
        || !lens_fingerprint(device, context, fingerprint_shader.Get(), views[1], actual[1])) return false;
    const int direct = distance(expected[0], actual[0]) + distance(expected[1], actual[1]);
    const int reverse = distance(expected[0], actual[1]) + distance(expected[1], actual[0]);
    const int front = direct < reverse ? 0 : 1;
    if ((front ? reverse : direct) > 512 || std::abs(direct-reverse) < 1024) {
        std::fprintf(stderr, "Cannot verify lens order (direct=%d, reversed=%d). Use OpenCL.\n",
                     direct, reverse);
        return false;
    }
    std::printf("Verified lens order %d%d (direct=%d, reversed=%d)\n", front, 1-front, direct, reverse);
    context->CSSetShader(compute.Get(), nullptr, 0);
    ComPtr<IMFSinkWriter> writer;
    DWORD stream{};
#ifdef INSTA360_MAIN10
    std::unique_ptr<Main10Stitch> main10;
    Main10Encoder software;
    if (preset) {
        if (width % 4 || height % 2 || width > 7680 || height > 3840) return false;
        main10 = std::make_unique<Main10Stitch>(device, context, width, height, views[front].texture.Get(),
                                               views[1-front].texture.Get(), shader);
        software.open(output, width, height, config[1], std::filesystem::path(preset).string(),
                      std::filesystem::path(crf).string());
        std::puts("Direct3D 12 planar output uses a CPU-readable UMA buffer");
    } else
#endif
    if (!open_encoder(output, device, width, height, config[1], writer, stream)) return false;
    for (UINT frame = 0; frame < config[0]; ++frame) {
        std::array<float,4> quaternion{};
        if (!poses.read(reinterpret_cast<char*>(quaternion.data()), sizeof(quaternion))) return false;
        const LONGLONG timestamp = static_cast<LONGLONG>(frame) * 10000000 * 1001 / 30000;
        if ((frame && (!read_lens(reader.Get(), lenses[0], device, context, views[0], times[0])
                       || !read_lens(reader.Get(), lenses[1], device, context, views[1], times[1])))
            || times[0] != times[1] || std::llabs(times[0]-timestamp) > 10) {
            std::fprintf(stderr, "Lens timestamps differ from the expected cadence at frame %u\n", frame);
            return false;
        }
#ifdef INSTA360_MAIN10
        if (main10) {
            main10->dispatch(quaternion, frame);
            if (frame) main10->encode(software, frame - 1);
        } else
#endif
        {
            ComPtr<ID3D11Texture2D> sphere;
            ComPtr<ID3D11UnorderedAccessView1> out_y, out_uv;
            if (FAILED(texture(device, width, height, DXGI_FORMAT_NV12, D3D11_BIND_UNORDERED_ACCESS, &sphere))
                || FAILED(output_view(device, sphere.Get(), 0, &out_y))
                || FAILED(output_view(device, sphere.Get(), 1, &out_uv))) return false;
            ID3D11ShaderResourceView* inputs[] = {views[front].y.Get(), views[front].uv.Get(),
                                                 views[1-front].y.Get(), views[1-front].uv.Get()};
            ID3D11UnorderedAccessView* outputs[] = {out_y.Get(), out_uv.Get()};
            context->UpdateSubresource(orientation.Get(), 0, nullptr, quaternion.data(), 0, 0);
            context->CSSetShaderResources(0, 4, inputs);
            context->CSSetUnorderedAccessViews(0, 2, outputs, nullptr);
            context->Dispatch((width + 7) / 8, (height + 7) / 8, 1);
            ID3D11ShaderResourceView* empty_inputs[] = {nullptr, nullptr, nullptr, nullptr};
            ID3D11UnorderedAccessView* empty_outputs[] = {nullptr, nullptr};
            context->CSSetShaderResources(0, 4, empty_inputs);
            context->CSSetUnorderedAccessViews(0, 2, empty_outputs, nullptr);
            if (!write_texture(writer.Get(), stream, sphere.Get(), frame)) return false;
        }
        if ((frame + 1) % 15 == 0 || frame + 1 == config[0]) {
            std::printf("out_time_us=%lld\n", (timestamp + 333667)/10);
            std::fflush(stdout);
        }
    }
    context->Flush();
    std::printf("Submitted %u stitched frames\n", config[0]);
#ifdef INSTA360_MAIN10
    if (main10) {
        main10->encode(software, config[0] - 1);
        software.finish();
        return SUCCEEDED(device->GetDeviceRemovedReason());
    }
#endif
    return SUCCEEDED(device->GetDeviceRemovedReason()) && report("Finalize HEVC export", writer->Finalize());
} catch (const std::exception& error) {
    std::fprintf(stderr, "%s\n", error.what());
    return false;
}

int wmain(int argc, wchar_t** argv)
{
    if (argc == 2 && std::wcscmp(argv[1], L"--protocol-version") == 0) {
        std::puts("insta360-d3d11-1");
        return 0;
    }
    if (argc == 2 && std::wcscmp(argv[1], L"--main10-version") == 0) {
#ifdef INSTA360_MAIN10
        std::puts("insta360-main10-1");
        return 0;
#else
        return 1;
#endif
    }
    const bool main10 = argc == 8 && std::wcscmp(argv[1], L"--stitch-main10") == 0;
    const bool stitch = main10 || (argc == 6 && std::wcscmp(argv[1], L"--stitch") == 0);
    const wchar_t* output = stitch ? argv[5] : argc == 2 ? argv[1] : nullptr;
    const wchar_t* input = stitch || (argc == 3 && std::wcscmp(argv[1], L"--decode") == 0) ? argv[2] : nullptr;
    if ((argc > 2 && !input) || (output && GetFileAttributesW(output) != INVALID_FILE_ATTRIBUTES)) {
        std::fputs("Usage: d3d11_surface_probe [new-output.mp4 | --decode source.insv | "
                   "--stitch source.insv shader.hlsl poses.bin new-output.mp4 | "
                   "--stitch-main10 source.insv shader.hlsl poses.bin new-output.mp4 preset crf]\n", stderr);
        return 1;
    }
    ComPtr<ID3D11Device> base;
    ComPtr<ID3D11DeviceContext> context;
    HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
                                   D3D11_CREATE_DEVICE_VIDEO_SUPPORT, nullptr, 0,
                                   D3D11_SDK_VERSION, &base, nullptr, &context);
    if (!report("Create D3D11 hardware device", hr)) return 1;
    ComPtr<ID3D11Device3> device;
    if (!report("Query ID3D11Device3", base.As(&device))) return 1;
    ComPtr<IDXGIDevice> dxgi;
    ComPtr<IDXGIAdapter> adapter;
    DXGI_ADAPTER_DESC details{};
    if (SUCCEEDED(base.As(&dxgi)) && SUCCEEDED(dxgi->GetAdapter(&adapter))
        && SUCCEEDED(adapter->GetDesc(&details))) {
        std::printf("Adapter: %ls\n", details.Description);
    }

    ComPtr<ID3D11Texture2D> lens;
    hr = texture(device.Get(), 3840, 3840, DXGI_FORMAT_P010, D3D11_BIND_SHADER_RESOURCE, &lens);
    if (!report("Create shader-readable 3840x3840 P010", hr)) return 2;
    ComPtr<ID3D11ShaderResourceView1> y, uv;
    const bool source_y = report("Create P010 luma shader view", source_view(device.Get(), lens.Get(), 0, &y));
    const bool source_uv = report("Create P010 chroma shader view", source_view(device.Get(), lens.Get(), 1, &uv));
    ComPtr<ID3D11Texture2D> sphere;
    hr = texture(device.Get(), 7680, 3840, DXGI_FORMAT_NV12, D3D11_BIND_UNORDERED_ACCESS, &sphere);
    if (!report("Create writable 7680x3840 NV12", hr)) return 3;
    ComPtr<ID3D11UnorderedAccessView1> out_y, out_uv;
    const bool target_y = report("Create NV12 luma compute view", output_view(device.Get(), sphere.Get(), 0, &out_y));
    const bool target_uv = report("Create NV12 chroma compute view", output_view(device.Get(), sphere.Get(), 1, &out_uv));
    if (!source_y || !source_uv || !target_y || !target_uv) return 4;

    // A small copy verifies that the views work in a dispatch and produce
    // the expected luma and chroma bytes. The readback is for verification.
    constexpr UINT size = 64;
    std::vector<uint16_t> pixels(size * size * 3 / 2, 256u << 6);
    for (size_t i = size * size; i < pixels.size(); i += 2) {
        pixels[i] = 512u << 6;
        pixels[i + 1] = 768u << 6;
    }
    D3D11_SUBRESOURCE_DATA data{pixels.data(), size * 2, 0};
    y.Reset(); uv.Reset(); out_y.Reset(); out_uv.Reset();
    lens.Reset(); sphere.Reset();
    if (!report("Upload P010 verification pattern", texture(device.Get(), size, size,
            DXGI_FORMAT_P010, D3D11_BIND_SHADER_RESOURCE, &lens, &data))
        || FAILED(texture(device.Get(), size, size, DXGI_FORMAT_NV12,
                          D3D11_BIND_UNORDERED_ACCESS, &sphere))
        || FAILED(source_view(device.Get(), lens.Get(), 0, &y))
        || FAILED(source_view(device.Get(), lens.Get(), 1, &uv))
        || FAILED(output_view(device.Get(), sphere.Get(), 0, &out_y))
        || FAILED(output_view(device.Get(), sphere.Get(), 1, &out_uv))) return 5;

    const char shader[] = R"(
        Texture2D<float> input_y : register(t0);
        Texture2D<float2> input_uv : register(t1);
        RWTexture2D<float> output_y : register(u0);
        RWTexture2D<float2> output_uv : register(u1);
        [numthreads(8,8,1)] void main(uint3 p : SV_DispatchThreadID) {
            uint width, height;
            output_uv.GetDimensions(width, height);
            output_y[p.xy] = input_y.Load(int3(p.xy % 64, 0));
            if (p.x < width && p.y < height)
                output_uv[p.xy] = input_uv.Load(int3(p.xy % 32, 0));
        }
    )";
    ComPtr<ID3DBlob> code, errors;
    hr = D3DCompile(shader, sizeof(shader) - 1, nullptr, nullptr, nullptr, "main",
                    "cs_5_0", 0, 0, &code, &errors);
    if (!report("Compile compute test", hr)) {
        if (errors) std::fputs(static_cast<const char*>(errors->GetBufferPointer()), stderr);
        return 6;
    }
    ComPtr<ID3D11ComputeShader> compute;
    if (!report("Create compute test",
                device->CreateComputeShader(code->GetBufferPointer(), code->GetBufferSize(),
                                            nullptr, &compute))) return 6;
    ID3D11ShaderResourceView* inputs[] = {y.Get(), uv.Get()};
    ID3D11UnorderedAccessView* outputs[] = {out_y.Get(), out_uv.Get()};
    context->CSSetShader(compute.Get(), nullptr, 0);
    context->CSSetShaderResources(0, 2, inputs);
    context->CSSetUnorderedAccessViews(0, 2, outputs, nullptr);
    context->Dispatch(size / 8, size / 8, 1);
    ID3D11UnorderedAccessView* unbind[] = {nullptr, nullptr};
    context->CSSetUnorderedAccessViews(0, 2, unbind, nullptr);

    D3D11_TEXTURE2D_DESC desc{};
    sphere->GetDesc(&desc);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Texture2D> staging;
    if (!report("Create verification readback", device->CreateTexture2D(&desc, nullptr, &staging))) return 7;
    context->CopyResource(staging.Get(), sphere.Get());
    D3D11_MAPPED_SUBRESOURCE mapped{};
    if (!report("Map verification readback", context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped))) return 7;
    const auto* bytes = static_cast<const uint8_t*>(mapped.pData);
    const auto* chroma = bytes + mapped.RowPitch * size;
    std::printf("Verification samples: Y=%u U=%u V=%u\n", bytes[0], chroma[0], chroma[1]);
    const bool matches = bytes[0] == 64 && chroma[0] == 128 && chroma[1] == 191;
    context->Unmap(staging.Get(), 0);
    if (!matches) return 8;
    if (!output && !input) return 0;

    if (output && !input) {
        sphere.Reset(); out_y.Reset(); out_uv.Reset();
        if (!report("Allocate GPU encoder surface", texture(device.Get(), 7680, 3840,
                DXGI_FORMAT_NV12, D3D11_BIND_UNORDERED_ACCESS, &sphere))
            || FAILED(output_view(device.Get(), sphere.Get(), 0, &out_y))
            || FAILED(output_view(device.Get(), sphere.Get(), 1, &out_uv))) return 9;
        outputs[0] = out_y.Get();
        outputs[1] = out_uv.Get();
        context->CSSetUnorderedAccessViews(0, 2, outputs, nullptr);
        context->Dispatch(7680 / 8, 3840 / 8, 1);
        context->CSSetUnorderedAccessViews(0, 2, unbind, nullptr);
        context->Flush();
    }

    ComPtr<ID3D10Multithread> multithread;
    if (!report("Query D3D multithread guard", context.As(&multithread))) return 9;
    multithread->SetMultithreadProtected(TRUE);
    if (!report("Initialize COM", CoInitializeEx(nullptr, COINIT_MULTITHREADED))) return 10;
    hr = MFStartup(MF_VERSION);
    const bool started = report("Initialize Media Foundation", hr);
    const bool verified = started && (input
        ? decode_textures(input, device.Get(), context.Get(), stitch ? argv[3] : nullptr,
                          stitch ? argv[4] : nullptr, output,
                          main10 ? argv[6] : nullptr, main10 ? argv[7] : nullptr)
        : encode_pattern(output, device.Get(), sphere.Get()));
    if (started) MFShutdown();
    CoUninitialize();
    return verified ? 0 : 11;
}
