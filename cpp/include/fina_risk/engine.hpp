#pragma once

#include "fina_risk/leg_result.hpp"
#include "fina_risk/observations.hpp"
#include "fina_risk/terms.hpp"

#include <string>
#include <vector>

namespace fina::risk::fcn {

struct CompiledTerms {
    FcnTerms terms;
    EvidenceStatus status{EvidenceStatus::verified};
    std::string reason;
};

CompiledTerms compile_canonical_terms(const std::string& request_json);
EngineResult price(const FcnTerms& terms, const ObservationCubeView& cube);
std::string assemble_json(const EngineResult& result);
std::string price_canonical_request_json(const std::string& request_json,
                                         const std::vector<double>& paths,
                                         std::size_t paths_count,
                                         std::size_t observations,
                                         std::size_t underlyings,
                                         const std::vector<int>& dates);

}  // namespace fina::risk::fcn
