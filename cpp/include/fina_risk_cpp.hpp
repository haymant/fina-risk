#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace fina::risk {

struct LegResult {
    std::string name;
    int multiplier{};
    double pv{};
};

struct RiskResult {
    double pv{};
    double put_option_price{};
    std::vector<LegResult> legs;
};

struct ParityResult {
    std::size_t instruments{};
    std::size_t underlyings{};
    std::size_t paths{};
    double pv_checksum{};
    double delta_checksum{};
    double delta_dollar_checksum{};
    double gamma_checksum{};
    double taylor_forecast_checksum{};
    double taylor_actual_checksum{};
    double taylor_unexplained_checksum{};
    double elapsed_seconds{};
    double instruments_per_second{};
    std::string engine;
};

struct BenchmarkResult {
    std::size_t instruments{};
    std::size_t underlyings{};
    std::size_t paths{};
    std::size_t steps{};
    std::size_t factors{};
    double path_build_seconds{};
    double pricing_seconds{};
    double total_seconds{};
    double instruments_per_second{};
    std::uint64_t peak_path_cube_bytes{};
    double price_checksum{};
    double price_mean{};
};

// Native non-MCP kernel shared by fixture pricing, risk and benchmark adapters.
RiskResult price_terminal_legs(const std::vector<double>& terminal_spots,
                               const std::vector<double>& reference_spots,
                               double strike, double discount_factor,
                               double coupon_pv);

RiskResult price_fixture(const std::string& request_json,
                         std::size_t paths = 30000,
                         std::uint64_t seed = 1729);

BenchmarkResult run_benchmark(const std::string& instruments_json,
                              const std::string& market_json,
                              std::size_t paths = 30000,
                              std::uint64_t seed = 20260909);

// C++ parity lane: identical math to parity_benchmark.cpp operating on a
// caller-supplied shared float32 terminal cube. Accepts the augmented
// instrument/market JSON schemas (`benchmark.instruments.v1.mcp` /
// `benchmark.market.v1`) so the MCP backend chooses the executing parity.
ParityResult run_cpp_parity(const std::string& instruments_json,
                            const std::string& market_json,
                            const std::vector<float>& terminal,
                            std::uint64_t seed = 20260909,
                            double bump = 0.01);

std::string to_json(const BenchmarkResult& result);
std::string to_json(const ParityResult& result);
std::string to_json(const RiskResult& result);

}  // namespace fina::risk

#ifdef FINA_RISK_HAS_QUANTLIB_XAD
// The production build binds QuantLib pricing primitives and XAD tapes here.
// The fallback build keeps the same ABI and deterministic fixture semantics.
#endif

#ifdef FINA_RISK_HAS_DUCKDB
// The production build exposes Arrow/Parquet/S3 persistence through this boundary.
#endif

#ifdef FINA_RISK_HAS_PYBIND11
// pybind11 bindings are generated in bindings/ and intentionally do not own MCP.
#endif
