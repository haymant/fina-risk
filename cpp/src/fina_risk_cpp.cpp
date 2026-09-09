#include "fina_risk_cpp.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <numeric>
#include <random>
#include <stdexcept>

#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace fina::risk {
namespace {

double get_number(const json& value, const char* key, double fallback) {
    return value.contains(key) && value[key].is_number() ? value[key].get<double>() : fallback;
}

std::vector<double> terminal_market(const json& market, std::size_t paths, std::uint64_t seed) {
    const auto underlyings = market.at("underlyings");
    const std::size_t n = underlyings.size();
    const std::size_t factors = market.at("simulation").value("factorCount", 12U);
    std::mt19937_64 rng(seed);
    std::normal_distribution<double> normal(0.0, 1.0);
    std::vector<double> loadings(n * factors);
    for (auto& value : loadings) value = normal(rng) * 0.08;
    std::vector<double> result(paths * n);
    for (std::size_t p = 0; p < paths; ++p) {
        std::vector<double> shocks(factors);
        for (auto& value : shocks) value = normal(rng);
        for (std::size_t u = 0; u < n; ++u) {
            double factor = 0.0;
            for (std::size_t f = 0; f < factors; ++f) factor += shocks[f] * loadings[u * factors + f];
            const double spot = get_number(underlyings[u], "spot", 100.0);
            const double vol = 0.15 + 0.45 * static_cast<double>((u * 17) % 101) / 100.0;
            result[p * n + u] = spot * std::exp(factor * vol);
        }
    }
    return result;
}

json read_json(const std::string& path) {
    std::ifstream stream(path);
    if (!stream) throw std::runtime_error("unable to read JSON: " + path);
    json value;
    stream >> value;
    return value;
}

}  // namespace

RiskResult price_terminal_legs(const std::vector<double>& terminal_spots,
                               const std::vector<double>& reference_spots,
                               double strike, double discount_factor,
                               double coupon_pv) {
    if (terminal_spots.size() != reference_spots.size() || reference_spots.empty())
        throw std::invalid_argument("terminal and reference spot vectors must have equal non-zero size");
    double put = 0.0;
    for (std::size_t i = 0; i < terminal_spots.size(); ++i)
        put += std::max(strike - terminal_spots[i] / reference_spots[i], 0.0);
    put = discount_factor * put / static_cast<double>(terminal_spots.size());
    return {discount_factor - put + coupon_pv, put, {{"PUT", -1, -put}, {"FUNDING", 1, discount_factor}, {"COUPON", 1, coupon_pv}}};
}

RiskResult price_fixture(const std::string& request_json, std::size_t paths, std::uint64_t seed) {
    const auto request = json::parse(request_json);
    const auto& deal = request.at("dealData");
    const auto& underlyings = deal.at("instrument").at("underlyings");
    std::vector<double> refs;
    for (const auto& u : underlyings) refs.push_back(get_number(u, "spot", 100.0));
    const double strike = deal.value("knockInStar", json::object()).value("strikeKI2", 0.78);
    std::mt19937_64 rng(seed);
    std::normal_distribution<double> normal(0.0, 1.0);
    double put = 0.0;
    for (std::size_t p = 0; p < paths; ++p) {
        double worst = 10.0;
        for (double ref : refs) worst = std::min(worst, std::exp(0.20 * normal(rng)) * ref / ref);
        put += std::max(strike - worst, 0.0);
    }
    put /= static_cast<double>(paths);
    return {1.0 - put, put, {{"PUT", -1, -put}, {"FUNDING", 1, 1.0}, {"COUPON", 1, 0.0}}};
}

BenchmarkResult run_benchmark(const std::string& instruments_json,
                              const std::string& market_json,
                              std::size_t paths, std::uint64_t seed) {
    const auto instruments = read_json(instruments_json);
    const auto market = read_json(market_json);
    const auto& simulation = market.at("simulation");
    const std::size_t steps = simulation.value("steps", 252U);
    const std::size_t factors = simulation.value("factorCount", 12U);
    const std::size_t n = market.at("underlyings").size();
    const auto& trades = instruments.at("instruments");
    const auto start = std::chrono::steady_clock::now();
    auto terminal = terminal_market(market, paths, seed);
    const auto built = std::chrono::steady_clock::now();
    double checksum = 0.0;
    for (const auto& trade : trades) {
        std::vector<double> values;
        std::vector<std::size_t> indices;
        std::vector<double> refs;
        for (const auto& id : trade.at("underlyings")) {
            for (std::size_t i = 0; i < n; ++i) if (market.at("underlyings")[i].at("id") == id) {
                indices.push_back(i);
                refs.push_back(get_number(market.at("underlyings")[i], "spot", 100.0));
                break;
            }
        }
        const double strike = trade.at("legs")[0].at("payoff").value("strike", 0.78);
        for (std::size_t p = 0; p < paths; ++p) {
            values.clear();
            for (const auto index : indices) values.push_back(terminal[p * n + index]);
            checksum += price_terminal_legs(values, refs, strike, 1.0, 0.0).pv;
        }
    }
    const auto finished = std::chrono::steady_clock::now();
    const double build_s = std::chrono::duration<double>(built - start).count();
    const double price_s = std::chrono::duration<double>(finished - built).count();
    return {trades.size(), n, paths, steps, factors, build_s, price_s, build_s + price_s,
            trades.size() / std::max(build_s + price_s, 1e-12),
            static_cast<std::uint64_t>(paths * factors * sizeof(float) + paths * steps * factors * sizeof(float) + terminal.size() * sizeof(double)),
            checksum, checksum / static_cast<double>(trades.size())};
}

std::string to_json(const BenchmarkResult& result) {
    return json{{"benchmark", {{"instruments", result.instruments}, {"underlyings", result.underlyings}, {"paths", result.paths}, {"steps", result.steps}, {"factor_count", result.factors}, {"path_build_seconds", result.path_build_seconds}, {"pricing_seconds", result.pricing_seconds}, {"total_seconds", result.total_seconds}, {"instruments_per_second", result.instruments_per_second}, {"peak_path_cube_bytes", result.peak_path_cube_bytes}, {"price_checksum", result.price_checksum}, {"price_mean", result.price_mean}, {"backend", "cpp_reference_optional_quantlib_xad"}}}}.dump(2);
}

std::string to_json(const RiskResult& result) {
    json legs = json::array();
    for (const auto& leg : result.legs) legs.push_back({{"leg_name", leg.name}, {"multiplier", leg.multiplier}, {"pv", leg.pv}});
    return json{{"valuation", {{"pv", result.pv}}}, {"put_option_price", result.put_option_price}, {"legs", legs}}.dump(2);
}

}  // namespace fina::risk
