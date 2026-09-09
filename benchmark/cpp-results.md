# C++ benchmark results

Benchmark corpus: 2,000 heterogeneous three-leg instruments, 1,200 underlyings, 30,000 paths, 252 steps, and 12 factors. The native executable was built with CMake `Release`, `-O3`, `-march=native`, `-mtune=native`, `-ffast-math`, and OpenMP.

| Backend | Threads | Path build | Pricing | Total | Instruments/sec |
|---|---:|---:|---:|---:|---:|
| Python NumPy reference | default | included | included | 3.32 s | 601.58 |
| C++ optimized fallback | 1 | 0.467 s | 1.500 s | 1.967 s | 1,016.69 |
| C++ optimized fallback | 8 | 0.204 s | 0.389 s | **0.593 s** | **3,374.14** |

The optimized C++ path uses precompiled underlying indices, a contiguous float32 terminal cube, allocation-free inner pricing loops, OpenMP trade-level parallelism, and a deterministic lightweight benchmark RNG. The 8-thread checksum was `59627804.28345022`; the 1-thread checksum was `59627804.28344861`, a negligible reduction-order difference.

The C++ fallback currently benchmarks the native execution substrate. Production parity should replace its benchmark RNG and payoff adapters with QuantLib C++ and XAD C++ while retaining the same DTOs and MCP boundary. Vercel continues to run the Python MCP endpoint; native benchmarks should run in a CI/Linux build image because Vercel Python functions do not provide the native QuantLib/XAD toolchain.
