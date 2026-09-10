#include "fina_risk_cpp.hpp"

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
//   - EKI knock-in at 78% ...... put = mean(max(strikeKI2 - worst, 0)); priced
//     unconditionally over all paths as a conservative stand-in for the binary
//     knock-in trigger (knock-in ⇔ worst <= KIBarrier on the final fixing).
//   - KIBarrier 70% ............ surfaced as metadata; not a runtime gate here.
//   - Memory coupon N1/N2 ...... the RGACCLKO loop in price_fixture pays
//     (N2 - N1)/N2 of each period's accruRate, scaled by the note denomination
//     and discounted from paymentDate — the "accrued unpaid coupon" behaviour
//     of a memory callable.
//   - global/local KO .......... reflected economically through the locked
//     coupon state N1 (which coupon periods are already gone) rather than by
//     simulating the daily lock path.
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
    //   - Exercise price (78%) ..... knockInStar.strikeKI2 (0.78).
    //   - Time to expiry ........... (expiryDate - evaluationDate)/365, using
    //     dealData.expiryDate (Final Fixing Date 01 Feb 2027 in the sheet).
    //   - Discount curve ........... marketData.discCurves[0].curve[0].rate
    //     -> funding leg df = exp(-rate*time).
    //   - Vol surface .............. nearest strike to quoted spot and nearest
    //     maturity to evaluationDate + 0.40*365 (~5 months) on each eqVol
    //     surface, values in percent divided by 100.
    //   - Correlation .............. marketData.corr[0].correlation[0].
    //     correlation, used to orthogonalise the two Gaussian drivers:
    //     z2 = rho*z1 + sqrt(1-rho^2)*z2'.
    //   - MC scheme ................. correlated GBM with risk-neutral drift
    //     (rate - 0.5*vol^2)*dt under the discount curve; steps clamp to
    //     [2, 194] = round(time*252) trading days (the sheet has 9 periods
    //     x ~21 days). EKI = European knock-in: the worst-of ratio is observed
    //     at the terminal date only (Final Fixing Date), matching
    //     knockInType="EKI". The knock-in trigger (KIBarrier=0.70) is not gated
    //     here — the unconditional put below is the conservative version of the
    //     physical-delivery payoff, see the top-of-file design overview.
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
    const int evaluation_date = request.at("marketData").value("evaluationDate", 0);
    const int expiry_date = deal.value("expiryDate", deal.value("maturityDate", evaluation_date));
    const double time = std::max(expiry_date - evaluation_date, 0) / 365.0;
    // (modern) the rate falls back through three nested ternaries; `const` +
    // single assignment means the reader can reason about `rate` locally.
    const double rate = request.at("marketData").value("discCurves", json::array()).empty()
        ? 0.0 : request.at("marketData").at("discCurves").at(0).value("curve", json::array()).empty()
            ? 0.0 : request.at("marketData").at("discCurves").at(0).at("curve").at(0).value("rate", 0.0);
    const double discount_factor = std::exp(-rate * time);
    // (modern) fill constructor `vector(n, value)`: n copies of 0.45.
    std::vector<double> vols(refs.size(), 0.45);
    if (request.at("marketData").contains("eqVol")) {
        for (std::size_t i = 0; i < refs.size(); ++i) {
            for (const auto& surface : request.at("marketData").at("eqVol")) {
                if (surface.value("_id", "") != request.at("marketData").at("equity").at(i).value("_id", "")) continue;
                const auto values = surface.value("vol", json::array());
                const auto strikes = surface.value("strike", json::array());
                const auto maturities = surface.value("maturity", json::array());
                std::size_t col = 0;
                double best_strike_distance = 1.0e300;
                for (std::size_t k = 0; k < strikes.size(); ++k) {
                    const double distance = std::abs(strikes.at(k).get<double>() - quoted[i]);
                    if (distance < best_strike_distance) { best_strike_distance = distance; col = k; }
                }
                std::size_t row = 0;
                double best_maturity_distance = 1.0e300;
                const double target_maturity = evaluation_date + 0.40 * 365.0;
                for (std::size_t k = 0; k < maturities.size(); ++k) {
                    const double distance = std::abs(maturities.at(k).get<double>() - target_maturity);
                    if (distance < best_maturity_distance) { best_maturity_distance = distance; row = k; }
                }
                if (!values.empty() && values.at(row).is_array()) vols[i] = values.at(row).at(std::min(col, values.at(row).size() - 1)).get<double>() / 100.0;
                break;
            }
        }
    }
    double correlation = 0.0;
    // (modern) decorrelated-shock idiom: because the payoff only needs the
    // *terminal* worst ratio, the two Gaussian drivers can be generated
    // sequentially with Cholesky-style orthogonalisation z2 = rho*z1 +
    // sqrt(1-rho^2)*z2'. The `try/catch(...)` swallows a missing or badly
    // shaped `corr` block (optional market data) — but note the ellipsis
    // catches *every* exception, so keep it as narrow as the intent really is.
    try { correlation = request.at("marketData").at("corr").at(0).at("correlation").at(0).value("correlation", 0.0); }
    catch (...) { correlation = 0.0; }
    std::mt19937_64 rng(seed);
    std::normal_distribution<double> normal(0.0, 1.0);
    double put = 0.0;
    // (modern) nested std::clamp via min/max; the inner value is *intentionally*
    // int-cast (std::round returns double). `dt` is a plain scalar double.
    const int steps = std::max(2, std::min(194, static_cast<int>(std::round(time * 252.0))));
    const double dt = time / steps;
    const double orthogonal_scale = std::sqrt(std::max(1.0 - correlation * correlation, 0.0));
    for (std::size_t p = 0; p < paths; ++p) {
        std::vector<double> log_spot(quoted.size());
        for (std::size_t i = 0; i < quoted.size(); ++i) log_spot[i] = std::log(quoted[i]);
        for (int step = 0; step < steps; ++step) {
            const double z1 = normal(rng);
            const double z2 = correlation * z1 + orthogonal_scale * normal(rng);
            if (!log_spot.empty()) log_spot[0] += (rate - 0.5 * vols[0] * vols[0]) * dt + vols[0] * std::sqrt(dt) * z1;
            if (log_spot.size() > 1) log_spot[1] += (rate - 0.5 * vols[1] * vols[1]) * dt + vols[1] * std::sqrt(dt) * z2;
        }
        double worst = 10.0;
        for (std::size_t i = 0; i < refs.size(); ++i) worst = std::min(worst, std::exp(log_spot[i]) / refs[i]);
        put += std::max(strike - worst, 0.0);
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

}  // namespace fina::risk
