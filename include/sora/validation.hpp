#pragma once
// Fast input checks the engine performs itself (it never assumes `sora-tools validate` has run).

#include <string>
#include <vector>

#include "sora/dataset.hpp"
#include "sora/segmentation.hpp"

namespace sora {

struct Finding {
    std::string id;         // e.g. INV-RC-001
    std::string severity;   // error | warning | info
    std::string message;
    std::uint64_t count = 0;
};

struct Diagnostics {
    std::vector<Finding> findings;
    bool has_errors() const;
};

// INV-IN-* (identity, integrity) and INV-RC-001 (allowance vs stage history at the reference date).
Diagnostics check_inputs(Duck& duck, const Dataset& d, const Segmentation& s);

}  // namespace sora
