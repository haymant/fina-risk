#pragma once

#include "fina_risk/terms.hpp"

namespace fina::risk::fcn {

inline double funding_amount(const FundingTerms& terms, double notional) noexcept {
    return terms.enabled ? notional * terms.return_ratio : 0.0;
}

}  // namespace fina::risk::fcn
