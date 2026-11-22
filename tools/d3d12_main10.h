#pragma once

#include <d3d11_4.h>
#include <d3d12.h>
#include <stdexcept>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/opt.h>
}

inline void main10_check(HRESULT hr, const char* operation)
{
    if (!report(operation, hr)) throw std::runtime_error(operation);
}

inline void av_check(int result, const char* operation)
{
    if (result >= 0) return;
    char error[AV_ERROR_MAX_STRING_SIZE]{};
    av_strerror(result, error, sizeof(error));
    throw std::runtime_error(std::string(operation) + ": " + error);
}

class Main10Encoder {
    AVFormatContext* mux = nullptr;
    AVCodecContext* codec = nullptr;
    AVFrame* frame = nullptr;
    AVPacket* packet = nullptr;
    AVStream* stream = nullptr;

    void receive()
    {
        for (;;) {
            const int result = avcodec_receive_packet(codec, packet);
            if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) return;
            av_check(result, "Receive x265 packet");
            packet->duration = 1;
            av_packet_rescale_ts(packet, codec->time_base, stream->time_base);
            packet->stream_index = stream->index;
            av_check(av_interleaved_write_frame(mux, packet), "Write MP4 packet");
        }
    }

public:
    ~Main10Encoder()
    {
        av_packet_free(&packet);
        av_frame_free(&frame);
        avcodec_free_context(&codec);
        if (mux) {
            avio_closep(&mux->pb);
            avformat_free_context(mux);
        }
    }

    void open(const wchar_t* path, UINT width, UINT height, UINT bitrate,
              const std::string& preset, const std::string& crf)
    {
        const auto filename = std::filesystem::path(path).u8string();
        av_check(avformat_alloc_output_context2(&mux, nullptr, "mp4", filename.c_str()), "Create MP4");
        const AVCodec* encoder = avcodec_find_encoder_by_name("libx265");
        if (!encoder) throw std::runtime_error("FFmpeg build lacks libx265");
        codec = avcodec_alloc_context3(encoder);
        stream = avformat_new_stream(mux, nullptr);
        frame = av_frame_alloc();
        packet = av_packet_alloc();
        if (!codec || !stream || !frame || !packet) throw std::bad_alloc();
        codec->width = width;
        codec->height = height;
        codec->time_base = {1001, 30000};
        codec->framerate = {30000, 1001};
        codec->thread_count = 0; // Match FFmpeg CLI's automatic frame threading.
        codec->pix_fmt = AV_PIX_FMT_YUV420P10LE;
        codec->sample_aspect_ratio = {1, 1};
        codec->color_range = AVCOL_RANGE_MPEG;
        codec->colorspace = AVCOL_SPC_BT709;
        codec->color_trc = AVCOL_TRC_BT709;
        codec->color_primaries = AVCOL_PRI_BT709;
        codec->chroma_sample_location = AVCHROMA_LOC_LEFT;
        codec->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
        av_check(av_opt_set(codec->priv_data, "preset", preset.c_str(), 0), "Select x265 preset");
        if (bitrate) codec->bit_rate = bitrate;
        else av_check(av_opt_set(codec->priv_data, "crf", crf.c_str(), 0), "Set x265 CRF");
        av_check(avcodec_open2(codec, encoder, nullptr), "Open x265");
        stream->time_base = codec->time_base;
        stream->avg_frame_rate = codec->framerate;
        av_check(avcodec_parameters_from_context(stream->codecpar, codec), "Copy codec parameters");
        av_check(avio_open(&mux->pb, filename.c_str(), AVIO_FLAG_WRITE), "Open MP4");
        av_check(avformat_write_header(mux, nullptr), "Write MP4 header");
    }

    void submit(void* pixels, UINT index)
    {
        const std::size_t luma = static_cast<std::size_t>(codec->width) * codec->height * 2;
        auto* data = static_cast<uint8_t*>(pixels);
        frame->buf[0] = av_buffer_create(data, luma * 3 / 2, [](void*, uint8_t*) {}, nullptr,
                                         AV_BUFFER_FLAG_READONLY);
        if (!frame->buf[0]) throw std::bad_alloc();
        frame->data[0] = data;
        frame->data[1] = data + luma;
        frame->data[2] = data + luma + luma/4;
        frame->linesize[0] = codec->width * 2;
        frame->linesize[1] = frame->linesize[2] = codec->width;
        frame->width = codec->width;
        frame->height = codec->height;
        frame->format = codec->pix_fmt;
        frame->pts = index;
        av_check(avcodec_send_frame(codec, frame), "Submit shared planes to x265");
        receive();
        if (av_buffer_get_ref_count(frame->buf[0]) != 1)
            throw std::runtime_error("Encoder retained the shared input");
        av_frame_unref(frame);
    }

    void finish()
    {
        av_check(avcodec_send_frame(codec, nullptr), "Flush x265");
        receive();
        av_check(av_write_trailer(mux), "Finalize MP4");
    }
};

class Main10Stitch {
    struct Surface {
        ComPtr<ID3D12CommandAllocator> allocator;
        ComPtr<ID3D12Resource> output;
        UINT64 ready = 0;
    };
    ComPtr<ID3D12Device> gpu;
    ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12GraphicsCommandList> list;
    ComPtr<ID3D12Fence> fence;
    ComPtr<ID3D11Fence> decoder_fence;
    ComPtr<ID3D11DeviceContext4> decoder;
    ComPtr<ID3D12RootSignature> root;
    ComPtr<ID3D12PipelineState> pipeline;
    ComPtr<ID3D12DescriptorHeap> views;
    std::array<ComPtr<ID3D12Resource>, 2> lenses;
    std::array<Surface, 2> surfaces;
    UINT width, height;
    std::size_t bytes;
    UINT64 sequence = 0;

public:
    Main10Stitch(ID3D11Device3* device, ID3D11DeviceContext* context, UINT w, UINT h,
                 ID3D11Texture2D* front, ID3D11Texture2D* back, const std::string& shader)
        : width(w), height(h), bytes(static_cast<std::size_t>(w) * h * 3)
    {
        ComPtr<IDXGIDevice> dxgi;
        ComPtr<IDXGIAdapter> adapter;
        ComPtr<ID3D11Device5> shared_device;
        main10_check(device->QueryInterface(IID_PPV_ARGS(&dxgi)), "Query DXGI device");
        main10_check(dxgi->GetAdapter(&adapter), "Get decoder adapter");
        main10_check(device->QueryInterface(IID_PPV_ARGS(&shared_device)), "Query D3D11 shared fence support");
        main10_check(context->QueryInterface(IID_PPV_ARGS(&decoder)), "Query decoder context");
        main10_check(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&gpu)),
                     "Create Direct3D 12 on decoder adapter");
        D3D12_FEATURE_DATA_ARCHITECTURE1 architecture{};
        main10_check(gpu->CheckFeatureSupport(D3D12_FEATURE_ARCHITECTURE1, &architecture,
                                              sizeof(architecture)), "Check GPU memory architecture");
        if (!architecture.CacheCoherentUMA) throw std::runtime_error("Cache-coherent UMA is required");
        main10_check(shared_device->CreateFence(0, D3D11_FENCE_FLAG_SHARED, IID_PPV_ARGS(&decoder_fence)),
                     "Create decoder fence");
        HANDLE handle{};
        main10_check(decoder_fence->CreateSharedHandle(nullptr, GENERIC_ALL, nullptr, &handle), "Share decoder fence");
        const auto open_fence = gpu->OpenSharedHandle(handle, IID_PPV_ARGS(&fence));
        CloseHandle(handle);
        main10_check(open_fence, "Open decoder fence");

        ComPtr<ID3DBlob> code, errors, signature;
        const auto compiled = D3DCompile(shader.data(), shader.size(), nullptr, nullptr, nullptr,
                                         "main10", "cs_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &code, &errors);
        if (FAILED(compiled) && errors)
            std::fputs(static_cast<const char*>(errors->GetBufferPointer()), stderr);
        main10_check(compiled, "Compile planar stitch shader");
        const D3D12_DESCRIPTOR_RANGE range{D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 4, 0, 0, 0};
        D3D12_ROOT_PARAMETER parameters[3]{};
        parameters[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        parameters[0].DescriptorTable = {1, &range};
        parameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
        parameters[1].Descriptor.ShaderRegister = 2;
        parameters[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
        parameters[2].Constants.Num32BitValues = 4;
        D3D12_STATIC_SAMPLER_DESC sampler{};
        sampler.Filter = D3D12_FILTER_MIN_MAG_LINEAR_MIP_POINT;
        sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
        sampler.ComparisonFunc = D3D12_COMPARISON_FUNC_NEVER;
        sampler.MaxLOD = D3D12_FLOAT32_MAX;
        const D3D12_ROOT_SIGNATURE_DESC signature_desc{3, parameters, 1, &sampler, D3D12_ROOT_SIGNATURE_FLAG_NONE};
        main10_check(D3D12SerializeRootSignature(&signature_desc, D3D_ROOT_SIGNATURE_VERSION_1, &signature, nullptr),
                     "Serialize planar shader layout");
        main10_check(gpu->CreateRootSignature(0, signature->GetBufferPointer(), signature->GetBufferSize(),
                                              IID_PPV_ARGS(&root)), "Create planar shader layout");
        D3D12_COMPUTE_PIPELINE_STATE_DESC pipeline_desc{};
        pipeline_desc.pRootSignature = root.Get();
        pipeline_desc.CS = {code->GetBufferPointer(), code->GetBufferSize()};
        main10_check(gpu->CreateComputePipelineState(&pipeline_desc, IID_PPV_ARGS(&pipeline)), "Create planar pipeline");
        const D3D12_COMMAND_QUEUE_DESC queue_desc{};
        main10_check(gpu->CreateCommandQueue(&queue_desc, IID_PPV_ARGS(&queue)), "Create compute queue");
        for (auto& surface : surfaces)
            main10_check(gpu->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                                                     IID_PPV_ARGS(&surface.allocator)), "Create command allocator");
        main10_check(gpu->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, surfaces[0].allocator.Get(), pipeline.Get(),
                                            IID_PPV_ARGS(&list)), "Create command list");
        main10_check(list->Close(), "Close initial command list");

        const D3D12_DESCRIPTOR_HEAP_DESC view_desc{D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 4,
                                                  D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 0};
        main10_check(gpu->CreateDescriptorHeap(&view_desc, IID_PPV_ARGS(&views)), "Create lens descriptor heap");
        auto slot = views->GetCPUDescriptorHandleForHeapStart();
        ID3D11Texture2D* inputs[] = {front, back};
        for (UINT i = 0; i < 2; ++i) {
            ComPtr<IDXGIResource1> resource;
            main10_check(inputs[i]->QueryInterface(IID_PPV_ARGS(&resource)), "Query shared lens resource");
            main10_check(resource->CreateSharedHandle(nullptr, DXGI_SHARED_RESOURCE_READ, nullptr, &handle),
                         "Share decoded lens");
            const auto opened = gpu->OpenSharedHandle(handle, IID_PPV_ARGS(&lenses[i]));
            CloseHandle(handle);
            main10_check(opened, "Open P010 lens in D3D12");
            for (UINT plane = 0; plane < 2; ++plane) {
                D3D12_SHADER_RESOURCE_VIEW_DESC view{};
                view.Format = plane ? DXGI_FORMAT_R16G16_UNORM : DXGI_FORMAT_R16_UNORM;
                view.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
                view.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
                view.Texture2D.MipLevels = 1;
                view.Texture2D.PlaneSlice = plane;
                gpu->CreateShaderResourceView(lenses[i].Get(), &view, slot);
                slot.ptr += gpu->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
            }
        }

        const D3D12_HEAP_PROPERTIES heap{D3D12_HEAP_TYPE_CUSTOM, D3D12_CPU_PAGE_PROPERTY_WRITE_BACK,
                                         D3D12_MEMORY_POOL_L0, 1, 1};
        D3D12_RESOURCE_DESC buffer{};
        buffer.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        buffer.Width = bytes;
        buffer.Height = buffer.DepthOrArraySize = buffer.MipLevels = 1;
        buffer.SampleDesc.Count = 1;
        buffer.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        buffer.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
        for (auto& surface : surfaces)
            main10_check(gpu->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer,
                                                       D3D12_RESOURCE_STATE_UNORDERED_ACCESS, nullptr,
                                                       IID_PPV_ARGS(&surface.output)), "Create CPU-readable planar output");
    }

    void dispatch(const std::array<float, 4>& rotation, UINT index)
    {
        auto& surface = surfaces[index % surfaces.size()];
        if (FAILED(decoder->Signal(decoder_fence.Get(), ++sequence))) throw std::runtime_error("Signal decoder");
        decoder->Flush();
        if (FAILED(queue->Wait(fence.Get(), sequence)) || FAILED(surface.allocator->Reset()) ||
            FAILED(list->Reset(surface.allocator.Get(), pipeline.Get()))) throw std::runtime_error("Start planar stitch");
        // The shared P010 textures are in COMMON after the D3D11 submission.
        // Read-only implicit promotion decays back to COMMON at submission completion.
        list->SetComputeRootSignature(root.Get());
        list->SetDescriptorHeaps(1, views.GetAddressOf());
        list->SetComputeRootDescriptorTable(0, views->GetGPUDescriptorHandleForHeapStart());
        list->SetComputeRootUnorderedAccessView(1, surface.output->GetGPUVirtualAddress());
        list->SetComputeRoot32BitConstants(2, 4, rotation.data(), 0);
        list->Dispatch((width+15)/16, (height+7)/8, 1);
        if (FAILED(list->Close())) throw std::runtime_error("Close planar command list");
        ID3D12CommandList* commands[] = {list.Get()};
        queue->ExecuteCommandLists(1, commands);
        surface.ready = ++sequence;
        if (FAILED(queue->Signal(fence.Get(), surface.ready)) ||
            FAILED(decoder->Wait(decoder_fence.Get(), surface.ready)))
            throw std::runtime_error("Schedule stitch completion");
    }

    void encode(Main10Encoder& encoder, UINT index)
    {
        const auto& surface = surfaces[index % surfaces.size()];
        if (FAILED(fence->SetEventOnCompletion(surface.ready, nullptr)) ||
            FAILED(gpu->GetDeviceRemovedReason())) throw std::runtime_error("Complete planar stitch");
        const D3D12_RANGE read{0, bytes}, no_write{0, 0};
        void* pixels = nullptr;
        if (FAILED(surface.output->Map(0, &read, &pixels))) throw std::runtime_error("Map shared planar frame");
        try {
            encoder.submit(pixels, index);
        } catch (...) {
            surface.output->Unmap(0, &no_write);
            throw;
        }
        surface.output->Unmap(0, &no_write);
    }
};
