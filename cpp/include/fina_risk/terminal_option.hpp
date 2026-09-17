#pragma once

#include "fina_risk/barriers.hpp"
#include "fina_risk/terms.hpp"

#include <algorithm>

namespace fina::risk::fcn {

inline double terminal_option_payoff(const TerminalTerms& terms, bool knock_in, double performance) noexcept {
    if (terms.ki_enabled && !knock_in) return 0.0;
    return std::max(terms.strike - performance, 0.0);
}

inline bool update_knock_in(const TerminalTerms& terms, double performance) noexcept {
    return terms.ki_enabled && compare(performance, terms.ki_barrier, terms.ki_operator);
}

}  // namespace fina::risk::fcn
