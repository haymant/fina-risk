#pragma once

#include "fina_risk/terms.hpp"

#include <algorithm>

namespace fina::risk::fcn {

inline bool in_range(double value, const CouponPeriodTerms& terms, bool lower_inclusive, bool upper_inclusive) noexcept {
    const bool above = lower_inclusive ? value >= terms.lower_bound : value > terms.lower_bound;
    const bool below = upper_inclusive ? value <= terms.upper_bound : value < terms.upper_bound;
    return above && below;
}

inline double period_rate(const CouponPeriodTerms& terms, double qualifying_fixings, double carried_memory) noexcept {
    const double total = std::max(static_cast<double>(terms.total_fixings), 1.0);
    const double unpaid = std::max(qualifying_fixings - static_cast<double>(terms.already_paid_fixings), 0.0);
    return terms.fixed_coupon + terms.range_rate * std::min((unpaid + carried_memory) / total, 1.0);
}

inline double next_memory(const CouponPeriodTerms& terms, double qualifying_fixings) noexcept {
    const double total = std::max(static_cast<double>(terms.total_fixings), 1.0);
    const double unpaid = std::max(qualifying_fixings - static_cast<double>(terms.already_paid_fixings), 0.0);
    return std::max(total - unpaid, 0.0);
}

}  // namespace fina::risk::fcn
