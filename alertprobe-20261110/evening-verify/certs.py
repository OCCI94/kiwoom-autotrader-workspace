import sqlite3, sys, hashlib, json
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for r in db.execute("select id,day,ord_no,captured_at,length(payload_json),digest,payload_json from paper_expired_order_certificates where id>=26 order by id"):
    p = r[6]; d = json.loads(p)
    print(r[0], r[1], r[2], r[3], r[4], 'digest', r[5][:16], 'sha(payload)', hashlib.sha256(p.encode()).hexdigest()[:16])
    print('   keys:', sorted(d.keys())[:30])
    for k in ('schema','version','kind','reason','status','basis'):
        if k in d: print('   ', k, '=', str(d[k])[:120])
print('--- orders/intents/ttl for the two')
for o in ('218972','219563'):
    print(db.execute("select day,mode,ord_no,symbol,side,strategy,quantity,registered_at from orders where ord_no=?", (o,)).fetchall())
    print(db.execute("select day,side,symbol,quantity,strategy,state,reason,order_number from intents where order_number=?", (o,)).fetchall())
    print(db.execute("select count(*), sum(quantity) from fills where ord_no=?", (o,)).fetchall())
    print(db.execute("select day,observed_at,status,cancellation_ord_no,reported_cancel_quantity from buy_ttl_cancellation_outcomes where original_ord_no=?", (o,)).fetchall())
print('--- today orders/fills'); print(db.execute("select count(*) from orders where day='20261110'").fetchall(), db.execute("select count(*) from fills where day='20261110'").fetchall())
print(db.execute("select day,count(*) from orders group by day order by day desc limit 6").fetchall())
