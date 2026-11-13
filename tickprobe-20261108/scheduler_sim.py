"""현행 vs 제안 스케줄러의 후보 평가 처리량 시뮬레이션(결정론적, 데이터 불필요).
현행: 분 경계마다 scan = urgent + s5_quota + ordinary[:1] + ...; 틱마다 batch_size(4)개만 평가;
      틱 시간 = 고정 오버헤드 + urgent 조회비 + 평가 수 × 종목비. 틱 ≥ 60s면 매 틱 재구성.
제안: 보유 청산은 캐시된 소유권으로 전부 처리(종목당 c_exit), 후보 슬롯 K개 보장, 시간 예산 B.
사용: python3 scheduler_sim.py
"""
import itertools
def current(owned, candidates, fixed, per_owned_lookup, per_eval, batch=4, horizon=6*3600):
    t=0.0; cand_evals=0; ticks=0
    while t<horizon:
        pre=fixed+owned*per_owned_lookup
        scan=['U']*owned+['C']*candidates           # urgent 먼저, 분 경계마다 재구성(틱≥60s 가정)
        run=scan[:batch]; evals=len(run); cand_evals+=run.count('C')
        t+=pre+evals*per_eval; ticks+=1
    return ticks, cand_evals
def proposed(owned, candidates, fixed, per_owned_lookup_cached, per_exit, per_eval, K=2, budget=45.0, horizon=6*3600):
    t=0.0; cand_evals=0; ticks=0; cursor=0
    while t<horizon:
        pre=fixed+owned*per_owned_lookup_cached
        spent=pre+owned*per_exit                      # 보유 청산 전부(캐시된 조회 재사용)
        k=0
        while k<K or (spent+per_eval<=budget and k<candidates):
            if k>=candidates: break
            spent+=per_eval; k+=1; cursor=(cursor+1)%max(1,candidates)
        cand_evals+=k; t+=spent; ticks+=1
    return ticks, cand_evals
print('가정(측정 근거): 고정 오버헤드=탐색 5 HTTP+prepare 1 HTTP≈8 s(11-04 pre-batch 12 s 중 보유 2개 조회 제외), 종목 평가≈2.5 s(11-06 batch 10 s/4종목), 보유 조회(현행 라이브)≈ (pre-batch−8)/보유수')
rows=[]
for label,owned,fixed,lookup in (('11-04형(보유2, 건강)',2,8.0,2.0),('11-05형(보유6+대기2, 저하)',8,8.0,7.0),('11-06형(보유6+대기2, 더 저하)',8,8.0,12.0)):
    for cands in (30,):
        tc,ec=current(owned,cands,fixed,lookup,2.5)
        tp,ep=proposed(owned,cands,fixed,0.4,0.3,2.5,K=2,budget=45.0)
        rows.append((label,cands,tc,ec,tp,ep))
print(f'{"시나리오":34s} 후보  현행 틱수/후보평가  제안 틱수/후보평가 (6시간)')
for label,cands,tc,ec,tp,ep in rows: print(f'{label:34s} {cands:4d}  {tc:5d} / {ec:5d}      {tp:5d} / {ep:5d}')
print('\n해석: 현행은 보유(urgent)가 batch_size(4) 이상이면 틱 길이와 무관하게 후보 평가가 0이 된다. 제안은 보유 수와 무관하게 틱당 K개 이상 후보를 평가하고, 예산 안에서 더 돈다.')
