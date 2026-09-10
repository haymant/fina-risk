cmake_minimum_required(VERSION 3.20)
project(fina_risk_cpp_native VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

include(FetchContent)
# Header-only, no system dependency, fetched at configure time so the manylinux
# wheel build has no external apt/yum requirement.
FetchContent_Declare(
  nlohmann_json
  GIT_REPOSITORY https://github.com/nlohmann/json.git
  GIT_TAG v3.11.3
)
FetchContent_MakeAvailable(nlohmann_json)

find_package(pybind11 CONFIG REQUIRED)

pybind11_add_module(fina_risk_cpp ../cpp/bindings/fina_risk_pybind.cpp ../cpp/src/fina_risk_cpp.cpp)
target_include_directories(fina_risk_cpp PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../cpp/include)
target_link_libraries(fina_risk_cpp PRIVATE nlohmann_json::nlohmann_json)
target_compile_definitions(fina_risk_cpp PRIVATE FINA_RISK_HAS_PYBIND11=1)

target_compile_options(fina_risk_cpp PRIVATE $<$<CONFIG:Release>:-O3;-ffast-math>)

install(TARGETS fina_risk_cpp LIBRARY DESTINATION .)