# Consumption Forecast

Algorithm, thresholds and evidence of the adaptive next-day consumption forecast
(`app/optimizer/adaptive.py`). For the user-facing behaviour see
[Consumption](../energy/consumption.md); for where the code sits see
[Architecture](architecture.md#consumption-capture-and-forecast).

## Overview

`AdaptiveConsumptionForecaster` keeps the interface of the median forecaster (`build`,
`horizon_coverage_ratio`, `coverage_end`) and adds `analyze` and `next_day_forecast`. It separates the
**everyday profile** from the **consumption phase** (`normal`, `suspected_low_activity`). The data
cannot tell a holiday from other reasons, so a phase is never called absence.

Nothing is stored: the state is **replayed** from the stored history, bucket by bucket and without
look-ahead, on every call, so a restart reconstructs it exactly. Fewer than 14 learned days gives
`insufficient_history`, reported in `reasons`, and the median forecast answers.

## Rules and thresholds

| Rule | Value | Reason |
| --- | --- | --- |
| Observation | COMPLETE, or PARTIAL with at least 750 s | extrapolation stays below 1.2; ESTIMATED and MISSING never teach or trigger anything, an outage leaves windows thinner, never zero |
| Profile | capped mean (cap 1.5 x median) of the newest 8 samples per weekday and quarter hour, else 21 per time of day, at least 3 | a plain median under-forecasts energy, an uncapped mean follows single consumers |
| Learning lag | a bucket is learned 24 h after it ended, only in the normal state | equals the entry window: nothing of a phase onset has been learned when it is detected; the profile stays frozen during a phase and for the detection edge after a return |
| Enter | 24 h window, at least 68 observed buckets, capped energy ratio (bucket cap 2 x expected) below 0.60 | everyday windows sit near 1.0, a 14-day standby phase near 0.15; the cap limits a lone consumer |
| Return | the newest 16 informative buckets (expected at least 2 x the low level and 15 Wh more), at least 75 % closer to the profile than to the low level, at least 4 reproducing a profile peak (4 x low level, 70 % of the way), twice in a row, evidence not older than 8 h; buckets above 3 x expected are no evidence | a single spike, a heavy appliance or a few moderate hours cannot satisfy this; nights carry no evidence |
| Hysteresis | entering needs 68 fresh buckets after a return (the window is reset) and a ratio of 0.60, returning needs the evidence above | no flip-flop; the two paths use different measures |
| Level factor | median of the last 5 daily ratios (at least 3), applied beyond a 10 % dead band, clamped 0.2 to 5; profile samples are stored divided by the level | learns permanent change in about 3 days and shows it as a `level_changed` event |
| Promotion | a phase of 28 days becomes the new normal: the profile is rebuilt from it | a permanent drop is not held at "suspected" forever |
| Low-phase forecast | per quarter-hour median of the last 7 days of the phase (at least 3), else the standby level (median, first estimate from the newest 12 h of the entry window) | standby is flat, a scaled everyday profile has the wrong shape |

## Output and evaluation

`analyze` returns profile basis and days, level factor, phase, detection and return instants, data
coverage of the last 7 days, the daily spread, return evidence, reasons and the last events. The admin
API does not yet expose `analysis`.

The next local day (92, 96 or 100 slots) is stored by `ConsumptionHistorian` once per target day after
22:00 local time, for the adaptive and the median method (`consumption_forecast_runs/values`,
explanation as JSON). Later runs are compared with COMPLETE actuals (at least 90 % of the slots):
daily energy error and quarter-hour MAE, logged at INFO.

## Scenario evidence

`tests/test_consumption_adaptive.py` replays synthetic households (`tests/consumption_scenarios.py`,
about 9 kWh a day, noise and random loads, Europe/Berlin). A forecast sees only buckets ended before
it was made. Mean absolute daily-energy error, adaptive / median, six seeds, issued 22:00:

| Situation | Adaptive | Median |
| --- | --- | --- |
| Everyday, days 20 to 55 | 13.8 % (MAE 18.3 Wh) | 14.6 % (18.2 Wh) |
| 14-day low phase, days 2 to 14 | 6.7 % | 371 % |
| Same, return at 18:00: return day, +1, +2, +3 | 17.9, 12.6, 13.0, 13.1 % | 23.1, 17.8, 18.1, 18.5 % |
| No return, days 2 to 28 | 1.5 % | 372 % |
| Level +40 %, days 1-2 / 3-5 / 6-8 | 35 / 21 / 10 % | 36 / 36 / 35 % |
| Level -25 %, days 9-20 | 17 % (MAE 17 Wh) | 10.5 % (MAE 24 Wh) |

The median method stays near the everyday level during a low phase because the same weekday of the
older weeks still dominates, hence its large error there. Both methods under-forecast everyday energy
by about 13 % (random loads); the -25 % row is worse in daily energy because the generator's random
loads do not scale with the level.

## Detection delays

Eight seeds.

| Event | Recognised after |
| --- | --- |
| Low phase, onset at midnight | 18.0 to 18.8 h |
| Return, arrival at 18:00 or 15:00 | 3.2 to 3.8 h |
| Return, arrival at 06:00 | 4 to 5.5 h |
| Return, arrival at midday | 7 h |
| Return, arrival at 09:00 or 00:00 | 10 h (peaks are required) |
| Return, arrival at 21:00 | 14 h (the night gives no evidence) |

Before that signal the forecast stays low; this cannot be avoided.

## False-switch experiments

| Experiment | Result |
| --- | --- |
| 100 everyday days, 12 seeds | 0 false low detections |
| One-bucket spikes, 90-minute heavy appliances, or 250 W for three hours on half of the days | 0 false returns |
| 350 W for three hours | 4 of 8 seeds falsely returned (a person at home in practice) |

A false return costs about one day until a new phase is detected.

## Known limits

- A phase detected inside the first 14 days of history is learned as everyday.
- A history that starts inside a phase teaches it as everyday.
- After a promotion the return is a level change (about 3 days).
- The retained 60 days bound the replay, so older profile samples disappear.
