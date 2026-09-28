#include "fina_risk_cpp.hpp"

#ifdef FINA_RISK_HAS_PYBIND11
#include <cstring>

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <string>
#include <vector>

namespace py = pybind11;

namespace {

// The five handwritten bindings are deprecated, not removed: they keep their
// exact call shapes and marshalling, emit a DeprecationWarning, and delegate to
// run_termsheet so the term sheet picks the lane. PyErr_WarnEx returns -1 when
// the warning cannot be issued (warnings-as-errors); propagate that as a real
// exception instead of silently continuing.
void warn_deprecated(const char* name) {
    const std::string message =
        std::string("fina_risk_cpp.") + name +
        " is deprecated (kept for ABI compatibility). Call fina_risk_cpp.run_termsheet("
        "request_json, market_json='', cube, dates, ...) and let the term sheet pick the lane.";
    if (PyErr_WarnEx(PyExc_DeprecationWarning, message.c_str(), 1) != 0) {
        throw py::error_already_set();
    }
}

// Decode the union `cube` argument of run_termsheet: raw little-endian float32
// bytes, a 2-D float32 (P, U) terminal cube for the parity lane, or a 3-D
// float64 (P, O, U) daily cube for the daily lanes. Unsized dimensions are 0.
void decode_cube(py::object cube, std::vector<float>& terminal, std::vector<double>& daily,
                 std::size_t& P, std::size_t& O, std::size_t& U) {
    if (cube.is_none()) return;
    if (py::isinstance<py::bytes>(cube)) {
        const std::string buffer = cube.cast<std::string>();
        const auto* data = reinterpret_cast<const float*>(buffer.data());
        terminal.assign(data, data + buffer.size() / sizeof(float));
        return;
    }
    auto array = py::array::ensure(cube);
    if (!array) {
        throw py::type_error(
            "cube must be a float32 2-D (P, U) terminal, float64 3-D (P, O, U) daily, "
            "or a raw float32 bytes buffer");
    }
    const auto info = array.request();
    if (info.ndim == 2) {
        auto f32 = py::array_t<float, py::array::c_style | py::array::forcecast>::ensure(cube);
        if (!f32) throw py::type_error("2-D cube must be a float32 array-like (P, U) terminal");
        P = static_cast<std::size_t>(info.shape[0]);
        U = static_cast<std::size_t>(info.shape[1]);
        terminal.assign(f32.data(), f32.data() + f32.size());
        return;
    }
    if (info.ndim == 3) {
        auto f64 = py::array_t<double, py::array::c_style | py::array::forcecast>::ensure(cube);
        if (!f64) {
            throw py::type_error("3-D cube must be a float64 array-like (P, O, U) daily");
        }
        P = static_cast<std::size_t>(info.shape[0]);
        O = static_cast<std::size_t>(info.shape[1]);
        U = static_cast<std::size_t>(info.shape[2]);
        daily.assign(f64.data(), f64.data() + f64.size());
        return;
    }
    throw py::value_error(
        "cube must be (P, U) terminal, (P, O, U) daily, or raw float32 bytes");
}

// `paths` is accepted as a float so JSON callers passing e.g. 30000.0 do not
// hit a pybind11 `TypeError: incompatible function arguments`.
std::size_t truncate_paths(double paths) {
    return paths <= 0.0 ? std::size_t{0} : static_cast<std::size_t>(paths);
}

}  // namespace

PYBIND11_MODULE(fina_risk_cpp, module) {
    module.doc() = "Native fina-risk pricing/risk kernel; MCP remains in Python.";
    py::class_<fina::risk::RiskResult>(module, "RiskResult")
        .def_readonly("pv", &fina::risk::RiskResult::pv)
        .def_readonly("put_option_price", &fina::risk::RiskResult::put_option_price);

    // -----------------------------------------------------------------------
    // THE ONE dispatcher entry point. The term sheet picks the lane; nobody
    // hardcodes which kernel to call. `cube` is decoded by dimensionality:
    // bytes/2-D float32 -> the terminal (P, U) parity cube, 3-D float64 -> the
    // daily (P, O, U) cube; `dates` only exist for the daily lanes. Success
    // returns the lane payload plus an additive `dispatch` key; a mismatch is
    // refused with {"status": "refused", "dispatch": {lane, reason, refusals}}.
    // -----------------------------------------------------------------------
    module.def("run_termsheet",
        [](const std::string& request_json, const std::string& market_json, py::object cube,
           py::object dates, double paths, std::uint64_t seed, double bump) {
            std::vector<float> terminal;
            std::vector<double> daily;
            std::size_t P = 0, O = 0, U = 0;
            decode_cube(cube, terminal, daily, P, O, U);
            std::vector<int> dt;
            if (!dates.is_none()) {
                auto date_array =
                    py::array_t<int, py::array::c_style | py::array::forcecast>::ensure(dates);
                if (!date_array) throw py::type_error("dates must be an int array");
                dt.assign(date_array.data(), date_array.data() + date_array.size());
            }
            return fina::risk::run_termsheet(request_json, market_json, terminal, daily, P, O, U,
                                             dt, truncate_paths(paths), seed, bump);
        },
        py::arg("request_json"), py::arg("market_json") = "", py::arg("cube") = py::none(),
        py::arg("dates") = py::none(), py::arg("paths") = 30000.0, py::arg("seed") = 1729,
        py::arg("bump") = 0.01);

    // -----------------------------------------------------------------------
    // Deprecated lane bindings. Each one keeps its exact call shape and
    // marshalling, warns, and delegates through run_termsheet so the lane that
    // runs is chosen from the request, not from which binding was reached.
    // -----------------------------------------------------------------------
    module.def("price_fixture", [](const std::string& request_json, double paths,
                                   std::uint64_t seed) {
        warn_deprecated("price_fixture");
        const auto path_count = truncate_paths(paths);
        return fina::risk::run_termsheet(request_json, "", {}, {}, 0, 0, 0, {}, path_count, seed,
                                         0.01);
    }, py::arg("request_json"), py::arg("paths") = 30000.0, py::arg("seed") = 1729);

    // `terminal` accepts either a float32 array-like (the normal CRN cube) or a
    // raw little-endian float32 `bytes` buffer; both are normalised to the same
    // std::vector<float> cube.
    module.def("run_cpp_parity",
        [](const std::string& instruments_json, const std::string& market_json,
           py::object terminal, std::uint64_t seed, double bump) {
            warn_deprecated("run_cpp_parity");
            std::vector<float> cube;
            std::vector<double> unused_daily;
            std::size_t P = 0, O = 0, U = 0;
            decode_cube(terminal, cube, unused_daily, P, O, U);
            return fina::risk::run_termsheet(instruments_json, market_json, cube, unused_daily, P,
                                             O, U, {}, 0, seed, bump);
        },
        py::arg("instruments_json"), py::arg("market_json"), py::arg("terminal"),
        py::arg("seed") = 20260909, py::arg("bump") = 0.01);

    // Faithful daily lifecycle lane: per-period daily in-range fixing counts
    // (N1/N2), memory carry, global-KO accrual termination, and an EKI
    // (final-fixing) worst-of put. `paths` is (paths, observations, underlyings).
    module.def("run_daily_termsheet",
        [](const std::string& request_json,
           py::array_t<double, py::array::c_style | py::array::forcecast> paths,
           py::array_t<int, py::array::c_style | py::array::forcecast> dates, double bump) {
            warn_deprecated("run_daily_termsheet");
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
            return fina::risk::run_termsheet(request_json, "", {}, flat, P, O, U, dt, 0, 1729,
                                             bump);
        },
        py::arg("request_json"), py::arg("paths"), py::arg("dates"), py::arg("bump") = 0.01);
    module.def("run_daily_termsheet_batch",
        [](const std::string& instruments_json, const std::string& market_json,
           py::array_t<double, py::array::c_style | py::array::forcecast> paths,
           py::array_t<int, py::array::c_style | py::array::forcecast> dates) {
            warn_deprecated("run_daily_termsheet_batch");
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
            // The batch ABI takes no bump; run_termsheet's default covers it.
            return fina::risk::run_termsheet(instruments_json, market_json, {}, flat, P, O, U, dt,
                                             0, 1729, 0.01);
        },
        py::arg("instruments_json"), py::arg("market_json"), py::arg("paths"), py::arg("dates"));
    // Canonical RFQ entrypoint: compile explicit pricing-request legs and
    // fcn_terms once, then execute the real typed C++ lifecycle kernel.
    module.def("price_fcn_rakiplus",
        [](const std::string& canonical_request_json,
           py::array_t<double, py::array::c_style | py::array::forcecast> paths,
           py::array_t<int, py::array::c_style | py::array::forcecast> dates) {
            warn_deprecated("price_fcn_rakiplus");
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
            // The FCN ABI takes no bump; run_termsheet's default covers it.
            return fina::risk::run_termsheet(canonical_request_json, "", {}, flat, P, O, U, dt, 0,
                                             1729, 0.01);
        },
        py::arg("canonical_request_json"), py::arg("paths"), py::arg("dates"));
}
#endif
