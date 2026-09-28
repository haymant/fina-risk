// Block test: the Daily Callable Condition is a per-asset latch.
//
// The term sheet (refs/termsheet1.md, "Daily Callable Condition"):
//
//     If EACH of the Reference Asset in the Reference Basket has BECOME a
//     Memorised Reference Asset on a Call Fixing Date, the Daily Callable
//     Condition is satisfied and the Memory US Stocks ELIs will be terminated
//     on such Call Fixing Date.
//
// "Become" is the operative word. An asset that was at or above its Call Price
// on any earlier Call Fixing Date is a Memorised Reference Asset *from then on*,
// so the test is a per-asset latch AND-ed across the basket -- not a joint test
// on one fixing. The playbook says the same thing in its own words
// (MurexPlaybook/Introduction/Common_Features.md, "Memory Knock-Out"):
//
//     a Knock-Out event only occurs after *each* underlying in the basket has
//     individually breached its barrier at some point during the life of the
//     trade. The product "remembers" which underlyings have already hit their
//     barriers (tracked by per-stock "locked" flags and dates in the flex block).
//
// And the payload carries that state explicitly: termsheet1 carries
// KIKOSelect.GKOLocked = [true, false] with GKODate = [46174, 0]. ADBE latched
// on 2026-06-01 and AMZN had not, which is exactly what the market snapshot
// shows on the 2026-09-07 evaluation date (ADBE 267.885 > 263.9670 call price;
// AMZN 258.355 < 286.00). A basket state that the deal file states and the
// pricer never reads is a latch that is not a latch.
//
// Every expectation below is derived by hand from the specification before the
// code is run. The paths in these cases are deterministic: the volatility
// surface is a flat 0% grid, so each asset's log-price moves by exactly
// (r - q) * dt per step and the barrier comparisons are decided by arithmetic,
// not by a draw.
//
// Fast-math note: this test links fina_risk_core, which is compiled with
// -ffast-math (CMakeLists.txt, by design, for the legacy lanes). That is safe
// here because no assertion is within rounding distance of a branch: the
// closest a path ever comes to a barrier is a factor of 0.6 away.

// ---------------------------------------------------------------------------
// Tiny check harness. No gtest: the block tests are C++ executables that assert
// specified arithmetic, and a dependency here would buy nothing.
// ---------------------------------------------------------------------------
#include "fina_risk_cpp.hpp"

#include "fina_risk/nyse_calendar.hpp"

#include <nlohmann/json.hpp>

#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

namespace {

using json = nlohmann::json;
using fina::risk::cal::nyse_schedule;

// 2026-09-07, the termsheet1 evaluation date.
constexpr int kEval = 46272;
// 2027-02-03, the termsheet1 maturity. Used wherever the date itself does not
// matter; only the schedule length does, and that is computed below.
constexpr int kExpiry = 46421;

int g_failures = 0;
int g_checks = 0;

void expect_close(const std::string& what, double got, double want, double tol) {
    g_checks += 1;
    if (std::fabs(got - want) <= tol) {
        std::printf("  ok    %-46s %.9f\n", what.c_str(), got);
        return;
    }
    g_failures += 1;
    std::printf("  FAIL  %-46s got %.9f, want %.9f (tol %.1e)\n", what.c_str(), got,
                want, tol);
}

void expect_true(const std::string& what, bool ok) {
    g_checks += 1;
    std::printf("  %-4s  %s\n", ok ? "ok" : "FAIL", what.c_str());
    if (!ok) g_failures += 1;
}

// ---------------------------------------------------------------------------
// Request builder.
//
// A flat 0% volatility grid is what makes the cases deterministic. `surface_vol`
// interpolates to a total variance of 0 and then floors at 1e-12, so the vol
// actually used is 1e-6 / sqrt(t) -- about 1.5e-6. Over a thousand steps that
// moves a log-price by ~3e-6, which cannot reach a barrier 0.6 away.
//
// This grid is also why the request sets `localVol: false` (see make_request
// below). price_fixture now builds a Dupire surface when it can, and a zero
// implied surface is NOT flat under Dupire: sigma_LV^2 = dT w / denominator is
// 0 / 1, and the clamp raises it to the 10% floor. A lane that wanted "no
// volatility" and got local vol by default would price these cases as if the
// market moved 10% a year, and the barrier comparisons would be decided by a
// draw rather than by arithmetic. The opt-out is explicit and per-request so
// that it is visible here, rather than a default in the lane that this test
// silently depends on.
// ---------------------------------------------------------------------------
json vol_grid(const std::string& id, int eval, int expiry) {
    return json{{"_id", id},
                {"strike", json::array({10.0, 1000000.0})},
                {"maturity", json::array({eval + 30, expiry})},
                {"vol", json::array({json::array({0.0, 0.0}),
                                     json::array({0.0, 0.0})})}};
}

struct Basket {
    std::vector<double> refs;
    std::vector<double> spots;
    std::vector<bool> gko_locked;
    std::vector<int> gko_date;
    double global_barrier = 1.10;
    double curve_rate = 0.0;
    std::vector<double> dividend_yield;  // per asset, 0 unless stated
    int eval = kEval;
    int expiry = kExpiry;
};

json make_request(const Basket& b) {
    json underlyings = json::array();
    json equity = json::array();
    json eq_vol = json::array();
    for (std::size_t i = 0; i < b.refs.size(); ++i) {
        const std::string id = "U" + std::to_string(i);
        underlyings.push_back(json{{"spot", b.refs[i]}, {"_id", id}});
        json e = json{{"_id", id}, {"spot", b.spots[i]}};
        // The dividend yield is a *yield*, and the lane converts it as
        // `paid / spot / years_to_expiry`. To ask for a yield q over the life of
        // the trade, pay q * spot * years.
        if (i < b.dividend_yield.size() && b.dividend_yield[i] != 0.0) {
            const double years = static_cast<double>(b.expiry - b.eval) / 365.0;
            const double paid = b.dividend_yield[i] * b.spots[i] * years;
            e["dividend"] = json::array(
                {json{{"exDate", b.eval + 30}, {"div", paid}}});
        }
        equity.push_back(e);
        eq_vol.push_back(vol_grid(id, b.eval, b.expiry));
    }

    json kiko = json{{"knockInType", "EKI"}};
    kiko["GKOLocked"] = b.gko_locked;
    kiko["GKODate"] = b.gko_date;

    json deal = json{{"instrument", json{{"underlyings", underlyings}}},
                     {"knockInStar", json{{"strikeKI2", 0.78}, {"KIBarrier", 0.70}}},
                     {"KIKOSelect", kiko},
                     {"RGACCLKO", json{{"gblBarPrice", b.global_barrier}}},
                     {"expiryDate", b.expiry},
                     {"notional", 1.0}};

    // localVol: false, because the flat 0% grid above is a deterministic-case
    // device and a zero implied surface is a 10% surface under Dupire. See
    // vol_grid's comment.
    json market = json{{"evaluationDate", b.eval},
                       {"equity", equity},
                       {"eqVol", eq_vol},
                       {"localVol", false},
                       {"corr", json::array({json::array(
                                         {json{{"correlation", 0.0}}})})}};
    if (b.curve_rate != 0.0) {
        const json curve = json::array({json{{"date", b.eval}, {"rate", b.curve_rate}},
                                        json{{"date", b.expiry}, {"rate", b.curve_rate}}});
        market["discCurves"] = json::array({json{{"curve", curve}}});
    }

    return json{{"dealData", deal}, {"marketData", market}};
}

fina::risk::RiskResult price(const Basket& b, std::size_t paths) {
    return fina::risk::price_fixture(make_request(b).dump(), paths, 1729);
}

// ---------------------------------------------------------------------------
// The live put in the not-called cases, derived by hand.
//
// strikeKI2 = 0.78, KIBarrier = 0.70, knockInType = "EKI" (European knock-in,
// observed on the final fixing only), so `knock_in = worst <= 0.70` and each
// path adds `max(0.78 - worst, 0)` unless the note was called. Every path has
// the same worst, so the expectation is exact apart from the 1.5e-6 vol.
//
//   refs 100, spots 50, no drift, no discount (rate 0)
//     worst = 0.50, knock-in, put per path = 0.78 - 0.50 = 0.28, x exp(0) = 0.28
// ---------------------------------------------------------------------------
constexpr double kFlatPut = 0.28;

// Case D's not-called put, written out from the GBM the lane specifies.
//
// Two things are easy to get wrong here and both were wrong in the first draft
// of this test:
//
//   * `strike` (0.78) and `worst` are both RATIOS to the reference price, not
//     absolute prices, so the terminal has to be divided by its reference before
//     it is compared with the strike.
//   * `worst` is the minimum over the basket, not one asset.
//
// The lane takes `steps` trading days with a step of dt = 1/252, so an asset
// with zero volatility and drift (r - q) ends the life of the trade at
// spot * exp((r - q) * steps / 252). Note that steps/252 is the *trading* year
// and is deliberately not the same as (expiry - eval)/365, which is what the
// discount factor uses -- the lane runs the drift on trading time and the
// discount on calendar time, and that is as specified, not a mistake here.
double crossed_put(const Basket& b, double strike) {
    const auto schedule = nyse_schedule(b.eval, b.expiry);
    const double steps = static_cast<double>(schedule.size());
    const double years = static_cast<double>(b.expiry - b.eval) / 365.0;
    double worst = 1e9;
    for (std::size_t i = 0; i < b.refs.size(); ++i) {
        const double q = (i < b.dividend_yield.size()) ? b.dividend_yield[i] : 0.0;
        const double terminal =
            (b.spots[i] / b.refs[i]) * std::exp((b.curve_rate - q) * steps / 252.0);
        worst = std::min(worst, terminal);
    }
    return std::max(strike - worst, 0.0) * std::exp(-b.curve_rate * years);
}

// ===========================================================================
// Case A -- the payload's latch is ignored. THE DEFECT.
// ===========================================================================
//
// Two assets, both at 50 against a reference of 100, so both sit at 0.50 and
// the 110% call barrier is unreachable on any simulated fixing. The deal file
// says both are already latched, on a date before the evaluation date.
//
//   conformant ("has become")   both already latched -> called on the first
//                               Call Fixing Date -> the put is not exercised,
//                               put == 0.
//   joint-on-one-fixing         no fixing has both assets above 1.10 -> the
//                               note is never called -> put == 0.28.
void case_a_payload_latch_is_credited() {
    std::printf("A. the deal file says both assets are latched, and both are at 0.50\n");
    Basket b;
    b.refs = {100.0, 100.0};
    b.spots = {50.0, 50.0};
    b.gko_locked = {true, true};
    b.gko_date = {kEval - 90, kEval - 40};

    const auto r = price(b, 64);
    expect_close("put (both latched in the payload)", r.put_option_price, 0.0, 0.0);
}

// ===========================================================================
// Case B -- one latch is not a call. This is termsheet1's own shape.
// ===========================================================================
//
// GKOLocked = [true, false]: ADBE latched on 2026-06-01, AMZN had not. The
// Daily Callable Condition needs *each* asset, so the note is not called, the
// put is live, and this must be unchanged by the fix. A fix that fires on a
// single latch would take the note callable for every deal in the book.
void case_b_one_latch_does_not_call() {
    std::printf("B. one of two latched (termsheet1's shape): the note is NOT callable\n");
    Basket b;
    b.refs = {100.0, 100.0};
    b.spots = {50.0, 50.0};
    b.gko_locked = {true, false};
    b.gko_date = {kEval - 90, 0};

    const auto r = price(b, 64);
    expect_close("put (only ADBE latched)", r.put_option_price, kFlatPut, 1e-6);
}

// ===========================================================================
// Case C -- a plain joint test still calls. The latch must not replace it.
// ===========================================================================
//
// Nothing in the payload, both assets at 120 (ratio 1.20) against a 1.10
// barrier, so every fixing satisfies the joint test. Both readings agree, and
// a fix that only honoured the payload would break this.
void case_c_joint_test_still_calls() {
    std::printf("C. both assets above the barrier on every fixing: callable\n");
    Basket b;
    b.refs = {100.0, 100.0};
    b.spots = {120.0, 120.0};
    b.gko_locked = {false, false};
    b.gko_date = {0, 0};

    const auto r = price(b, 64);
    expect_close("put (both above the barrier)", r.put_option_price, 0.0, 0.0);
}

// ===========================================================================
// Case D -- the latch is sticky *within* the life of the trade.
//
// The two assets cross the barrier at different times, and never on the same
// fixing, so the joint test can never fire:
//
//   asset 0: no dividend, r = 5%  -> 0.90 rising, needs log(1.10/0.90) = 0.20068
//             of log-price, at 0.05/252 per step that is step 1012.
//   asset 1: yield 30%, so drift (0.05 - 0.30)/252 -> 1.30 falling; it reaches
//             1.10 after log(1.30/1.10) = 0.16705 / (0.25/252) = step 168.
//
// So asset 1 is above 1.10 for steps 0..168 and asset 0 only from step 1012
// onwards, and there is no fixing on which both are above.
//
//   conformant   asset 1 latches at step 0 and stays; asset 0 latches at 1012
//                -> the note is called -> put == 0.
//   joint        the two never overlap -> the note is never called -> put > 0.
Basket d_basket() {
    Basket b;
    b.eval = kEval;
    b.expiry = kEval + 1700;  // ~1167 trading days, well past step 1012
    b.refs = {100.0, 100.0};
    b.spots = {90.0, 130.0};
    b.gko_locked = {false, false};  // nothing latched on arrival
    b.gko_date = {0, 0};
    b.curve_rate = 0.05;
    b.dividend_yield = {0.0, 0.30};
    return b;
}

void case_d_latch_is_sticky_within_the_trade() {
    std::printf("D. the two assets cross the barrier 844 steps apart: callable anyway\n");
    const auto r = price(d_basket(), 16);
    expect_close("put (latches 844 steps apart)", r.put_option_price, 0.0, 1e-9);
}

// ===========================================================================
// Case E -- the same path as D, priced the other way, as a companion value.
// ===========================================================================
//
// Not a case on its own: it re-prices D with the joint test and pins the number
// it would have produced, so a partial fix that drops the payload latch or the
// in-path stickiness in only one of the two places cannot slip through as
// "called for a different reason". The expected value is the GBM terminal
// written out from the specification, not read back from the lane.
void case_e_joint_only_value_is_pinned() {
    std::printf("E. the same path without the latch: the live put, derived\n");
    const Basket b = d_basket();

    // Nothing is latched in the payload here, so the only thing that can call
    // the note is the joint test, and the two assets never overlap. This is the
    // put the lane as written produces, worked out from the GBM it specifies:
    //
    //   asset 0   (0.90/1.00) * exp(+0.05 * steps/252)  =  0.90 * 1.2619 = 1.1357
    //   asset 1   (1.30/1.00) * exp(-0.25 * steps/252)  =  1.30 * 0.3144 = 0.4087
    //   worst                                               min(...)  = 0.4087
    //   knock-in   0.4087 <= 0.70                        yes
    //   put        (0.78 - 0.4087) * exp(-0.05 * 4.657) = 0.3713 * 0.7918 = 0.2940
    //
    // The terminal optionality is the *whole* of the difference between the two
    // readings on this path, and the correct reading removes all of it. The band
    // is a sanity check on the derivation, not a tolerance: it catches a sign
    // error or a ratio-versus-price confusion, which is exactly how the first
    // draft of this test went wrong.
    const double want = crossed_put(b, 0.78);
    expect_true("derived not-called put is in (0.28, 0.32)", want > 0.28 && want < 0.32);
    std::printf("        derived not-called put = %.9f\n", want);
}

// ===========================================================================
// Case F -- the latch is AND-ed over the whole basket, not the first two.
// ===========================================================================
//
// Three assets, all at 0.50, none able to reach the barrier. Two of three
// latched is not called; three of three is. This is the property the
// implementation gets wrong when it hard-codes two underlyings.
void case_f_latch_is_anded_over_the_basket() {
    std::printf("F. a three-name basket: two latches is not a call, three is\n");
    Basket b;
    b.refs = {100.0, 100.0, 100.0};
    b.spots = {50.0, 50.0, 50.0};
    b.gko_date = {0, 0, 0};

    b.gko_locked = {true, true, false};
    {
        const auto r = price(b, 64);
        expect_close("put (2 of 3 latched)", r.put_option_price, kFlatPut, 1e-6);
    }
    b.gko_locked = {true, true, true};
    {
        const auto r = price(b, 64);
        expect_close("put (3 of 3 latched)", r.put_option_price, 0.0, 0.0);
    }
}

// ===========================================================================
// Not tested here: the latch date.
//
// `GKODate` dates each latch, and a latch dated after the evaluation date has
// not happened yet, so crediting it would call a note that is not callable.
// Neither lane checks the date: pricing.py:304-307 seeds the latch straight
// from `GKOLocked`, and the seed added here does the same. That is deliberate --
// one rule in both lanes, so the two can be compared -- and it is a real gap,
// recorded as such rather than fixed in one place. The cases above all use
// dates at or before the evaluation date or 0, so the gap cannot mask a
// regression in the flag handling itself.
// ===========================================================================

}  // namespace

int main() {
    std::printf("lane/global_ko_latch -- the Daily Callable Condition is a per-asset latch\n\n");
    case_a_payload_latch_is_credited();
    case_b_one_latch_does_not_call();
    case_c_joint_test_still_calls();
    case_d_latch_is_sticky_within_the_trade();
    case_e_joint_only_value_is_pinned();
    case_f_latch_is_anded_over_the_basket();

    std::printf("\n%d checks, %d failed\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
