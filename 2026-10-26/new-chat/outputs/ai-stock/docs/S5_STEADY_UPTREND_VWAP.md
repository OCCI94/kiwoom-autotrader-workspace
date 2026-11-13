# S5 steady_uptrend_vwap — paper engine contract

Strategy ID: `S5_steady_uptrend_vwap`. Signal reason: `steady_uptrend_vwap`.
The source default remains disabled. The explicitly authorized 2026-11-01 paper
options append S5 to the existing S2/S3/S4 selection; real mode rejects S5.

## Observed inputs and entry

The `research_v1` steady-uptrend rule uses seven complete KST five-minute
candles, strictly prior 20 daily candles, and every actual completed minute
since 09:00. Current and last completed prices must exceed the HLC3 cumulative
VWAP and the cumulative VWAP must not decline over the preceding five minutes.
The prior daily MA5 must exceed MA20 and the prior daily close must exceed its
five-session-earlier close. Exact candle, volume and shallow-drawdown formulas
are in `trading/steady_uptrend_vwap.py`.

Only a newly observed five-minute boundary with a preceding observed baseline
can enter. No retrospective signal replay, synthetic missing minute, or future
bar is accepted. Earliest full warmup is 09:35; enabling monitoring at 09:00
does not mean an unconditional opening purchase. Existing bounded round-robin
sampling remains, so not every candidate signal is observed.

Prior daily data uses one bounded shared-queue ka10081 page per symbol/session/
day. S5 session minute reads reuse the existing queue and pacer. No new daemon,
research polling, independent login or market-data socket is introduced.

ADR20 extension must be below 6 and a current, complete local DART risk review
must explicitly contain no unresolved major risk. Missing/expired evidence
blocks entry, including B3 additions. It does not disable owned-position exits.
The existing Toss read-only evidence worker is unchanged. At deployment review,
its DART file covers 042510 and 053800; other symbols require new evidence.

## Shared order protection

Basic 25 symbols and 15 dynamic slots, at most 40 entry candidates and 8 occupied
symbols. Holdings, pending buys and intents count across all strategies.
Per-symbol and per-order principal cap: KRW 1,000,000. B3 initial allocation:
50% (KRW 500,000); one addition requires +50bp profit, prior-20-volume factor
1.5 and the verified post-fill path. At most two accepted buy orders per symbol
per day, including additions and previous orders. Theme limit: two occupied
symbols. Unfilled buy TTL: 180 seconds. Cancellation ACK does not release money;
final cancellation and partial executions must reconcile first.

S5 initial orders require fresh zero-position v5 evidence, same-session 00
state, integrated pending-order reconciliation and ledger watermark. Source-bound
v4 decisions independently recompute the full signal. Compact source pages and
OHLCV retain the complete observed session while keeping evidence below 64KiB.

## Exits and restart

Existing protection is retained: current price at or below average basis -3%,
current price at or above +10%, or current price below the observed last-20-
minute SMA. S5 also exits after 30 minutes from the **first actual verified 00
unit fill**, and from 15:19 using a fresh quote. These last two exits do not
require a new chart. Order acceptance and later partial fills do not reset the
clock. Ownership, first fill and the initial B3 generation are reconstructed
from immutable order/00/REST evidence after restart. Unknown ownership or an
unproved fill is never guessed. Sending an exit order does not guarantee a fill.

The prior positive five-sample research used fixed +30-minute hypothetical
exits. B3 sizing, protective exits, shared exposure and partial fills make the
live paper policy different; that historical return is not a deployment result.

## Three P&L tracks

Gross uses verified actual sold-lot consideration. Mock Net uses reconciled
actual paper charges; an unresolved cost allocation stays null. Real-Fee
Equivalent is a labelled research reference:

`Gross - matched_buy_amount*0.00015 - matched_sell_amount*0.00015 - matched_sell_amount*0.002`

No extra hypothetical slippage is subtracted from actual execution prices.
This is not the 110/41/43bp backtest ledger or a verified personal fee schedule.
The read-only S5 generation projector requires v5 zero holdings, matching B3/
exit claims and corroborated 00 units. FIFO handles actual acquired/sold shares;
missing history, unconfirmed partial cancellations and stream gaps stay unknown.
