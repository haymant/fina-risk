#pragma once

#include <cstddef>
#include <vector>

namespace fina::risk::fcn {

std::size_t observation_on_or_before(const std::vector<int>& dates, int target, std::size_t limit) noexcept;
std::size_t first_observation_after(const std::vector<int>& dates, int target, std::size_t limit) noexcept;

}  // namespace fina::risk::fcn
