# Exact Python/C++ parity: 100k instruments / 1k paths

The earlier benchmark comparison was not apples-to-apples: Python and C++ generated different path cubes and did not calculate identical risk scopes. This retest generates one augmented 100,000-instrument / 1,200-underlying corpus and one shared float32 terminal path cube with 1,000 paths. Both engines read those exact files and execute the same central-bump PV, delta, gamma, and first-order Taylor P&L formulas.

| Artifact | SHA-256 |
|---|---|
| `instruments.json` | `e84cd547dd7aa5c645e29f2753add46188405f5066c10598a2f037cec550cdf2` |
| `market.json` | `7dbb02017a8518b02e847798ab71a68a5e2fbecfa82628eb76a565788b3901e6` |
| `paths.bin` | `d1babd7de3412e39abae448538de45e4779f8556a00c999d51ed7cbebead0b68` |

## Results

| Metric | Python | C++ | Absolute residual | Relative residual |
|---|---:|---:|---:|---:|
| PV checksum | 93,741.65008399007 | 93,741.65008399046 | 3.93e-10 | 4.19e-15 |
| Delta checksum | -143.91795059749685 | -143.91795059749640 | 4.55e-13 | 3.16e-15 |
| Dollar-delta checksum | -32,682.05398914241 | -32,682.05398914235 | 6.18e-11 | 1.89e-15 |
| Gamma checksum | 9.880407877676737 | 9.880407877677019 | 2.82e-13 | 2.86e-14 |
| Taylor forecast checksum | -316.77613476702317 | -316.77613476702624 | 3.07e-12 | 9.69e-15 |
| Taylor actual checksum | -319.17265221206630 | -319.17265221207026 | 3.98e-12 | 1.25e-14 |
| Taylor unexplained checksum | -2.396517445043116 | -2.396517445044026 | 9.09e-13 | 3.80e-13 |

Acceptance threshold: `abs(C++ - Python) <= 1e-8 * max(1, abs(Python))`. All checks passed.

## Timing

| Engine | Elapsed | Throughput |
|---|---:|---:|
| Python shared-cube parity | 66.07 s | 1,513.51 instruments/s |
| C++ shared-cube parity, 8 threads | 3.83 s | 26,142.54 instruments/s |

The C++ result is approximately **17.3× faster** for this identical compute scope. This is a valid compute comparison because ingestion artifacts, market data, path cube, path count, seed metadata, bump size, instrument records, and formulas are shared. It does not claim that the current fallback is production QuantLib/XAD AAD; method provenance remains `CRN_BUMP_REVALUE_WITH_PATHWISE_DELTA` until those native adapters are installed.
