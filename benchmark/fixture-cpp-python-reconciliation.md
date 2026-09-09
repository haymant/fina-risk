# C++ / Python fixture reconciliation

The fixture contains three jobs. Canonical aggregation uses PUT and FUNDING from the first job and replaces its coupon with the separately priced coupon job. Dollar delta is defined as:

`dollar_delta = normalized PV delta per $1 quoted spot × quoted spot × notional`

The run used 30,000 paths, seed 1729, and 1% relative quoted-spot central bumps.

| Metric | Python | C++ | Difference (C++ − Python) |
|---|---:|---:|---:|
| Canonical PV | 1.4738664748 | 1.4735007294 | -0.0003657454 |
| ADBE normalized delta | 0.0002476105 | 0.0002441922 | -0.0000034184 |
| ADBE dollar delta | 3,316.56 | 3,270.77 | -45.79 |
| AMZN normalized delta | 0.0003447248 | 0.0003469439 | +0.0000022191 |
| AMZN dollar delta | 4,453.07 | 4,481.73 | +28.67 |

The Python canonical PV bump values were 1.4745060918 / 1.4731794688 for ADBE and 1.4747198021 / 1.4729385748 for AMZN. The C++ values were 1.4741326397 / 1.4728243312 and 1.4743549265 / 1.4725622327 respectively.

The C++ result is within Monte Carlo / RNG implementation noise of the Python reference. The remaining small residual is expected because the implementations use different normal random-number streams and native floating-point execution. The C++ implementation now matches the Python fixture’s three-job aggregation, quoted-spot bump convention, volatility surface selection, discounting, coupon carry, and dollar-delta definition.
