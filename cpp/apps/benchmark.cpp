#include "fina_risk_cpp.hpp"

#include <fstream>
#include <iostream>
#include <string>

#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace {

json load_fixture(const std::string& path) {
    std::ifstream stream(path);
    if (!stream) throw std::runtime_error("unable to read fixture: " + path);
    json root;
    stream >> root;
    if (root.contains("Chunk")) return root.at("Chunk").at("Jobs").at(0).at("commonData");
    if (root.contains("commonData")) return root.at("commonData");
    return root;
}

void run_fixture(const std::string& path, std::size_t paths, std::uint64_t seed) {
    const json base = load_fixture(path);
    const auto base_result = fina::risk::price_fixture(base.dump(), paths, seed);
    json output = {
        {"paths", paths}, {"seed", seed}, {"pv", base_result.pv},
        {"put_option_price", base_result.put_option_price}, {"dollar_delta", json::array()},
        {"backend", "cpp_fixture_reference"}
    };
    const auto& equities = base.at("marketData").at("equity");
    for (std::size_t i = 0; i < equities.size(); ++i) {
        const double spot = equities[i].at("spot").get<double>();
        const double bump = spot * 0.01;
        json up = base;
        json down = base;
        up["marketData"]["equity"][i]["spot"] = spot + bump;
        down["marketData"]["equity"][i]["spot"] = spot - bump;
        const auto up_result = fina::risk::price_fixture(up.dump(), paths, seed);
        const auto down_result = fina::risk::price_fixture(down.dump(), paths, seed);
        const double delta_per_dollar = (up_result.pv - down_result.pv) / (2.0 * bump);
        output["dollar_delta"].push_back({
            {"underlying", equities[i].at("_id")},
            {"quoted_spot", spot},
            {"delta_per_dollar_spot", delta_per_dollar},
            {"one_percent_pnl", delta_per_dollar * bump},
            {"pv_up", up_result.pv}, {"pv_down", down_result.pv}
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
