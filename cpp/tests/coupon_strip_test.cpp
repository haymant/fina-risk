// Block test: coupon_strip
//
// Scope: this file tests ONE payoff-graph block -- the `coupon_strip` block,
// whose kernel is fina::risk::fcn::period_rate (cpp/include/fina_risk/coupon.hpp).
//
// What this test does NOT touch, on purpose:
//   - the path cube, the Monte Carlo loop, discounting, schedules
//   - the coupon barrier, knock-out, or the memory on/off switch
//   - coupon_quote_scale (units are applied by the caller, engine.cpp:322)
//   - range_accrual (in_range) and memory_carry (next_memory) -- separate blocks
//
// How the expected values were obtained:
//   By hand, from the term-sheet sentence, NOT by running the kernel and
//   recording what it printed. A test that copies the implementation's output
//   back into an assertion is a tautology and catches nothing. The sentence is:
//
//     "The coupon for a period is the fixed rate, plus the range rate multiplied
//      by the share of that period's fixings which qualified, counting any
//      unpaid fixings carried in from before, capped at the full range rate."
//
//   Written as arithmetic:
//     total  = max(total_fixings, 1)
//     unpaid = max(qualifying_fixings - already_paid_fixings, 0)
//     rate   = fixed_coupon + range_rate * min((unpaid + carried_memory) / total, 1)
//
// Build: registered with CTest as `block/coupon_strip`. It must NOT be compiled
// with -ffast-math: the specification is a real number, the kernel is a double,
// and the comparison is therefore to a tolerance rather than bit-exact.

#include "fina_risk/coupon.hpp"

#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

namespace {

int g_failures = 0;
int g_checks = 0;

void expect_rate(const char* name, const fina::risk::fcn::CouponPeriodTerms& period,
                 double qualifying, double carried, double expected) {
    ++g_checks;
    const double actual = fina::risk::fcn::period_rate(period, qualifying, carried);
    // 1e-12 absolute: the spec is a real number, the kernel is a double, and
    // fixed + range * fraction is not exactly representable in binary.
    //
    // The finite check is not belt-and-braces, it is the difference between
    // pinning a guard and not pinning it. Every IEEE comparison against NaN is
    // false, so `fabs(actual - expected) > tol` is FALSE for a NaN and a NaN
    // used to report as a pass. Deleting the `max(total_fixings, 1.0)` floor
    // made this case return NaN, and the case whose comment says the guard is
    // load-bearing went on passing -- verified by breaking it and re-running.
    const bool finite = std::isfinite(actual);
    if (!finite || std::fabs(actual - expected) > 1e-12) {
        ++g_failures;
        if (!finite) {
            std::printf("  FAIL  %-34s expected %.10f, got non-finite (%f)\n", name, expected, actual);
        } else {
            std::printf("  FAIL  %-34s expected %.10f, got %.10f\n", name, expected, actual);
        }
    } else {
        std::printf("  ok    %-34s %.10f\n", name, actual);
    }
}

fina::risk::fcn::CouponPeriodTerms period(double fixed_coupon, double range_rate,
                                          int already_paid_fixings, int total_fixings) {
    fina::risk::fcn::CouponPeriodTerms p;
    p.fixed_coupon = fixed_coupon;
    p.range_rate = range_rate;
    p.already_paid_fixings = already_paid_fixings;
    p.total_fixings = total_fixings;
    return p;
}

}  // namespace

int main() {
    std::printf("block/coupon_strip -- kernel fina::risk::fcn::period_rate (coupon.hpp)\n");

    // A period of 5 fixings of which 2 were already paid before this period
    // began; fixed 0.005, range rate 0.010.
    const auto p5 = period(0.005, 0.010, 2, 5);

    // Nothing qualified. The two already-paid fixings are worth nothing now, and
    // the guard max(0 - 2, 0) must not produce a negative share.
    expect_rate("no_fixings_in_range", p5, 0.0, 0.0, 0.005);

    // 5 of 5 fixings qualified, 2 already paid -> 3 of 5 still earn -> 0.6 share.
    expect_rate("partial_qualification", p5, 5.0, 0.0, 0.011);

    // More fixings qualified than the period has (cannot happen in the engine,
    // where qualifying is counted by incrementing 1.0). The share must cap at
    // the full range rate rather than overpay.
    expect_rate("full_range_caps", period(0.005, 0.010, 0, 5), 10.0, 0.0, 0.015);

    // One fixing qualified here, and 2 unpaid fixings were carried in from an
    // earlier period -> (1 + 2) / 5 = 0.6 share. This is the memory feature.
    expect_rate("memory_carried_in", period(0.005, 0.010, 0, 5), 1.0, 2.0, 0.011);

    // Fewer fixings qualified than were already paid. The unpaid count must
    // floor at zero, so the period earns only the fixed rate.
    expect_rate("already_paid_exceeds_qualifying", period(0.005, 0.010, 3, 5), 1.0, 0.0, 0.005);

    // A period with no fixing count at all. The divisor is floored at 1 so this
    // must not divide by zero.
    //
    // This case does NOT arise on the example deal. `payoff.py:110` reads
    // jobs[-1] -- the COUPON job -- whose N2 is [20, 1, 21, 22, 21, 21, 22, 20,
    // 22, 19], with no zeros. The two legs that do carry N2 = 0 (PUT, FUNDING)
    // each hold only two placeholder rows and are discarded before the kernel
    // runs. The case is tested anyway: the guard is load-bearing, and a test
    // suite that only covers inputs the current pipeline happens to produce is a
    // suite that breaks when the pipeline changes.
    //
    // And it is load-bearing in a second sense: the two cases below are the ONLY
    // ones that can see the `max(total_fixings, 1.0)` floor, because every other
    // case has a non-zero total. Remove the floor and 0/0 makes the rate NaN.
    // A NaN is not caught by a tolerance comparison -- every comparison against
    // it is false -- so `expect_rate` checks finiteness explicitly. Without that
    // check this case reports a pass with the guard deleted.
    expect_rate("zero_total_fixings_guard", period(0.005, 0.010, 0, 0), 0.0, 0.0, 0.005);

    // Same degenerate period, but with one unpaid fixing carried in. Against a
    // floored divisor of 1, that single fixing earns the whole range rate.
    expect_rate("zero_total_with_memory_uses_one", period(0.005, 0.010, 0, 0), 0.0, 1.0, 0.015);

    // Memory is counted in fixings, not in whole periods, so it can be
    // fractional. 1.5 carried into a 4-fixing period -> 0.375 share.
    expect_rate("fractional_memory", period(0.005, 0.010, 0, 4), 0.0, 1.5, 0.00875);

    // Legacy-sourced coupons arrive with fixed_coupon = 0 (payoff.py:171), so
    // the block degenerates to range_rate * share. Pin that explicitly.
    expect_rate("legacy_fixed_coupon_is_zero", period(0.0, 0.010, 0, 4), 2.0, 0.0, 0.005);

    // A zero range rate must return exactly the fixed rate, whatever the share.
    // The fixed rate here is 0.0015, chosen to be obviously arbitrary: it is not
    // in the term sheet and not in the [0.006, 0.018] band the synthetic deal
    // generator draws from, so no reader can mistake it for a deal value. (An
    // earlier revision used 0.00713 here, which is the unsourced literal in
    // fcn-terms-projection.example.json -- a number that has no business being
    // pinned in a block test.)
    expect_rate("zero_range_rate", period(0.0015, 0.0, 0, 10), 10.0, 0.0, 0.0015);

    std::printf("%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
