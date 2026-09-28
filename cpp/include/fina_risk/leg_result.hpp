#pragma once

#include "fina_risk/types.hpp"

#include <string>
#include <vector>

namespace fina::risk::fcn {

struct LegResult {
    std::string id;
    std::string name;
    std::string role;
    double pv{};
    std::string currency;
    // The unit `pv` is denominated in, stated rather than assumed. Legs are
    // quoted per unit of notional, and the coupon leg additionally carries
    // `coupon_quote_scale` (the ten-point price quotation convention). Cashflows
    // in the same result are absolute currency amounts. Both used to be reported
    // with no unit at all, which is how a per-unit number ended up in a field
    // called `discounted_amount` next to an absolute one.
    std::string unit{"currency_per_unit_notional"};
    std::string payoff_graph_node;
    std::string origin{"pricing_engine"};
    std::string evidence_status{"implemented_and_evidenced"};
};

struct EngineResult {
    std::string status{"ok"};
    std::string engine_marker{"cpp_fcn_rakiplus_v1"};
    std::string source_revision;
    std::string request_id;
    std::string process_id;
    std::string terms_schema{"fcn-terms-projection.v1"};
    std::string result_schema{"fcn-native-pricing-result.v2"};
    double pv{};
    std::vector<LegResult> legs;
    std::vector<Cashflow> cashflows;
    std::vector<StateTransition> state_transitions;
    std::string selected_branch;
    EvidenceStatus evidence_status{EvidenceStatus::verified};
    std::string unsupported_reason;
    double ki_probability{};
    double ko_probability{};
};

}  // namespace fina::risk::fcn
