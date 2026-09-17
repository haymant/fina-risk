#pragma once

#include "fina_risk/terms.hpp"

namespace fina::risk::fcn {

inline bool compare(double value, double barrier, Comparison op) noexcept {
    switch (op) {
        case Comparison::greater: return value > barrier;
        case Comparison::greater_equal: return value >= barrier;
        case Comparison::less: return value < barrier;
        case Comparison::less_equal: return value <= barrier;
    }
    return false;
}

inline KnockOutKind resolve_ko(bool local_hit, bool global_hit, KnockOutKind precedence) noexcept {
    if (local_hit && global_hit) return precedence;
    if (global_hit) return KnockOutKind::global;
    if (local_hit) return KnockOutKind::local;
    return KnockOutKind::none;
}

}  // namespace fina::risk::fcn
