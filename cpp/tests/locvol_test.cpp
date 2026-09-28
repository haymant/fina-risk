// Block test: the Dupire local-volatility surface.
//
// The surface is implemented twice -- here in C++
// (include/fina_risk/locvol.hpp, now used by fina_risk_cpp.cpp's price_fixture)
// and again in Python (src/fina_risk/locvol.py, used by pricing._simulate_locvol).
// Before this port the C++ terminal lane had no local vol at all, so the two
// lanes priced different models from the same payload.
//
// This test does three things, in that order of importance:
//
//   1. Asserts three facts about the Dupire transform that can be derived by
//      hand from the formula, with no reference implementation involved. These
//      are the specification checks:
//        a. A flat implied smile -- flat across strike, and constant in tenor --
//           returns that implied vol exactly. Proof: y2 = 0, so dx w = dxx w = 0
//           and the Dupire denominator collapses to 1, leaving
//           sigma_LV^2 = dT w. And w = sigma_imp^2 * t is linear in t, so its
//           derivative is sigma_imp^2. This is locvol.py's own claim that "a flat
//           implied surface collapses sigma_LV back to the constant implied
//           vol", and it is what makes the scalar lane the special case of this
//           one.
//        b. Constant *total variance* across tenor is the other degenerate
//           case: w stops depending on t, so dT w = 0, and sigma_LV falls to the
//           clamp floor kClampLo rather than to the smile. A surface can be
//           shaped and still carry no time information.
//        c. The surface must not depend on which axis the payload chose. The
//           same grid delivered transposed must give the same numbers.
//   2. Asserts the surface is finite and positive over its whole dense axis for
//      the real eqVol grids, and that t and S are clamped outside the fitted
//      range rather than extrapolated.
//   3. Prints sigma(t, S) over a grid on stdout for every real surface so
//      tests/test_locvol.py can diff it value-for-value against the Python
//      copy. That test is the one that catches a formula that is subtly but
//      consistently different; the checks above only catch gross errors.
//
// Run:   ctest --test-dir build -R block/locvol
// Dump:  ./fina-risk-block-locvol path/to/termsheet1.md.json
//
// Mutation results, recorded so a future change to these assertions knows which
// ones are load-bearing. Each row was applied to locvol.hpp, rebuilt, and run:
//
//   caught by ctest  caught by test_locvol.py
//   ---------------  ----------------------
//   transpose branch always taken      yes            yes
//   end coupling m[0, 1] dropped       yes            yes
//   dT w one-sided everywhere          yes            yes
//   0.25 -> 0.5 on the (dx w)^2 term   no             yes
//   log-linear -> linear in strike     no             yes
//   kClampLo 0.10 -> 0.20              yes            yes
//   transpose detection removed        yes            yes
//   365.0 -> 365.25 day count          no             yes
//   non-finite denominator floor       NO             NO
//
// The last row is the honest gap. That floor cannot be reached from a grid the
// constructor accepts, so no test can distinguish it from its absence; it is
// kept for fidelity with locvol.py and the reason is recorded at the call site.
// The other eight are all pinned by tests/test_locvol.py, and four of them only
// by the diff against Python -- which is the reason that test exists rather than
// trusting the structural checks alone.

#include "fina_risk/locvol.hpp"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using json = nlohmann::json;
using fina::risk::vol::ImpliedVolGrid;
using fina::risk::vol::LocalVolSurface;

int failures = 0;

void check(bool condition, const char* what) {
    if (!condition) {
        std::fprintf(stderr, "  FAIL %s\n", what);
        ++failures;
    }
}

void check_close(double actual, double expected, double tolerance, const char* what) {
    // Rejecting non-finite values here is the point, not a nicety: every IEEE
    // comparison against NaN is false, so a bare fabs(actual - expected) >
    // tolerance reports a pass on a NaN. A tolerance test that cannot fail is
    // not a test.
    if (!std::isfinite(actual) || !std::isfinite(expected) || std::fabs(actual - expected) > tolerance) {
        std::fprintf(stderr, "  FAIL %s (expected %.17g, got %.17g)\n", what, expected, actual);
        ++failures;
    }
}

// The grid both dumped below and checked for finiteness. t values are chosen to
// sit inside the fitted tenor range of the real surface (tenors run from
// 46299 to 48095 against a 46272 evaluation date, so 0.07y to 4.997y) and to
// include the extrapolation ends. S values span the quoted spot through a wide
// multiple, so the log-linear strike interpolation is exercised on both wings.
const double kTValues[] = {0.0, 0.05, 0.25, 0.402740, 0.75, 1.5, 3.0, 4.9, 5.5};
const double kSValues[] = {100.0, 150.0, 239.823, 260.0, 400.0, 800.0};

ImpliedVolGrid to_grid(const json& surface) {
    ImpliedVolGrid grid;
    grid.id = surface.value("_id", "");
    for (const auto& s : surface.at("strike")) grid.strike.push_back(s.get<double>());
    for (const auto& m : surface.at("maturity")) grid.maturity.push_back(m.get<double>());
    for (const auto& row : surface.at("vol")) {
        std::vector<double> r;
        for (const auto& v : row) r.push_back(v.get<double>());
        grid.vol.push_back(std::move(r));
    }
    return grid;
}

// Transpose the vol array only. `strike` and `maturity` keep their meaning and
// their units; the payload's two axes are a [tenor][absolute strike] grid or a
// [absolute strike][tenor] grid, and which one arrived is exactly the question.
json transpose_vol(const json& surface) {
    json out = surface;
    const json& rows = surface.at("vol");
    json cols = json::array();
    for (std::size_t j = 0; j < rows.at(0).size(); ++j) {
        json col = json::array();
        for (std::size_t i = 0; i < rows.size(); ++i) col.push_back(rows.at(i).at(j));
        cols.push_back(std::move(col));
    }
    out["vol"] = std::move(cols);
    return out;
}

// One tenor only: dT w is then a one-sided difference from the single row, and
// the flat-smile identity has to survive it.
void test_single_tenor_flat_smile_is_exact() {
    ImpliedVolGrid grid;
    grid.id = "single-tenor-flat";
    for (double k : {80.0, 90.0, 100.0, 110.0, 120.0}) grid.strike.push_back(k);
    grid.maturity.push_back(46272 + 200);
    grid.vol.push_back({20.0, 20.0, 20.0, 20.0, 20.0});

    const LocalVolSurface lv(grid, 46272, 100.0, 0.04);
    for (double t : kTValues)
        for (double s : kSValues)
            check_close(lv.sigma(t, s), 0.20, 1e-12, "a single flat tenor returns the implied vol");
}

// (1a) flat across strike and tenor -> the implied vol comes back exactly.
void test_flat_implied_surface_is_exact() {
    ImpliedVolGrid grid;
    grid.id = "flat-in-strike-and-tenor";
    for (double k : {80.0, 90.0, 100.0, 110.0, 120.0}) grid.strike.push_back(k);
    for (int k = 0; k < 4; ++k) grid.maturity.push_back(46272 + 100 + 90 * k);
    for (int i = 0; i < 4; ++i) grid.vol.push_back({20.0, 20.0, 20.0, 20.0, 20.0});

    const LocalVolSurface lv(grid, 46272, 100.0, 0.04);
    for (double t : kTValues)
        for (double s : kSValues)
            check_close(lv.sigma(t, s), 0.20, 1e-12,
                        "a flat implied surface collapses to the constant implied vol");
}

// The floor a degenerate surface must land on, written as a literal rather than
// as fina::risk::vol::kClampLo. Asserting a constant against the same constant
// is how a specification check quietly becomes a tautology: raising the clamp to
// 0.20 in the header would leave every such assertion satisfied, and that
// mutation is verified to be caught by exactly this choice.
constexpr double kExpectedFloor = 0.10;

// A zero implied grid is the degenerate case the global-KO lane test actually
// uses, and it is the reason that test has to opt out of local vol.
//
// The derivation is exact: w = sigma_imp^2 * t is identically zero, so dT w is
// zero at every point, so sigma_LV^2 = 0 / denominator = 0, and the clamp raises
// it to kClampLo. The result is a 10% volatility grid, not a flat one. A lane
// that wanted "no volatility" and got local vol by default would price its
// deterministic cases as if the market moved.
void test_zero_vol_grid_floor() {
    ImpliedVolGrid grid;
    grid.id = "zero";
    for (double k : {80.0, 90.0, 100.0, 110.0, 120.0}) grid.strike.push_back(k);
    for (int k = 0; k < 3; ++k) grid.maturity.push_back(46272 + 100 + 90 * k);
    for (int i = 0; i < 3; ++i) grid.vol.push_back({0.0, 0.0, 0.0, 0.0, 0.0});

    const LocalVolSurface lv(grid, 46272, 100.0, 0.04);
    for (double t : kTValues)
        for (double s : kSValues)
            check_close(lv.sigma(t, s), kExpectedFloor, 1e-12,
                        "a zero implied grid is 0, not a 0 surface under Dupire");
}

// The natural spline solve, against a system small enough to solve by hand.
//
// locvol.py assembles the system as
//     m = diag(diag) + diag(sub[1:], +1) + diag(sub[1:], -1)
// where diag[0] = diag[-1] = 1, diag[i] = 2*(h[i-1] + h[i]) inside, and
// sub[i] = h[i] for i in 1..n-2 with zeros at both ends. The two end couplings
// are not part of the interior pattern: m[0, 1] = sub[1] = h[1], while
// m[n-1, n-2] = sub[n-1] = 0.
//
// For x = [0, 1, 2, 3], y = [0, 1, 4, 9] that is a four-by-four solve, written
// out here by hand:
//     row 0:  1*y''[0]      + 1*y''[1]          = 0
//     row 1:  1*y''[0] + 4*y''[1] + 1*y''[2]     = 12
//     row 2:            1*y''[1] + 4*y''[2]      = 12
//     row 3:                        1*y''[3]      = 0
// from which y''[3] = 0, y''[0] = -y''[1], and the middle two give
// 3*y''[1] + y''[2] = 12 and y''[1] + 4*y''[2] = 12, hence
// y''[1] = 12*13/47 = 3.272727... and y''[0] = -y''[1].
//
// The expected values below are that solution, written as exact fractions. The
// end rows are the point of the test: filling the off-diagonals from the
// interior loop alone -- the obvious way to write it -- drops m[0, 1] and reads
// one past the end of the spacing array for the top-right corner, and returns
// [0, 2.4, 2.4, 0] instead. That answer is still symmetric, still diagonally
// dominant, and still has zero second derivatives at both ends, so no structural
// check notices. It moved sigma by about 1e-3 relative. Only the values catch it.
void test_natural_spline_solves_by_hand() {
    const std::vector<double> x = {0.0, 1.0, 2.0, 3.0};
    const std::vector<double> y = {0.0, 1.0, 4.0, 9.0};
    const std::vector<double> y2 = fina::risk::vol::natural_second_derivatives(x, y);

    // 36/11, -36/11, 24/11, 0 -- the exact solution of the system above.
    check_close(y2.at(0), -36.0 / 11.0, 1e-12, "the natural end row couples y''[0] to y''[1]");
    check_close(y2.at(1), 36.0 / 11.0, 1e-12, "the first interior second derivative");
    check_close(y2.at(2), 24.0 / 11.0, 1e-12, "the second interior second derivative");
    check_close(y2.at(3), 0.0, 1e-12, "the last end row is the identity, so y''[n-1] is zero");

    // A linear smile has no curvature, and this one is a real system rather than
    // an empty one, so it also proves the right-hand side is assembled.
    std::vector<double> linear(x.size());
    for (std::size_t i = 0; i < x.size(); ++i) linear[i] = 3.0 * x[i] + 1.0;
    for (double v : fina::risk::vol::natural_second_derivatives(x, linear))
        check_close(v, 0.0, 1e-12, "a linear smile has no second derivative");

    // Degenerate inputs must not produce a solve at all. A repeated knot means
    // h[i] = 0, and the spacing appears in the right-hand side as a divisor.
    std::vector<double> repeated = x;
    repeated[2] = repeated[1];
    const std::vector<double> bad = fina::risk::vol::natural_second_derivatives(repeated, linear);
    for (double v : bad) check_close(v, 0.0, 0.0, "a non-increasing axis refuses to be solved");

    std::vector<double> nan_y = y;
    nan_y[1] = std::numeric_limits<double>::quiet_NaN();
    for (double v : fina::risk::vol::natural_second_derivatives(x, nan_y))
        check_close(v, 0.0, 0.0, "a NaN in the smile refuses to be solved");

    std::vector<double> short_x = {1.0, 2.0};
    std::vector<double> short_y = {0.2, 0.3};
    const std::vector<double> too_few = fina::risk::vol::natural_second_derivatives(short_x, short_y);
    for (double v : too_few) check_close(v, 0.0, 0.0, "fewer than three knots has no spline");
}

// (1c) the surface is a function of the numbers, not of the axis layout.
void test_axis_orientation_does_not_matter(const json& real_surface) {
    const ImpliedVolGrid direct = to_grid(real_surface);
    const ImpliedVolGrid flipped = to_grid(transpose_vol(real_surface));
    const LocalVolSurface a(direct, 46272, 239.823, 0.037405);
    const LocalVolSurface b(flipped, 46272, 239.823, 0.037405);
    for (double t : kTValues)
        for (double s : kSValues)
            check_close(b.sigma(t, s), a.sigma(t, s), 1e-12,
                        "a transposed eqVol grid prices the same surface");
}

// A grid matching neither axis is malformed. Guessing which axis was meant is
// the failure mode this lane refuses rather than reproduces.
void test_malformed_grid_is_refused() {
    ImpliedVolGrid grid;
    grid.id = "malformed";
    for (double k : {80.0, 90.0, 100.0}) grid.strike.push_back(k);
    for (int k = 0; k < 10; ++k) grid.maturity.push_back(46272 + 30 * (k + 1));
    for (int i = 0; i < 5; ++i) grid.vol.push_back({20.0, 20.0, 20.0, 20.0, 20.0});
    bool threw = false;
    try {
        const LocalVolSurface lv(grid, 46272, 100.0, 0.04);
        (void)lv;
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    check(threw, "a vol grid matching neither the tenor nor the strike count is refused");

    ImpliedVolGrid empty;
    empty.id = "empty";
    empty.strike = {100.0};
    empty.maturity = {46272 + 30};
    bool threw_empty = false;
    try {
        const LocalVolSurface lv(empty, 46272, 100.0, 0.04);
        (void)lv;
    } catch (const std::invalid_argument&) {
        threw_empty = true;
    }
    check(threw_empty, "an eqVol surface with no vol grid is refused");
}

// (2) finiteness and range clamping on the real grids.
void check_real_surface(const LocalVolSurface& lv, const std::string& id) {
    for (std::size_t k = 0; k < lv.tenor_axis().size(); ++k) {
        for (double level : lv.strike_axis()) {
            const double sigma = lv.sigma(lv.tenor_axis()[k], level);
            if (!std::isfinite(sigma) || sigma <= 0.0) {
                std::fprintf(stderr, "  FAIL %s sigma(t=%.6f, S=%.6f) = %.17g\n", id.c_str(),
                             lv.tenor_axis()[k], level, sigma);
                ++failures;
                break;
            }
        }
    }
    // t below the first tenor and above the last must clamp, not extrapolate.
    const double lo = lv.tenor_axis().front();
    const double hi = lv.tenor_axis().back();
    for (double s : kSValues) {
        check_close(lv.sigma(lo - 5.0, s), lv.sigma(lo, s), 1e-12, "t below the first tenor clamps to it");
        check_close(lv.sigma(hi + 5.0, s), lv.sigma(hi, s), 1e-12, "t above the last tenor clamps to it");
    }
    // S outside the dense axis must clamp to the row's endpoints.
    const double s_lo = lv.strike_axis().front();
    const double s_hi = lv.strike_axis().back();
    for (double t : kTValues) {
        check_close(lv.sigma(t, s_lo * 0.5), lv.sigma(t, s_lo), 1e-12, "S below the axis clamps to the left row value");
        check_close(lv.sigma(t, s_hi * 2.0), lv.sigma(t, s_hi), 1e-12, "S above the axis clamps to the right row value");
    }
}

}  // namespace

int main(int argc, char** argv) {
    test_natural_spline_solves_by_hand();
    test_single_tenor_flat_smile_is_exact();
    test_flat_implied_surface_is_exact();
    test_zero_vol_grid_floor();

    if (argc < 2) {
        std::fprintf(stderr, "usage: %s <termsheet1.md.json>\n", argv[0]);
        return 2;
    }
    json request;
    try {
        std::string text;
        {
            std::FILE* f = std::fopen(argv[1], "rb");
            if (!f) {
                std::fprintf(stderr, "cannot open %s\n", argv[1]);
                return 2;
            }
            char buffer[65536];
            std::size_t got = 0;
            while ((got = std::fread(buffer, 1, sizeof(buffer), f)) > 0) text.append(buffer, got);
            std::fclose(f);
        }
        request = json::parse(text);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "cannot parse %s: %s\n", argv[1], e.what());
        return 2;
    }

    // Collect the eqVol grids from every job, first occurrence of each id wins.
    std::vector<std::string> seen;
    std::vector<ImpliedVolGrid> grids;
    for (const auto& job : request.at("Chunk").at("Jobs")) {
        const auto& md = job.at("commonData").at("marketData");
        for (const auto& surface : md.at("eqVol")) {
            const std::string id = surface.value("_id", "");
            if (std::find(seen.begin(), seen.end(), id) != seen.end()) continue;
            seen.push_back(id);
            grids.push_back(to_grid(surface));
        }
    }
    check(grids.size() >= 2, "the term sheet supplies one eqVol grid per underlying");

    for (const auto& grid : grids) {
        const LocalVolSurface lv(grid, 46272, grid.id == seen.front() ? 239.823 : 260.0, 0.037405);
        check_real_surface(lv, grid.id);
        // (3) dump for tests/test_locvol.py
        for (double t : kTValues)
            for (double s : kSValues)
                std::printf("SIGMA\t%s\t%.17g\t%.17g\t%.17g\n", grid.id.c_str(), t, s, lv.sigma(t, s));
        // And the full dense axis at the expiry, so the diff covers the whole
        // surface and not only the handful of points a caller happens to hit.
        for (double level : lv.strike_axis())
            std::printf("AXIS\t%s\t0.402740\t%.17g\t%.17g\n", grid.id.c_str(), level,
                        lv.sigma(0.402740, level));
    }

    if (!grids.empty()) {
        const json& real = [&] {
            for (const auto& job : request.at("Chunk").at("Jobs"))
                if (job.at("commonData").at("marketData").contains("eqVol") &&
                    job.at("commonData").at("marketData").at("eqVol").at(0).value("_id", "") == seen.front())
                    return job.at("commonData").at("marketData").at("eqVol").at(0);
            return json();
        }();
        test_axis_orientation_does_not_matter(real);
    }
    test_malformed_grid_is_refused();

    if (failures == 0) std::printf("block/locvol ok (%zu surfaces)\n", grids.size());
    return failures == 0 ? 0 : 1;
}
