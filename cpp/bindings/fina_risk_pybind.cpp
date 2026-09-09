#include "fina_risk_cpp.hpp"

#ifdef FINA_RISK_HAS_PYBIND11
#include <pybind11/pybind11.h>

namespace py = pybind11;

PYBIND11_MODULE(fina_risk_cpp, module) {
    module.doc() = "Native fina-risk pricing/risk kernel; MCP remains in Python.";
    py::class_<fina::risk::RiskResult>(module, "RiskResult")
        .def_readonly("pv", &fina::risk::RiskResult::pv)
        .def_readonly("put_option_price", &fina::risk::RiskResult::put_option_price);
    module.def("price_fixture", [](const std::string& request_json, std::size_t paths, std::uint64_t seed) {
        return fina::risk::to_json(fina::risk::price_fixture(request_json, paths, seed));
    }, py::arg("request_json"), py::arg("paths") = 30000, py::arg("seed") = 1729);
}
#endif
