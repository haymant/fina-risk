#pragma once

// Local (Dupire) volatility derived from the legacy `eqVol` implied grid.
//
// This is a line-for-line port of `src/fina_risk/locvol.py`. It exists because
// the same surface was implemented in exactly one place, in Python, and the C++
// terminal lane therefore priced a frozen scalar: one vol per name, chosen once
// at the exercise and knock-in moneyness before the path loop started. Two
// implementations of a model is a silent way to make the two lanes disagree, so
// the port is verified by `tests/test_locvol.py`, which diffs a grid of
// sigma(t, S) printed by this header against the Python `LocalVolSurface`.
//
// A flat implied surface collapses sigma_LV back to the constant implied vol, so
// the old scalar lane is the special case of this one. One exception is
// deliberate: a *zero* implied surface produces sigma_LV = kClampLo, not 0,
// because 0/1 = 0 falls to the defensive floor. Callers that need the old
// behaviour must not build this surface at all.
//
// QuantLib is not used here, deliberately. Its `LocalVolSurface`
// (`ql::LocalVolSurface`, Gatheral's "Volatility Surface" formula 1.10) is a
// different object from this one: it is documented in its own header as
// "untested, probably unreliable", its `d2wdy2` blows up in the wings, and
// QuantLib answers that with `NoExceptLocalVolSurface`, which swallows the
// exception and returns a stand-in number. This lane's whole contract is that it
// refuses rather than guesses, so an error-swallowing approximation of the model
// is the wrong dependency. The exact transform also has to match the Python
// lane's numbers to be comparable with it, and only this port does.
//
// Like the other block headers, this one is header-only and carries no JSON
// dependency, so `block/locvol` can test it without the -ffast-math policy the
// library uses. A block test asserts specified arithmetic.

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <stdexcept>
#include <string>
#include <vector>

namespace fina::risk::vol {

// Dense absolute-strike axis size, and the defensive clamp on sigma_LV. These
// are the same numbers as locvol.py's GRID_X / LV_CLAMP_LO / LV_CLAMP_HI, and
// they are load-bearing: changing either one moves the PV, so they are part of
// the specification rather than a tuning knob.
inline constexpr int kGridX = 96;
inline constexpr double kClampLo = 0.10;
inline constexpr double kClampHi = 3.00;

// The `eqVol` payload as delivered: a sparse [tenor][absolute strike] grid in
// percentage points. Orientation is resolved in the constructor, not here.
struct ImpliedVolGrid {
    std::string id;
    std::vector<double> strike;
    std::vector<double> maturity;  // Excel serials, as delivered.
    std::vector<std::vector<double>> vol;
};

// np.clip: propagates NaN the way numpy does, which std::min/std::max do not
// (they are specified to return the non-NaN operand under fmin/fmax semantics).
// Here the only NaNs are the ones a caller must see, so they must survive.
template <typename T>
inline T np_clip(T value, T lo, T hi) noexcept {
    return value < lo ? lo : (hi < value ? hi : value);
}

inline double np_max(double value, double lo) noexcept {
    return value < lo ? lo : value;
}

// Second derivatives of the natural cubic spline through (x, y).
//
// Solves the tridiagonal system locvol.py hands to np.linalg.solve:
//   diag  1 at both ends, 2*(h[i-1] + h[i]) inside
//   sub   h[i] on row i, super h[i+1] on row i
//   rhs   6*((y[i+1] - y[i])/h[i] - (y[i] - y[i-1])/h[i-1]) inside, 0 at both ends
// The matrix is symmetric with positive pivots, so the Thomas algorithm is
// stable here and agrees with the dense solve to rounding.
inline std::vector<double> natural_second_derivatives(const std::vector<double>& x,
                                                      const std::vector<double>& y) {
    const std::size_t n = x.size();
    std::vector<double> second(n, 0.0);
    if (n < 3 || y.size() != n) return second;
    std::vector<double> h(n - 1);
    bool usable = true;
    for (std::size_t i = 0; i + 1 < n; ++i) {
        h[i] = x[i + 1] - x[i];
        if (!(h[i] > 0.0)) usable = false;
    }
    for (std::size_t i = 0; i < n; ++i)
        if (!std::isfinite(y[i])) usable = false;
    if (!usable) return second;

    // The tridiagonals, filled to match numpy's construction exactly:
    //   sub = zeros(n), with sub[i] = h[i] for i in 1..n-2 and 0 at both ends
    //   m   = diag(diag) + diag(sub[1:], +1) + diag(sub[1:], -1)
    // np.diag(sub[1:], +1) puts sub[i+1] at m[i, i+1], and np.diag(sub[1:], -1)
    // puts sub[i] at m[i, i-1]. So the interior off-diagonals are h[i+1] above
    // and h[i] below, and the two end couplings are NOT part of that pattern:
    // m[0, 1] = sub[1] = h[1], while m[n-1, n-2] = sub[n-1] = 0.
    //
    // Filling the off-diagonals from the interior loop alone -- the obvious way
    // to write this -- therefore drops m[0, 1] and reads h[n-1] for the top
    // right corner, one past the end of a length-(n-1) array. That is what the
    // first draft did, and it moved sigma by ~1e-3 relative while still passing
    // every structural check, because the matrix stays symmetric and
    // diagonally dominant either way. The ends are set explicitly below.
    std::vector<double> lower(n, 0.0), diag(n, 0.0), upper(n, 0.0), rhs(n, 0.0);
    diag.front() = 1.0;
    diag.back() = 1.0;
    for (std::size_t i = 1; i + 1 < n; ++i) {
        diag[i] = 2.0 * (h[i - 1] + h[i]);
        lower[i] = h[i];
        if (i + 2 < n) upper[i] = h[i + 1];
        rhs[i] = 6.0 * ((y[i + 1] - y[i]) / h[i] - (y[i] - y[i - 1]) / h[i - 1]);
    }
    if (n > 2) upper[0] = h[1];
    for (std::size_t i = 1; i < n; ++i) {
        const double factor = lower[i] / diag[i - 1];
        diag[i] -= factor * upper[i - 1];
        rhs[i] -= factor * rhs[i - 1];
    }
    second[n - 1] = diag[n - 1] == 0.0 ? 0.0 : rhs[n - 1] / diag[n - 1];
    for (std::size_t i = n - 1; i-- > 0;) {
        second[i] = diag[i] == 0.0 ? 0.0 : (rhs[i] - upper[i] * second[i + 1]) / diag[i];
    }
    return second;
}

// Value, first and second derivative of the natural spline at one point.
struct SplineTriple {
    double value = 0.0;
    double first = 0.0;
    double second = 0.0;
};

inline SplineTriple spline_eval(const std::vector<double>& x, const std::vector<double>& y,
                                const std::vector<double>& y2, double xp) {
    const std::size_t n = x.size();
    SplineTriple out;
    if (n < 2) return out;
    // np.searchsorted(x, xp) - 1, clipped to the last usable segment.
    auto upper_it = std::lower_bound(x.begin(), x.end(), xp);
    std::size_t seg = static_cast<std::size_t>(upper_it - x.begin());
    seg = seg == 0 ? 0 : seg - 1;
    if (seg > n - 2) seg = n - 2;
    const double h = x[seg + 1] - x[seg];
    if (h == 0.0) return out;
    const double t = np_clip((xp - x[seg]) / h, 0.0, 1.0);
    const double one_minus_t = 1.0 - t;
    out.value = one_minus_t * y[seg] + t * y[seg + 1] +
                (h * h / 6.0) * ((t * t * t - t) * y2[seg] +
                                 (one_minus_t * one_minus_t * one_minus_t - one_minus_t) * y2[seg + 1]);
    out.first = (y[seg + 1] - y[seg] +
                 (h * h / 6.0) * ((3.0 * t * t - 1.0) * y2[seg] -
                                  (3.0 * one_minus_t * one_minus_t - 1.0) * y2[seg + 1])) /
                h;
    out.second = one_minus_t * y2[seg + 1] + t * y2[seg];
    return out;
}

// sigma_LV(t, S) for one underlying.
//
// Construction, in the order locvol.py builds it:
//   1. order the tenors, convert to years from the evaluation serial
//   2. a dense absolute-strike axis, log-spaced from 0.55x to 1.05x the
//      delivered strike range
//   3. per tenor, a natural cubic spline of the implied smile in log moneyness
//      x = ln(K / F(t)), F(t) = spot * exp(rate * t)
//   4. total variance w = sigma_imp^2 * t, and dx w, dxx w from the spline
//   5. dT w by central difference along the tenor axis
//   6. the Dupire denominator
//        1 - (x/w) dx w + 1/4 (-1/4 - 1/w) (dx w)^2 + 1/2 dxx w
//      and sigma_LV^2 = dT w / that, floored where the denominator is not
//      finite and clamped to [kClampLo, kClampHi]
//   7. sigma_LV(t, S): linear in tenor, log-linear in strike
class LocalVolSurface {
public:
    LocalVolSurface(const ImpliedVolGrid& grid, int eval_serial, double spot, double rate) {
        const std::size_t n_strike = grid.strike.size();
        const std::size_t n_tenor = grid.maturity.size();
        if (n_strike == 0 || n_tenor == 0 || grid.vol.empty())
            throw std::invalid_argument("eqVol surface for " + grid.id + " has no vol grid");
        if (spot <= 0.0) throw std::invalid_argument("eqVol surface for " + grid.id + " needs a positive spot");

        // Orientation: locvol.py takes the grid as-is when the row count already
        // equals the tenor count, and transposes otherwise. A grid matching
        // neither is malformed, and guessing which axis it meant is exactly the
        // class of silent wrong-number this lane refuses to produce.
        //
        // Either way the grid is read as n_tenor rows of n_strike values, so the
        // inner loop is always over the *strike* count. Sizing it off the row
        // length instead is the subtle bug here: a transposed grid's rows are
        // tenors long, so a strike-length loop would silently read the wrong
        // number of values and the spline would collapse to a straight line.
        const bool rows_are_tenors = grid.vol.size() == n_tenor;
        const bool cols_are_tenors = grid.vol.front().size() == n_tenor;
        if (!rows_are_tenors && !cols_are_tenors)
            throw std::invalid_argument("eqVol surface for " + grid.id + " is " +
                                        std::to_string(grid.vol.size()) + "x" +
                                        std::to_string(grid.vol.front().size()) + " against " +
                                        std::to_string(n_tenor) + " tenors and " + std::to_string(n_strike) +
                                        " strikes");
        const std::size_t expected_row_len = rows_are_tenors ? n_strike : n_tenor;
        for (const auto& row : grid.vol)
            if (row.size() != expected_row_len)
                throw std::invalid_argument("eqVol surface for " + grid.id + " has a row of " +
                                            std::to_string(row.size()) + ", expected " +
                                            std::to_string(expected_row_len));

        std::vector<std::size_t> order(n_tenor);
        for (std::size_t k = 0; k < n_tenor; ++k) order[k] = k;
        // Stable, where numpy's default argsort is not. It only matters if the
        // payload repeats a tenor, which is a defect either way; being
        // deterministic is worth more here than matching an unstable sort.
        std::stable_sort(order.begin(), order.end(), [&grid](std::size_t a, std::size_t b) {
            return grid.maturity[a] < grid.maturity[b];
        });

        t_axis_.reserve(n_tenor);
        for (std::size_t k = 0; k < n_tenor; ++k)
            t_axis_.push_back((grid.maturity[order[k]] - static_cast<double>(eval_serial)) / 365.0);

        const double strike_lo = std::log(std::max(*std::min_element(grid.strike.begin(), grid.strike.end()) * 0.55, 1e-6));
        const double strike_hi = std::log(*std::max_element(grid.strike.begin(), grid.strike.end()) * 1.05);
        const double step = (strike_hi - strike_lo) / static_cast<double>(kGridX - 1);
        s_axis_.reserve(kGridX);
        for (int i = 0; i < kGridX; ++i) s_axis_.push_back(std::exp(strike_lo + i * step));
        log_s_.reserve(kGridX);
        for (double level : s_axis_) log_s_.push_back(std::log(level));

        const std::size_t n_s = static_cast<std::size_t>(kGridX);
        std::vector<std::vector<double>> w(n_tenor, std::vector<double>(n_s));
        std::vector<std::vector<double>> wx(n_tenor, std::vector<double>(n_s));
        std::vector<std::vector<double>> wxx(n_tenor, std::vector<double>(n_s));
        std::vector<std::vector<double>> x_dense(n_tenor, std::vector<double>(n_s));

        for (std::size_t k = 0; k < n_tenor; ++k) {
            std::vector<double> y(n_strike);
            for (std::size_t j = 0; j < n_strike; ++j)
                y[j] = (rows_are_tenors ? grid.vol[order[k]][j] : grid.vol[j][order[k]]) / 100.0;
            const double forward = spot * std::exp(rate * t_axis_[k]);
            std::vector<double> x_k(n_strike);
            for (std::size_t j = 0; j < n_strike; ++j) x_k[j] = std::log(grid.strike[j] / forward);
            const std::vector<double> y2 = natural_second_derivatives(x_k, y);
            const double x_lo = *std::min_element(x_k.begin(), x_k.end());
            const double x_hi = *std::max_element(x_k.begin(), x_k.end());
            const double tk = t_axis_[k];
            for (std::size_t i = 0; i < n_s; ++i) {
                const double xd = np_clip(std::log(s_axis_[i] / forward), x_lo, x_hi);
                const SplineTriple s = spline_eval(x_k, y, y2, xd);
                w[k][i] = s.value * s.value * tk;
                wx[k][i] = 2.0 * s.value * tk * s.first;
                wxx[k][i] = 2.0 * tk * (s.first * s.first + s.value * s.second);
                x_dense[k][i] = xd;
            }
        }

        v_.assign(n_tenor, std::vector<double>(n_s, kClampLo));
        for (std::size_t k = 0; k < n_tenor; ++k) {
            for (std::size_t i = 0; i < n_s; ++i) {
                // dT w by central difference along the tenor axis, one-sided at
                // the two ends. With a single tenor there is no difference to
                // take, so it degrades to w / t.
                double dw_dt = 0.0;
                if (k == 0) {
                    if (n_tenor == 1)
                        dw_dt = w[k][i] / std::max(t_axis_[k], 1e-9);
                    else
                        dw_dt = (w[1][i] - w[0][i]) / std::max(t_axis_[1] - t_axis_[0], 1e-9);
                } else if (k + 1 == n_tenor) {
                    dw_dt = (w[k][i] - w[k - 1][i]) / std::max(t_axis_[k] - t_axis_[k - 1], 1e-9);
                } else {
                    dw_dt = (w[k + 1][i] - w[k - 1][i]) / std::max(t_axis_[k + 1] - t_axis_[k - 1], 1e-9);
                }
                const double w_safe = np_max(w[k][i], 1e-12);
                const double denominator = 1.0 - (x_dense[k][i] / w_safe) * wx[k][i] +
                                           0.25 * (-0.25 - 1.0 / w_safe) * wx[k][i] * wx[k][i] +
                                           0.5 * wxx[k][i];
                // Ported from locvol.py's `where=np.isfinite(denominator)`. It is
                // kept for fidelity with the Python lane, not because it is
                // reachable: wx and wxx are products of finite spline derivatives
                // and a finite tenor, so the denominator cannot be non-finite on
                // a grid this constructor accepted. No test reaches this branch,
                // and deleting it is verified not to change any output on the
                // real surfaces. It stays because a future caller may build a
                // LocalVolSurface from a surface this constructor does not
                // validate, and a silent inf there would propagate into the
                // path loop as a NaN spot.
                const double lv2 = std::isfinite(denominator) ? dw_dt / denominator : kClampLo * kClampLo;
                v_[k][i] = std::sqrt(np_clip(lv2, kClampLo * kClampLo, kClampHi * kClampHi));
            }
        }
    }

    // The interpolated local-vol row over the dense strike axis, linear in tenor
    // and clamped to the fitted tenor range.
    std::vector<double> row(double t) const {
        if (t_axis_.size() == 1) return v_[0];
        const double tc = np_clip(t, t_axis_.front(), t_axis_.back());
        auto it = std::lower_bound(t_axis_.begin(), t_axis_.end(), tc);
        std::size_t i = static_cast<std::size_t>(it - t_axis_.begin());
        i = i == 0 ? 0 : i - 1;
        if (i > t_axis_.size() - 2) i = t_axis_.size() - 2;
        const double span = t_axis_[i + 1] - t_axis_[i];
        const double weight = span > 0.0 ? (tc - t_axis_[i]) / span : 0.0;
        std::vector<double> out(v_[i].size());
        for (std::size_t s = 0; s < out.size(); ++s) out[s] = (1.0 - weight) * v_[i][s] + weight * v_[i + 1][s];
        return out;
    }

    // Local vol at year `t` for spot `level`. Log-linear in the strike, which is
    // what makes the surface's moneyness sense preserved across spot levels.
    double sigma(double t, double level) const {
        const std::vector<double> local_row = row(t);
        if (local_row.empty()) return 0.0;
        const double lv = std::log(level);
        if (lv <= log_s_.front()) return local_row.front();
        if (lv >= log_s_.back()) return local_row.back();
        auto it = std::lower_bound(log_s_.begin(), log_s_.end(), lv);
        const std::size_t j = static_cast<std::size_t>(it - log_s_.begin()) - 1;
        if (j + 1 >= local_row.size()) return local_row.back();
        const double slope = (local_row[j + 1] - local_row[j]) / (log_s_[j + 1] - log_s_[j]);
        return slope * (lv - log_s_[j]) + local_row[j];
    }

    const std::vector<double>& strike_axis() const noexcept { return s_axis_; }
    const std::vector<double>& log_strike_axis() const noexcept { return log_s_; }
    const std::vector<double>& tenor_axis() const noexcept { return t_axis_; }
    const std::vector<std::vector<double>>& grid() const noexcept { return v_; }

private:
    std::vector<double> t_axis_;
    std::vector<double> s_axis_;
    std::vector<double> log_s_;
    std::vector<std::vector<double>> v_;
};

}  // namespace fina::risk::vol
