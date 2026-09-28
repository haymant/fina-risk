cmake_minimum_required(VERSION 3.20)
project(fina_risk_cpp VERSION 0.2.0 LANGUAGES CXX)

option(FINA_RISK_BUILD_XAD_AAD "Build the native XAD reverse-mode AAD benchmark" OFF)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT DEFINED CMAKE_POSITION_INDEPENDENT_CODE)
  set(CMAKE_POSITION_INDEPENDENT_CODE ON)
endif()

find_package(nlohmann_json 3.11 REQUIRED)
find_package(OpenMP QUIET)
find_package(QuantLib QUIET)
find_package(XAD QUIET)
find_package(Arrow QUIET)
find_package(duckdb QUIET)
find_package(AWSSDK QUIET COMPONENTS s3)
find_package(pybind11 QUIET)

add_library(fina_risk_core
  src/fina_risk_cpp.cpp
  src/fina_risk/engine.cpp
  src/fina_risk/schedule.cpp
  src/fina_risk/result_assembly.cpp
  src/fina_risk/coupon.cpp
  src/fina_risk/barriers.cpp
  src/fina_risk/terminal_option.cpp
  src/fina_risk/funding.cpp
  src/fina_risk/settlement.cpp
)
target_include_directories(fina_risk_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(fina_risk_core PUBLIC nlohmann_json::nlohmann_json)
# Semantic conformance is the native pricing contract; do not use fast-math to
# change barrier equality or lifecycle branch behaviour.
target_compile_options(fina_risk_core PRIVATE $<$<CONFIG:Release>:-O3;-march=native;-mtune=native;-ffast-math>)
# The canonical lifecycle source must retain NaN/missing-fixing checks and
# ordered barrier equality semantics. It is intentionally excluded from the
# legacy benchmark's fast-math policy; the ABI-compatible lanes keep it.
set_source_files_properties(src/fina_risk/engine.cpp PROPERTIES COMPILE_OPTIONS "-fno-fast-math")

if (OpenMP_CXX_FOUND)
  target_link_libraries(fina_risk_core PUBLIC OpenMP::OpenMP_CXX)
  target_compile_definitions(fina_risk_core PUBLIC FINA_RISK_HAS_OPENMP=1)
endif()

if (QuantLib_FOUND AND XAD_FOUND)
  target_compile_definitions(fina_risk_core PUBLIC FINA_RISK_HAS_QUANTLIB_XAD=1)
  target_link_libraries(fina_risk_core PUBLIC QuantLib::QuantLib XAD::XAD)
endif()
if (duckdb_FOUND AND Arrow_FOUND AND AWSSDK_FOUND)
  target_compile_definitions(fina_risk_core PUBLIC FINA_RISK_HAS_DUCKDB=1)
  target_link_libraries(fina_risk_core PUBLIC duckdb Arrow::arrow_shared AWS::aws-cpp-sdk-s3)
endif()

add_executable(fina-risk-cpp-benchmark apps/benchmark.cpp)
target_link_libraries(fina-risk-cpp-benchmark PRIVATE fina_risk_core)
target_compile_options(fina-risk-cpp-benchmark PRIVATE $<$<CONFIG:Release>:-O3;-march=native;-mtune=native>)

add_executable(fina-risk-cpp-e2e apps/e2e_benchmark.cpp)
target_link_libraries(fina-risk-cpp-e2e PRIVATE fina_risk_core)
if (OpenMP_CXX_FOUND)
  target_link_libraries(fina-risk-cpp-e2e PRIVATE OpenMP::OpenMP_CXX)
endif()
target_compile_options(fina-risk-cpp-e2e PRIVATE $<$<CONFIG:Release>:-O3;-march=native;-mtune=native>)

add_executable(fina-risk-cpp-parity apps/parity_benchmark.cpp)
target_link_libraries(fina-risk-cpp-parity PRIVATE fina_risk_core)
target_compile_options(fina-risk-cpp-parity PRIVATE $<$<CONFIG:Release>:-O3;-march=native;-mtune=native>)

add_executable(fina-risk-cpp-daily-termsheet apps/daily_termsheet.cpp)
target_link_libraries(fina-risk-cpp-daily-termsheet PRIVATE nlohmann_json::nlohmann_json)
target_compile_options(fina-risk-cpp-daily-termsheet PRIVATE $<$<CONFIG:Release>:-O3;-march=native;-mtune=native>)

if(FINA_RISK_BUILD_XAD_AAD)
  find_package(XAD CONFIG REQUIRED)
  find_path(QUANTLIB_INCLUDE_DIR ql/quantlib.hpp REQUIRED)
  find_library(QUANTLIB_LIBRARY QuantLib REQUIRED)
  add_executable(fina-risk-cpp-aad apps/aad_benchmark.cpp)
  target_include_directories(fina-risk-cpp-aad PRIVATE ${QUANTLIB_INCLUDE_DIR})
  target_link_libraries(fina-risk-cpp-aad PRIVATE XAD::xad ${QUANTLIB_LIBRARY})
  target_compile_definitions(fina-risk-cpp-aad PRIVATE FINA_RISK_HAS_QUANTLIB_XAD=1)
  target_compile_options(fina-risk-cpp-aad PRIVATE $<$<CONFIG:Release>:-O3;-march=native;-mtune=native>)
endif()

if (pybind11_FOUND)
  pybind11_add_module(fina_risk_cpp bindings/fina_risk_pybind.cpp)
  target_compile_definitions(fina_risk_cpp PRIVATE FINA_RISK_HAS_PYBIND11=1)
  target_link_libraries(fina_risk_cpp PRIVATE fina_risk_core)
endif()

if(FINA_RISK_BUILD_XAD_AAD)
  install(TARGETS fina-risk-cpp-aad RUNTIME DESTINATION bin)
endif()
# --- Block tests -------------------------------------------------------------
# One test executable per payoff-graph block. These are deliberately NOT linked
# against fina_risk_core: a block kernel is a pure header function, so a block
# test needs only the headers. That keeps each test free of the JSON layer, the
# path cube, and the pricing loop, which is what makes a block independently
# checkable against its term-sheet specification.
#
# They must not be compiled with -ffast-math. A block test asserts specified
# arithmetic, so the comparison is to a tolerance; reassociation would move the
# last bits and turn a specification check into a compiler check.
include(CTest)
add_executable(fina-risk-block-coupon-strip tests/coupon_strip_test.cpp)
target_include_directories(fina-risk-block-coupon-strip PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
add_test(NAME block/coupon_strip COMMAND fina-risk-block-coupon-strip)

# The NYSE calendar is implemented twice -- here in C++ and again in
# src/fina_risk/daily_termsheet.py. This test checks the C++ side against
# invariants and dumps the schedule on stdout; tests/test_nyse_calendar.py
# then diffs that dump against the Python copy day for day.
add_executable(fina-risk-block-nyse-calendar tests/nyse_calendar_test.cpp)
target_include_directories(fina-risk-block-nyse-calendar PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
add_test(NAME block/nyse_calendar COMMAND fina-risk-block-nyse-calendar 2015 2040)

# A LANE test, not a block test, and the prefix says so.
#
# The global-KO defect is not in a block. `resolve_ko` (barriers.hpp) is the
# block, it is six lines, and it is correct. The defect is that
# fina_risk_cpp.cpp's price_fixture never calls it: it inlines a joint test
# across the basket on a single fixing and ignores the per-asset latch the deal
# file states in KIKOSelect.GKOLocked. So the only way to test the defect is to
# price a deal and look at the put, which means this test does need the JSON
# layer, the path loop, and the library's -ffast-math policy -- all three of
# which the block tests above are written to avoid.
#
# That is not a workaround; it is the point. A block test for global_ko_gate
# would pass today and prove nothing, because the kernel it names is never
# reached from this lane. Naming the test `lane/` keeps the distinction visible
# in ctest output rather than hiding a lane test among block tests.
add_executable(fina-risk-lane-global-ko-latch tests/global_ko_latch_test.cpp)
target_link_libraries(fina-risk-lane-global-ko-latch PRIVATE fina_risk_core)
add_test(NAME lane/global_ko_latch COMMAND fina-risk-lane-global-ko-latch)

install(TARGETS fina-risk-cpp-benchmark fina-risk-cpp-e2e fina-risk-cpp-parity fina-risk-cpp-daily-termsheet RUNTIME DESTINATION bin)
