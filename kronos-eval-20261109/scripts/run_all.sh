#!/bin/bash
E=/Users/tr-oh/Documents/Codex/kronos-eval-20261109
export HF_HOME=$E/hf-cache HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
PY=$E/venv/bin/python; LOG=$E/logs/run_all.log
echo "$(date '+%F %T') start" >> $LOG
run() { echo "$(date '+%F %T') run $*" >> $LOG; nice -n 10 $PY $E/scripts/run_kronos.py --device mps --threads 4 --chunk 24 "$@" >> $LOG 2>&1; echo "$(date '+%F %T') exit=$? $*" >> $LOG; }
run --setting paper --lookback 90
run --setting default --lookback 90
run --setting paper --lookback 256
for f in $E/results/preds_paper_L90_Kronos-small_mps.csv $E/results/preds_default_L90_Kronos-small_mps.csv $E/results/preds_paper_L256_Kronos-small_mps.csv; do
  [ -f "$f" ] && { echo "$(date '+%F %T') score $f" >> $LOG; $PY $E/scripts/score.py --preds "$f" >> $LOG 2>&1; }
done
echo "$(date '+%F %T') all done" >> $LOG
touch $E/results/DONE
