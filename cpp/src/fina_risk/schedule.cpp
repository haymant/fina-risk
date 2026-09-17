#include "fina_risk/schedule.hpp"

#include <algorithm>

namespace fina::risk::fcn {

std::size_t observation_on_or_before(const std::vector<int>& dates, int target, std::size_t limit) noexcept {
    const std::size_t bounded = std::min(limit, dates.size());
    const auto first = dates.begin();
    const auto last = first + static_cast<std::ptrdiff_t>(bounded);
    const auto found = std::upper_bound(first, last, target);
    return found == first ? 0U : static_cast<std::size_t>(found - first - 1);
}

std::size_t first_observation_after(const std::vector<int>& dates, int target, std::size_t limit) noexcept {
    const std::size_t bounded = std::min(limit, dates.size());
    const auto first = dates.begin();
    const auto last = first + static_cast<std::ptrdiff_t>(bounded);
    return static_cast<std::size_t>(std::upper_bound(first, last, target) - first);
}

}  // namespace fina::risk::fcn
