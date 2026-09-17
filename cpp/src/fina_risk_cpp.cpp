#include "fina_risk_cpp.hpp"
#include "fina_risk/engine.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <numeric>
#include <random>
#include <stdexcept>
#include <unordered_map>

#ifdef _OPENMP
#include <omp.h>
#endif

#include <nlohmann/json.hpp>

using json = nlohmann::json;

// ============================================================================
// fina_risk_cpp.cpp — native non-MCP risk/benchmark kernel
// ----------------------------------------------------------------------------
// This translation unit implements the C++ side of the fina-risk MCP service.
// It owns no MCP transport: it is driven through the pybind11 bindings in
// `bindings/` (imported as `fina_risk_cpp`) or compiled as a standalone CLI.
//
// DESIGN OVERVIEW — two pricing lanes
//   1. price_fixture / price_terminal_legs — deal-level fixture pricing for the
//      legacy/job JSON schema. Reads one deal's `dealData` + `marketData`, runs
//      a correlated GBM Monte-Carlo (or consumes caller-supplied terminal
//      spots) and decomposes the valuation into PUT / FUNDING / COUPON legs.
//      This is the lane that exercises the term-sheet features documented
//      below. Its PV is consumed by the MCP `price_fixture` tool and by the
//      bump-revaluation Greeks in `pricing.py`.
//   2. run_benchmark / run_cpp_parity — high-cardinality parity lanes. Both
//      price the *same* float32 terminal cube so the C++ and NumPy kernels see
//      identical paths (shared common random numbers). run_benchmark is the
//      standalone C++ reference that builds its own factor-model cube via
//      terminal_market; run_cpp_parity is the production parity kernel that
//      consumes the corpus cube pre-built by `build_benchmark_corpus`, so its
//      `seed` argument is a no-op (the CRN contract is "one cube, one seed,
//      both lanes"; see cpp_parity.py).
//
// SHARED CRN / PATH / PAYOFF STRUCTURE
//   - The *terminal cube* is a flat float array `[path][underlying]`, one row
//     per scenario, one column per reference asset. Every pricing path reduces
//     the cube to a per-path *performance ratio* `spot(t)/ref` and a per-basket
//     *worst-of* minimum.
//   - *Shared CRNs*: the cube is built once outside this file and reused for
//     (a) the base price, (b) every up/down bump revaluation used to form
//     delta/gamma by finite differences, and (c) the final parallel-basket
//     shock. No new randomness is drawn inside the parity loop, so the Greeks
//     and the Taylor P&L are measured on exactly the same scenario set — this
//     is the `CRN_BUMP_REVALUE_WITH_PATHWISE_DELTA` method reported by the MCP.
//   - *Payoff structure*: instruments are compiled at load time from their
//     `underlyings` ids and `legs[0].payoff.strike` into a `(basket indices,
//     strike)` tuple. The per-trade PV contribution is `1 - put` with
//     `put = mean(max(strike - worst_ratio, 0))` — a short worst-of put, which
//     is the embedded knock-in option of the term sheet (see below).
//
// TERM-SHEET FEATURES (see skills/fina-risk/refs/termsheet1.md and
// termsheet1.md.json for the full deal geometry)
// The product is a USD non-principal-protected ELI linked to a worst-of basket
// with a Daily Memory Callable feature and a Final-Fixing-Date Knock-in. The
// machine-readable deal carries the feature switches under `dealData`:
//   - KIKOSelect.knockIn=true, knockInType="EKI" (European knock-in observed
//     only on the final fixing date), ITMPayment="Delivery" (physical delivery
//     of the worst-performing asset on knock-in).
//   - KIKOSelect.localKO=false / globalKO=true, with the memory-lock carry
//     GKOLocked[] + GKODate[]. "global KO" means the daily auto-call condition
//     fires when *all* basket assets are locked; "local KO" would be per-asset.
//     The model consumes the *current lock state* (via the N1/N2 coupon carry)
//     rather than simulating the daily lock path day by day.
//   - knockInStar.KIBarrier=0.70 (knock-in price, 70% of initial) and
//     strikeKI2=0.78 == maturBarrier (exercise price, 78% of initial).
//   - RGACCLKO (the accrual/cash-dividend ledger): per-period accruRate,
//     endDate/paymentDate schedules, gblBarPrice=1.10 (the 110% call/memory
//     barrier), N1 (coupon already paid / locked) and N2 (total coupons due).
// How the code takes care of each feature:
//   - Worst-of basket .......... every kernel takes min() over the basket
//     performance ratios.
//   - EKI knock-in at 78% ...... the fixture lane observes the worst-of ratio on
//     the final fixing date only and pays max(strikeKI2 - worst, 0) *only* when
//     worst <= KIBarrier (European knock-in). This is the faithful, conservative
//     gate: paths that never touch the knock-in barrier receive nothing.
//   - KIBarrier 70% ............ the runtime knock-in gate in `price_fixture`.
//   - Vol surface .............. `price_fixture` interpolates the full eqVol
//     grid (linear in strike; linear in total variance across maturities) at the
//     exercise *and* knock-in moneyness and keeps the higher (downside-wing)
//     vol per underlying, rather than a single ATM point.
//   - NYSE calendar ............ fixing/observation dates come from
//     `nyse_schedule` (Mon-Fri minus US market holidays), not every weekday.
//   - Curve / dividends ........ the discount rate is interpolated to the option
//     expiry and the GBM drift is (r - q) with q from the equity cash-dividend
//     schedule.
//   - Memory coupon N1/N2 ...... the RGACCLKO loop in price_fixture pays
//     (N2 - N1)/N2 of each period's accruRate, scaled by the note denomination
//     and discounted from paymentDate — the "accrued unpaid coupon" behaviour
//     of a memory callable.
//   - global/local KO .......... the daily auto-call requires *all* underlyings
//     at/above the 110% call barrier on the same fixing. Pre-locked memory state
//     (GKOLocked) is intentionally NOT credited: under-detecting calls only
//     raises the short-put reserve, which is the conservative sell-side choice.
// ============================================================================
// MODERN C++ QUICK REFERENCE (readers catching up on C++11 -> C++20)
// This file is intentionally small but exercises the post-2011 idioms that
// replaced older C++98 style. Key ones, each marked inline at its site:
//
//   * Type aliases: `using json = nlohmann::json;` (C++11) replaces `typedef`
//     and is the usual way to bring a third-party type into scope.
//   * `auto` deduction + `const` correctness: `const auto&` when a value is
//     only read. The compiler deduces the type — you never spell long template
//     types twice. No manual memory management: every `std::vector`/`std::string`
//     is a RAII owner (no `new`/`delete`, no raw pointers on the hot path).
//   * Anonymous namespace `namespace { }` (C++11): internal linkage for the
//     helpers in this file — the modern replacement for file-scope `static`.
//   * `inline` free functions: since C++17 this also means "ODR-safe if
//     defined in more than one translation unit"; harmless here and signals
//     "cheap, eligible for inlining".
//   * `<random>` (C++11): `std::mt19937_64` (a seeded 64-bit Mersenne Twister)
//     + `std::normal_distribution` replace the legacy `rand()`/`srand()`
//     global state (not thread-safe, poor quality, C-style).
//   * Range-based `for (const auto& x : v)` (C++11): no manual
//     index/iterator arithmetic where it adds nothing.
//   * `std::vector` as the universal owning buffer: `.at(i)` is bounds-checked
//     (throws), `operator[]` is unchecked hot-loop access; `std::vector<T>(n)`
//     value-initializes (zeros) every element; `.reserve(n)` pre-grows to avoid
//     reallocations; `.emplace()`/`push_back(std::move(x))` avoid copies.
//   * Brace-initialization / aggregate init `return {a, b, {{...}}}`: builds a
//     struct (even nested) directly. Since C++17 returning a prvalue like this
//     is *guaranteed copy elision* — constructed directly into the caller's
//     memory, zero copies or moves.
//   * `std::chrono::steady_clock` — the monotonic clock for elapsed-time
//     measurement; immune to wall-clock/NTP jumps (system_clock is not).
//   * `static_cast<T>(x)` — the explicit, compiler-checked cast used whenever a
//     numeric conversion must be visible (e.g. signed loop indices for OpenMP).
//   * Exceptions instead of error codes: `throw std::invalid_argument` /
//     `std::runtime_error`; RAII guarantees all locals (vectors, file streams)
//     are destroyed on unwinding — no manual cleanup, no `errno` plumbing.
//   * `#pragma omp parallel for reduction(+:sum)`: compiler-generated thread
//     partition + per-thread partials + thread-safe combine, so the hot loops
//     carry no locks. Loop counters are cast to signed `std::int64_t` because
//     OpenMP requires a *signed* loop index.
//   * `const auto& node = obj.at("key");` uses *lifetime extension*: a
//     temporary bound to a `const&` lives as long as the reference, so JSON
//     chains can be walked safely without owning the intermediates.
//     (`auto node = ...` would deep-copy the node instead.)
//   * `(void)seed;` — canonical way to silence an "unused parameter" warning
//     when an ABI-stable function must keep a parameter it does not use.
// ============================================================================

namespace fina::risk {
// (C++17) nested namespace definition: shorthand for
// `namespace fina { namespace risk { ... } }`.
namespace {
// (C++11) anonymous namespace: these helpers have internal linkage (visible
// only in this translation unit) — the modern replacement for file-scope
// `static`, and it lets the compiler inline-optimize freely.

inline std::uint64_t next_u64(std::uint64_t& state) {
    // (inline, C++17) the `inline` keyword marks this as "cheap, defined once"
    // so the compiler may inline it everywhere; it is no longer a "hint" that
    // requires a separate non-inline definition.
    //
    // SplitMix64 mixing step: a bijective 64-bit scramble (xorshift triple +
    // final integer multiply) used to derive per-path RNG states cheaply and
    // reproducibly from a single master seed. `ULL` marks a 64-bit literal.
    state ^= state >> 12;
    state ^= state << 25;
    state ^= state >> 27;
    return state * 2685821657736338717ULL;
}

inline float fast_normal(std::uint64_t& state) {
    // Irwin-Hall approximation: twelve uniforms have near-normal shape without
    // transcendental calls. This is reserved for the benchmark path generator;
    // production QuantLib/XAD builds provide the configured RNG/distribution.
    //
    // (modern numeric idiom) `(next_u64(state) >> 11) * (1.0/2^53)` maps the
    // *top* 53 bits of the 64-bit state into [0, 1): `>> 11` discards the low
    // 11 bits and keeps the high-entropy bits, and multiplying by the
    // reciprocal constant is faster than dividing. (2^53 is the largest power
    // of two representable exactly in an IEEE-754 double, so every produced
    // value is a distinct representable number.)
    float sum = 0.0F;
    for (int i = 0; i < 12; ++i)
        sum += static_cast<float>((next_u64(state) >> 11) * (1.0 / 9007199254740992.0));
    return sum - 6.0F;
}

double get_number(const json& value, const char* key, double fallback) {
    // (modern JSON access for optional fields) nlohmann's `.contains()` +
    // `.is_number()` turns "field may be absent" into a safe default instead
    // of an exception; `.get<double>()` exposes the typed conversion. The
    // `const json&` / `const char*` parameters avoid copying nodes/strings.
    return value.contains(key) && value[key].is_number() ? value[key].get<double>() : fallback;
}

// ---------------------------------------------------------------------------
// Linear interpolation on a sorted x-grid (flat extrapolation at both ends).
// Shared by the vol-surface (strike / maturity) and discount-curve reads.
// ---------------------------------------------------------------------------
inline double linear_interp(const std::vector<double>& xs, const std::vector<double>& ys, double x) {
    if (xs.empty() || ys.empty()) return 0.0;
    if (x <= xs.front()) return ys.front();
    if (x >= xs.back()) return ys.back();
    for (std::size_t k = 1; k < xs.size() && k < ys.size(); ++k) {
        if (x <= xs[k]) {
            const double span = xs[k] - xs[k - 1];
            const double t = span == 0.0 ? 0.0 : (x - xs[k - 1]) / span;
            return ys[k - 1] + t * (ys[k] - ys[k - 1]);
        }
    }
    return ys.back();
}

// ---------------------------------------------------------------------------
// US (NYSE) trading calendar. The term sheet's underlyings declare
// `"calendar": "NYSE"`, so fixing/observation dates must exclude exchange
// holidays rather than counting every Mon-Fri weekday. Serial dates are the
// Excel 1900 system (same as Python's `date(1899,12,30) + serial`).
// ---------------------------------------------------------------------------
struct CivilDate {
    int year{};
    int month{};
    int day{};
};

inline CivilDate civil_from_serial(int serial) {
    // Howard Hinnant's days_from_civil inverse; z is days since 1970-01-01.
    const long z = static_cast<long>(serial) - 25569L + 719468L;
    const long era = (z >= 0 ? z : z - 146096L) / 146097L;
    const unsigned doe = static_cast<unsigned>(z - era * 146097L);
    const unsigned yoe = (doe - doe / 1460U + doe / 36524U - doe / 146096U) / 365U;
    const int year = static_cast<int>(yoe) + static_cast<int>(era) * 400;
    const unsigned doy = doe - (365U * yoe + yoe / 4U - yoe / 100U);
    const unsigned mp = (5U * doy + 2U) / 153U;
    const unsigned day = doy - (153U * mp + 2U) / 5U + 1U;
    const unsigned month = mp + (mp < 10U ? 3U : static_cast<unsigned>(-9));
    return {year + (month <= 2U ? 1 : 0), static_cast<int>(month), static_cast<int>(day)};
}

inline long days_from_civil(int year, unsigned month, unsigned day) {
    year -= month <= 2U;
    const long era = (year >= 0 ? year : year - 399) / 400;
    const unsigned yoe = static_cast<unsigned>(year - era * 400);
    const unsigned mp = month > 2U ? month - 3U : month + 9U;
    const unsigned doy = (153U * mp + 2U) / 5U + day - 1U;
    const unsigned doe = yoe * 365U + yoe / 4U - yoe / 100U + doy;
    return era * 146097L + static_cast<long>(doe) - 719468L;
}

inline int serial_from_ymd(int year, int month, int day) {
    return static_cast<int>(days_from_civil(year, static_cast<unsigned>(month), static_cast<unsigned>(day)) + 25569L);
}

inline int weekday_mon0(int serial) {  // 0=Mon .. 6=Sun (1970-01-01 was a Thursday)
    const long z = static_cast<long>(serial) - 25569L;
    return static_cast<int>(((z % 7) + 10) % 7);
}

inline int observed_weekday(int year, int month, int day) {
    const int serial = serial_from_ymd(year, month, day);
    const int w = weekday_mon0(serial);
    if (w == 5) return serial - 1;  // Saturday -> preceding Friday
    if (w == 6) return serial + 1;  // Sunday -> following Monday
    return serial;
}

inline int nth_weekday_of_month(int year, int month, int n, int target) {
    const int first = serial_from_ymd(year, month, 1);
    const int delta = (target - weekday_mon0(first) + 7) % 7;
    return first + delta + (n - 1) * 7;
}

inline int last_weekday_of_month(int year, int month, int target) {
    const int next_month = month == 12 ? 1 : month + 1;
    const int next_year = month == 12 ? year + 1 : year;
    const int last = serial_from_ymd(next_year, next_month, 1) - 1;
    return last - ((weekday_mon0(last) - target + 7) % 7);
}

inline int easter_sunday(int year) {
    // Anonymous Gregorian computus.
    const int a = year % 19, b = year / 100, c = year % 100, d = b / 4, e = b % 4;
    const int f = (b + 8) / 25, g = (b - f + 1) / 3, h = (19 * a + b - d - g + 15) % 30;
    const int i = c / 4, k = c % 4, l = (32 + 2 * e + 2 * i - h - k) % 7;
    const int m = (a + 11 * h + 22 * l) / 451;
    const int month = (h + l - 7 * m + 114) / 31;
    const int day = ((h + l - 7 * m + 114) % 31) + 1;
    return serial_from_ymd(year, month, day);
}

inline bool is_us_market_holiday(int serial) {
    const CivilDate c = civil_from_serial(serial);
    const int y = c.year;
    if (serial == observed_weekday(y, 1, 1)) return true;              // New Year's Day
    if (serial == nth_weekday_of_month(y, 1, 3, 0)) return true;        // MLK Day
    if (serial == nth_weekday_of_month(y, 2, 3, 0)) return true;        // Washington's Birthday
    if (serial == easter_sunday(y) - 2) return true;                    // Good Friday
    if (serial == last_weekday_of_month(y, 5, 0)) return true;          // Memorial Day
    if (y >= 2022 && serial == observed_weekday(y, 6, 19)) return true; // Juneteenth
    if (serial == observed_weekday(y, 7, 4)) return true;               // Independence Day
    if (serial == nth_weekday_of_month(y, 9, 1, 0)) return true;        // Labor Day
    if (serial == nth_weekday_of_month(y, 11, 4, 3)) return true;       // Thanksgiving
    if (serial == observed_weekday(y, 12, 25)) return true;             // Christmas
    return false;
}

inline std::vector<int> nyse_schedule(int start, int end) {
    std::vector<int> dates;
    for (int serial = start; serial <= end; ++serial) {
        if (weekday_mon0(serial) <= 4 && !is_us_market_holiday(serial)) dates.push_back(serial);
    }
    return dates;
}

// ---------------------------------------------------------------------------
// Full eqVol surface read. The legacy grid is [maturity][strike] in percent.
// Interpolate linearly across strikes and, across maturities, linearly in
// *total variance* (sigma^2 * T) so the term structure is variance-consistent.
// The conservative sell-side pick is the higher (downside-wing) of the vol at
// the exercise and knock-in strikes.
// ---------------------------------------------------------------------------
inline double surface_vol(const json& surface, double strike_abs, int target_maturity, int eval) {
    const auto strikes_json = surface.value("strike", json::array());
    const auto maturities_json = surface.value("maturity", json::array());
    const auto values = surface.value("vol", json::array());
    if (strikes_json.empty() || values.empty()) return 0.45;
    std::vector<double> strikes;
    for (const auto& x : strikes_json) strikes.push_back(x.get<double>());
    std::vector<double> row_vol;
    std::vector<double> maturities;
    for (const auto& x : maturities_json) maturities.push_back(x.get<double>());
    for (const auto& row : values) {
        std::vector<double> r;
        for (const auto& x : row) r.push_back(x.get<double>() / 100.0);
        row_vol.push_back(linear_interp(strikes, r, strike_abs));
    }
    if (maturities.size() != row_vol.size() || maturities.size() < 2) {
        return row_vol.empty() ? 0.45 : row_vol.front();
    }
    std::vector<double> times, total_variance;
    for (std::size_t k = 0; k < maturities.size(); ++k) {
        const double t = std::max((maturities[k] - eval) / 365.0, 1e-6);
        times.push_back(t);
        total_variance.push_back(row_vol[k] * row_vol[k] * t);
    }
    const double target_t = std::max((target_maturity - eval) / 365.0, 1e-6);
    const double w = linear_interp(times, total_variance, target_t);
    return std::sqrt(std::max(w, 1e-12) / target_t);
}

inline double interp_curve_rate(const json& disc_curves, int target_maturity, int eval) {
    if (!disc_curves.is_array() || disc_curves.empty()) return 0.0;
    const auto curve = disc_curves.at(0).value("curve", json::array());
    if (curve.empty()) return 0.0;
    std::vector<double> times, rates;
    for (const auto& pillar : curve) {
        times.push_back((pillar.value("date", eval) - eval) / 365.0);
        rates.push_back(pillar.value("rate", 0.0));
    }
    return linear_interp(times, rates, std::max((target_maturity - eval) / 365.0, 0.0));
}

std::vector<float> terminal_market(const json& market, std::size_t paths, std::uint64_t seed) {
    // Standalone factor-model terminal cube generator used by run_benchmark.
    // Design:
    //   - Cross-asset dependence comes from a shared factor-loading matrix
    //     [n x factorCount] ~ N(0, 0.08) drawn once from a master mt19937_64.
    //     There is no explicit covariance matrix: correlated spots emerge from
    //     each underlying being a different linear combination of the same
    //     common factors, and `factors` doubles as the cube's rank proxy.
    //   - Path independence: every path derives its own RNG state via
    //     `seed ^ (golden_ratio * (p+1))`, so path rows are reproducible in any
    //     thread/order under the OpenMP static schedule (see loop below) —
    //     this keeps the wall-clock build bit-identical run to run.
    //   - Per-asset vol is a deterministic pseudo-value in [0.15, 0.60] instead
    //     of an explicitly supplied surface, keeping the standalone reference
    //     lane self-contained (the parity lane consumes the richer in-memory
    //     corpus cube from Python instead).
    //   - fast_normal (Irwin–Hall) supplies the standard normals; this is the
    //     benchmark-only approximation — the production QuantLib/XAD build
    //     provides the configured RNG/distribution.
    const auto underlyings = market.at("underlyings");
    const std::size_t n = underlyings.size();
    const std::size_t factors = market.at("simulation").value("factorCount", 12U);
    // (modern) <random>: `mt19937_64` is a seeded, reproducible, non-global
    // engine; `normal_distribution` maps it to N(0,1). `seed` makes a run
    // bit-identical to replay; nothing here touches the deprecated rand().
    std::mt19937_64 rng(seed);
    std::normal_distribution<double> normal(0.0, 1.0);
    // (modern) `std::vector<float>(n)` *value-initializes* every element to
    // 0.0f (a guaranteed property of the fill-constructor), so the range-for
    // below overwrites fully-typed storage with no prior uninitialized read.
    std::vector<float> loadings(n * factors);
    for (auto& value : loadings) value = static_cast<float>(normal(rng) * 0.08);
    std::vector<float> spot_values(n);
    std::vector<float> vol_values(n);
    for (std::size_t u = 0; u < n; ++u) {
        spot_values[u] = static_cast<float>(get_number(underlyings[u], "spot", 100.0));
        vol_values[u] = 0.15F + 0.45F * static_cast<float>((u * 17) % 101) / 100.0F;
    }
    std::vector<float> result(paths * n);
#pragma omp parallel for schedule(static)
    // (OpenMP idiom) loop counters must be *signed* for OpenMP to compute the
    // iteration partition, so we iterate over `std::int64_t signed_p` and cast
    // back to `std::size_t` for indexing. `schedule(static)` splits the range
    // into equal contiguous chunks; combined with the per-path seed below this
    // makes the parallel build bit-identical to the serial one.
    for (std::int64_t signed_p = 0; signed_p < static_cast<std::int64_t>(paths); ++signed_p) {
        const std::size_t p = static_cast<std::size_t>(signed_p);
        // (modern hashing idiom) 0x9E3779B97F4A7C15 is the binary golden-ratio
        // constant: multiplying a consecutive counter `p+1` by it and XOR-ing
        // with the master seed scatters adjacent p values to unrelated 64-bit
        // states — cheap, cache-friendly, and reproducible per path.
        std::uint64_t path_seed = seed ^ (0x9E3779B97F4A7C15ULL * (p + 1));
        std::vector<float> shocks(factors);
        for (auto& value : shocks) value = fast_normal(path_seed);
        for (std::size_t u = 0; u < n; ++u) {
            float factor = 0.0F;
            for (std::size_t f = 0; f < factors; ++f) factor += shocks[f] * loadings[u * factors + f];
            result[p * n + u] = spot_values[u] * std::exp(factor * vol_values[u]);
        }
    }
    return result;
}

json read_json(const std::string& path) {
    // (RAII) `std::ifstream` closes itself on scope exit — no manual
    // close()/error plumbing. `stream >> value` streams JSON straight into the
    // nlohmann tree (nlohmann's operator>>). Returning the local `value` by
    // value is NRVO: mainstream compilers elide the copy (at worst it is a
    // move), never an actual deep-copy + destroy.
    std::ifstream stream(path);
    if (!stream) throw std::runtime_error("unable to read JSON: " + path);
    json value;
    stream >> value;
    return value;
}

}  // namespace

RiskResult price_terminal_legs(const std::vector<double>& terminal_spots,
                               const std::vector<double>& reference_spots,
                               double strike, double discount_factor,
                               double coupon_pv) {
    // Prices a *single scenario path* (or an averaged/aggregated terminal
    // vector) against the worst-of payoff structure shared by every lane.
    //   - put     = df * mean(max(strike - terminal/ref, 0))  (short worst-of put)
    //   - funding = df                                        (return of principal)
    //   - coupon  = coupon_pv                                 (already discounted)
    // The worst-of reduction happens at the same place as in the parity lanes
    // (see run_cpp_parity) so every kernel prices the same payoff geometry.
    //
    // (modern) `const std::vector<double>&` parameters: read-only input,
    // passed by reference to avoid copying; the compiler enforces "cannot be
    // modified" at the call site. `std::size_t` comes from <cstddef> and is
    // the guaranteed unsigned type for container sizes — never `int` or
    // `unsigned`, which can truncate on 64-bit.
    //
    // (modern) the final `return` uses *brace/aggregate initialization*: the
    // outer braces fill `RiskResult{PV, put_option_price, legs}`; the nested
    // `{{"PUT", -1, -put}, ...}` is an initializer_list<LegResult> that the
    // vector constructor consumes in place. Since C++17 this prvalue return is
    // *guaranteed copy elision* — the RiskResult is constructed directly in
    // the caller's storage, zero copies, zero moves.
    if (terminal_spots.size() != reference_spots.size() || reference_spots.empty())
        throw std::invalid_argument("terminal and reference spot vectors must have equal non-zero size");
    double put = 0.0;
    for (std::size_t i = 0; i < terminal_spots.size(); ++i)
        put += std::max(strike - terminal_spots[i] / reference_spots[i], 0.0);
    put = discount_factor * put / static_cast<double>(terminal_spots.size());
    return {discount_factor - put + coupon_pv, put, {{"PUT", -1, -put}, {"FUNDING", 1, discount_factor}, {"COUPON", 1, coupon_pv}}};
}

RiskResult price_fixture(const std::string& request_json, std::size_t paths, std::uint64_t seed) {
    // Deal-level fixture pricing for the job JSON schema (the concrete term
    // sheet lives in skills/fina-risk/refs/termsheet1.{md,md.json}). It maps
    // the deal fields onto the shared worst-of payoff as follows:
    //   - Reference basket ......... dealData.instrument.underlyings[].spot
    //     (initial spots; e.g. 239.97 / 260.00); quoted spot overrides come
    //     from marketData.equity[].spot when revaluing at a later date.
    //   - Exercise price (78%) ..... knockInStar.strikeKI2 (0.78); knock-in
    //     price knockInStar.KIBarrier (0.70).
    //   - Time to expiry ........... (expiryDate - evaluationDate)/365, using
    //     dealData.expiryDate (Final Fixing Date 01 Feb 2027 in the sheet).
    //   - Discount curve ........... marketData.discCurves[0].curve interpolated
    //     linearly to the expiry (the old read took the shortest 1M pillar).
    //   - Vol surface .............. full eqVol grid interpolated (linear in
    //     strike; linear in total variance across maturities) at the exercise
    //     and knock-in moneyness; the higher (downside-wing) vol is kept.
    //   - Dividends ................ equity cash-dividend schedule over the
    //     remaining life -> drift (r - q).
    //   - Correlation .............. marketData.corr[0].correlation[0].
    //     correlation, used to orthogonalise the two Gaussian drivers:
    //     z2 = rho*z1 + sqrt(1-rho^2)*z2'.
    //   - MC scheme ................. correlated GBM (drift (r-q-0.5*vol^2)*dt,
    //     dt = 1/252) over the real NYSE trading calendar to the expiry. EKI =
    //     European knock-in on the final fixing date only
    //     (knockInType="EKI"); the put pays max(strikeKI2 - worst, 0) only when
    //     worst <= KIBarrier and the note was not globally called.
    //   - Memory coupon ............. dealData.RGACCLKO: for each accrual
    //     period the paid/total coupon counts N1/N2 give the "accrued unpaid
    //     coupon" of the memory callable. `unpaid = max(N2 - N1, 0)` of this
    //     period is still owed, scaled by `10.0 * accruRate` (the accrual
    //     ledger rate is denominated per 1,000 of notional, so x10 rescales to
    //     the note) and discounted back from the period paymentDate. The
    //     global memory-call barrier level is carried in gblBarPrice=1.10 (the
    //     110% Daily Memory Callable threshold) and the per-asset local
    //     barrier in locBarPrice=999.99 (effectively disabled here),
    //     mirroring KIKOSelect.globalKO/localKO.
    //   - Leg decomposition ......... PUT (-1) + FUNDING (+1) + COUPON (+1):
    //     pv = funding - put + coupon, matching price_terminal_legs.
    const auto request = json::parse(request_json);
    // (modern) `const auto` keeps `request` immutable; `.at("key")` on nlohmann
    // is *bounds-checked* (throws json::out_of_range on a missing key), so all
    // `.at()` reads fail loudly instead of silently inventing data.
    const auto& deal = request.at("dealData");
    // (modern + lifetime extension) `deal` is a `const&` to a JSON *child* of
    // `request` (nlohmann nodes are value-like). Because request outlives deal,
    // reading `deal` is safe; and a temporary that is bound to a `const&` gets
    // its life extended to the reference's scope — so `deal.at(...)` returns
    // temporaries that `underlyings` can also safely bind `const&` to. With
    // plain `auto` these would be deep copies of the whole subtree.
    const auto& underlyings = deal.at("instrument").at("underlyings");
    std::vector<double> refs;
    for (const auto& u : underlyings) refs.push_back(get_number(u, "spot", 100.0));
    std::vector<double> quoted = refs;
    if (request.contains("marketData") && request["marketData"].contains("equity")) {
        // (modern) `.contains()` + `operator[]` for *optional* sections; the
        // spread of `std::min(a.size(), b.size())` is well-typed because both
        // sides are `std::size_t` (if one were `int`, the comparison would be
        // signed/unsigned — the compiler would warn).
        const auto& equities = request["marketData"]["equity"];
        for (std::size_t i = 0; i < std::min(refs.size(), equities.size()); ++i)
            quoted[i] = get_number(equities[i], "spot", refs[i]);
    }
    const double strike = deal.value("knockInStar", json::object()).value("strikeKI2", 0.78);
    const double ki_barrier = deal.value("knockInStar", json::object()).value("KIBarrier", 0.70);
    const std::string knock_in_type = deal.value("KIKOSelect", json::object()).value("knockInType", std::string("EKI"));
    // EKI = European knock-in observed on the final fixing date only. When the
    // deal declares no knock-in type (funding/coupon legs) the put collapses to
    // zero because those legs carry strikeKI2 = 0.
    const bool european_knock_in = knock_in_type != "Continuous";
    const double global_barrier = deal.value("RGACCLKO", json::object()).value("gblBarPrice", 1.10);
    const int evaluation_date = request.at("marketData").value("evaluationDate", 0);
    const int expiry_date = deal.value("expiryDate", deal.value("maturityDate", evaluation_date));
    const double time = std::max(expiry_date - evaluation_date, 0) / 365.0;
    // Curve interpolation to the option expiry (the legacy read took curve[0],
    // the shortest 1M pillar, and used it for every maturity).
    const double rate = interp_curve_rate(
        request.at("marketData").value("discCurves", json::array()), expiry_date, evaluation_date);
    const double discount_factor = std::exp(-rate * time);
    // Conservative full-surface read: interpolate the eqVol grid (linear in
    // strike; linear in total variance across maturities) at both the exercise
    // and knock-in moneyness and keep the higher (downside-wing) vol.
    std::vector<double> vols(refs.size(), 0.45);
    std::vector<double> dividends(refs.size(), 0.0);
    if (request.at("marketData").contains("eqVol") && request.at("marketData").contains("equity")) {
        for (std::size_t i = 0; i < refs.size(); ++i) {
            for (const auto& surface : request.at("marketData").at("eqVol")) {
                if (surface.value("_id", "") != request.at("marketData").at("equity").at(i).value("_id", "")) continue;
                const double exercise_strike = strike * refs[i];
                const double knock_in_strike = ki_barrier * refs[i];
                const double vol_exercise = surface_vol(surface, exercise_strike, expiry_date, evaluation_date);
                const double knock_in_vol = surface_vol(surface, knock_in_strike, expiry_date, evaluation_date);
                vols[i] = std::max(vol_exercise, knock_in_vol);
                break;
            }
        }
    }
    // Dividend yield from the equity cash-dividend schedule (ex-date in the
    // remaining life), so the drift is (r - q) rather than r.
    if (request.at("marketData").contains("equity")) {
        for (std::size_t i = 0; i < refs.size(); ++i) {
            const auto& equity = request.at("marketData").at("equity").at(i);
            const auto it = equity.find("dividend");
            double paid = 0.0;
            if (it != equity.end() && it->is_array()) {
                for (const auto& entry : *it) {
                    const int ex_date = static_cast<int>(entry.value("exDate", 0));
                    if (ex_date > evaluation_date && ex_date <= expiry_date) paid += entry.value("div", 0.0);
                }
            }
            if (time > 0.0 && quoted[i] > 0.0) dividends[i] = paid / quoted[i] / time;
        }
    }
    double correlation = 0.0;
    // (modern) decorrelated-shock idiom: two Gaussian drivers are generated
    // sequentially with Cholesky-style orthogonalisation z2 = rho*z1 +
    // sqrt(1-rho^2)*z2'. The `try/catch(...)` swallows a missing or badly
    // shaped `corr` block (optional market data).
    try { correlation = request.at("marketData").at("corr").at(0).at("correlation").at(0).value("correlation", 0.0); }
    catch (...) { correlation = 0.0; }
    std::mt19937_64 rng(seed);
    std::normal_distribution<double> normal(0.0, 1.0);
    double put = 0.0;
    // Real NYSE fixing calendar (the term sheet references NYSE); dt = 1/252
    // per trading day over the actual schedule length.
    const std::vector<int> schedule = nyse_schedule(evaluation_date, expiry_date);
    const int steps = std::max(2, static_cast<int>(schedule.size()));
    const double dt = 1.0 / 252.0;
    const double orthogonal_scale = std::sqrt(std::max(1.0 - correlation * correlation, 0.0));
    for (std::size_t p = 0; p < paths; ++p) {
        std::vector<double> log_spot(quoted.size());
        for (std::size_t i = 0; i < quoted.size(); ++i) log_spot[i] = std::log(quoted[i]);
        bool called = false;
        for (int step = 0; step < steps; ++step) {
            const double z1 = normal(rng);
            const double z2 = correlation * z1 + orthogonal_scale * normal(rng);
            // Conservative global-KO read: the daily auto-call requires every
            // underlying at/above the 110% call barrier on the SAME fixing;
            // pre-locked memory state is intentionally not credited (that only
            // makes the call easier and would lower the reserve).
            bool all_above = true;
            for (std::size_t i = 0; i < quoted.size(); ++i) {
                const double z = i == 0 ? z1 : z2;
                log_spot[i] += (rate - dividends[i] - 0.5 * vols[i] * vols[i]) * dt
                    + vols[i] * std::sqrt(dt) * z;
                if (std::exp(log_spot[i]) / refs[i] < global_barrier) all_above = false;
            }
            if (all_above) called = true;
        }
        double worst = 10.0;
        for (std::size_t i = 0; i < refs.size(); ++i) worst = std::min(worst, std::exp(log_spot[i]) / refs[i]);
        const bool knock_in = !european_knock_in || worst <= ki_barrier;
        if (knock_in && !called) put += std::max(strike - worst, 0.0);
    }
    put = discount_factor * put / static_cast<double>(paths);
    double coupon = 0.0;
    const auto& rg = deal.value("RGACCLKO", json::object());
    const auto ends = rg.value("endDate", json::array());
    const auto payments = rg.value("paymentDate", json::array());
    const auto rates = rg.value("accruRate", json::array());
    const auto paid = rg.value("N1", json::array());
    const auto total = rg.value("N2", json::array());
    const double notional = deal.value("notional", 1.0);
    for (std::size_t i = 0; i < ends.size() && i < payments.size() && i < rates.size() && i < paid.size() && i < total.size(); ++i) {
        const int unpaid = std::max(total.at(i).get<int>() - paid.at(i).get<int>(), 0);
        const int fixings = std::max(total.at(i).get<int>(), 1);
        coupon += 10.0 * rates.at(i).get<double>() * static_cast<double>(unpaid) / fixings
            * std::exp(-rate * std::max(payments.at(i).get<int>() - evaluation_date, 0) / 365.0);
    }
    const double funding = discount_factor;
    const double pv = funding - put + coupon;
    return {pv, put, {{"PUT", -1, -put}, {"FUNDING", 1, funding}, {"COUPON", 1, coupon}}};
}

BenchmarkResult run_benchmark(const std::string& instruments_json,
                              const std::string& market_json,
                              std::size_t paths, std::uint64_t seed) {
    // Legacy standalone benchmark lane. Unlike the parity lane below it reads
    // instrument/market JSON from disk and builds its own cube in-process via
    // terminal_market, so it exercises the full C++ path end to end. It shares
    // the same compiled worst-of payoff structure (indices + strike) so its
    // checksum is directly comparable with the NumPy kernel running on the
    // same corpus.
    //   - Trades are compiled once from `underlyings` ids into basket indices
    //     via the market id->index map, and each strike is read from
    //     legs[0].payoff.strike.
    //   - The cube is terminal-only (one [path][underlying] slice): the
    //     reported peak_path_cube_bytes also budgets a hypothetical full
    //     [path][step][factor] factor-cube that the production QuantLib/XAD
    //     build materialises, so the metric stays meaningful across builds.
    //   - Pricing is the per-path worst-of: for each trade, at each path,
    //     worst = min over basket of terminal/spot, summed as
    //     `1 - max(strike - worst, 0)` with a single OpenMP reduction.
    const auto instruments = read_json(instruments_json);
    const auto market = read_json(market_json);
    const auto& simulation = market.at("simulation");
    const std::size_t steps = simulation.value("steps", 252U);
    const std::size_t factors = simulation.value("factorCount", 12U);
    const std::size_t n = market.at("underlyings").size();
    const auto& trades = instruments.at("instruments");
    std::unordered_map<std::string, std::size_t> id_to_index;
    std::vector<double> spots(n);
    for (std::size_t i = 0; i < n; ++i) {
        id_to_index.emplace(market.at("underlyings")[i].at("id").get<std::string>(), i);
        spots[i] = get_number(market.at("underlyings")[i], "spot", 100.0);
    }
    struct CompiledTrade { std::vector<std::size_t> indices; double strike; };
    // (modern) plain aggregate struct — no hand-written constructors needed;
    // `{}` aggregates are copy/move-usable out of the box. `reserve()` avoids
    // reallocations/copies as the vector grows; `emplace` (above) and
    // `push_back(std::move(item))` (below) transfer ownership instead of
    // copying: after the move, `item` is an empty shell we never touch again.
    std::vector<CompiledTrade> compiled;
    compiled.reserve(trades.size());
    for (const auto& trade : trades) {
        CompiledTrade item;
        item.strike = trade.at("legs")[0].at("payoff").value("strike", 0.78);
        for (const auto& id : trade.at("underlyings")) item.indices.push_back(id_to_index.at(id.get<std::string>()));
        compiled.push_back(std::move(item));
    }
    // (modern) steady_clock is the monotonic stopwatch: measuring elapsed time
    // between two steady_clock::now() calls and casting the difference to
    // std::chrono::duration<double> is the idiomatic replacement for
    // clock()/gettimeofday, and is immune to wall-clock/NTP changes.
    const auto start = std::chrono::steady_clock::now();
    auto terminal = terminal_market(market, paths, seed);
    const auto built = std::chrono::steady_clock::now();
    double checksum = 0.0;
#pragma omp parallel for schedule(static) reduction(+:checksum)
    for (std::int64_t trade_index = 0; trade_index < static_cast<std::int64_t>(compiled.size()); ++trade_index) {
        // (signed-index OpenMP idiom, see terminal_market; `reduction(+:...)`
        // gives each thread a private partial that is combined at the barrier
        // — the accumulation `checksum +=` is race-free with no mutex).
        const auto& trade = compiled[static_cast<std::size_t>(trade_index)];
        for (std::size_t p = 0; p < paths; ++p) {
            // Worst-of basket reduction on the shared cube: same ratio/min
            // structure as run_cpp_parity (the parity contract).
            double worst = 1.0e30;
            for (const auto index : trade.indices)
                worst = std::min(worst, static_cast<double>(terminal[p * n + index]) / spots[index]);
            checksum += 1.0 - std::max(trade.strike - worst, 0.0);
        }
    }
    const auto finished = std::chrono::steady_clock::now();
    const double build_s = std::chrono::duration<double>(built - start).count();
    const double price_s = std::chrono::duration<double>(finished - built).count();
    return {trades.size(), n, paths, steps, factors, build_s, price_s, build_s + price_s,
            trades.size() / std::max(build_s + price_s, 1e-12),
            static_cast<std::uint64_t>(paths * factors * sizeof(float) + paths * steps * factors * sizeof(float) + terminal.size() * sizeof(float)),
            checksum, checksum / static_cast<double>(trades.size())};
}

ParityResult run_cpp_parity(const std::string& instruments_json,
                            const std::string& market_json,
                            const std::vector<float>& terminal,
                            std::uint64_t seed,
                            double bump) {
    // Production C++ parity lane ("cpp_native"). This is the kernel timed by
    // the MCP `cpp` benchmark and cross-checked bit-for-bit against the Python
    // mirror in cpp_parity.py (tolerance |C++ - Python| <= 1e-8 * max(1,|Py|)).
    //
    // SHARED CRN CONTRACT (the core design concern):
    //   The `terminal` cube is built ONCE by build_benchmark_corpus (NumPy,
    //   float32, fixed seed) and handed in from Python. `seed` is intentionally
    //   ignored ((void)seed) because the cube already encodes the path seed —
    //   both languages price the exact same paths. All Greeks and the Taylor
    //   P&L below share this same cube (common random numbers): the bump
    //   revaluations reuse the identical paths rather than redrawing shocks,
    //   which is what lets the C++ and Python checksums agree and removes
    //   sampling noise between the base/bumped valuations.
    //
    // PAYOFF + GREEKS per trade (worst-of basket from the shared cube):
    //   base_pv = 1 - base_put,  base_put = mean(max(strike - worst, 0))
    //   For each basket member k (central bump-revalue on the SAME paths):
    //     delta = (up_pv - down_pv) / (2 * bump * spot_k)      (# var change)
    //     gamma = (up_pv - 2*put + down_pv) / (bump*spot_k)^2
    //     where the k-th ratio is scaled by (1 +/- bump) and worst-of is
    //     recomputed over the basket — pathwise/CRN finite differences.
    //   Taylor forecast (2nd order, homogeneous +h parallel shock):
    //     forecast = sum_k [ delta_k*S_k*h + 0.5*gamma_k*(S_k*h)^2 ]
    //   Taylor actual = full-basket revalue under the same CRN cube minus
    //     base (a "true up" P&L), so the unexplained component
    //     actual - forecast measures the curvature/kink error of the Taylor2
    //     approximation (the max() kink and the knock-in discontinuity).
    //   Checksums accumulate across all trades via OpenMP reductions; the
    //     echo of `engine="cpp_native_parity"` lets the adapter tag the lane.
    const auto instruments = json::parse(instruments_json);
    const auto market = json::parse(market_json);
    const auto starts = std::chrono::steady_clock::now();
    // (modern) `(void)seed;` — the ABI keeps this parameter but the shared-cube
    // CRN contract means the path seed already lives inside `terminal`; casting
    // to void is the idiomatic way to mark a deliberately-unused parameter.
    (void)seed;  // the caller-supplied cube already encodes the path seed
    const auto& underlyings = market.at("underlyings");
    const std::size_t n = underlyings.size();
    const auto& trades = instruments.at("instruments");
    // (modern caveat) `const_cast` is the LAST-resort cast: it strips `const`
    // so the read-only trade array can be indexed by the OpenMP loop below
    // without copying. It is safe *only* because every use here is read-only;
    // the compiler no longer guards you once const is stripped, so keep the
    // cast as local and as short-lived as this.
    auto& records = const_cast<json&>(trades);
    std::unordered_map<std::string, std::size_t> id_to_index;
    std::vector<double> spots(n);
    for (std::size_t i = 0; i < n; ++i) {
        id_to_index.emplace(underlyings[i].at("id").get<std::string>(), i);
        spots[i] = get_number(underlyings[i], "spot", 100.0);
    }
    const std::size_t paths = terminal.size() / n;
    const double spot_h = bump;
    double pv_checksum = 0.0;
    double delta_checksum = 0.0;
    double delta_dollar_checksum = 0.0;
    double gamma_checksum = 0.0;
    double forecast_checksum = 0.0;
    double actual_checksum = 0.0;
#pragma omp parallel for schedule(static) reduction(+:pv_checksum,delta_checksum,delta_dollar_checksum,gamma_checksum,forecast_checksum,actual_checksum)
    for (std::int64_t t = 0; t < static_cast<std::int64_t>(records.size()); ++t) {
        const auto& trade = records[static_cast<std::size_t>(t)];
        std::vector<std::size_t> idx;
        for (const auto& id : trade.at("underlyings")) idx.push_back(id_to_index.at(id.get<std::string>()));
        const double strike = trade.at("legs")[0].at("payoff").value("strike", 0.78);
        // Worst-of performance ratios from the shared terminal cube.
        std::vector<double> worst(paths, 1.0e30);
        for (std::size_t p = 0; p < paths; ++p) {
            for (std::size_t k = 0; k < idx.size(); ++k) {
                const double ratio = static_cast<double>(terminal[p * n + idx[k]]) / spots[idx[k]];
                if (ratio < worst[p]) worst[p] = ratio;
            }
        }
        double put_sum = 0.0;
        for (std::size_t p = 0; p < paths; ++p) put_sum += std::max(strike - worst[p], 0.0);
        const double base_put = put_sum / static_cast<double>(paths);
        const double base_pv = 1.0 - base_put;
        // (modern) fill-constructor: n doubles, all 0.0 — the exact per-asset
        // arrays the Greeks accumulate into, sized by the basket membership.
        std::vector<double> deltas(idx.size(), 0.0);
        std::vector<double> gammas(idx.size(), 0.0);
        for (std::size_t k = 0; k < idx.size(); ++k) {
            double up_sum = 0.0;
            double down_sum = 0.0;
            for (std::size_t p = 0; p < paths; ++p) {
                double up_worst = 1.0e30;
                double down_worst = 1.0e30;
                for (std::size_t j = 0; j < idx.size(); ++j) {
                    const double ratio = static_cast<double>(terminal[p * n + idx[j]]) / spots[idx[j]];
                    up_worst = std::min(up_worst, ratio * (j == k ? 1.0 + spot_h : 1.0));
                    down_worst = std::min(down_worst, ratio * (j == k ? 1.0 - spot_h : 1.0));
                }
                up_sum += std::max(strike - up_worst, 0.0);
                down_sum += std::max(strike - down_worst, 0.0);
            }
            const double up = up_sum / static_cast<double>(paths);
            const double down = down_sum / static_cast<double>(paths);
            const double spot = spots[idx[k]];
            deltas[k] = (up - down) / (2.0 * spot_h * spot);
            gammas[k] = (up - 2.0 * base_put + down) / std::pow(spot_h * spot, 2.0);
        }
        double forecast = 0.0;
        for (std::size_t k = 0; k < idx.size(); ++k) {
            const double spot = spots[idx[k]];
            forecast += deltas[k] * spot * spot_h + 0.5 * gammas[k] * std::pow(spot * spot_h, 2.0);
            delta_dollar_checksum += deltas[k] * spot;
        }
        double shocked_put_sum = 0.0;
        for (std::size_t p = 0; p < paths; ++p)
            shocked_put_sum += std::max(strike - worst[p] * (1.0 + spot_h), 0.0);
        const double actual = shocked_put_sum / static_cast<double>(paths) - base_put;
        pv_checksum += base_pv;
        for (const auto delta : deltas) delta_checksum += delta;
        for (const auto gamma : gammas) gamma_checksum += gamma;
        forecast_checksum += forecast;
        actual_checksum += actual;
    }
    const auto finished = std::chrono::steady_clock::now();
    const double elapsed = std::chrono::duration<double>(finished - starts).count();
    const std::size_t instrument_count = records.size();
    // (modern) six parallel reduction accumulators in one OpenMP pragma: each
    // thread keeps private partials for all six and the runtime combines them
    // at the barrier — race-free constituent-level sums at full CPU utilization.
    return {instrument_count, n, paths, pv_checksum, delta_checksum, delta_dollar_checksum,
            gamma_checksum, forecast_checksum, actual_checksum, actual_checksum - forecast_checksum,
            elapsed, instrument_count / std::max(elapsed, 1e-12), "cpp_native_parity"};
}

std::string to_json(const ParityResult& result) {
    // (modern) nlohmann chart construction from a nested *initializer_list of
    // pairs*: `json{{"key", value}, ...}` builds the object leaf directly, no
    // imperative insert() calls. `.dump(2)` pretty-prints with 2-space indent.
    return json{
        {"instruments", result.instruments}, {"underlyings", result.underlyings},
        {"paths", result.paths}, {"pv_checksum", result.pv_checksum},
        {"delta_checksum", result.delta_checksum}, {"delta_dollar_checksum", result.delta_dollar_checksum},
        {"gamma_checksum", result.gamma_checksum}, {"taylor_forecast_checksum", result.taylor_forecast_checksum},
        {"taylor_actual_checksum", result.taylor_actual_checksum},
        {"taylor_unexplained_checksum", result.taylor_unexplained_checksum},
        {"elapsed_seconds", result.elapsed_seconds}, {"instruments_per_second", result.instruments_per_second},
        {"backend", result.engine}
    }.dump(2);
}

std::string to_json(const BenchmarkResult& result) {
    return json{{"benchmark", {{"instruments", result.instruments}, {"underlyings", result.underlyings}, {"paths", result.paths}, {"steps", result.steps}, {"factor_count", result.factors}, {"path_build_seconds", result.path_build_seconds}, {"pricing_seconds", result.pricing_seconds}, {"total_seconds", result.total_seconds}, {"instruments_per_second", result.instruments_per_second}, {"peak_path_cube_bytes", result.peak_path_cube_bytes}, {"price_checksum", result.price_checksum}, {"price_mean", result.price_mean}, {"backend", "cpp_reference_optional_quantlib_xad"}}}}.dump(2);
}

std::string to_json(const RiskResult& result) {
    json legs = json::array();
    for (const auto& leg : result.legs) legs.push_back({{"leg_name", leg.name}, {"multiplier", leg.multiplier}, {"pv", leg.pv}});
    return json{{"valuation", {{"pv", result.pv}}}, {"put_option_price", result.put_option_price}, {"legs", legs}}.dump(2);
}

namespace {

// Daily-lifecycle pricing of one term-sheet job against a shared
// (paths, observations, underlyings) spot cube. This is the faithful lane:
//  - the coupon counts the DAILY observations that sit inside [lowRange, upRange]
//    for each accrual period, nets the already-paid N1 fixings, carries the
//    memory shortfall forward, and stops accruing once global KO has fired;
//  - the worst-of PUT is knocked in on the FINAL fixing date only (EKI), and is
//    cancelled on any path that was globally called.
struct DailyPricing {
    double pv{};
    double put{};
    double coupon{};
    double funding{};
    double ki{};
    double ko{};
    std::vector<double> fixings;
    std::vector<double> memory;
};

DailyPricing price_daily_job(const json& common,
                             const std::vector<double>& paths,
                             std::size_t P, std::size_t O, std::size_t U,
                             const std::vector<int>& dates) {
    const auto& d = common.at("dealData");
    const auto& m = common.at("marketData");
    const auto& und = d.at("instrument").at("underlyings");
    const double rate = m.at("discCurves").at(0).at("curve").at(0).value("rate", 0.0);
    const int eval = m.value("evaluationDate", 0);
    const int expiry = d.value("expiryDate", d.value("maturityDate", eval));
    const double df = std::exp(-rate * std::max(expiry - eval, 0) / 365.0);
    std::vector<double> refs(U, 100.0);
    for (std::size_t i = 0; i < U && i < und.size(); ++i) refs[i] = und.at(i).value("spot", 100.0);
    const auto& rg = d.value("RGACCLKO", json::object());
    const double strike = d.value("knockInStar", json::object()).value("strikeKI2", d.value("strike", 0.78));
    const double kib = d.value("knockInStar", json::object()).value("KIBarrier", 0.70);
    const double gkb = rg.value("gblBarPrice", 1.10);
    std::size_t EO = static_cast<std::size_t>(std::upper_bound(dates.begin(), dates.end(), expiry) - dates.begin());
    if (EO == 0) EO = 1;
    if (EO > O) EO = O;
    std::vector<char> ki(P, 0), ko(P, 0);
    std::vector<std::size_t> ko_step(P, O);
    std::vector<double> terminal_worst(P, 1.0);
    for (std::size_t p = 0; p < P; ++p) {
        for (std::size_t o = 0; o < EO; ++o) {
            double worst = 1.0e30;
            bool all_above = true;
            for (std::size_t j = 0; j < U; ++j) {
                const double ratio = paths[(p * O + o) * U + j] / refs[j];
                worst = std::min(worst, ratio);
                if (ratio < gkb) all_above = false;
            }
            (void)worst;
            if (!ko[p] && all_above) { ko[p] = 1; ko_step[p] = o; }
        }
        double worst_t = 1.0e30;
        for (std::size_t j = 0; j < U; ++j)
            worst_t = std::min(worst_t, paths[(p * O + (EO - 1)) * U + j] / refs[j]);
        terminal_worst[p] = worst_t;
        // EKI: European knock-in observed only on the final fixing date.
        ki[p] = (worst_t <= kib) ? 1 : 0;
    }
    double put = 0.0;
    for (std::size_t p = 0; p < P; ++p)
        if (ki[p] && !ko[p]) put += std::max(strike - terminal_worst[p], 0.0);
    put = put / static_cast<double>(P) * df;

    const auto ends = rg.value("endDate", std::vector<int>{});
    const auto pays = rg.value("paymentDate", std::vector<int>{});
    const auto rates = rg.value("accruRate", std::vector<double>{});
    const auto n1 = rg.value("N1", std::vector<int>{});
    const auto n2 = rg.value("N2", std::vector<int>{});
    const auto lows = rg.value("lowRange", std::vector<double>{});
    const auto ups = rg.value("upRange", std::vector<double>{});
    const double notional = d.value("notional", 1.0);
    std::vector<double> cash(P, 0.0), memory(P, 0.0);
    int previous = eval;
    DailyPricing out;
    out.put = put;
    out.funding = df;
    out.ki = std::accumulate(ki.begin(), ki.end(), 0.0) / static_cast<double>(P);
    out.ko = std::accumulate(ko.begin(), ko.end(), 0.0) / static_cast<double>(P);
    for (std::size_t i = 0; i < ends.size() && i < pays.size() && i < rates.size() && i < n1.size() && i < n2.size(); ++i) {
        const auto it1 = std::upper_bound(dates.begin(), dates.end(), ends[i]);
        const auto it0 = std::upper_bound(dates.begin(), dates.end(), previous);
        if (ends[i] < dates.front()) { previous = ends[i]; continue; }
        const std::size_t e = it1 == dates.begin() ? 0 : static_cast<std::size_t>(it1 - dates.begin() - 1);
        const std::size_t s = static_cast<std::size_t>(it0 - dates.begin());
        if (e < s || rates[i] == 0.0) { previous = ends[i]; continue; }
        const double low = i < lows.size() ? lows[i] : 0.0;
        const double up = i < ups.size() ? ups[i] : 1.0e9;
        std::vector<double> obs(P, 0.0);
        for (std::size_t p = 0; p < P; ++p) {
            double count = 0.0;
            for (std::size_t o = s; o <= e && o < O; ++o) {
                double worst = 1.0e30;
                for (std::size_t j = 0; j < U; ++j)
                    worst = std::min(worst, paths[(p * O + o) * U + j] / refs[j]);
                if (worst >= low && worst <= up) count += 1.0;
            }
            obs[p] = count;
            const double future = std::max(count - static_cast<double>(n1[i]), 0.0);
            const double total = std::max(static_cast<double>(n2[i]), 1.0);
            const double frac = std::min((future + memory[p]) / total, 1.0);
            if (ko_step[p] > e)
                cash[p] += notional * rates[i] * frac * std::exp(-rate * std::max(pays[i] - eval, 0) / 365.0);
            memory[p] = (ko_step[p] > e && future < total) ? std::max(total - future, 0.0) : 0.0;
        }
        out.fixings.push_back(std::accumulate(obs.begin(), obs.end(), 0.0) / static_cast<double>(P));
        out.memory.push_back(std::accumulate(memory.begin(), memory.end(), 0.0) / static_cast<double>(P));
        previous = ends[i];
    }
    const double raw = std::accumulate(cash.begin(), cash.end(), 0.0) / static_cast<double>(P);
    out.coupon = raw / std::max(notional, 1.0) * d.value("legacyCouponQuoteScale", 10.0);
    out.pv = df - out.put + out.coupon;
    return out;
}

}  // namespace

std::string run_daily_termsheet_json(const std::string& request_json,
                                     const std::vector<double>& paths,
                                     std::size_t paths_count,
                                     std::size_t observations,
                                     std::size_t underlyings,
                                     const std::vector<int>& dates,
                                     double bump) {
    const json root = json::parse(request_json);
    const auto& jobs = root.at("Chunk").at("Jobs");
    const std::size_t P = paths_count, O = observations, U = underlyings;
    auto price_all = [&](const std::vector<double>& cube) {
        std::vector<DailyPricing> rs;
        rs.reserve(jobs.size());
        for (const auto& job : jobs) rs.push_back(price_daily_job(job.at("commonData"), cube, P, O, U, dates));
        return rs;
    };
    auto canonical = [](const std::vector<DailyPricing>& q) {
        return q[0].pv - q[0].coupon + (q.size() > 2 ? q[2].coupon : q[0].coupon);
    };
    const std::vector<DailyPricing> rs = price_all(paths);
    const double pv = canonical(rs);
    std::vector<double> deltas, gammas;
    for (std::size_t u = 0; u < U; ++u) {
        std::vector<double> up = paths, down = paths;
        for (std::size_t p = 0; p < P; ++p)
            for (std::size_t o = 0; o < O; ++o) {
                up[(p * O + o) * U + u] *= 1.0 + bump;
                down[(p * O + o) * U + u] *= 1.0 - bump;
            }
        const double pu = canonical(price_all(up));
        const double pd = canonical(price_all(down));
        deltas.push_back((pu - pd) / (2.0 * bump));
        gammas.push_back((pu - 2.0 * pv + pd) / (bump * bump));
    }
    const std::size_t coupon_job = rs.size() > 2 ? 2 : 0;
    json out{
        {"engine", "cpp_daily_termsheet_eki"},
        {"daily_observations", O},
        {"paths", P},
        {"underlyings", U},
        {"pv", pv},
        {"put_price", rs[0].put},
        {"coupon_pv", rs[coupon_job].coupon},
        {"funding", rs[0].funding},
        {"relative_delta", deltas},
        {"relative_gamma", gammas},
        {"ki_probability", rs[0].ki},
        {"ko_probability", rs[0].ko},
        {"coupon_fixings", rs[coupon_job].fixings},
        {"memory_carry", rs[coupon_job].memory},
        {"ki_monitoring", "EKI"},
        {"aad_engine", "disabled"},
    };
    return out.dump(2);
}

namespace {

// Compact per-instrument spec for the batched daily lane (all features on):
// refs / strike / EKI barrier / global-call barrier / expiry, and the coupon
// period schedule with lowRange/upRange + N1/N2. `market` carries rate + eval.
// Schedule fields are optional and let a corpus vary the calendar per trade:
//   - `ki_obs` .......... explicit EKI observation date (Excel serial); default
//     is the expiry grid index, so the terminal observation can differ.
//   - per-period `start`  first observation date (Excel serial) of the accrual
//     window; replaces the legacy ~one-month tail when supplied.
//   - per-period `stride`/`offset`  sample every k-th observation with a phase,
//     giving daily / weekly / monthly schedules. All map into the one shared
//     (paths, observations, underlyings) cube, so path sharing is preserved.
struct CompactDaily {
    double pv{};
    double put{};
    double coupon{};
    double funding{};
    double ki{};
    double ko{};
};

CompactDaily price_daily_compact(const json& inst,
                                 const std::vector<double>& paths,
                                 std::size_t P, std::size_t O, std::size_t U,
                                 const std::vector<int>& dates,
                                 double rate, int eval) {
    std::vector<double> refs(U, 100.0);
    const auto rj = inst.value("refs", std::vector<double>{});
    for (std::size_t i = 0; i < U && i < rj.size(); ++i) refs[i] = rj[i];
    const double strike = inst.value("strike", 0.78);
    const double kib = inst.value("ki", 0.70);
    const double gkb = inst.value("call", 1.10);
    const int expiry = inst.value("expiry", eval);
    const double notional = inst.value("notional", 1.0);
    const double quote_scale = inst.value("quote_scale", 10.0);
    const double df = std::exp(-rate * std::max(expiry - eval, 0) / 365.0);
    std::size_t EO = static_cast<std::size_t>(std::upper_bound(dates.begin(), dates.end(), expiry) - dates.begin());
    if (EO == 0) EO = 1;
    if (EO > O) EO = O;
    // Optional explicit EKI observation date (Excel serial). Defaults to the
    // expiry grid index; lets a corpus carry instruments whose terminal
    // observation is a different scheduled date.
    std::size_t terminal_index = EO - 1;
    const int ki_obs_date = inst.value("ki_obs", 0);
    if (ki_obs_date > 0) {
        const auto it = std::upper_bound(dates.begin(), dates.end(), ki_obs_date);
        terminal_index = it == dates.begin() ? 0 : static_cast<std::size_t>(it - dates.begin() - 1);
    }
    if (terminal_index >= O) terminal_index = O - 1;
    const std::size_t ko_end = std::min<std::size_t>(std::max(EO, terminal_index + 1), O);
    std::vector<char> ki(P, 0), ko(P, 0);
    std::vector<std::size_t> ko_step(P, O);
    std::vector<double> terminal_worst(P, 1.0);
    for (std::size_t p = 0; p < P; ++p) {
        for (std::size_t o = 0; o < ko_end; ++o) {
            bool all_above = true;
            for (std::size_t j = 0; j < U; ++j)
                if (paths[(p * O + o) * U + j] / refs[j] < gkb) all_above = false;
            if (!ko[p] && all_above) { ko[p] = 1; ko_step[p] = o; }
        }
        double worst_t = 1.0e30;
        for (std::size_t j = 0; j < U; ++j)
            worst_t = std::min(worst_t, paths[(p * O + terminal_index) * U + j] / refs[j]);
        terminal_worst[p] = worst_t;
        ki[p] = (worst_t <= kib) ? 1 : 0;  // EKI: observed on the selected date
    }
    double put = 0.0;
    for (std::size_t p = 0; p < P; ++p)
        if (ki[p] && !ko[p]) put += std::max(strike - terminal_worst[p], 0.0);
    put = put / static_cast<double>(P) * df;

    const auto periods = inst.value("periods", json::array());
    std::vector<double> cash(P, 0.0), memory(P, 0.0);
    for (const auto& per : periods) {
        const int end = per.value("end", 0);
        const int pay = per.value("pay", end);
        const int start_date = per.value("start", 0);
        const int stride = std::max(1, per.value("stride", 1));
        const int offset = per.value("offset", 0);
        const double prate = per.value("rate", 0.0);
        const int pn1 = per.value("n1", 0);
        const int pn2 = per.value("n2", 0);
        const double low = per.value("low", 0.0);
        const double up = per.value("up", 1.0e9);
        if (prate == 0.0 || end < dates.front()) continue;
        const auto it1 = std::upper_bound(dates.begin(), dates.end(), end);
        const std::size_t e = it1 == dates.begin() ? 0 : static_cast<std::size_t>(it1 - dates.begin() - 1);
        if (e > O) continue;
        // Per-period observation window: an explicit start date when supplied,
        // else the legacy ~one-month tail. `stride`/`offset` sample every k-th
        // observation so a corpus can carry daily/weekly/monthly schedules.
        std::size_t s = e < 20 ? 0 : e - 20;
        if (start_date > 0) {
            s = static_cast<std::size_t>(std::lower_bound(dates.begin(), dates.end(), start_date) - dates.begin());
        }
        if (s > e) s = e;
        for (std::size_t p = 0; p < P; ++p) {
            double count = 0.0;
            for (std::size_t o = s; o <= e && o < O; ++o) {
                if (stride > 1) {
                    const int phase = (static_cast<int>(o) - static_cast<int>(s) - offset) % stride;
                    if ((phase + stride) % stride != 0) continue;
                }
                double worst = 1.0e30;
                for (std::size_t j = 0; j < U; ++j)
                    worst = std::min(worst, paths[(p * O + o) * U + j] / refs[j]);
                if (worst >= low && worst <= up) count += 1.0;
            }
            const double future = std::max(count - static_cast<double>(pn1), 0.0);
            const double total = std::max(static_cast<double>(pn2), 1.0);
            const double frac = std::min((future + memory[p]) / total, 1.0);
            if (ko_step[p] > e)
                cash[p] += notional * prate * frac * std::exp(-rate * std::max(pay - eval, 0) / 365.0);
            memory[p] = (ko_step[p] > e && future < total) ? std::max(total - future, 0.0) : 0.0;
        }
    }
    const double raw = std::accumulate(cash.begin(), cash.end(), 0.0) / static_cast<double>(P);
    CompactDaily out;
    out.put = put;
    out.funding = df;
    out.coupon = raw / std::max(notional, 1.0) * quote_scale;
    out.pv = df - out.put + out.coupon;
    out.ki = std::accumulate(ki.begin(), ki.end(), 0.0) / static_cast<double>(P);
    out.ko = std::accumulate(ko.begin(), ko.end(), 0.0) / static_cast<double>(P);
    return out;
}

}  // namespace

std::string run_daily_termsheet_batch_json(const std::string& instruments_json,
                                           const std::string& market_json,
                                           const std::vector<double>& paths,
                                           std::size_t paths_count,
                                           std::size_t observations,
                                           std::size_t underlyings,
                                           const std::vector<int>& dates) {
    const json instruments = json::parse(instruments_json);
    const json market = json::parse(market_json);
    const auto& records = instruments.at("instruments");
    const double rate = market.value("rate", 0.0);
    const int eval = market.value("evaluation_date", 0);
    const std::size_t P = paths_count, O = observations, U = underlyings;
    const auto started = std::chrono::steady_clock::now();
    double put_sum = 0.0, coupon_sum = 0.0, funding_sum = 0.0, pv_sum = 0.0, ki_sum = 0.0, ko_sum = 0.0;
#pragma omp parallel for schedule(static) reduction(+:put_sum,coupon_sum,funding_sum,pv_sum,ki_sum,ko_sum)
    for (std::int64_t t = 0; t < static_cast<std::int64_t>(records.size()); ++t) {
        const auto r = price_daily_compact(records[static_cast<std::size_t>(t)], paths, P, O, U, dates, rate, eval);
        put_sum += r.put; coupon_sum += r.coupon; funding_sum += r.funding; pv_sum += r.pv;
        ki_sum += r.ki; ko_sum += r.ko;
    }
    const auto finished = std::chrono::steady_clock::now();
    const double elapsed = std::chrono::duration<double>(finished - started).count();
    const double n = static_cast<double>(records.size());
    json out{
        {"engine", "cpp_daily_termsheet_eki_batch"},
        {"instruments", records.size()},
        {"paths", P},
        {"observations", O},
        {"underlyings", U},
        {"ki_monitoring", "EKI"},
        {"aad_engine", "disabled"},
        {"put_checksum", put_sum},
        {"coupon_checksum", coupon_sum},
        {"funding_checksum", funding_sum},
        {"pv_checksum", pv_sum},
        {"mean_pv", pv_sum / std::max(n, 1.0)},
        {"ki_probability_mean", ki_sum / std::max(n, 1.0)},
        {"ko_probability_mean", ko_sum / std::max(n, 1.0)},
        {"elapsed_seconds", elapsed},
        {"instruments_per_second", n / std::max(elapsed, 1e-12)},
    };
    return out.dump(2);
}

std::string run_fcn_rakiplus_json(const std::string& canonical_request_json,
                                  const std::vector<double>& paths,
                                  std::size_t paths_count,
                                  std::size_t observations,
                                  std::size_t underlyings,
                                  const std::vector<int>& dates) {
    return fcn::price_canonical_request_json(
        canonical_request_json, paths, paths_count, observations, underlyings, dates);
}

}  // namespace fina::risk
