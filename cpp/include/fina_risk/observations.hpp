#pragma once

#include <cstddef>
#include <vector>

namespace fina::risk::fcn {

struct ObservationCubeView {
    const std::vector<double>& values;
    const std::vector<int>& dates;
    std::size_t paths{};
    std::size_t observations{};
    std::size_t underlyings{};

    [[nodiscard]] inline double spot(std::size_t path, std::size_t observation, std::size_t underlying) const noexcept {
        return values[(path * observations + observation) * underlyings + underlying];
    }
};

inline bool is_valid_fixing(double value) noexcept {
    return value == value && value > 0.0;
}

}  // namespace fina::risk::fcn
