# Engineering decisions

This repository demonstrates research infrastructure through fictional data, not a profitable trading strategy. The live collector, private signal rules, credentials and real trading data are outside its scope.

| Problem | Design decision | How it is checked |
|---|---|---|
| A late price can masquerade as a 5-minute observation | Validate target time, receipt time, price and status separately | Invalid or late metadata cannot enter valid-return statistics |
| Dropping missing exits can make execution results look better | Reserve cash for unresolved hypothetical positions | Terminal equity remains null while exits are unpriced |
| Many same-day signals share the same market shock | Bootstrap whole dates and report minimum-sample limits | Insufficient history does not produce a confidence interval |
| Position sizing can accidentally use future outcomes | Historical sizing requires observations completed strictly before entry | Tests reject look-ahead and inspect decision timestamps |
| A positive toy market-making result can be oversold | Label fill assumptions, inventory marks and missing market realism | Shared-tape examples expose inventory and marked P&L separately |
| A demo can be difficult to reproduce on another machine | Provide a synthetic SQLite walkthrough, explicit optional dependencies, LF text and artifact hashes | Cross-platform CI runs tests and verifies committed artifacts |

The next validation step for real-world use would be independent market data and calibrated execution assumptions. Synthetic outputs alone cannot establish economic value.

## Walkthrough failure contract

A completed manifest is published only after every report has been generated, the input DB hash has been rechecked and artifact hashes have been written. A failure records its stage and error type without publishing a completion marker. Temporary database destinations are restored even after schema/insert errors or interruption. Concurrent runs cannot take over the same output directory.

Input validation rejects malformed timestamps, nonfinite/nonpositive prices, inconsistent VALID labels and duplicate identities. Legitimate missing outcomes remain missing. Alias checks cover hard links as well as normalized paths, preventing report writes through another name for an input or another output. Full-run repeatability tests compare both analysis artifacts and their fingerprints for identical inputs/configuration/seeds.
