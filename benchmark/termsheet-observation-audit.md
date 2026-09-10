# Termsheet observation audit and AAD toggle

## AAD toggle

`fina-risk-cpp-aad` now defaults to AAD **off**:

```bash
./cpp/build-xad/fina-risk-cpp-aad instruments.json market.json paths.bin 1000
```

Explicit modes are:

```bash
# default behavior: all sensitivities use CRN bump/revalue
./cpp/build-xad/fina-risk-cpp-aad instruments.json market.json paths.bin 1000 --no-aad

# fixed-branch XAD reverse mode plus explicit CRN transition fallback
./cpp/build-xad/fina-risk-cpp-aad instruments.json market.json paths.bin 1000 --aad
```

The output reports `aad_enabled`, `backend`, `transition_policy`, and
`path_observation_mode`. Default output is `backend=CRN_bump_revalue` and
`aad_enabled=false`.

## Does the current termsheet run observe every day?

**No.** The current shared parity and AAD benchmark use a **terminal-only path cube**. Each path contains one terminal simulated spot per underlying. They do not evolve or inspect a daily time grid.

The termsheet itself contains event schedules rather than a daily observation array. For the three canonical jobs:

- Evaluation date: serial date `46272`.
- Option leg expiry: `46419`.
- Funding/coupon maturity: `46421`.
- The PUT job has range/event end dates `[46174, 46419]`.
- The coupon job has ten range-accrual period end dates: `[46171, 46174, 46204, 46237, 46266, 46296, 46328, 46357, 46391, 46419]`.
- The option declares discrete KI semantics with a `0.7` KI barrier.
- The fixture contains no explicit daily path cube or daily fixing-by-date array sufficient to claim daily monitoring.

Therefore the current benchmark is a **terminal payoff/AAD microbenchmark**, not a faithful daily EKI/range-memory lifecycle simulation of the full termsheet. A production-parity daily implementation must add a `(path, observation_date, underlying)` cube, evolve barrier and coupon-memory state on each scheduled observation, and include payment-date discounting.
