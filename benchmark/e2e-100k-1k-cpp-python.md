# E2E hybrid benchmark: 100k instruments / 1k paths

The run follows the documented journey: ingestion, structure compilation, shared path generation, hybrid risk calculation, normalized wide/long output, Arrow/Parquet conversion, and DuckDB OLAP aggregation.

| Backend | Scope | Total | Throughput |
|---|---|---:|---:|
| Python augmented | 100,000 records, 1,000 reused structures, 1,000 paths, hybrid AAD/fallback benchmark | 138.14 s | 723.97 instruments/s |
| C++ native E2E | 100,000 records, 1,000 paths, 300,000 risk rows, CSV-to-Parquet + DuckDB query | 7.36 s | 13,586 instruments/s end-to-end |
| C++ native compute only | Same C++ run excluding Arrow/Parquet conversion and DuckDB query | 5.32 s | 18,784 instruments/s |

The C++ E2E timing includes 3.04s ingestion, 0.17s structure compilation, 0.009s shared path generation, 2.10s risk calculation, 0.42s Arrow/Parquet conversion, and 0.056s DuckDB OLAP query. DuckDB’s corrected portfolio aggregation returns a PV checksum of `99364.566025` and a dollar-delta sum of `-8711.342190` across the normalized risk rows.

Method provenance is explicit. This environment does not have production QuantLib C++ and XAD C++ packages discoverable by CMake, so the native run reports `PATHWISE_NATIVE_FALLBACK` and `production_native_adapters_available=false`; it does not mislabel the fallback as AAD. When both adapters are installed, the same CMake target selects `AAD_FIXED_BRANCH` for smooth fixed-branch factors and retains `PATHWISE_TRANSITION_FALLBACK` / `CRN_BUMP_REVALUE` for discontinuities.

The C++ E2E writes `risk_wide.parquet` and `risk_long.parquet` through a local Arrow/Parquet adapter runner and queries them with DuckDB. The production S3/AWS SDK writer remains the next deployment adapter; local and Vercel Python MCP contracts remain unchanged.
