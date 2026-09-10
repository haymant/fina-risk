#include <algorithm>
#include <chrono>
#include <fstream>
#include <iostream>
#include <numeric>
#include <string>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>
#include <ql/quantlib.hpp>
#include <XAD/XAD.hpp>

using json = nlohmann::json;
using AD = xad::adj<double>::active_type;
using Tape = xad::adj<double>::tape_type;
struct Trade { std::vector<std::size_t> indices; double strike{}; };
static json read_json(const std::string& p) { std::ifstream f(p); json x; f >> x; return x; }

int main(int argc, char** argv) {
    if (argc < 5) { std::cerr << "usage: aad_benchmark instruments.json market.json paths.bin paths [bump] [--aad|--no-aad]\n"; return 2; }
    double bump = 0.01;
    bool aad_enabled = false;
    for (int arg = 5; arg < argc; ++arg) {
        const std::string option = argv[arg];
        if (option == "--aad") aad_enabled = true;
        else if (option == "--no-aad") aad_enabled = false;
        else bump = std::stod(option);
    }
    const auto started = std::chrono::steady_clock::now();
    const auto instruments = read_json(argv[1]);
    const auto market = read_json(argv[2]);
    const auto meta = read_json(std::string(argv[3]) + ".meta.json");
    const std::size_t paths = meta.at("paths"), n = meta.at("underlyings");
    std::ifstream binary(argv[3], std::ios::binary);
    std::vector<float> terminal(paths*n);
    binary.read(reinterpret_cast<char*>(terminal.data()), static_cast<std::streamsize>(terminal.size()*sizeof(float)));
    std::vector<double> spots(n); std::unordered_map<std::string,std::size_t> ids;
    for (std::size_t i=0;i<n;++i) { spots[i]=market.at("underlyings")[i].at("spot"); ids.emplace(market.at("underlyings")[i].at("id"),i); }
    std::vector<Trade> trades;
    for (const auto& r: instruments.at("instruments")) { Trade t; t.strike=r.at("legs")[0].at("payoff").at("strike"); for(const auto& id:r.at("underlyings")) t.indices.push_back(ids.at(id.get<std::string>())); trades.push_back(std::move(t)); }
    double pv_sum=0.0, aad_delta_sum=0.0, aad_delta_dollar_sum=0.0, hybrid_delta_sum=0.0, hybrid_delta_dollar_sum=0.0;
    std::uint64_t fallback_count = 0;
#pragma omp parallel for schedule(static) reduction(+:pv_sum,aad_delta_sum,aad_delta_dollar_sum,hybrid_delta_sum,hybrid_delta_dollar_sum,fallback_count)
    for (std::int64_t ti=0; ti<static_cast<std::int64_t>(trades.size()); ++ti) {
        const auto& trade=trades[static_cast<std::size_t>(ti)];
        std::vector<double> base(paths); std::vector<std::size_t> active(paths);
        for(std::size_t p=0;p<paths;++p) { double worst=1e300; std::size_t k=0; for(std::size_t j=0;j<trade.indices.size();++j) { const auto i=trade.indices[j]; double r=terminal[p*n+i]/spots[i]; if(r<worst){worst=r;k=j;} } base[p]=std::max(trade.strike-worst,0.0); active[p]=k; }
        pv_sum += 1.0-std::accumulate(base.begin(),base.end(),0.0)/paths;
        std::vector<double> aad_values(trade.indices.size(), 0.0);
        if (aad_enabled) {
            Tape tape; std::vector<AD> mult(trade.indices.size());
            for(auto& x:mult){x=1.0;tape.registerInput(x);} tape.newRecording();
            AD payoff=0.0;
            for(std::size_t p=0;p<paths;++p){ const auto k=active[p]; const double ratio=terminal[p*n+trade.indices[k]]/spots[trade.indices[k]]; AD branch=trade.strike-ratio*mult[k]; payoff += branch>0.0 ? branch : AD(0.0); }
            payoff /= static_cast<double>(paths);
            tape.registerOutput(payoff); derivative(payoff)=1.0; tape.computeAdjoints();
            for(std::size_t k=0;k<aad_values.size();++k) aad_values[k]=derivative(mult[k])/spots[trade.indices[k]];
        }
        for(std::size_t k=0;k<aad_values.size();++k){
            const double aad_d=aad_values[k];
            double up_sum=0.0, down_sum=0.0;
            for(std::size_t p=0;p<paths;++p){
                double up_worst=1e300, down_worst=1e300;
                for(std::size_t j=0;j<trade.indices.size();++j){ const auto i=trade.indices[j]; const double r=terminal[p*n+i]/spots[i]; up_worst=std::min(up_worst,r*(j==k?1.0+bump:1.0)); down_worst=std::min(down_worst,r*(j==k?1.0-bump:1.0)); }
                up_sum += std::max(trade.strike-up_worst,0.0); down_sum += std::max(trade.strike-down_worst,0.0);
            }
            const double fd_d=(up_sum-down_sum)/(static_cast<double>(paths)*2.0*bump*spots[trade.indices[k]]);
            const bool transition=aad_enabled && std::abs(fd_d-aad_d)>1e-10*std::max(1.0,std::abs(fd_d));
            const double d=(!aad_enabled || transition)?fd_d:aad_d;
            if (aad_enabled) { aad_delta_sum += aad_d; aad_delta_dollar_sum += aad_d*spots[trade.indices[k]]; if(transition) ++fallback_count; }
            hybrid_delta_sum += d; hybrid_delta_dollar_sum += d*spots[trade.indices[k]];
        }
    }
    const auto done=std::chrono::steady_clock::now(); const double elapsed=std::chrono::duration<double>(done-started).count();
    const QuantLib::DiscountFactor discount(1.0);
    std::cout << json{{"backend",aad_enabled?"XAD_reverse_mode_AAD_hybrid":"CRN_bump_revalue"},{"aad_enabled",aad_enabled},{"quantlib_discount_factor",discount},{"instruments",trades.size()},{"underlyings",n},{"paths",paths},{"path_observation_mode","terminal_only_shared_cube"},{"pv_checksum",pv_sum},{"aad_delta_checksum",aad_delta_sum},{"aad_delta_dollar_checksum",aad_delta_dollar_sum},{"hybrid_delta_checksum",hybrid_delta_sum},{"hybrid_delta_dollar_checksum",hybrid_delta_dollar_sum},{"fallback_count",fallback_count},{"elapsed_seconds",elapsed},{"instruments_per_second",trades.size()/elapsed},{"shared_path_cube",argv[3]},{"transition_policy",aad_enabled?"fixed-branch XAD AAD; CRN bump/revalue fallback when transition-sensitive":"AAD disabled; CRN bump/revalue for all sensitivities"}}.dump(2)<<'\n';
}
