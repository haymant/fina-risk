#include "fina_risk/engine.hpp"

#include "fina_risk/barriers.hpp"
#include "fina_risk/coupon.hpp"
#include "fina_risk/funding.hpp"
#include "fina_risk/schedule.hpp"
#include "fina_risk/settlement.hpp"
#include "fina_risk/state.hpp"
#include "fina_risk/terminal_option.hpp"

#include <algorithm>
#include <cmath>
#include <nlohmann/json.hpp>
#include <numeric>
#include <stdexcept>

namespace fina::risk::fcn {
namespace {
using json = nlohmann::json;

Comparison comparison_from(const json& value, Comparison fallback) {
    if (!value.is_string()) return fallback;
    const auto text = value.get<std::string>();
    if (text == ">") return Comparison::greater;
    if (text == ">=") return Comparison::greater_equal;
    if (text == "<") return Comparison::less;
    if (text == "<=") return Comparison::less_equal;
    throw std::invalid_argument("unsupported comparison operator: " + text);
}

KnockOutKind precedence_from(const json& value) {
    if (!value.is_string() || value.get<std::string>() == "global") return KnockOutKind::global;
    if (value.get<std::string>() == "local") return KnockOutKind::local;
    throw std::invalid_argument("same_day_ko_precedence must be local or global");
}

const json* object_member(const json& object, const char* key) {
    const auto found = object.find(key);
    return found == object.end() ? nullptr : &*found;
}

double numeric(const json& object, const char* key, double fallback = 0.0) {
    const auto* item = object_member(object, key);
    return item && item->is_number() ? item->get<double>() : fallback;
}

int integer(const json& object, const char* key, int fallback = 0) {
    const auto* item = object_member(object, key);
    return item && item->is_number_integer() ? item->get<int>() : fallback;
}

std::string text(const json& object, const char* key, const std::string& fallback = "") {
    const auto* item = object_member(object, key);
    return item && item->is_string() ? item->get<std::string>() : fallback;
}

void add_transition(EngineResult& result, std::size_t path, int date, const char* transition, const char* source) {
    // Individual-path transitions can be large for Monte Carlo. The native result
    // includes deterministic exemplars; aggregate probabilities remain exact.
    if (result.state_transitions.size() < 64U) {
        result.state_transitions.push_back({path, std::to_string(date), transition, source});
    }
}

EngineResult error_result(EvidenceStatus status, const std::string& reason) {
    EngineResult result;
    result.status = status == EvidenceStatus::unsupported ? "unsupported" : "unresolved";
    result.evidence_status = status;
    result.unsupported_reason = reason;
    result.selected_branch = "not_priced";
    return result;
}

}  // namespace

CompiledTerms compile_canonical_terms(const std::string& request_json) {
    const json request = json::parse(request_json);
    if (!request.contains("instrument_key") || !request.contains("market_data") || !request.contains("legs") || !request.contains("parameters")) {
        return {{}, EvidenceStatus::unsupported, "canonical pricing-request fields instrument_key, market_data, legs, parameters are required"};
    }

    FcnTerms terms;
    terms.instrument_key = request.at("instrument_key").get<std::string>();
    terms.request_id = text(request, "request_id", terms.instrument_key);
    terms.process_id = text(request, "process_id");
    terms.source_revision = text(request, "source_revision");
    const auto& market = request.at("market_data");
    terms.evaluation_date = integer(market, "evaluation_date");
    if (terms.evaluation_date == 0) return {{}, EvidenceStatus::unsupported, "market_data.evaluation_date must be an Excel serial date"};
    const auto& underlyings = market.at("underlyings");
    if (!underlyings.is_array() || underlyings.empty()) return {{}, EvidenceStatus::unsupported, "at least one underlying is required"};
    for (const auto& underlying : underlyings) {
        const double ref = numeric(underlying, "reference_spot");
        if (!(ref > 0.0)) return {{}, EvidenceStatus::unsupported, "every underlying requires reference_spot > 0"};
        terms.reference_spots.push_back(ref);
    }
    if (market.contains("curves") && market.at("curves").is_array() && !market.at("curves").empty()) {
        const auto& curve = market.at("curves").at(0);
        if (curve.contains("pillars") && curve.at("pillars").is_array() && !curve.at("pillars").empty()) {
            terms.rate = numeric(curve.at("pillars").at(0), "rate");
        }
    }

    const json fcn = request.value("fcn_terms", json::object());
    const json common = request.value("common_economics", json::object());
    terms.notional = numeric(fcn, "notional", numeric(common, "notional", 1.0));
    terms.currency = text(fcn, "currency", text(common, "payment_currency", "USD"));
    terms.final_fixing_date = integer(fcn, "final_fixing_date", integer(fcn, "maturity_date", terms.evaluation_date));
    terms.maturity_date = integer(fcn, "maturity_date", terms.final_fixing_date);
    terms.coupon_quote_scale = numeric(fcn, "coupon_quote_scale", 1.0);
    terms.coupon_memory = fcn.value("coupon_memory", false);
    terms.physical_delivery = fcn.value("physical_delivery", false);
    terms.range_lower_inclusive = fcn.value("range_lower_inclusive", true);
    terms.range_upper_inclusive = fcn.value("range_upper_inclusive", true);
    terms.use_worst_of = text(fcn, "performance_indicator", "worst_of") == "worst_of";
    if (!terms.use_worst_of) return {{}, EvidenceStatus::unsupported, "only performance_indicator=worst_of is implemented"};
    if (fcn.value("continuous_monitoring", false)) return {{}, EvidenceStatus::unsupported, "continuous monitoring is not represented by a discrete path cube"};
    if (fcn.value("memory_ko", false) && text(fcn, "memory_ko_mode") != "per_underlying_ever") {
        return {{}, EvidenceStatus::ambiguous, "memory_ko requires explicit memory_ko_mode=per_underlying_ever"};
    }

    for (const auto& leg : request.at("legs")) {
        const std::string leg_type = text(leg, "leg_type");
        const json payoff = leg.value("payoff", json::object());
        if (leg_type == "intrinsic_option") {
            terms.terminal.strike = numeric(payoff, "strike");
            const json ki = payoff.value("knock_in", json::object());
            terms.terminal.ki_barrier = numeric(ki, "barrier");
            terms.terminal.ki_enabled = !ki.empty();
            terms.terminal.ki_operator = comparison_from(ki.value("operator", json("<=")), Comparison::less_equal);
            const std::string settlement = text(payoff, "settlement", terms.physical_delivery ? "physical_delivery" : "cash");
            terms.terminal.settlement = settlement == "physical_delivery" || settlement == "delivery" ? SettlementKind::physical : SettlementKind::cash;
            terms.physical_delivery = terms.terminal.settlement == SettlementKind::physical;
        } else if (leg_type == "funding") {
            terms.funding.enabled = payoff.value("notional_return", true);
            terms.funding.return_ratio = numeric(payoff, "return_ratio", 1.0);
        }
    }
    if (terms.terminal.strike <= 0.0 || terms.terminal.ki_barrier <= 0.0) {
        return {{}, EvidenceStatus::unsupported, "PUT / Terminal Optionality leg requires positive strike and knock_in.barrier"};
    }

    const json barrier = fcn.value("barriers", json::object());
    terms.barriers.local_enabled = barrier.value("local_enabled", false);
    terms.barriers.global_enabled = barrier.value("global_enabled", false);
    terms.barriers.memory_ko = barrier.value("memory_ko", fcn.value("memory_ko", false));
    terms.barriers.local_barrier = numeric(barrier, "local_barrier");
    terms.barriers.global_barrier = numeric(barrier, "global_barrier");
    terms.barriers.local_operator = comparison_from(barrier.value("local_operator", json(">=")), Comparison::greater_equal);
    terms.barriers.global_operator = comparison_from(barrier.value("global_operator", json(">=")), Comparison::greater_equal);
    terms.barriers.same_day_precedence = precedence_from(barrier.value("same_day_ko_precedence", json("global")));

    if (fcn.contains("coupon_periods")) {
        if (!fcn.at("coupon_periods").is_array()) return {{}, EvidenceStatus::unsupported, "fcn_terms.coupon_periods must be an array"};
        for (const auto& source : fcn.at("coupon_periods")) {
            CouponPeriodTerms period;
            period.id = text(source, "id", "period-" + std::to_string(terms.coupon_periods.size() + 1));
            period.start_date = integer(source, "start_date", terms.evaluation_date);
            period.end_date = integer(source, "end_date", terms.maturity_date);
            period.payment_date = integer(source, "payment_date", period.end_date);
            period.range_rate = numeric(source, "range_rate");
            period.fixed_coupon = numeric(source, "fixed_coupon");
            period.lower_bound = numeric(source, "lower_bound", 0.0);
            period.upper_bound = numeric(source, "upper_bound", 1.0e12);
            period.already_paid_fixings = integer(source, "already_paid_fixings");
            period.total_fixings = integer(source, "total_fixings");
            period.coupon_barrier = numeric(source, "coupon_barrier");
            period.local_ko_barrier = numeric(source, "local_ko_barrier", terms.barriers.local_barrier);
            period.local_ko_coupon = numeric(source, "local_ko_coupon");
            period.global_ko_coupon = numeric(source, "global_ko_coupon");
            if (period.total_fixings < 0 || period.end_date < period.start_date) {
                return {{}, EvidenceStatus::unsupported, "coupon periods require non-negative total_fixings and ordered dates"};
            }
            terms.coupon_periods.push_back(period);
        }
    } else {
        // A request without an explicit lifecycle schedule may still price the
        // terminal optionality and funding. Its coupon is explicitly zero.
        terms.coupon_quote_scale = 1.0;
    }
    return {std::move(terms), EvidenceStatus::verified, {}};
}

EngineResult price(const FcnTerms& terms, const ObservationCubeView& cube) {
    if (cube.paths == 0 || cube.observations == 0 || cube.underlyings != terms.reference_spots.size() || cube.dates.size() < cube.observations) {
        return error_result(EvidenceStatus::unsupported, "path cube dimensions or dates do not match compiled FCN terms");
    }

    EngineResult result;
    result.source_revision = terms.source_revision;
    result.request_id = terms.request_id;
    result.process_id = terms.process_id;
    const std::size_t final_index = observation_on_or_before(cube.dates, terms.final_fixing_date, cube.observations);
    std::vector<double> coupon_cash_sum(terms.coupon_periods.size(), 0.0);
    std::vector<double> coupon_pv_sum(terms.coupon_periods.size(), 0.0);
    std::vector<double> funding_cash_by_observation(cube.observations, 0.0);
    std::vector<double> funding_pv_by_observation(cube.observations, 0.0);
    double put_cash_sum = 0.0;
    double put_pv_sum = 0.0;
    std::size_t ki_paths = 0;
    std::size_t ko_paths = 0;
    std::size_t local_ko_paths = 0;
    std::size_t global_ko_paths = 0;
    result.state_transitions.reserve(64U);
    PathState state;
    state.local_memory_locks.resize(cube.underlyings);
    state.global_memory_locks.resize(cube.underlyings);

    for (std::size_t path = 0; path < cube.paths; ++path) {
        state.terminated = false;
        state.knock_in_seen = false;
        state.local_ko_seen = false;
        state.global_ko_seen = false;
        state.selected_ko = KnockOutKind::none;
        state.ko_observation = -1;
        state.unpaid_coupon = 0.0;
        std::fill(state.local_memory_locks.begin(), state.local_memory_locks.end(), 0U);
        std::fill(state.global_memory_locks.begin(), state.global_memory_locks.end(), 0U);
        double terminal_performance = 1.0;
        for (std::size_t obs = 0; obs <= final_index; ++obs) {
            double worst = 1.0e300;
            bool all_local_memory = true;
            bool all_global_memory = true;
            for (std::size_t underlying = 0; underlying < cube.underlyings; ++underlying) {
                const double spot = cube.spot(path, obs, underlying);
                if (!is_valid_fixing(spot)) return error_result(EvidenceStatus::unresolved, "missing or invalid fixing in native path cube");
                const double performance = spot / terms.reference_spots[underlying];
                worst = std::min(worst, performance);
                if (terms.barriers.memory_ko) {
                    if (terms.barriers.local_enabled && compare(performance, terms.barriers.local_barrier, terms.barriers.local_operator)) state.local_memory_locks[underlying] = 1U;
                    if (terms.barriers.global_enabled && compare(performance, terms.barriers.global_barrier, terms.barriers.global_operator)) state.global_memory_locks[underlying] = 1U;
                    all_local_memory = all_local_memory && state.local_memory_locks[underlying] != 0U;
                    all_global_memory = all_global_memory && state.global_memory_locks[underlying] != 0U;
                }
            }
            if (obs == final_index) {
                terminal_performance = worst;
                state.knock_in_seen = update_knock_in(terms.terminal, worst);
                if (state.knock_in_seen) add_transition(result, path, cube.dates[obs], "knock_in", "/fcn_terms/terminal/knock_in");
            }
            const bool local_hit = terms.barriers.local_enabled && (terms.barriers.memory_ko ? all_local_memory : compare(worst, terms.barriers.local_barrier, terms.barriers.local_operator));
            const bool global_hit = terms.barriers.global_enabled && (terms.barriers.memory_ko ? all_global_memory : compare(worst, terms.barriers.global_barrier, terms.barriers.global_operator));
            const KnockOutKind ko = resolve_ko(local_hit, global_hit, terms.barriers.same_day_precedence);
            if (ko != KnockOutKind::none) {
                state.terminated = true;
                state.selected_ko = ko;
                state.ko_observation = static_cast<int>(obs);
                state.local_ko_seen = ko == KnockOutKind::local;
                state.global_ko_seen = ko == KnockOutKind::global;
                add_transition(result, path, cube.dates[obs], to_string(ko), "/fcn_terms/barriers");
                break;
            }
        }

        if (state.knock_in_seen) ++ki_paths;
        if (state.terminated) {
            ++ko_paths;
            if (state.selected_ko == KnockOutKind::local) ++local_ko_paths;
            if (state.selected_ko == KnockOutKind::global) ++global_ko_paths;
        }

        for (std::size_t period_index = 0; period_index < terms.coupon_periods.size(); ++period_index) {
            const auto& period = terms.coupon_periods[period_index];
            const std::size_t begin = first_observation_after(cube.dates, period.start_date, cube.observations);
            const std::size_t end = observation_on_or_before(cube.dates, period.end_date, cube.observations);
            if (begin > end || begin >= cube.observations) continue;
            if (state.terminated && static_cast<std::size_t>(state.ko_observation) < begin) break;
            const std::size_t capped_end = state.terminated ? std::min(end, static_cast<std::size_t>(state.ko_observation)) : end;
            double qualifying = 0.0;
            double period_end_performance = 1.0;
            for (std::size_t obs = begin; obs <= capped_end; ++obs) {
                double worst = 1.0e300;
                for (std::size_t underlying = 0; underlying < cube.underlyings; ++underlying) {
                    const double spot = cube.spot(path, obs, underlying);
                    if (!is_valid_fixing(spot)) return error_result(EvidenceStatus::unresolved, "missing or invalid fixing in coupon observation");
                    worst = std::min(worst, spot / terms.reference_spots[underlying]);
                }
                period_end_performance = worst;
                if (in_range(worst, period, terms.range_lower_inclusive, terms.range_upper_inclusive)) qualifying += 1.0;
            }
            const bool coupon_barrier_ok = period.coupon_barrier <= 0.0 || period_end_performance >= period.coupon_barrier;
            const double carried = terms.coupon_memory ? state.unpaid_coupon : 0.0;
            double rate = period.fixed_coupon;
            if (coupon_barrier_ok) rate += period_rate(period, qualifying, carried) - period.fixed_coupon;
            const bool is_ko_period = state.terminated && static_cast<std::size_t>(state.ko_observation) <= end;
            if (is_ko_period) {
                rate += state.selected_ko == KnockOutKind::local ? period.local_ko_coupon : period.global_ko_coupon;
            }
            const int settlement_date = is_ko_period ? cube.dates[static_cast<std::size_t>(state.ko_observation)] : period.payment_date;
            const double cash = terms.notional * rate;
            const double discounted = cash * std::exp(-terms.rate * std::max(settlement_date - terms.evaluation_date, 0) / 365.0);
            coupon_cash_sum[period_index] += cash;
            coupon_pv_sum[period_index] += discounted;
            state.unpaid_coupon = terms.coupon_memory ? (coupon_barrier_ok ? next_memory(period, qualifying) : std::max(static_cast<double>(period.total_fixings), 1.0)) : 0.0;
            if (is_ko_period) break;
        }

        const int settlement_date = state.terminated ? cube.dates[static_cast<std::size_t>(state.ko_observation)] : terms.maturity_date;
        const std::size_t funding_observation = state.terminated ? static_cast<std::size_t>(state.ko_observation) : final_index;
        const double funding_cash = funding_amount(terms.funding, terms.notional);
        funding_cash_by_observation[funding_observation] += funding_cash;
        funding_pv_by_observation[funding_observation] += funding_cash * std::exp(-terms.rate * std::max(settlement_date - terms.evaluation_date, 0) / 365.0);
        if (!state.terminated) {
            const double payoff = terminal_option_payoff(terms.terminal, state.knock_in_seen, terminal_performance);
            const double cash = terms.notional * payoff;
            put_cash_sum += cash;
            put_pv_sum += cash * std::exp(-terms.rate * std::max(terms.maturity_date - terms.evaluation_date, 0) / 365.0);
        }
    }

    const double inv_paths = 1.0 / static_cast<double>(cube.paths);
    double coupon_pv = 0.0;
    for (std::size_t i = 0; i < terms.coupon_periods.size(); ++i) {
        const double expected_cash = coupon_cash_sum[i] * inv_paths;
        const double expected_pv = coupon_pv_sum[i] * inv_paths * terms.coupon_quote_scale / std::max(terms.notional, 1.0);
        coupon_pv += expected_pv;
        if (expected_cash != 0.0) {
            result.cashflows.push_back({"COUPON", std::to_string(terms.coupon_periods[i].payment_date), expected_cash, expected_pv,
                                        terms.currency, false, "/fcn_terms/coupon_periods/" + std::to_string(i)});
        }
    }
    double funding_pv = 0.0;
    for (std::size_t obs = 0; obs < cube.observations; ++obs) {
        if (funding_cash_by_observation[obs] == 0.0) continue;
        const double expected_cash = funding_cash_by_observation[obs] * inv_paths;
        const double expected_pv = funding_pv_by_observation[obs] * inv_paths / std::max(terms.notional, 1.0);
        funding_pv += expected_pv;
        result.cashflows.push_back({"FUNDING", std::to_string(cube.dates[obs]), expected_cash, expected_pv,
                                    terms.currency, false, "/fcn_terms/funding"});
    }
    const double put_pv = put_pv_sum * inv_paths / std::max(terms.notional, 1.0);
    if (put_cash_sum != 0.0) {
        const auto descriptor = settlement_for(terms, put_cash_sum);
        result.cashflows.push_back({"PUT / Terminal Optionality", std::to_string(terms.maturity_date), -put_cash_sum * inv_paths,
                                    -put_pv, terms.currency, descriptor.physical_delivery, "/fcn_terms/terminal"});
    }
    result.legs = {
        {"funding", "FUNDING", "funding", funding_pv, terms.currency, "funding.principal_return"},
        {"coupon", "COUPON", "coupon", coupon_pv, terms.currency, "coupon.range_accrual"},
        {"put", "PUT / Terminal Optionality", "terminal_optionality", -put_pv, terms.currency, "terminal.ki_put"},
    };
    result.pv = funding_pv + coupon_pv - put_pv;
    result.ki_probability = static_cast<double>(ki_paths) * inv_paths;
    result.ko_probability = static_cast<double>(ko_paths) * inv_paths;
    result.selected_branch = ko_paths == 0 ? (ki_paths == 0 ? "no_ki_maturity" : (ki_paths == cube.paths ? "ki_maturity" : "mixed_ki_maturity"))
                                             : (local_ko_paths == ko_paths ? "local_ko" : (global_ko_paths == ko_paths ? "global_ko" : "mixed_ko"));
    return result;
}

std::string price_canonical_request_json(const std::string& request_json,
                                         const std::vector<double>& paths,
                                         std::size_t paths_count,
                                         std::size_t observations,
                                         std::size_t underlyings,
                                         const std::vector<int>& dates) {
    try {
        const CompiledTerms compiled = compile_canonical_terms(request_json);
        if (compiled.status != EvidenceStatus::verified) return assemble_json(error_result(compiled.status, compiled.reason));
        EngineResult result = price(compiled.terms, {paths, dates, paths_count, observations, underlyings});
        // Error envelopes still identify the deterministic request/process that
        // failed so callers can retain lifecycle provenance without guessing.
        result.source_revision = compiled.terms.source_revision;
        result.request_id = compiled.terms.request_id;
        result.process_id = compiled.terms.process_id;
        return assemble_json(result);
    } catch (const std::exception& error) {
        return assemble_json(error_result(EvidenceStatus::unsupported, error.what()));
    }
}

}  // namespace fina::risk::fcn
