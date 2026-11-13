import sqlite3, sys, hashlib
def h(p):
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True); out = {}
    for t in ('orders','fills','intents','order_decisions','buy_ttl_cancellation_outcomes','buy_ttl_cancellation_settlements','holdings','profits'):
        m = hashlib.sha256(); n = 0
        for r in db.execute(f"select * from [{t}] order by 1,2,3,4,5"):
            m.update(repr(r).encode()); n += 1
        out[t] = (n, m.hexdigest()[:12])
    out['certs'] = db.execute("select count(*), max(id) from paper_expired_order_certificates").fetchone()
    return out
a, b = h(sys.argv[1]), h(sys.argv[2])
for k in a: print(f"{k:36s} 08:45 {a[k]}  21:14 {b[k]}  {'same' if a[k]==b[k] else 'CHANGED'}")
