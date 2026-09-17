#include "fina_risk/coupon.hpp"

// Coupon predicates and memory arithmetic are header-visible pure functions so
// the path × observation loop is inlined without dynamic dispatch or allocation.
