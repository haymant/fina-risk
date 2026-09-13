#include "fina_risk_cpp.hpp"

#ifdef FINA_RISK_HAS_PYBIND11
#include <cstring>

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

PYBIND11_MODULE(fina_risk_cpp, module) {
    module.doc() = "Native fina-risk pricing/risk kernel; MCP remains in Python.";
    py::class_<fina::risk::RiskResult>(module, "RiskResult")
        .def_readonly("pv", &fina::risk::RiskResult::pv)
        .def_readonly("put_option_price", &fina::risk::RiskResult::put_option_price);
    // `paths` is accepted as a float so JSON callers passing e.g. 30000.0 do
    // not hit a pybind11 `TypeError: incompatible function arguments`; it is
    // truncated to a non-negative std::size_t internally.
    module.def("price_fixture", [](const std::string& request_json, double paths, std::uint64_t seed) {
        const auto path_count = paths <= 0.0 ? std::size_t{0} : static_cast<std::size_t>(paths);
        return fina::risk::to_json(fina::risk::price_fixture(request_json, path_count, seed));
    }, py::arg("request_json"), py::arg("paths") = 30000.0, py::arg("seed") = 1729);
    // `terminal` accepts either a float32 array-like (the normal CRN cube) or a
    // raw little-endian float32 `bytes` buffer. The bytes form is the common
    // caller mistake that previously raised `TypeError: incompatible function
    // arguments`; both are normalised to the same `std::vector<float>` cube.
    module.def("run_cpp_parity",
        [](const std::string& instruments_json, const std::string& market_json,
           py::object terminal, std::uint64_t seed, double bump) {
            std::vector<float> cube;
            if (py::isinstance<py::bytes>(terminal)) {
                const std::string buffer = terminal.cast<std::string>();
                const auto* data = reinterpret_cast<const float*>(buffer.data());
                cube.assign(data, data + buffer.size() / sizeof(float));
            } else {
                auto array = py::array_t<float, py::array::c_style | py::array::forcecast>::ensure(terminal);
                if (!array) {
                    throw py::type_error(
                        "terminal must be a float32 array-like or a raw float32 bytes buffer");
                }
                cube.assign(array.data(), array.data() + array.size());
            }
            return fina::risk::to_json(fina::risk::run_cpp_parity(instruments_json, market_json, cube, seed, bump));
        },
        py::arg("instruments_json"), py::arg("market_json"), py::arg("terminal"),
        py::arg("seed") = 20260909, py::arg("bump") = 0.01);
    // Faithful daily lifecycle lane: per-period daily in-range fixing counts
    // (N1/N2), memory carry, global-KO accrual termination, and an EKI
    // (final-fixing) worst-of put. `paths` is (paths, observations, underlyings).
    module.def("run_daily_termsheet",
        [](const std::string& request_json,
           py::array_t<double, py::array::c_style | py::array::forcecast> paths,
           py::array_t<int, py::array::c_style | py::array::forcecast> dates,
           double bump) {
            auto buf = paths.request();
            if (buf.ndim != 3) {
                throw py::value_error("paths must be (paths, observations, underlyings)");
            }
            const std::size_t P = static_cast<std::size_t>(buf.shape[0]);
            const std::size_t O = static_cast<std::size_t>(buf.shape[1]);
            const std::size_t U = static_cast<std::size_t>(buf.shape[2]);
            std::vector<double> flat(P * O * U);
            std::memcpy(flat.data(), buf.ptr, flat.size() * sizeof(double));
            std::vector<int> dt(dates.data(), dates.data() + dates.size());
            return fina::risk::run_daily_termsheet_json(request_json, flat, P, O, U, dt, bump);
        },
        py::arg("request_json"), py::arg("paths"), py::arg("dates"), py::arg("bump") = 0.01);
    module.def("run_daily_termsheet_batch",
        [](const std::string& instruments_json, const std::string& market_json,
           py::array_t<double, py::array::c_style | py::array::forcecast> paths,
           py::array_t<int, py::array::c_style | py::array::forcecast> dates) {
            auto buf = paths.request();
            if (buf.ndim != 3) {
                throw py::value_error("paths must be (paths, observations, underlyings)");
            }
            const std::size_t P = static_cast<std::size_t>(buf.shape[0]);
            const std::size_t O = static_cast<std::size_t>(buf.shape[1]);
            const std::size_t U = static_cast<std::size_t>(buf.shape[2]);
            std::vector<double> flat(P * O * U);
            std::memcpy(flat.data(), buf.ptr, flat.size() * sizeof(double));
            std::vector<int> dt(dates.data(), dates.data() + dates.size());
            return fina::risk::run_daily_termsheet_batch_json(instruments_json, market_json, flat, P, O, U, dt);
        },
        py::arg("instruments_json"), py::arg("market_json"), py::arg("paths"), py::arg("dates"));
}
#endif
