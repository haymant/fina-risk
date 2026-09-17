#pragma once

#include "fina_risk/types.hpp"

#include <string>
#include <vector>

namespace fina::risk::fcn {

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
