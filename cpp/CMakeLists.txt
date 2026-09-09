cmake_minimum_required(VERSION 3.20)
project(fina_risk_cpp VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(nlohmann_json 3.11 REQUIRED)
find_package(QuantLib QUIET)
find_package(XAD QUIET)
find_package(Arrow QUIET)
find_package(duckdb QUIET)
find_package(AWSSDK QUIET COMPONENTS s3)
find_package(pybind11 QUIET)

add_library(fina_risk_core src/fina_risk_cpp.cpp)
target_include_directories(fina_risk_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(fina_risk_core PUBLIC nlohmann_json::nlohmann_json)

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

if (pybind11_FOUND)
  pybind11_add_module(fina_risk_cpp bindings/fina_risk_pybind.cpp)
  target_compile_definitions(fina_risk_cpp PRIVATE FINA_RISK_HAS_PYBIND11=1)
  target_link_libraries(fina_risk_cpp PRIVATE fina_risk_core)
endif()

install(TARGETS fina-risk-cpp-benchmark RUNTIME DESTINATION bin)
