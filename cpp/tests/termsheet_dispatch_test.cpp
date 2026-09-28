// Lane test: run_termsheet picks the lane and either delegates or refuses.
//
// block/lane_dispatch pins the classifier's decisions; this test pins the
// execution half -- the actual run_termsheet entry point in fina_risk_cpp.cpp
// that reads the request, lets the classifier choose, and either runs the
// kernel and merges the `dispatch` key, or refuses with a dispatch envelope.
// It needs the library, because the point is the delegation: the fixture case
// below must go through pick_lane AND price_fixture, and a refusal must bite
// before any kernel runs.
//
// Cases, derived by hand from the lane inventory:
//
//   fixture request (dealData/marketData, localVol: false)
//       -> lane price_fixture; result is a normal lane payload PLUS a
//          `dispatch` key naming the lane; the put is finite.
//   a Chunk.Jobs term sheet with no cube
//       -> refused; the envelope reports the would-be lane (run_daily_termsheet)
//          and the two missing inputs (daily cube, dates).
//   instruments + underlyings-only market + a terminal float32 cube
//       -> lane run_cpp_parity; the payload is the parity checksum object plus
//          `dispatch`, and the sums are finite.
//
// Non-finite rejections: every numeric field we pin must pass std::isfinite,
// because a NaN would silently poison a key-based consumer. The fast-math note
// from lane/global_ko_latch applies here too: this links fina_risk_core
// (compiled with -ffast-math by design), which is safe because no assertion is
// within rounding distance of a branch.

#include "fina_risk_cpp.hpp"

#include <nlohmann/json.hpp>

#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

namespace {

using json = nlohmann::json;

int g_failures = 0;
int g_checks = 0;

void expect_true(const std::string& what, bool ok) {
    g_checks += 1;
    std::printf("  %-4s  %s\n", ok ? "ok" : "FAIL", what.c_str());
    if (!ok) g_failures += 1;
}

// The global_ko_latch fixture shape, trimmed: two underlyings, flat 0% grid,
// localVol explicitly false so the geometry is deterministic.
json fixture_request() {
    auto grid = [] {
        return json{{"_id", "G"},
                    {"strike", json::array({10.0, 1000000.0})},
                    {"maturity", json::array({46302, 46421})},
                    {"vol", json::array({json::array({0.0, 0.0}),
                                         json::array({0.0, 0.0})})}};
    };
    json equity = json::array();
    json eq_vol = json::array();
    for (int i = 0; i < 2; ++i) {
        const std::string id = "U" + std::to_string(i);
        equity.push_back(json{{"_id", id}, {"spot", 50.0}});
        eq_vol.push_back(grid());
    }
    json deal = json{{"instrument", json{{"underlyings",
                                          json::array({json{{"spot", 100.0}, {"_id", "U0"}},
                                                       json{{"spot", 100.0}, {"_id", "U1"}}})}}},
                     {"knockInStar", json{{"strikeKI2", 0.78}, {"KIBarrier", 0.70}}},
                     {"KIKOSelect", json{{"knockInType", "EKI"},
                                         {"GKOLocked", json::array({false, false})},
                                         {"GKODate", json::array({0, 0})}}},
                     {"expiryDate", 46421},
                     {"notional", 1.0}};
    json market = json{{"evaluationDate", 46272},
                       {"equity", equity},
                       {"eqVol", eq_vol},
                       {"localVol", false},
                       {"corr", json::array({json::array({
                                         json{{"correlation", 0.0}}})})}};
    return json{{"dealData", deal}, {"marketData", market}};
}

json chunk_request() {
    return json::parse(R"json({
      "Chunk": {"Jobs": [
        {"commonData": {"dealData": {}, "marketData": {}}}
      ]}
    })json");
}

}  // namespace

int main() {
    std::printf("lane/termsheet_dispatch -- run_termsheet picks, delegates, refuses\n\n");

    {   // 1. fixture delegation: the dispatcher must run price_fixture and add
        //    the dispatch key without touching the lane payload's other keys.
        const std::string out =
            fina::risk::run_termsheet(fixture_request().dump(), "", {}, {}, 0, 0, 0, {}, 64, 1729);
        const json result = json::parse(out);
        expect_true("fixture: dispatch.lane == price_fixture",
                    result.at("dispatch").at("lane") == "price_fixture");
        expect_true("fixture: dispatch.chosen_reason non-empty",
                    !result.at("dispatch").at("chosen_reason").get<std::string>().empty());
        const double pv = result.at("valuation").at("pv").get<double>();
        expect_true("fixture: valuation.pv is finite", std::isfinite(pv));
    }

    {   // 2. refusal: a Chunk.Jobs term sheet with no cube and no dates must be
        //    refused, keeping the would-be lane so the caller can fix the inputs.
        const std::string out = fina::risk::run_termsheet(chunk_request().dump());
        const json result = json::parse(out);
        expect_true("chunk: status == refused", result.at("status") == "refused");
        expect_true("chunk: dispatch.lane == run_daily_termsheet",
                    result.at("dispatch").at("lane") == "run_daily_termsheet");
        expect_true("chunk: refused for the daily cube",
                    result.at("dispatch").at("refusals").size() == 2);
    }

    {   // 3. parity delegation: instruments + underlyings-only market + a
        //    terminal float32 cube must reach run_cpp_parity, and the checksums
        //    must be finite. The cube is two paths of ones against a flat
        //    reference, so the parity sums are whatever the kernel computes --
        //    the dispatch key and finiteness are what this test pins.
        const json instruments = json::parse(R"json({
          "instruments": [{
            "instrumentId": "ELI-DSP-00001",
            "underlyings": ["S0", "S1"],
            "legs": [{"leg_id": 1, "leg_type": "intrinsic_option",
                      "leg_name": "PUT", "multiplier": -1,
                      "payoff": {"basket": "worst_of", "strike": 1.0}}]
          }]
        })json");
        const json market = json::parse(R"json({
          "underlyings": [{"id": "S0", "spot": 100.0}, {"id": "S1", "spot": 100.0}]
        })json");
        std::vector<float> terminal{1.0f, 1.0f, 1.0f, 1.0f};
        const std::string out = fina::risk::run_termsheet(
            instruments.dump(), market.dump(), terminal, {}, 0, 0, 0, {}, 0, 1729, 0.01);
        const json result = json::parse(out);
        expect_true("parity: dispatch.lane == run_cpp_parity",
                    result.at("dispatch").at("lane") == "run_cpp_parity");
        expect_true("parity: pv_checksum is finite",
                    std::isfinite(result.at("pv_checksum").get<double>()));
        expect_true("parity: delta_checksum is finite",
                    std::isfinite(result.at("delta_checksum").get<double>()));
    }

    std::printf("\n%d checks, %d failed\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}