#pragma once

// One term-sheet entry point, five pricing lanes. This header is the pure,
// block-testable decision component of a term-sheet-driven lane dispatcher.
//
// Before this header, every caller picked the lane by hand: server.py unwraps
// Chunk.Jobs and calls price_fixture, cpp_parity.py/market_calibrate hand the
// terminal cube to run_cpp_parity, daily_termsheet.py/fcn_native.py hand the
// daily cube to one of the daily lanes. Picking by hand is how a lane and its
// term sheet drift apart silently, so the dispatcher inverts the flow: the
// term sheet's root keys select the lane, and the inputs that were actually
// supplied validate the pick.
//
// Rules (from the lane inventory; block/lane_dispatch pins each row):
//
//   canonical RFQ root keys
//     instrument_key + market_data + legs + parameters   -> price_fcn_rakiplus
//     requires the daily (P, O, U) cube and dates
//   legacy term sheet root Chunk.Jobs[]                  -> run_daily_termsheet
//     requires the daily (P, O, U) cube and dates
//   an `instruments` array root:
//     market rate / evaluation_date                      -> run_daily_termsheet_batch
//     market underlyings only                            -> run_cpp_parity
//     (requires the terminal (P, U) float32 cube)
//   a single commonData root dealData + marketData       -> price_fixture
//     simulates internally, so any supplied cube or dates is a mismatch
//   anything else                                        -> refused
//
// The classifier is pure: it takes the already-parsed request and market JSON
// plus a description of which inputs exist (not their values), and returns the
// chosen lane with a reason plus every refusal. It links nothing but
// nlohmann_json, so a block test can pin it without fina_risk_core (which
// carries the -ffast-math policy).

#include <nlohmann/json.hpp>

#include <string>
#include <vector>

namespace fina::risk::dispatch {

// The five term-sheet lanes, by the binding names run_termsheet reports.
enum class Lane {
    price_fixture,
    run_cpp_parity,
    run_daily_termsheet,
    run_daily_termsheet_batch,
    price_fcn_rakiplus,
    none,
};

inline const char* lane_name(Lane lane) {
    switch (lane) {
        case Lane::price_fixture:
            return "price_fixture";
        case Lane::run_cpp_parity:
            return "run_cpp_parity";
        case Lane::run_daily_termsheet:
            return "run_daily_termsheet";
        case Lane::run_daily_termsheet_batch:
            return "run_daily_termsheet_batch";
        case Lane::price_fcn_rakiplus:
            return "price_fcn_rakiplus";
        case Lane::none:
            return "none";
    }
    return "none";
}

// What exists, not what the caller wants. cube dimensionality distinguishes the
// terminal (P, U) cube from the daily (P, O, U) cube; dates only exist for the
// daily lanes.
struct DispatchInputs {
    bool has_terminal_cube = false;
    bool has_daily_cube = false;
    bool has_dates = false;
};

struct LaneChoice {
    Lane lane = Lane::none;
    std::string chosen_reason;
    std::vector<std::string> refusals;

    bool valid() const { return refusals.empty() && lane != Lane::none; }
};

// Refusal wording is part of the contract: tests/test_lane_dispatch.py diffs
// block/lane_dispatch output against these exact strings.
inline const char* const kCanonicalTerminalCubeRefusal = "terminal cube cannot feed the canonical FCN lane";
inline const char* const kCanonicalCubeRefusal = "canonical FCN request requires the daily (P, O, U) cube";
inline const char* const kCanonicalDatesRefusal = "canonical FCN request requires observation dates";
inline const char* const kDailyTerminalCubeRefusal = "terminal cube cannot feed the daily lane";
inline const char* const kDailyCubeRefusal = "daily accrual demanded but no (P, O, U) cube supplied";
inline const char* const kDailyDatesRefusal = "daily accrual demanded but no observation dates supplied";
inline const char* const kBatchTerminalCubeRefusal = "terminal cube cannot feed the batch lane";
inline const char* const kBatchCubeRefusal = "batch lane requires the daily (P, O, U) cube";
inline const char* const kBatchDatesRefusal = "batch lane requires observation dates";
inline const char* const kParityCubeRefusal = "parity lane requires the terminal (P, U) float32 cube";
inline const char* const kParityDailyRefusal = "daily cube cannot feed the terminal parity lane";
inline const char* const kFixtureCubeRefusal = "price_fixture simulates internally; a supplied cube or dates is a mismatch";
inline const char* const kInstrumentsNoMarketRefusal =
    "instruments corpus requires market underlyings (parity) or rate/evaluation_date (batch)";
inline const char* const kUnknownShapeRefusal = "unrecognized term-sheet shape";

inline LaneChoice pick_lane(const nlohmann::json& request,
                            const nlohmann::json& market,
                            const DispatchInputs& inputs) {
    LaneChoice choice;

    const bool canonical = request.contains("instrument_key") && request.contains("market_data") &&
                           request.contains("legs") && request.contains("parameters");
    const bool chunk = request.contains("Chunk") && request.at("Chunk").contains("Jobs");
    const bool instruments = request.contains("instruments") && request.at("instruments").is_array();
    const bool common = request.contains("dealData") && request.contains("marketData");

    if (canonical) {
        choice.lane = Lane::price_fcn_rakiplus;
        choice.chosen_reason = "canonical RFQ root keys (instrument_key/market_data/legs/parameters)";
        if (inputs.has_terminal_cube)
            choice.refusals.emplace_back(kCanonicalTerminalCubeRefusal);
        if (!inputs.has_daily_cube)
            choice.refusals.emplace_back(kCanonicalCubeRefusal);
        if (!inputs.has_dates)
            choice.refusals.emplace_back(kCanonicalDatesRefusal);
        return choice;
    }
    if (chunk) {
        choice.lane = Lane::run_daily_termsheet;
        choice.chosen_reason = "legacy term sheet root Chunk.Jobs";
        if (inputs.has_terminal_cube)
            choice.refusals.emplace_back(kDailyTerminalCubeRefusal);
        if (!inputs.has_daily_cube)
            choice.refusals.emplace_back(kDailyCubeRefusal);
        if (!inputs.has_dates)
            choice.refusals.emplace_back(kDailyDatesRefusal);
        return choice;
    }
    if (instruments) {
        if (market.contains("rate") || market.contains("evaluation_date")) {
            choice.lane = Lane::run_daily_termsheet_batch;
            choice.chosen_reason = "instruments array with market rate/evaluation_date";
            if (inputs.has_terminal_cube)
                choice.refusals.emplace_back(kBatchTerminalCubeRefusal);
            if (!inputs.has_daily_cube)
                choice.refusals.emplace_back(kBatchCubeRefusal);
            if (!inputs.has_dates)
                choice.refusals.emplace_back(kBatchDatesRefusal);
            return choice;
        }
        if (market.contains("underlyings")) {
            choice.lane = Lane::run_cpp_parity;
            choice.chosen_reason = "instruments array with market underlyings";
            if (!inputs.has_terminal_cube)
                choice.refusals.emplace_back(kParityCubeRefusal);
            if (inputs.has_daily_cube)
                choice.refusals.emplace_back(kParityDailyRefusal);
            return choice;
        }
        choice.chosen_reason = "instruments array without a usable market";
        choice.refusals.emplace_back(kInstrumentsNoMarketRefusal);
        return choice;
    }
    if (common) {
        choice.lane = Lane::price_fixture;
        choice.chosen_reason = "single commonData root (dealData/marketData)";
        if (inputs.has_terminal_cube || inputs.has_daily_cube || inputs.has_dates)
            choice.refusals.emplace_back(kFixtureCubeRefusal);
        return choice;
    }
    choice.chosen_reason = "no lane root keys matched";
    choice.refusals.emplace_back(kUnknownShapeRefusal);
    return choice;
}

}  // namespace fina::risk::dispatch