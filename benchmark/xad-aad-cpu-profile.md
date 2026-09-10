# Native XAD AAD and CPU-controlled benchmark

## CPU profile

The benchmark host is a 6-vCPU KVM guest:

- CPU: Intel Xeon Processor @ 2.50GHz
- Physical cores: 3
- Logical CPUs: 6
- NUMA nodes: 1
- SIMD: AVX2 and AVX-512 available
- Compiler: GCC 13.3.0
- CMake: 3.28.3
- Python: project `uv` environment

The Python benchmark uses one ordinary Python instrument loop. NumPy vectorizes individual path operations, but the outer 100k-instrument loop is not multiprocessing/thread-pool parallelized, so process CPU time is approximately one core.

C++ OpenMP was tested at 1, 3, 6, and 8 threads on the same shared 100k/1k corpus:

| Threads | App compute time | Throughput | Process user CPU | Result |
|---:|---:|---:|---:|---|
| 1 | 7.17 s | 13,954/s | 7.86 s | baseline |
| 3 | 4.44 s | 22,531/s | 8.04 s | better |
| 6 | 3.81 s | 26,256/s | 7.93 s | best measured |
| 8 | 3.81 s | 26,262/s | 7.90 s | no material gain; oversubscribed |

The fair default for this 6-logical-CPU host is **6 OpenMP threads**, not 8. Eight threads are not faster within measurement noise and can increase contention on a shared worker.

## Python versus native XAD hybrid

Both engines consume the same 100,000-instrument corpus, 1,200-underlying market, and shared 1,000-path terminal cube.

| Engine | Method | Wall time | User CPU | Peak RSS |
|---|---|---:|---:|---:|
| Python | CRN bump/revalue with pathwise delta | 67.37 s | 67.33 s | 568 MiB |
| C++ | XAD reverse-mode fixed branch + CRN transition fallback, 6 threads | 19.41 s | 15.21 s | 783 MiB |

The native hybrid benchmark reports:

- XAD reverse-mode AAD delta checksum before fallback: `-143.83707833897`
- Hybrid delta checksum: `-143.91795059731695`
- Hybrid dollar-delta checksum: `-32682.05398906863`
- Python delta checksum: `-143.91795059749685`
- Python dollar-delta checksum: `-32682.05398914241`
- Hybrid delta residual: approximately `1.80e-10`
- Hybrid dollar-delta residual: approximately `7.38e-08`
- PV checksum: `93741.65008399007` in both runs

The native run used the actual XAD `xad::adj<double>` reverse-mode tape and QuantLib C++ linkage. It records a fixed branch for smooth paths and explicitly falls back to central CRN bump/revalue when the active worst-of or payoff branch is transition-sensitive. The run detected 299,762 transition-sensitive factor observations out of 300,000 and therefore correctly avoided presenting those discontinuous observations as pure AAD.

This is the first run in the branch that uses actual XAD reverse-mode AAD. The production QuantLibAAD integration remains an adapter step around the same QuantLib/XAD build; the current smoke benchmark verifies XAD tape behavior with a QuantLib discount-factor primitive and the fina-risk payoff kernel.
