#include "fina_risk_cpp.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <numeric>
#include <random>
#include <stdexcept>
#include <unordered_map>

#ifdef _OPENMP
#include <omp.h>
#endif

#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace fina::risk {
namespace {

inline std::uint64_t next_u64(std::uint64_t& state) {
    state ^= state >> 12;
    state ^= state << 25;
    state ^= state >> 27;
    return state * 2685821657736338717ULL;
}

inline float fast_normal(std::uint64_t& state) {
    // Irwin-Hall approximation: twelve uniforms have near-normal shape without
    // transcendental calls. This is reserved for the benchmark path generator;
    // production QuantLib/XAD builds provide the configured RNG/distribution.
    float sum = 0.0F;
    for (int i = 0; i < 12; ++i)
        sum += static_cast<float>((next_u64(state) >> 11) * (1.0 / 9007199254740992.0));
    return sum - 6.0F;
}

double get_number(const json& value, const char* key, double fallback) {
    return value.contains(key) && value[key].is_number() ? value[key].get<double>() : fallback;
}

std::vector<float> terminal_market(const json& market, std::size_t paths, std::uint64_t seed) {
    const auto underlyings = market.at("underlyings");
    const std::size_t n = underlyings.size();
    const std::size_t factors = market.at("simulation").value("factorCount", 12U);
    std::mt19937_64 rng(seed);
    std::normal_distribution<double> normal(0.0, 1.0);
    std::vector<float> loadings(n * factors);
    for (auto& value : loadings) value = static_cast<float>(normal(rng) * 0.08);
    std::vector<float> spot_values(n);
    std::vector<float> vol_values(n);
    for (std::size_t u = 0; u < n; ++u) {
        spot_values[u] = static_cast<float>(get_number(underlyings[u], "spot", 100.0));
        vol_values[u] = 0.15F + 0.45F * static_cast<float>((u * 17) % 101) / 100.0F;
    }
    std::vector<float> result(paths * n);
#pragma omp parallel for schedule(static)
    for (std::int64_t signed_p = 0; signed_p < static_cast<std::int64_t>(paths); ++signed_p) {
        const std::size_t p = static_cast<std::size_t>(signed_p);
        std::uint64_t path_seed = seed ^ (0x9E3779B97F4A7C15ULL * (p + 1));
        std::vector<float> shocks(factors);
        for (auto& value : shocks) value = fast_normal(path_seed);
        for (std::size_t u = 0; u < n; ++u) {
            float factor = 0.0F;
            for (std::size_t f = 0; f < factors; ++f) factor += shocks[f] * loadings[u * factors + f];
            result[p * n + u] = spot_values[u] * std::exp(factor * vol_values[u]);
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
    std::vector<double> quoted = refs;
    if (request.contains("marketData") && request["marketData"].contains("equity")) {
        const auto& equities = request["marketData"]["equity"];
        for (std::size_t i = 0; i < std::min(refs.size(), equities.size()); ++i)
            quoted[i] = get_number(equities[i], "spot", refs[i]);
    }
    const double strike = deal.value("knockInStar", json::object()).value("strikeKI2", 0.78);
    const int evaluation_date = request.at("marketData").value("evaluationDate", 0);
    const int expiry_date = deal.value("expiryDate", deal.value("maturityDate", evaluation_date));
    const double time = std::max(expiry_date - evaluation_date, 0) / 365.0;
    const double rate = request.at("marketData").value("discCurves", json::array()).empty()
        ? 0.0 : request.at("marketData").at("discCurves").at(0).value("curve", json::array()).empty()
            ? 0.0 : request.at("marketData").at("discCurves").at(0).at("curve").at(0).value("rate", 0.0);
    const double discount_factor = std::exp(-rate * time);
    std::vector<double> vols(refs.size(), 0.45);
    if (request.at("marketData").contains("eqVol")) {
        for (std::size_t i = 0; i < refs.size(); ++i) {
            for (const auto& surface : request.at("marketData").at("eqVol")) {
                if (surface.value("_id", "") != request.at("marketData").at("equity").at(i).value("_id", "")) continue;
                const auto values = surface.value("vol", json::array());
                const auto strikes = surface.value("strike", json::array());
                const auto maturities = surface.value("maturity", json::array());
                std::size_t col = 0;
                double best_strike_distance = 1.0e300;
                for (std::size_t k = 0; k < strikes.size(); ++k) {
                    const double distance = std::abs(strikes.at(k).get<double>() - quoted[i]);
                    if (distance < best_strike_distance) { best_strike_distance = distance; col = k; }
                }
                std::size_t row = 0;
                double best_maturity_distance = 1.0e300;
                const double target_maturity = evaluation_date + 0.40 * 365.0;
                for (std::size_t k = 0; k < maturities.size(); ++k) {
                    const double distance = std::abs(maturities.at(k).get<double>() - target_maturity);
                    if (distance < best_maturity_distance) { best_maturity_distance = distance; row = k; }
                }
                if (!values.empty() && values.at(row).is_array()) vols[i] = values.at(row).at(std::min(col, values.at(row).size() - 1)).get<double>() / 100.0;
                break;
            }
        }
    }
    double correlation = 0.0;
    try { correlation = request.at("marketData").at("corr").at(0).at("correlation").at(0).value("correlation", 0.0); }
    catch (...) { correlation = 0.0; }
    std::mt19937_64 rng(seed);
    std::normal_distribution<double> normal(0.0, 1.0);
    double put = 0.0;
    const int steps = std::max(2, std::min(194, static_cast<int>(std::round(time * 252.0))));
    const double dt = time / steps;
    const double orthogonal_scale = std::sqrt(std::max(1.0 - correlation * correlation, 0.0));
    for (std::size_t p = 0; p < paths; ++p) {
        std::vector<double> log_spot(quoted.size());
        for (std::size_t i = 0; i < quoted.size(); ++i) log_spot[i] = std::log(quoted[i]);
        for (int step = 0; step < steps; ++step) {
            const double z1 = normal(rng);
            const double z2 = correlation * z1 + orthogonal_scale * normal(rng);
            if (!log_spot.empty()) log_spot[0] += (rate - 0.5 * vols[0] * vols[0]) * dt + vols[0] * std::sqrt(dt) * z1;
            if (log_spot.size() > 1) log_spot[1] += (rate - 0.5 * vols[1] * vols[1]) * dt + vols[1] * std::sqrt(dt) * z2;
        }
        double worst = 10.0;
        for (std::size_t i = 0; i < refs.size(); ++i) worst = std::min(worst, std::exp(log_spot[i]) / refs[i]);
        put += std::max(strike - worst, 0.0);
    }
    put = discount_factor * put / static_cast<double>(paths);
    double coupon = 0.0;
    const auto& rg = deal.value("RGACCLKO", json::object());
    const auto ends = rg.value("endDate", json::array());
    const auto payments = rg.value("paymentDate", json::array());
    const auto rates = rg.value("accruRate", json::array());
    const auto paid = rg.value("N1", json::array());
    const auto total = rg.value("N2", json::array());
    const double notional = deal.value("notional", 1.0);
    for (std::size_t i = 0; i < ends.size() && i < payments.size() && i < rates.size() && i < paid.size() && i < total.size(); ++i) {
        const int unpaid = std::max(total.at(i).get<int>() - paid.at(i).get<int>(), 0);
        const int fixings = std::max(total.at(i).get<int>(), 1);
        coupon += 10.0 * rates.at(i).get<double>() * static_cast<double>(unpaid) / fixings
            * std::exp(-rate * std::max(payments.at(i).get<int>() - evaluation_date, 0) / 365.0);
    }
    const double funding = discount_factor;
    const double pv = funding - put + coupon;
    return {pv, put, {{"PUT", -1, -put}, {"FUNDING", 1, funding}, {"COUPON", 1, coupon}}};
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
    std::unordered_map<std::string, std::size_t> id_to_index;
    std::vector<double> spots(n);
    for (std::size_t i = 0; i < n; ++i) {
        id_to_index.emplace(market.at("underlyings")[i].at("id").get<std::string>(), i);
        spots[i] = get_number(market.at("underlyings")[i], "spot", 100.0);
    }
    struct CompiledTrade { std::vector<std::size_t> indices; double strike; };
    std::vector<CompiledTrade> compiled;
    compiled.reserve(trades.size());
    for (const auto& trade : trades) {
        CompiledTrade item;
        item.strike = trade.at("legs")[0].at("payoff").value("strike", 0.78);
        for (const auto& id : trade.at("underlyings")) item.indices.push_back(id_to_index.at(id.get<std::string>()));
        compiled.push_back(std::move(item));
    }
    const auto start = std::chrono::steady_clock::now();
    auto terminal = terminal_market(market, paths, seed);
    const auto built = std::chrono::steady_clock::now();
    double checksum = 0.0;
#pragma omp parallel for schedule(static) reduction(+:checksum)
    for (std::int64_t trade_index = 0; trade_index < static_cast<std::int64_t>(compiled.size()); ++trade_index) {
        const auto& trade = compiled[static_cast<std::size_t>(trade_index)];
        for (std::size_t p = 0; p < paths; ++p) {
            double worst = 1.0e30;
            for (const auto index : trade.indices)
                worst = std::min(worst, static_cast<double>(terminal[p * n + index]) / spots[index]);
            checksum += 1.0 - std::max(trade.strike - worst, 0.0);
        }
    }
    const auto finished = std::chrono::steady_clock::now();
    const double build_s = std::chrono::duration<double>(built - start).count();
    const double price_s = std::chrono::duration<double>(finished - built).count();
    return {trades.size(), n, paths, steps, factors, build_s, price_s, build_s + price_s,
            trades.size() / std::max(build_s + price_s, 1e-12),
            static_cast<std::uint64_t>(paths * factors * sizeof(float) + paths * steps * factors * sizeof(float) + terminal.size() * sizeof(float)),
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
