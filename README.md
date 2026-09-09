# QuantLab — Market Data Engine

A market data engine for futures order flow research, built in Python from scratch. This repository is phase one of a larger project: the layer that produces, validates, labels and reshapes OHLCV bars so that analytical engines can sit on top of clean data.

Everything here targets NQ and MNQ (E-mini and Micro E-mini Nasdaq 100 futures), 1-minute bars, New York session. The design rule for the whole project is that no component should be a black box, so each function is written out rather than pulled from a library, and each one comes with the reasoning behind it.

The synthetic data generator exists because the real bottleneck in this kind of work is not the analytics, it is getting tick data. Rather than wait for a data feed, the engine generates OHLCV that is structurally valid so the downstream modules can be built and tested now. That choice buys speed and it costs realism, and the limitations section below is specific about where it costs.

## What is in here

`data/data_loader.py` holds four things. A synthetic OHLCV generator, an integrity validator, a session labeler and a multi-timeframe resampler. The file runs on its own and prints a full validation pass, so `python data/data_loader.py` is the fastest way to see what it does.

## The math

**Synthetic price path.** Prices follow a geometric random walk with drift. Each bar draws a return from a normal distribution and applies it multiplicatively to the previous close:

```
r[t]     ~ Normal(mu, sigma)          mu = 0.00002, sigma = 0.0003 per bar
close[t] = close[t-1] * (1 + r[t])
```

Multiplicative rather than additive is the right choice here because it keeps prices positive and makes a move proportional to the price level, which is how real instruments behave. A 20-point move on NQ means something different at 17,000 than at 8,000, and compounding returns captures that automatically.

The bar is then assembled around the close. The open inherits the previous close, and the high and low are pushed out from the open-close range by a half-normal offset:

```
open[t] = close[t-1]
high[t] = max(open, close) + |Normal(0, sigma * base_price)|
low[t]  = min(open, close) - |Normal(0, sigma * base_price)|
```

Taking the absolute value is what guarantees the high never lands below the body and the low never lands above it, which is the property the validator checks for downstream.

Volume is drawn from a lognormal, `exp(Normal(6.5, 0.5))`, which gives a median near 665 contracts per minute with a long right tail. Lognormal rather than normal because volume cannot go negative and real volume distributions are right-skewed, with most minutes quiet and a few very heavy.

**Integrity validation.** Six rules, and any failure raises rather than warns. The required columns must exist, no nulls anywhere, high must be at or above low, high at or above both open and close, low at or below both open and close, and volume non-negative. These are the invariants that any OHLCV bar has to satisfy by construction, so a violation means the data is corrupt and there is no point continuing. Failing loudly here is deliberate. A silent bad bar propagates into every engine built on top.

**Session labeling.** The hour is converted to a decimal so that time comparisons become plain arithmetic, `hour + minute/60`, which turns 09:30 into 9.5 and 13:45 into 13.75. Bars are then bucketed into pre_market, open, mid_morning, lunch, afternoon and after_hours using `np.select` over the interval conditions. This matters because intraday markets are not stationary. Volume, spread and participation at 09:35 have almost nothing in common with 12:30, and any statistic computed across the whole day averages those regimes into something that describes neither.

**Resampling.** Higher timeframes follow the standard OHLCV aggregation, where the open is the first open of the window, the high is the max of highs, the low is the min of lows, the close is the last close and volume is the sum. Empty windows are dropped, and session labels get recomputed on the new index rather than carried over, since a 5-minute bar can span a session boundary that a 1-minute bar could not.

## Install and run

Python 3.12 or newer.

```bash
git clone <your-repo-url>
cd QuantLab
python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\activate
pip install -r requirements.txt
python data/data_loader.py
```

The script generates 20 calendar days of 1-minute bars, validates them, labels the sessions, resamples to 5 minutes and prints a preview of both timeframes.

## Known limitations

This section is the honest inventory. Everything below was measured by running the code, not guessed at.

**The volume profile in the comments is not implemented.** The generator's comment says volume should be higher at the open and into the close. It is not. Volume is a flat lognormal draw with no time-of-day shaping at all. Measured on 20 days, mean volume is 750 in the first 30 minutes, 759 through lunch and 764 in the last 30 minutes, which is noise around a constant. The comment describes an intention, not the code. Anything that depends on realistic intraday volume shape, and volume profile work is exactly that, will not get it from this generator yet.

**There are no overnight gaps.** The open is built with `np.roll` over the whole close series, so the roll crosses day boundaries too and the first bar of each session opens exactly at the previous session's close. Real NQ gaps overnight, often meaningfully, and gap behavior is a big part of how the first minutes of a session trade. Any model tested only on this data has never seen a gap.

**Returns are independent and normal, so the data is too well behaved.** Measured on the generated series, skew comes out at 0.01 and excess kurtosis at 0.04, both effectively zero, and the lag-1 autocorrelation of absolute returns is 0.003. Real futures returns have fat tails and strong volatility clustering, meaning large moves arrive in bursts. This generator has neither. The practical consequence is that risk metrics computed here will look better than they would on real data, because the tail events that break models are absent by construction.

**`days` counts calendar days, not trading days.** The docstring says trading days. The loop iterates over calendar days and skips weekends, so `days=20` produces 14 sessions. Not a correctness bug but the parameter does not mean what it says.

**Two session labels are effectively dead.** Since the generator only emits 09:30 to 16:00, `pre_market` never appears at all and `after_hours` catches only the single 16:00:00 bar of each day, 14 bars out of 5,474. The labeler handles the full day correctly, there is just no data outside the session to label.

**The resampler misassigns sessions on off-grid timeframes.** Pandas resampling anchors to its own clock grid, so resampling to `1h` puts the 09:30 to 10:00 stretch into the bucket stamped 09:00, and the labeler then reads that stamp and calls it `pre_market`. The same bucket is also only half a session's worth of bars while every other hourly bar is full. Timeframes that divide the session cleanly (`5min`, `15min`, `30min`) are fine. Anything that does not divide the 09:30 open (`1h`, `2h`, `4h`) produces a mislabeled and partial first bar. A fix means anchoring the resample to the session open with the `origin` argument rather than the default grid.

**Everything here is synthetic.** No real market data has touched this engine. The interfaces are designed so that a real feed drops in without changing downstream code, but that claim is untested until a real feed is actually connected.

## Roadmap

Phase one, the data engine described above, is done. What follows is planned and not written.

Phase two is a VWAP engine, covering session and anchored VWAP with standard deviation bands. Phase three is a volume profile engine for POC, value area and high and low volume nodes, where the main design problem is bin resolution against the 0.25 tick size. Phase four is order flow analytics, delta and imbalance detection, which is the point where OHLCV genuinely stops being enough and real bid/ask data becomes necessary. Phase five prepares the structure for depth and heatmap data. Phase six is a quantitative risk engine covering Monte Carlo simulation, drawdown modeling and position sizing. Phase seven is execution analytics over a real trade log.

The phases after four depend on data this repository does not have, and the plan is to state that plainly rather than approximate it and pretend otherwise.

## How this was built

Built with AI assistance; the measurements in the limitations section are my own, produced by instrumenting the code and checking what it does against what the comments claim. I'd rather publish a repository that names its own gaps than one that hides them.

Leonardo González Martínez — Mechatronics Engineering, Tecnológico de Monterrey
