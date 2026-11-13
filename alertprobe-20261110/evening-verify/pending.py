import sys, json, time
pkg, tool, ledger = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, tool); sys.path.insert(0, pkg)
sys.dont_write_bytecode = True
import receiver_release_preflight as release
import kiwoom_autotrader, inspect
t0 = time.time()
try:
    with release.readonly_snapshot(ledger, pending=True) as db:
        intents, rows, groups = release._pending(db, release._moment(None))
    print(json.dumps({"pkg": pkg.rsplit('/',1)[-1], "module": kiwoom_autotrader.__file__.split('scratchpad/')[-1],
        "pending_intents": intents, "unfilled_orders": len(rows), "rows": rows, "groups": groups,
        "seconds": round(time.time()-t0,1)}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"pkg": pkg.rsplit('/',1)[-1], "error": type(e).__name__, "msg": str(e)[:200], "seconds": round(time.time()-t0,1)}))
