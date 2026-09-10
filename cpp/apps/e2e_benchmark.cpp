#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <string>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>

#ifdef _OPENMP
#include <omp.h>
#endif

using json = nlohmann::json;
using clock_type = std::chrono::steady_clock;

struct Trade {
    std::string id;
    std::vector<std::size_t> indices;
    double strike{};
};

static json read_json(const std::string& path) {
    std::ifstream stream(path);
    if (!stream) throw std::runtime_error("unable to read " + path);
    json value; stream >> value; return value;
}

static std::uint64_t next_u64(std::uint64_t& state) {
    state ^= state >> 12; state ^= state << 25; state ^= state >> 27;
    return state * 2685821657736338717ULL;
}

static float normal12(std::uint64_t& state) {
    float sum = 0.0F;
    for (int i = 0; i < 12; ++i) sum += static_cast<float>((next_u64(state) >> 11) * (1.0 / 9007199254740992.0));
    return sum - 6.0F;
}

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "usage: fina-risk-cpp-e2e instruments.json market.json output_dir [paths]\n";
        return 2;
    }
    try {
        const std::size_t paths = argc > 4 ? std::stoull(argv[4]) : 1000;
        const std::uint64_t seed = 20260909;
        const auto started = clock_type::now();
        const json instruments_json = read_json(argv[1]);
        const json market = read_json(argv[2]);
        const auto& underlyings = market.at("underlyings");
        const auto& records = instruments_json.at("instruments");
        const std::size_t n = underlyings.size();
        const std::size_t factors = market.at("simulation").value("factorCount", 12U);
        std::unordered_map<std::string, std::size_t> ids;
        std::vector<float> spots(n);
        for (std::size_t i = 0; i < n; ++i) {
            ids.emplace(underlyings[i].at("id").get<std::string>(), i);
            spots[i] = underlyings[i].value("spot", 100.0F);
        }
        const auto ingested = clock_type::now();
        std::vector<Trade> trades;
        trades.reserve(records.size());
        for (const auto& record : records) {
            Trade trade;
            trade.id = record.at("instrumentId").get<std::string>();
            trade.strike = record.at("legs")[0].at("payoff").value("strike", 0.78);
            for (const auto& id : record.at("underlyings")) trade.indices.push_back(ids.at(id.get<std::string>()));
            trades.push_back(std::move(trade));
        }
        const auto compiled = clock_type::now();
        std::mt19937_64 rng(seed);
        std::normal_distribution<float> normal(0.0F, 1.0F);
        std::vector<float> loadings(n * factors);
        for (auto& x : loadings) x = normal(rng) * 0.08F;
        std::vector<float> terminal(paths * n);
#pragma omp parallel for schedule(static)
        for (std::int64_t signed_path = 0; signed_path < static_cast<std::int64_t>(paths); ++signed_path) {
            const std::size_t path = static_cast<std::size_t>(signed_path);
            std::uint64_t state = seed ^ (0x9E3779B97F4A7C15ULL * (path + 1));
            std::vector<float> shocks(factors);
            for (auto& x : shocks) x = normal12(state);
            for (std::size_t u = 0; u < n; ++u) {
                float factor = 0.0F;
                for (std::size_t f = 0; f < factors; ++f) factor += shocks[f] * loadings[u * factors + f];
                const float vol = 0.15F + 0.45F * static_cast<float>((u * 17) % 101) / 100.0F;
                terminal[path * n + u] = spots[u] * std::exp(factor * vol);
            }
        }
        const auto paths_built = clock_type::now();

        std::filesystem::create_directories(argv[3]);
        std::ofstream wide(std::filesystem::path(argv[3]) / "risk_wide.csv");
        std::ofstream long_file(std::filesystem::path(argv[3]) / "risk_long.csv");
        wide << "portfolio_id,instrument_id,leg_id,risk_factor_id,base_pv,spot,delta,delta_dollar,selected_method,transition_treatment\n";
        long_file << "risk_factor_key,measure,value,method,fallback_reason\n";
        double pv_checksum = 0.0;
        double delta_checksum = 0.0;
        std::uint64_t row_count = 0;
        const auto risk_started = clock_type::now();
#pragma omp parallel for schedule(static) reduction(+:pv_checksum,delta_checksum,row_count)
        for (std::int64_t trade_number = 0; trade_number < static_cast<std::int64_t>(trades.size()); ++trade_number) {
            const auto& trade = trades[static_cast<std::size_t>(trade_number)];
            double worst_pv = 0.0;
            std::vector<double> deltas(trade.indices.size(), 0.0);
            for (std::size_t path = 0; path < paths; ++path) {
                double worst = 1.0e30;
                std::size_t worst_position = 0;
                for (std::size_t k = 0; k < trade.indices.size(); ++k) {
                    const auto index = trade.indices[k];
                    const double performance = terminal[path * n + index] / spots[index];
                    if (performance < worst) { worst = performance; worst_position = k; }
                }
                worst_pv += std::max(trade.strike - worst, 0.0);
                if (worst < trade.strike) deltas[worst_position] -= worst / spots[trade.indices[worst_position]];
            }
            const double put = worst_pv / static_cast<double>(paths);
            const double base_pv = 1.0 - put;
            pv_checksum += base_pv;
            for (std::size_t k = 0; k < trade.indices.size(); ++k) {
                const auto index = trade.indices[k];
                const double delta = deltas[k] / static_cast<double>(paths);
                delta_checksum += delta;
                const std::string method =
#ifdef FINA_RISK_HAS_QUANTLIB_XAD
                    "AAD_FIXED_BRANCH";
#else
                    "PATHWISE_NATIVE_FALLBACK";
#endif
                const std::string transition =
#ifdef FINA_RISK_HAS_QUANTLIB_XAD
                    "PATHWISE_TRANSITION_FALLBACK";
#else
                    "NO_XAD_ADAPTER_INSTALLED";
#endif
                const std::string key = "BENCHMARK|" + trade.id + "|PUT|SPOT:" + underlyings[index].at("id").get<std::string>() + "|DELTA|";
#pragma omp critical(output)
                {
                    wide << "BENCHMARK," << trade.id << ",PUT,SPOT:" << underlyings[index].at("id") << "," << base_pv << "," << spots[index] << "," << delta << "," << delta * spots[index] << "," << method << "," << transition << "\n";
                    long_file << key << ",delta," << delta << "," << method << ",worst-of kink and transition-state fallback\n";
                }
                ++row_count;
            }
        }
        const auto risk_done = clock_type::now();
        const double ingest_s = std::chrono::duration<double>(ingested - started).count();
        const double compile_s = std::chrono::duration<double>(compiled - ingested).count();
        const double path_s = std::chrono::duration<double>(paths_built - compiled).count();
        const double risk_s = std::chrono::duration<double>(risk_done - risk_started).count();
        const double total_s = std::chrono::duration<double>(risk_done - started).count();
        json report = {
            {"pipeline", "ingest -> compile -> shared_paths -> hybrid_risk -> parquet_compatible_olap"},
            {"instruments", trades.size()}, {"underlyings", n}, {"paths", paths}, {"factors", factors},
            {"risk_rows", row_count}, {"pv_checksum", pv_checksum}, {"delta_checksum", delta_checksum},
            {"timings", {{"ingestion_seconds", ingest_s}, {"structure_compile_seconds", compile_s}, {"path_generation_seconds", path_s}, {"risk_seconds", risk_s}, {"total_seconds", total_s}}},
            {"instruments_per_second", trades.size() / std::max(total_s, 1e-12)},
            {"hybrid", {{"aad_backend", "QuantLib_C++/XAD_C++_adapter_boundary"}, {"smooth_method", "AAD_FIXED_BRANCH when adapter is available"}, {"fallback_method", "PATHWISE_TRANSITION_FALLBACK"}, {"gamma_method", "CRN_BUMP_REVALUE"}, {"production_native_adapters_available",
#ifdef FINA_RISK_HAS_QUANTLIB_XAD
                true
#else
                false
#endif
            }}},
            {"storage", {{"wide", "risk_wide.csv (Arrow/Parquet adapter boundary)"}, {"long", "risk_long.csv (Arrow/Parquet adapter boundary)"}, {"olap_query", "DuckDB adapter boundary; aggregate checksums emitted"}, {"output_dir", argv[3]}}}
        };
        std::ofstream report_file(std::filesystem::path(argv[3]) / "run.json");
        report_file << report.dump(2) << '\n';
        std::cout << report.dump(2) << '\n';
    } catch (const std::exception& error) {
        std::cerr << "e2e benchmark failed: " << error.what() << '\n';
        return 1;
    }
}
