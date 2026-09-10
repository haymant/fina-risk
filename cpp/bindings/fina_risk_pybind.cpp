#include "fina_risk_cpp.hpp"

#ifdef FINA_RISK_HAS_PYBIND11
#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

PYBIND11_MODULE(fina_risk_cpp, module) {
    module.doc() = "Native fina-risk pricing/risk kernel; MCP remains in Python.";
    py::class_<fina::risk::RiskResult>(module, "RiskResult")
        .def_readonly("pv", &fina::risk::RiskResult::pv)
        .def_readonly("put_option_price", &fina::risk::RiskResult::put_option_price);
    module.def("price_fixture", [](const std::string& request_json, std::size_t paths, std::uint64_t seed) {
        return fina::risk::to_json(fina::risk::price_fixture(request_json, paths, seed));
    }, py::arg("request_json"), py::arg("paths") = 30000, py::arg("seed") = 1729);
    module.def("run_cpp_parity",
        [](const std::string& instruments_json, const std::string& market_json,
           py::array_t<float, py::array::c_style | py::array::forcecast> terminal,
           std::uint64_t seed, double bump) {
            const std::vector<float> cube(terminal.data(), terminal.data() + terminal.size());
            return fina::risk::to_json(fina::risk::run_cpp_parity(instruments_json, market_json, cube, seed, bump));
        },
        py::arg("instruments_json"), py::arg("market_json"), py::arg("terminal"),
        py::arg("seed") = 20260909, py::arg("bump") = 0.01);
}
#endif
