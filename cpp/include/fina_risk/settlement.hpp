#pragma once

#include "fina_risk/terms.hpp"

namespace fina::risk::fcn {

struct SettlementDescriptor {
    SettlementKind kind{SettlementKind::cash};
    bool physical_delivery{};
};

inline SettlementDescriptor settlement_for(const FcnTerms& terms, double terminal_payoff) noexcept {
    return {terms.terminal.settlement, terms.physical_delivery && terminal_payoff > 0.0};
}

}  // namespace fina::risk::fcn
