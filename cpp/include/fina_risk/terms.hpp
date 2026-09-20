#pragma once

#include "fina_risk/types.hpp"

#include <algorithm>
#include <cstddef>
#include <string>
#include <utility>
#include <vector>

namespace fina::risk::fcn {

/// Linear-in-rate interpolation of a zero curve (ACT/365) to a target Excel
/// serial date; flat beyond the first/last pillar. Empty curve -> `fallback`.
inline double curve_rate_at(const std::vector<std::pair<int, double>>& pillars, double fallback, int target_date) {
    if (pillars.empty()) {
        return fallback;
    }
    if (target_date <= pillars.front().first) {
        return pillars.front().second;
    }
    if (target_date >= pillars.back().first) {
        return pillars.back().second;
    }
    for (std::size_t i = 1; i < pillars.size(); ++i) {
        if (target_date <= pillars[i].first) {
            const int d0 = pillars[i - 1].first;
            const int d1 = pillars[i].first;
            const double r0 = pillars[i - 1].second;
            const double r1 = pillars[i].second;
            const double w = (d1 == d0) ? 0.0 : static_cast<double>(target_date - d0) / static_cast<double>(d1 - d0);
            return r0 + w * (r1 - r0);
        }
    }
    return pillars.back().second;
}

struct CouponPeriodTerms {
    std::string id;
    int start_date{};
    int end_date{};
    int payment_date{};
    double range_rate{};
    double fixed_coupon{};
    double lower_bound{};
    double upper_bound{1.0e12};
    int already_paid_fixings{};
    int total_fixings{};
    double coupon_barrier{};
    double local_ko_barrier{};
    double local_ko_coupon{};
    double global_ko_coupon{};
};

struct BarrierTerms {
    bool local_enabled{};
    bool global_enabled{};
    bool memory_ko{};
    double local_barrier{};
    double global_barrier{};
    Comparison local_operator{Comparison::greater_equal};
    Comparison global_operator{Comparison::greater_equal};
    KnockOutKind same_day_precedence{KnockOutKind::global};
};

struct TerminalTerms {
    bool ki_enabled{true};
    double ki_barrier{};
    Comparison ki_operator{Comparison::less_equal};
    double strike{};
    SettlementKind settlement{SettlementKind::cash};
};

struct FundingTerms {
    bool enabled{true};
    double return_ratio{1.0};
};

struct FcnTerms {
    std::string instrument_key;
    std::string request_id;
    std::string process_id;
    std::string currency{"USD"};
    double notional{1.0};
    double coupon_quote_scale{1.0};
    int evaluation_date{};
    int final_fixing_date{};
    int maturity_date{};
    double rate{};
    /// Zero-curve pillars `(excel_serial_date, rate)`, sorted ascending; used to
    /// discount each cashflow at its own settlement/payment date. `rate` stays the
    /// first pillar for backward compatibility.
    std::vector<std::pair<int, double>> curve_pillars;
    std::vector<double> reference_spots;
    std::vector<CouponPeriodTerms> coupon_periods;
    BarrierTerms barriers;
    TerminalTerms terminal;
    FundingTerms funding;
    bool use_worst_of{true};
    bool range_lower_inclusive{true};
    bool range_upper_inclusive{true};
    bool coupon_memory{};
    bool physical_delivery{};
    std::string source_revision;
};

}  // namespace fina::risk::fcn
