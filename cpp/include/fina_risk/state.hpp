#pragma once

#include "fina_risk/types.hpp"

#include <vector>

namespace fina::risk::fcn {

struct PathState {
    bool terminated{};
    bool knock_in_seen{};
    bool local_ko_seen{};
    bool global_ko_seen{};
    KnockOutKind selected_ko{KnockOutKind::none};
    int ko_observation{-1};
    std::vector<unsigned char> local_memory_locks;
    std::vector<unsigned char> global_memory_locks;
    double unpaid_coupon{};
};

}  // namespace fina::risk::fcn
