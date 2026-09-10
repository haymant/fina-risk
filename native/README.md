# fina-risk-cpp

Native C++ parity kernel for [fina-risk](..): a pybind11 binding exposing
`run_cpp_parity` for the CRN bump-revalue worst-of pricing, delta, gamma, and
Taylor P&L checksums computed on a shared float32 terminal cube. This is the
C++ lane of the Python/C++ pricing-parity benchmark.

Build: `pip install .` (scikit-build-core) or `uv build --wheel`.

Wheel tags are built for cp311/cp312/cp313 across manylinux, pair published by
the repo tag-triggered GitHub Action (TestPyPI then PyPI), mirroring `fina-core`.