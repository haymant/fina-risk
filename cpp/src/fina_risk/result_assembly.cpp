#include "fina_risk/engine.hpp"

#include <nlohmann/json.hpp>

namespace fina::risk::fcn {
namespace {
using json = nlohmann::json;
}

std::string assemble_json(const EngineResult& result) {
    json legs = json::array();
    for (const auto& leg : result.legs) {
        legs.push_back({{"id", leg.id}, {"name", leg.name}, {"role", leg.role}, {"pv", leg.pv},
                        {"currency", leg.currency}, {"payoff_graph_node", leg.payoff_graph_node},
                        {"origin", leg.origin}, {"evidence_status", leg.evidence_status}});
    }
    json cashflows = json::array();
    for (const auto& flow : result.cashflows) {
        cashflows.push_back({{"leg", flow.leg}, {"date", flow.date}, {"amount", flow.amount},
                             {"discounted_amount", flow.discounted_amount}, {"currency", flow.currency},
                             {"physical_delivery", flow.physical_delivery}, {"source_term_path", flow.source_term_path}});
    }
    json transitions = json::array();
    for (const auto& transition : result.state_transitions) {
        transitions.push_back({{"path_index", transition.path_index}, {"date", transition.date},
                               {"transition", transition.transition}, {"source_term_path", transition.source_term_path}});
    }
    return json{{"status", result.status}, {"engine", result.engine_marker}, {"engine_marker", result.engine_marker},
                {"source_revision", result.source_revision}, {"request_id", result.request_id},
                {"process_id", result.process_id}, {"terms_schema", result.terms_schema},
                {"result_schema", result.result_schema}, {"pv", result.pv}, {"legs", legs},
                {"cashflows", cashflows}, {"state_transitions", transitions},
                {"selected_branch", result.selected_branch},
                {"evidence_status", to_string(result.evidence_status)},
                {"unsupported_reason", result.unsupported_reason},
                {"ki_probability", result.ki_probability}, {"ko_probability", result.ko_probability}}.dump(2);
}

}  // namespace fina::risk::fcn
