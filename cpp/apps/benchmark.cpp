#include "fina_risk_cpp.hpp"

#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "usage: fina-risk-cpp-benchmark instruments.json market.json [paths]\n";
        return 2;
    }
    const std::size_t paths = argc > 3 ? std::stoull(argv[3]) : 30000;
    try {
        std::cout << fina::risk::to_json(fina::risk::run_benchmark(argv[1], argv[2], paths)) << '\n';
    } catch (const std::exception& error) {
        std::cerr << "benchmark failed: " << error.what() << '\n';
        return 1;
    }
}
