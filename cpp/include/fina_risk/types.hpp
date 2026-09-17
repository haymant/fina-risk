#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace fina::risk::fcn {

enum class Comparison { greater, greater_equal, less, less_equal };
enum class KnockOutKind { none, local, global };
enum class SettlementKind { cash, physical };
enum class EvidenceStatus { verified, unsupported, ambiguous, unresolved };

inline const char* to_string(Comparison value) noexcept {
    switch (value) {
        case Comparison::greater: return ">";
        case Comparison::greater_equal: return ">=";
        case Comparison::less: return "<";
        case Comparison::less_equal: return "<=";
    }
    return "unknown";
}

inline const char* to_string(KnockOutKind value) noexcept {
    switch (value) {
        case KnockOutKind::none: return "none";
        case KnockOutKind::local: return "local_ko";
        case KnockOutKind::global: return "global_ko";
    }
    return "none";
}

inline const char* to_string(SettlementKind value) noexcept {
    return value == SettlementKind::physical ? "physical" : "cash";
}

inline const char* to_string(EvidenceStatus value) noexcept {
    switch (value) {
        case EvidenceStatus::verified: return "implemented_and_evidenced";
        case EvidenceStatus::unsupported: return "unsupported";
        case EvidenceStatus::ambiguous: return "ambiguous_in_source";
        case EvidenceStatus::unresolved: return "unresolved";
    }
    return "unresolved";
}

struct Cashflow {
    std::string leg;
    std::string date;
    double amount{};
    double discounted_amount{};
    std::string currency;
    bool physical_delivery{};
    std::string source_term_path;
};

struct StateTransition {
    std::size_t path_index{};
    std::string date;
    std::string transition;
    std::string source_term_path;
};

}  // namespace fina::risk::fcn
