#include <algorithm>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <numeric>
#include <string>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>
#ifdef _OPENMP
#include <omp.h>
#endif
using json = nlohmann::json;

struct Trade { std::vector<std::size_t> indices; double strike{}; };
static json read_json(const std::string& p) { std::ifstream f(p); json x; f >> x; return x; }

int main(int argc, char** argv) {
    if (argc < 5) { std::cerr << "usage: parity_benchmark instruments.json market.json paths.bin paths [bump]\n"; return 2; }
    const double bump = argc > 5 ? std::stod(argv[5]) : 0.01;
    const auto started = std::chrono::steady_clock::now();
    const auto instruments = read_json(argv[1]);
    const auto market = read_json(argv[2]);
    const auto meta = read_json(std::string(argv[3]) + ".meta.json");
    const std::size_t paths = meta.at("paths");
    const std::size_t n = meta.at("underlyings");
    std::ifstream binary(argv[3], std::ios::binary);
    std::vector<float> terminal(paths * n);
    binary.read(reinterpret_cast<char*>(terminal.data()), static_cast<std::streamsize>(terminal.size() * sizeof(float)));
    std::vector<double> spots(n);
    std::unordered_map<std::string, std::size_t> ids;
    for (std::size_t i = 0; i < n; ++i) { spots[i] = market.at("underlyings")[i].at("spot"); ids.emplace(market.at("underlyings")[i].at("id"), i); }
    std::vector<Trade> trades;
    for (const auto& record : instruments.at("instruments")) {
        Trade t; t.strike = record.at("legs")[0].at("payoff").at("strike");
        for (const auto& id : record.at("underlyings")) t.indices.push_back(ids.at(id.get<std::string>()));
        trades.push_back(std::move(t));
    }
    const auto loaded = std::chrono::steady_clock::now();
    double pv_sum = 0.0, delta_sum = 0.0, delta_dollar_sum = 0.0, gamma_sum = 0.0, forecast_sum = 0.0, actual_sum = 0.0;
#pragma omp parallel for schedule(static) reduction(+:pv_sum,delta_sum,delta_dollar_sum,gamma_sum,forecast_sum,actual_sum)
    for (std::int64_t ti = 0; ti < static_cast<std::int64_t>(trades.size()); ++ti) {
        const auto& trade = trades[static_cast<std::size_t>(ti)];
        std::vector<double> base(paths), deltas(trade.indices.size()), gammas(trade.indices.size());
        for (std::size_t p = 0; p < paths; ++p) {
            double worst = 1e30;
            for (auto index : trade.indices) worst = std::min(worst, static_cast<double>(terminal[p*n+index]) / spots[index]);
            base[p] = std::max(trade.strike - worst, 0.0);
        }
        const double base_mean = std::accumulate(base.begin(), base.end(), 0.0) / static_cast<double>(paths);
        for (std::size_t k = 0; k < trade.indices.size(); ++k) {
            double up_sum = 0.0, down_sum = 0.0;
            for (std::size_t p = 0; p < paths; ++p) {
                double up_worst = 1e30, down_worst = 1e30;
                for (std::size_t j = 0; j < trade.indices.size(); ++j) {
                    const auto index = trade.indices[j];
                    const double ratio = static_cast<double>(terminal[p*n+index]) / spots[index];
                    up_worst = std::min(up_worst, ratio * (j == k ? 1.0 + bump : 1.0));
                    down_worst = std::min(down_worst, ratio * (j == k ? 1.0 - bump : 1.0));
                }
                up_sum += std::max(trade.strike - up_worst, 0.0);
                down_sum += std::max(trade.strike - down_worst, 0.0);
            }
            const double up = up_sum / paths, down = down_sum / paths;
            const double spot = spots[trade.indices[k]];
            deltas[k] = (up - down) / (2.0 * bump * spot);
            gammas[k] = (up - 2.0 * base_mean + down) / (bump * spot) * (1.0 / (bump * spot));
        }
        const double base_pv = 1.0 - base_mean;
        double forecast = 0.0;
        for (std::size_t k = 0; k < trade.indices.size(); ++k) {
            const double spot = spots[trade.indices[k]];
            forecast += deltas[k] * spot * bump + 0.5 * gammas[k] * (spot*bump) * (spot*bump);
            delta_dollar_sum += deltas[k] * spot;
        }
        double shocked_sum = 0.0;
        for (std::size_t p = 0; p < paths; ++p) shocked_sum += std::max(trade.strike - (trade.indices.empty() ? 0.0 : std::min({static_cast<double>(terminal[p*n+trade.indices[0]])/spots[trade.indices[0]], static_cast<double>(terminal[p*n+trade.indices[1]])/spots[trade.indices[1]], static_cast<double>(terminal[p*n+trade.indices[2]])/spots[trade.indices[2]]}) * (1.0 + bump)), 0.0);
        const double actual = shocked_sum / paths - base_mean;
        pv_sum += base_pv; delta_sum += std::accumulate(deltas.begin(), deltas.end(), 0.0); gamma_sum += std::accumulate(gammas.begin(), gammas.end(), 0.0); forecast_sum += forecast; actual_sum += actual;
    }
    const auto finished = std::chrono::steady_clock::now();
    const double elapsed = std::chrono::duration<double>(finished-started).count();
    std::cout << json{{"instruments",trades.size()},{"underlyings",n},{"paths",paths},{"pv_checksum",pv_sum},{"delta_checksum",delta_sum},{"delta_dollar_checksum",delta_dollar_sum},{"gamma_checksum",gamma_sum},{"taylor_forecast_checksum",forecast_sum},{"taylor_actual_checksum",actual_sum},{"taylor_unexplained_checksum",actual_sum-forecast_sum},{"elapsed_seconds",elapsed},{"instruments_per_second",trades.size()/elapsed},{"shared_path_cube",argv[3]},{"method","CRN_BUMP_REVALUE_WITH_PATHWISE_DELTA"}}.dump(2) << '\n';
}
