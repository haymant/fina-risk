#include "fina_risk_cpp.hpp"

#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace {

std::vector<json> load_jobs(const std::string& path) {
    std::ifstream stream(path);
    if (!stream) throw std::runtime_error("unable to read fixture: " + path);
    json root;
    stream >> root;
    if (root.contains("Chunk")) {
        std::vector<json> jobs;
        for (const auto& job : root.at("Chunk").at("Jobs")) jobs.push_back(job.at("commonData"));
        return jobs;
    }
    if (root.contains("commonData")) return {root.at("commonData")};
    return {root};
}

json bumped(const json& request, std::size_t index, double shift) {
    json result = request;
    result["marketData"]["equity"][index]["spot"] =
        result["marketData"]["equity"][index].at("spot").get<double>() + shift;
    return result;
}

void run_fixture(const std::string& path, std::size_t paths, std::uint64_t seed) {
    const auto jobs = load_jobs(path);
    std::vector<fina::risk::RiskResult> base_results;
    base_results.reserve(jobs.size());
    for (const auto& job : jobs) base_results.push_back(fina::risk::price_fixture(job.dump(), paths, seed));

    double canonical_pv = base_results.at(0).pv;
    if (base_results.size() >= 3) {
        // Python's canonical server uses PUT/FUNDING from the first job and
        // replaces its coupon with the separately priced coupon job.
        canonical_pv = base_results[0].pv - base_results[0].legs[2].pv + base_results[2].legs[2].pv;
    }
    json output = {
        {"paths", paths}, {"seed", seed}, {"job_count", jobs.size()},
        {"pv", canonical_pv}, {"job_pvs", json::array()},
        {"dollar_delta", json::array()}, {"backend", "cpp_fixture_reference"}
    };
    for (const auto& result : base_results) output["job_pvs"].push_back(result.pv);

    const auto& market = jobs.at(0).at("marketData");
    const auto& equities = market.at("equity");
    const double notional = jobs.at(0).at("dealData").value("notional", 1.0);
    for (std::size_t i = 0; i < equities.size(); ++i) {
        const double spot = equities[i].at("spot").get<double>();
        const double bump = spot * 0.01;
        double pv_up = 0.0;
        double pv_down = 0.0;
        std::vector<fina::risk::RiskResult> up_results;
        std::vector<fina::risk::RiskResult> down_results;
        for (const auto& job : jobs) {
            up_results.push_back(fina::risk::price_fixture(bumped(job, i, bump).dump(), paths, seed));
            down_results.push_back(fina::risk::price_fixture(bumped(job, i, -bump).dump(), paths, seed));
        }
        pv_up = up_results[0].pv;
        pv_down = down_results[0].pv;
        if (jobs.size() >= 3) {
            pv_up = up_results[0].pv - up_results[0].legs[2].pv + up_results[2].legs[2].pv;
            pv_down = down_results[0].pv - down_results[0].legs[2].pv + down_results[2].legs[2].pv;
        }
        const double normalized_delta = (pv_up - pv_down) / (2.0 * bump);
        const double dollar_delta = normalized_delta * spot * notional;
        output["dollar_delta"].push_back({
            {"underlying", equities[i].at("_id")}, {"quoted_spot", spot},
            {"normalized_delta_per_dollar_spot", normalized_delta},
            {"notional", notional}, {"dollar_delta_hedge_amount", dollar_delta},
            {"one_percent_pnl", normalized_delta * bump * notional},
            {"pv_up", pv_up}, {"pv_down", pv_down}
        });
    }
    std::cout << output.dump(2) << '\n';
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "usage: fina-risk-cpp-benchmark benchmark instruments.json market.json [paths]\n"
                  << "   or: fina-risk-cpp-benchmark fixture termsheet1.md.json [paths] [seed]\n";
        return 2;
    }
    try {
        if (std::string(argv[1]) == "fixture") {
            const std::size_t paths = argc > 3 ? std::stoull(argv[3]) : 30000;
            const std::uint64_t seed = argc > 4 ? std::stoull(argv[4]) : 1729;
            run_fixture(argv[2], paths, seed);
            return 0;
        }
        const std::size_t paths = argc > 3 ? std::stoull(argv[3]) : 30000;
        std::cout << fina::risk::to_json(fina::risk::run_benchmark(argv[1], argv[2], paths)) << '\n';
    } catch (const std::exception& error) {
        std::cerr << "benchmark failed: " << error.what() << '\n';
        return 1;
    }
}
