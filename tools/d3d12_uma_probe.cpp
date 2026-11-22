// Checks whether a compute shader can write directly into CPU-readable memory.
#include <windows.h>
#include <d3d12.h>
#include <d3d11_1.h>
#include <d3dcompiler.h>
#include <dxgi1_6.h>
#include <wrl/client.h>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <utility>
#ifdef UMA_WITH_X265
#include <filesystem>
#include <fstream>
#include <memory>
#include <x265.h>
#endif

using Microsoft::WRL::ComPtr;
constexpr std::uint32_t width = 7680, height = 3840;
constexpr std::uint32_t words = width * height * 3 / 4;
constexpr std::size_t bytes = words * sizeof(std::uint32_t);

void check(HRESULT result, const char* operation) {
    if (FAILED(result)) {
        std::fprintf(stderr, "%s failed: 0x%08lx\n", operation, static_cast<unsigned long>(result));
        throw std::runtime_error(operation);
    }
}

void probe_d3d11_interop(IDXGIAdapter1* adapter, ID3D12Device* d3d12) {
    ComPtr<ID3D11Device> d3d11;
    check(D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, nullptr, 0,
                            D3D11_SDK_VERSION, &d3d11, nullptr, nullptr), "D3D11CreateDevice");
    ComPtr<ID3D11Device1> device;
    check(d3d11.As(&device), "Query D3D11.1");
    D3D12_HEAP_PROPERTIES heap{};
    heap.Type = D3D12_HEAP_TYPE_CUSTOM;
    heap.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_WRITE_BACK;
    heap.MemoryPoolPreference = D3D12_MEMORY_POOL_L0;
    heap.CreationNodeMask = heap.VisibleNodeMask = 1;
    D3D12_RESOURCE_DESC description{};
    description.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    description.Width = bytes;
    description.Height = description.DepthOrArraySize = description.MipLevels = 1;
    description.SampleDesc.Count = 1;
    description.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    description.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    ComPtr<ID3D12Resource> shared;
    check(d3d12->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_SHARED, &description,
                                         D3D12_RESOURCE_STATE_COMMON, nullptr, IID_PPV_ARGS(&shared)),
          "Share CPU-readable D3D12 UAV");
    HANDLE handle = nullptr;
    check(d3d12->CreateSharedHandle(shared.Get(), nullptr, GENERIC_ALL, nullptr, &handle),
          "Create shared resource handle");
    ComPtr<ID3D11Buffer> buffer;
    const HRESULT opened = device->OpenSharedResource1(handle, IID_PPV_ARGS(&buffer));
    CloseHandle(handle);
    check(opened, "Open CPU-readable D3D12 buffer as D3D11");
    puts("D3D11 can open the CPU-readable buffer");
}

void probe_shared_lens(IDXGIAdapter1* adapter, ID3D12Device* d3d12) {
    ComPtr<ID3D11Device> d3d11;
    check(D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, nullptr, 0,
                            D3D11_SDK_VERSION, &d3d11, nullptr, nullptr), "D3D11CreateDevice");
    D3D11_TEXTURE2D_DESC description{};
    description.Width = description.Height = 3840;
    description.MipLevels = description.ArraySize = 1;
    description.SampleDesc.Count = 1;
    description.Format = DXGI_FORMAT_P010;
    description.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    description.MiscFlags = D3D11_RESOURCE_MISC_SHARED_NTHANDLE | D3D11_RESOURCE_MISC_SHARED;
    ComPtr<ID3D11Texture2D> lens;
    check(d3d11->CreateTexture2D(&description, nullptr, &lens), "Create shared P010 lens");
    ComPtr<IDXGIResource1> resource;
    check(lens.As(&resource), "Query IDXGIResource1");
    HANDLE handle = nullptr;
    check(resource->CreateSharedHandle(nullptr, DXGI_SHARED_RESOURCE_READ, nullptr, &handle),
          "Create P010 handle");
    ComPtr<ID3D12Resource> input;
    const HRESULT opened = d3d12->OpenSharedHandle(handle, IID_PPV_ARGS(&input));
    CloseHandle(handle);
    check(opened, "Open P010 in D3D12");
    puts("D3D12 can open the D3D11 P010 lens");
}

#ifdef UMA_WITH_X265
class HevcOutput {
    const x265_api* api = x265_api_get(10);
    std::ofstream stream;
    std::unique_ptr<x265_encoder, void (*)(x265_encoder*)> encoder;
    x265_picture picture{};
    int written = 0;

    int encode(x265_picture* input) {
        x265_nal* nals = nullptr;
        std::uint32_t count = 0;
        const int result = api->encoder_encode(encoder.get(), &nals, &count, input, nullptr);
        if (result < 0) throw std::runtime_error("x265 encode failed");
        for (std::uint32_t i = 0; i < count; ++i)
            stream.write(reinterpret_cast<const char*>(nals[i].payload), nals[i].sizeBytes);
        if (!stream) throw std::runtime_error("HEVC output write failed");
        written += result;
        return result;
    }

public:
    explicit HevcOutput(const std::filesystem::path& path)
        : stream(path, std::ios::binary), encoder(nullptr, api ? api->encoder_close : nullptr) {
        if (!api || api->bit_depth != 10 || !stream) throw std::runtime_error("Cannot initialize Main10 output");
        std::unique_ptr<x265_param, void (*)(x265_param*)> params(api->param_alloc(), api->param_free);
        if (!params || api->param_default_preset(params.get(), "ultrafast", nullptr) < 0)
            throw std::runtime_error("Cannot initialize x265 preset");
        params->sourceWidth = width;
        params->sourceHeight = height;
        params->fpsNum = 30000;
        params->fpsDenom = 1001;
        params->internalCsp = X265_CSP_I420;
        params->bAnnexB = 1;
        params->bRepeatHeaders = 1;
        params->bCopyPicToFrame = 1;
        params->frameNumThreads = 1;
        if (api->param_parse(params.get(), "pools", "none") < 0)
            throw std::runtime_error("Cannot configure x265 thread pools");
        if (api->param_apply_profile(params.get(), "main10") < 0)
            throw std::runtime_error("Cannot select Main10 profile");
        encoder.reset(api->encoder_open(params.get()));
        if (!encoder) throw std::runtime_error("Cannot open x265");
        api->picture_init(params.get(), &picture);
        picture.bitDepth = 10;
        picture.stride[0] = width * 2;
        picture.stride[1] = picture.stride[2] = width;
    }

    void submit(void* mapped, int frame) {
        auto* planar = static_cast<std::uint16_t*>(mapped);
        picture.planes[0] = planar;
        picture.planes[1] = planar + width * height;
        picture.planes[2] = planar + width * height * 5 / 4;
        picture.pts = frame;
        // x265 copies the input into its own padded frame during this call.
        encode(&picture);
    }

    void finish(int expected) {
        while (encode(nullptr)) {}
        if (written != expected) throw std::runtime_error("Unexpected x265 frame count");
        stream.close();
        if (!stream) throw std::runtime_error("HEVC output close failed");
    }
};
#endif

int wmain(int argc, wchar_t** argv) try {
    const bool shared_lens = argc == 2 && std::wcscmp(argv[1], L"--shared-lens") == 0;
    const bool interop = shared_lens || (argc == 2 && std::wcscmp(argv[1], L"--interop") == 0);
#ifdef UMA_WITH_X265
    if (!interop && argc != 1 && argc != 3)
        throw std::runtime_error("Usage: d3d12_uma_probe [new-staged.hevc new-direct.hevc]");
    const bool encode_frames = argc == 3;
    std::array<std::unique_ptr<HevcOutput>, 2> encoders;
    if (encode_frames) {
        const std::filesystem::path staged = argv[1], direct = argv[2];
        if (staged == direct || std::filesystem::exists(staged) || std::filesystem::exists(direct))
            throw std::runtime_error("Choose two distinct new output files");
        encoders[0] = std::make_unique<HevcOutput>(staged);
        encoders[1] = std::make_unique<HevcOutput>(direct);
    }
#else
    if (!interop && argc != 1) throw std::runtime_error("Build with UMA_WITH_X265 to enable HEVC output");
    constexpr bool encode_frames = false;
#endif
    ComPtr<IDXGIFactory6> factory;
    check(CreateDXGIFactory1(IID_PPV_ARGS(&factory)), "CreateDXGIFactory1");
    ComPtr<IDXGIAdapter1> adapter;
    check(factory->EnumAdapterByGpuPreference(
        0, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE, IID_PPV_ARGS(&adapter)), "EnumAdapter");
    DXGI_ADAPTER_DESC1 adapter_description{};
    check(adapter->GetDesc1(&adapter_description), "GetDesc1");
    std::printf("Adapter: %ls\n", adapter_description.Description);
    ComPtr<ID3D12Device> device;
    check(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)), "D3D12CreateDevice");
    D3D12_FEATURE_DATA_ARCHITECTURE1 architecture{};
    check(device->CheckFeatureSupport(D3D12_FEATURE_ARCHITECTURE1, &architecture, sizeof(architecture)),
          "CheckFeatureSupport");
    std::printf("UMA: %d, CacheCoherentUMA: %d\n", architecture.UMA, architecture.CacheCoherentUMA);
    if (!architecture.UMA) return 2;
    if (interop) {
        if (shared_lens) probe_shared_lens(adapter.Get(), device.Get());
        else probe_d3d11_interop(adapter.Get(), device.Get());
        return 0;
    }

    const char shader[] = R"(
        RWByteAddressBuffer output : register(u0);
        cbuffer Frame : register(b0) { uint salt; uint planar; }
        uint sample_at(uint index) {
            if (index < 7680 * 3840)
                return 64 + ((index % 7680 + (index / 7680) * 2 + salt * 31) % 877);
            return index < 7680 * 3840 * 5 / 4 ? 464 + salt * 7 : 560 - salt * 7;
        }
        [numthreads(64, 1, 1)]
        void main(uint3 id : SV_DispatchThreadID) {
            uint index = id.x + id.y * 7680;
            uint value = planar ? sample_at(index * 2) | (sample_at(index * 2 + 1) << 16) : index ^ salt;
            output.Store(index * 4, value);
        }
    )";
    ComPtr<ID3DBlob> code, signature;
    check(D3DCompile(shader, sizeof(shader) - 1, nullptr, nullptr, nullptr, "main", "cs_5_0",
                     D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &code, nullptr), "D3DCompile");
    D3D12_ROOT_PARAMETER root_parameters[2]{};
    root_parameters[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    root_parameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    root_parameters[1].Constants.Num32BitValues = 2;
    D3D12_ROOT_SIGNATURE_DESC root_description{};
    root_description.NumParameters = 2;
    root_description.pParameters = root_parameters;
    check(D3D12SerializeRootSignature(&root_description, D3D_ROOT_SIGNATURE_VERSION_1,
                                     &signature, nullptr), "SerializeRootSignature");
    ComPtr<ID3D12RootSignature> root;
    check(device->CreateRootSignature(0, signature->GetBufferPointer(), signature->GetBufferSize(),
                                      IID_PPV_ARGS(&root)), "CreateRootSignature");
    D3D12_COMPUTE_PIPELINE_STATE_DESC pipeline_description{};
    pipeline_description.pRootSignature = root.Get();
    pipeline_description.CS = {code->GetBufferPointer(), code->GetBufferSize()};
    ComPtr<ID3D12PipelineState> pipeline;
    check(device->CreateComputePipelineState(&pipeline_description, IID_PPV_ARGS(&pipeline)),
          "CreateComputePipelineState");
    D3D12_COMMAND_QUEUE_DESC queue_description{};
    ComPtr<ID3D12CommandQueue> queue;
    check(device->CreateCommandQueue(&queue_description, IID_PPV_ARGS(&queue)), "CreateCommandQueue");
    ComPtr<ID3D12CommandAllocator> allocator;
    check(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocator)),
          "CreateCommandAllocator");
    ComPtr<ID3D12GraphicsCommandList> list;
    check(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator.Get(), pipeline.Get(),
                                    IID_PPV_ARGS(&list)), "CreateCommandList");
    check(list->Close(), "Close");
    ComPtr<ID3D12Fence> fence;
    check(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)), "CreateFence");

    auto buffer = [&](D3D12_HEAP_TYPE type, D3D12_RESOURCE_STATES state, bool uav) {
        D3D12_HEAP_PROPERTIES heap{};
        heap.Type = type;
        heap.CreationNodeMask = heap.VisibleNodeMask = 1;
        if (type == D3D12_HEAP_TYPE_CUSTOM) {
            heap.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_WRITE_BACK;
            heap.MemoryPoolPreference = D3D12_MEMORY_POOL_L0;
        }
        D3D12_RESOURCE_DESC description{};
        description.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        description.Width = bytes;
        description.Height = description.DepthOrArraySize = description.MipLevels = 1;
        description.SampleDesc.Count = 1;
        description.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        description.Flags = uav ? D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS : D3D12_RESOURCE_FLAG_NONE;
        ComPtr<ID3D12Resource> resource;
        check(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &description,
                                              state, nullptr, IID_PPV_ARGS(&resource)),
              type == D3D12_HEAP_TYPE_CUSTOM ? "Create CPU-readable UAV" : "Create buffer");
        return resource;
    };
    auto gpu = buffer(D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, true);
    auto readback = buffer(D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST, false);
    auto shared = buffer(D3D12_HEAP_TYPE_CUSTOM, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, true);
    const int runs = encode_frames ? 6 : 21;
    std::array<double, 2> totals{};
    std::uint64_t sequence = 0;
    for (int iteration = 0; iteration < runs; ++iteration) {
        for (bool direct : {false, true}) {
            auto* destination = direct ? shared.Get() : gpu.Get();
            auto* cpu = direct ? shared.Get() : readback.Get();
            const auto started = std::chrono::steady_clock::now();
            check(allocator->Reset(), "Reset allocator");
            check(list->Reset(allocator.Get(), pipeline.Get()), "Reset list");
            list->SetComputeRootSignature(root.Get());
            list->SetComputeRootUnorderedAccessView(0, destination->GetGPUVirtualAddress());
            ++sequence;
            const auto salt = encode_frames ? static_cast<std::uint32_t>(iteration)
                                            : static_cast<std::uint32_t>(sequence) * 0x9e3779b9u;
            const std::uint32_t constants[] = {salt, encode_frames};
            list->SetComputeRoot32BitConstants(1, 2, constants, 0);
            list->Dispatch(width / 64, words / width, 1);
            if (!direct) {
                D3D12_RESOURCE_BARRIER barrier{};
                barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
                barrier.Transition = {gpu.Get(), D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES,
                                      D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE};
                list->ResourceBarrier(1, &barrier);
                list->CopyBufferRegion(readback.Get(), 0, gpu.Get(), 0, bytes);
                std::swap(barrier.Transition.StateBefore, barrier.Transition.StateAfter);
                list->ResourceBarrier(1, &barrier);
            }
            check(list->Close(), "Close");
            ID3D12CommandList* commands[] = {list.Get()};
            queue->ExecuteCommandLists(1, commands);
            check(queue->Signal(fence.Get(), sequence), "Signal");
            check(fence->SetEventOnCompletion(sequence, nullptr), "Wait for fence");
            const D3D12_RANGE read_range{0, bytes}, write_range{0, 0};
            void* mapped = nullptr;
            check(cpu->Map(0, &read_range, &mapped), "Map for CPU read");
            if (iteration) totals[direct] += std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - started).count();
#ifdef UMA_WITH_X265
            if (encode_frames) {
                encoders[direct]->submit(mapped, iteration);
            } else
#endif
            {
                const auto* values = static_cast<const std::uint32_t*>(mapped);
                for (std::uint32_t index = 0; index < words; ++index) {
                    if (values[index] != (index ^ salt)) throw std::runtime_error("GPU output mismatch");
                }
            }
            cpu->Unmap(0, &write_range);
        }
    }
#ifdef UMA_WITH_X265
    if (encode_frames) {
        for (auto& output : encoders) output->finish(runs);
        std::printf("Encoded %d planar Main10 frames per path.\n", runs);
    } else
#endif
    {
        std::printf("Verified every word of %zu bytes per frame over %d runs per path.\n", bytes, runs);
    }
    std::printf("GPU write + copy + fence + map: %.2f ms\n", totals[0] / (runs - 1));
    std::printf("GPU direct write + fence + map: %.2f ms\n", totals[1] / (runs - 1));
    return 0;
} catch (const std::exception& error) {
    std::fprintf(stderr, "%s\n", error.what());
    return 1;
}
