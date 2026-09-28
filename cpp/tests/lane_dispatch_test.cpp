// Block test: the term-sheet lane dispatcher.
//
// fina_risk prices through five lanes, and before include/fina_risk/lane_dispatch.hpp
// every caller picked the lane by hand: server.py unwraps Chunk.Jobs into a
// single common and calls price_fixture, cpp_parity.py hands the terminal cube
// to run_cpp_parity, daily_termsheet.py and fcn_native.py hand the daily cube
// to one of the daily lanes. A lane picked by hand and a lane picked from the
// term sheet drift apart silently, so the dispatcher inverts the flow: the root
// keys choose the lane, and the inputs that were actually supplied validate the
// choice.
//
// Every expectation below is derived by hand from the lane inventory, not from
// the kernel:
//
//   canonical RFQ root keys (instrument_key/market_data/legs/parameters)
//     -> price_fcn_rakiplus, needs the daily (P, O, U) cube and dates
//   Chunk.Jobs[]                                  -> run_daily_termsheet, same inputs
//   instruments array + market rate/evaluation_date -> run_daily_termsheet_batch
//   instruments array + market underlyings only     -> run_cpp_parity, terminal cube
//   dealData + marketData                           -> price_fixture, no cube/dates
//   anything else                                   -> refused
//
// The block test asserts each choice in C++ AND prints machine-readable CASE /
// REFUSED lines on stdout; tests/test_lane_dispatch.py parses those lines and
// diffs them against an independently hand-derived table, so a rule that drifts
// breaks the diff even if the C++ assertions were edited to match the bug.
//
// This test links nlohmann_json only (the classifier needs the request/market
// shapes), never fina_risk_core, so it never sees -ffast-math.

#include "fina_risk/lane_dispatch.hpp"

#include <nlohmann/json.hpp>

#include <cstdio>
#include <string>
#include <vector>

namespace {

using json = nlohmann::json;
using fina::risk::dispatch::DispatchInputs;
using fina::risk::dispatch::Lane;
using fina::risk::dispatch::LaneChoice;
using fina::risk::dispatch::lane_name;
using fina::risk::dispatch::pick_lane;

int failures = 0;

void check(bool condition, const char* what) {
    if (!condition) {
        std::fprintf(stderr, "  FAIL %s\n", what);
        ++failures;
    }
}

// One hand-derived row of the dispatcher table.
struct Case {
    const char* name;
    json request;
    json market;
    DispatchInputs inputs;
    Lane expected_lane;
    std::vector<std::string> expected_refusals;
};

void run_case(const Case& c) {
    const LaneChoice choice = pick_lane(c.request, c.market, c.inputs);
    const bool lane_matches = choice.lane == c.expected_lane;
    const bool refusals_match = choice.refusals == c.expected_refusals;
    check(lane_matches, c.name);
    if (!lane_matches) {
        std::fprintf(stderr, "    expected lane=%s actual lane=%s\n", lane_name(c.expected_lane),
                     lane_name(choice.lane));
    }
    check(refusals_match, c.name);
    for (std::size_t i = 0; i < choice.refusals.size() && i < c.expected_refusals.size(); ++i) {
        if (choice.refusals[i] != c.expected_refusals[i]) {
            std::fprintf(stderr, "    refusal %zu: expected \"%s\" actual \"%s\"\n", i,
                         c.expected_refusals[i].c_str(), choice.refusals[i].c_str());
        }
    }

    // Machine-readable line for tests/test_lane_dispatch.py.
    std::printf("CASE\t%s\t%s\t%zu\n", c.name, lane_name(choice.lane), choice.refusals.size());
    for (const auto& reason : choice.refusals)
        std::printf("REFUSED\t%s\t%s\n", c.name, reason.c_str());
}

// Built with json::parse instead of braced initializers so the nested object
// shapes stay unambiguous.
const json CANONICAL = json::parse(R"json({
  "instrument_key": "ELI-TEST-0001",
  "market_data": {"evaluation_date": 46272, "underlyings": []},
  "legs": [],
  "parameters": {}
})json");

const json CHUNK = json::parse(R"json({"Chunk": {"Jobs": []}})json");

const json PARITY_REQ = json::parse(R"json({
  "instruments": [
    {"instrumentId": "ELI-0001",
     "underlyings": ["EQ0001 US", "EQ0002 US"],
     "legs": [{"leg_id": 1, "payoff": {"strike": 0.78}}]}
  ]
})json");

const json PARITY_MARKET = json::parse(R"json({
  "underlyings": [
    {"id": "EQ0001 US", "spot": 100.0},
    {"id": "EQ0002 US", "spot": 100.0}
  ]
})json");

const json BATCH_REQ = json::parse(R"json({
  "instruments": [
    {"instrumentId": "ELI-0001",
     "refs": [100.0, 100.0],
     "strike": 0.78,
     "periods": []}
  ]
})json");

const json BATCH_MARKET = json::parse(R"json({
  "rate": 0.037405,
  "evaluation_date": 46272,
  "underlyings": [
    {"id": "EQ0001 US", "spot": 100.0},
    {"id": "EQ0002 US", "spot": 100.0}
  ]
})json");

const json FIXTURE = json::parse(R"json({
  "dealData": {},
  "marketData": {}
})json");

const DispatchInputs NONE;
const DispatchInputs TERMINAL_CUBE{/*has_terminal_cube=*/true};
const DispatchInputs DAILY_CUBE{/*has_terminal_cube=*/false, /*has_daily_cube=*/true};
const DispatchInputs DAILY_CUBE_DATES{/*has_terminal_cube=*/false, /*has_daily_cube=*/true,
                                      /*has_dates=*/true};

}  // namespace

int main() {
    using fina::risk::dispatch::kBatchCubeRefusal;
    using fina::risk::dispatch::kBatchDatesRefusal;
    using fina::risk::dispatch::kCanonicalCubeRefusal;
    using fina::risk::dispatch::kCanonicalDatesRefusal;
    using fina::risk::dispatch::kCanonicalTerminalCubeRefusal;
    using fina::risk::dispatch::kDailyCubeRefusal;
    using fina::risk::dispatch::kDailyDatesRefusal;
    using fina::risk::dispatch::kFixtureCubeRefusal;
    using fina::risk::dispatch::kInstrumentsNoMarketRefusal;
    using fina::risk::dispatch::kParityCubeRefusal;
    using fina::risk::dispatch::kParityDailyRefusal;
    using fina::risk::dispatch::kUnknownShapeRefusal;

    const std::vector<Case> cases{
        // --- valid picks (zero refusals) -----------------------------------
        {"canonical_rfq", CANONICAL, json::object(), DAILY_CUBE_DATES, Lane::price_fcn_rakiplus, {}},
        {"chunk_jobs", CHUNK, json::object(), DAILY_CUBE_DATES, Lane::run_daily_termsheet, {}},
        {"parity_corpus", PARITY_REQ, PARITY_MARKET, TERMINAL_CUBE, Lane::run_cpp_parity, {}},
        {"batch_corpus", BATCH_REQ, BATCH_MARKET, DAILY_CUBE_DATES, Lane::run_daily_termsheet_batch, {}},
        {"fixture_common", FIXTURE, json::object(), NONE, Lane::price_fixture, {}},

        // --- wrong/missing inputs (refused, would-be lane still reported) ---
        {"canonical_no_cube", CANONICAL, json::object(), NONE, Lane::price_fcn_rakiplus,
         {kCanonicalCubeRefusal, kCanonicalDatesRefusal}},
        {"canonical_terminal_cube", CANONICAL, json::object(), TERMINAL_CUBE, Lane::price_fcn_rakiplus,
         {kCanonicalTerminalCubeRefusal, kCanonicalCubeRefusal, kCanonicalDatesRefusal}},
        {"chunk_no_cube", CHUNK, json::object(), NONE, Lane::run_daily_termsheet,
         {kDailyCubeRefusal, kDailyDatesRefusal}},
        {"chunk_no_dates", CHUNK, json::object(), DAILY_CUBE, Lane::run_daily_termsheet,
         {kDailyDatesRefusal}},
        {"parity_no_cube", PARITY_REQ, PARITY_MARKET, NONE, Lane::run_cpp_parity,
         {kParityCubeRefusal}},
        {"parity_daily_cube", PARITY_REQ, PARITY_MARKET, DAILY_CUBE, Lane::run_cpp_parity,
         {kParityCubeRefusal, kParityDailyRefusal}},
        {"batch_no_cube", BATCH_REQ, BATCH_MARKET, NONE, Lane::run_daily_termsheet_batch,
         {kBatchCubeRefusal, kBatchDatesRefusal}},
        {"batch_no_dates", BATCH_REQ, BATCH_MARKET, DAILY_CUBE, Lane::run_daily_termsheet_batch,
         {kBatchDatesRefusal}},
        {"fixture_with_cube", FIXTURE, json::object(), DAILY_CUBE, Lane::price_fixture,
         {kFixtureCubeRefusal}},
        {"unknown_root", json::object(), json::object(), NONE, Lane::none, {kUnknownShapeRefusal}},
        {"instruments_no_market", PARITY_REQ, json::object(), TERMINAL_CUBE, Lane::none,
         {kInstrumentsNoMarketRefusal}},
    };

    for (const auto& c : cases) run_case(c);

    if (failures == 0) {
        std::printf("lane_dispatch: all %zu cases pass\n", cases.size());
        return 0;
    }
    std::fprintf(stderr, "lane_dispatch: %d of %zu cases FAIL\n", failures, cases.size());
    return 1;
}