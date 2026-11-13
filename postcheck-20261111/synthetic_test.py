"""postcheck.py 검증: 효과를 심어 둔 가짜 거래를 넣고 제대로 잡아내는지 본다.

심은 것 (실제 요인 자료의 최근 60거래일 위에 만든다)
- ARM_EDGE: 거래당 알파 +0.25%, β시장 0.5, β모멘텀 0.8
- ARM_NULL: 알파 0, β시장 0.5, β모멘텀 0.8
- EXP_R   : 알파 0, β시장 0.5, β모멘텀 0 (대조군)
- tag_real: '높음'이면 +0.2%p, '낮음'이면 −0.2%p. 차이 0.4%p, arm 평균에는 영향 없음 (ARM_EDGE, ARM_NULL)
- tag_noise, tag_num: 효과 없음
거래당 잡음 표준편차 1.5%, arm마다 하루 8건, 60일 = 480건.

사용: python3 -I -B synthetic_test.py <factors csv> <작업 폴더> <반복 수>
"""
import csv, os, random, re, subprocess, sys, collections

FACT, WORK, REPS = sys.argv[1], sys.argv[2], int(sys.argv[3])
os.makedirs(WORK, exist_ok=True)
days = [r for r in csv.DictReader(open(FACT, encoding='utf-8')) if r['mom_oc'] != ''][-60:]
SPEC = {'ARM_EDGE': (0.0025, 0.5, 0.8), 'ARM_NULL': (0.0, 0.5, 0.8), 'EXP_R': (0.0, 0.5, 0.0)}
HERE = os.path.dirname(os.path.abspath(__file__))


def make(seed, path):
    rng = random.Random(seed)
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(['arm', 'symbol', 'day', 'entry_time', 'exit_time', 'buy_amount', 'net_pnl', 'tag_real', 'tag_noise', 'tag_num'])
        for d in days:
            mkt, mom = float(d['mkt_oc']), float(d['mom_oc'])
            for arm, (alpha, bm, bq) in SPEC.items():
                for k in range(8):
                    real = rng.choice(['낮음', '높음']); noise = rng.choice(['가', '나'])
                    r = alpha + bm * mkt + bq * mom + rng.gauss(0, 0.015)
                    if arm != 'EXP_R':
                        r += 0.002 if real == '높음' else -0.002
                    w.writerow([arm, f'S{k:03d}', d['day'], f'10:{k:02d}:00', f'11:{k:02d}:00', 300000, round(r * 300000), real, noise, round(rng.gauss(0, 1), 4)])


def run(seed):
    t = os.path.join(WORK, 'trades_synth.csv'); o = os.path.join(WORK, 'report_synth.txt')
    make(seed, t)
    subprocess.run([sys.executable, '-I', '-B', os.path.join(HERE, 'postcheck.py'), '--trades', t, '--factors', FACT, '--out', o, '--quiet'], check=True)
    return open(o, encoding='utf-8').read()


def parse(text):
    out = {}
    for arm in SPEC:
        m = re.search(rf'^{arm} \| \d+ \|.*\[(양|음|0 포함)\]$', text, re.M)
        out[arm + '_naive'] = m.group(1) if m else None
        sec4 = text[text.index('## 4.'):text.index('## 5.')]
        m = re.search(rf'^{arm} \| (\d+) \| ([+-][\d.]+)% \| ([+-][\d.]+) \| [\d.]+ \| ([+-][\d.]+) \| ([+-][\d.]+) \| ([+-][\d.]+) \| ([+-][\d.]+) \| \d+% \| (.+)$', sec4, re.M)
        if m:
            out[arm + '_alpha_t'] = float(m.group(3)); out[arm + '_bm'] = float(m.group(4)); out[arm + '_bq'] = float(m.group(6)); out[arm + '_verdict'] = m.group(8).strip()
        sec2 = text[text.index('## 2.'):text.index('## 3.')]
        m = re.search(rf'^{arm} \| .*\| ([^|\n]+)$', sec2, re.M)
        out[arm + '_daily'] = m.group(1).strip() if m else None
        sec = re.search(rf'### {arm} —.*?(?=\n### |\n- 날 단위)', text, re.S)
        if sec:
            for tag in ('real', 'noise', 'num'):
                line = re.search(rf'^- {tag} .*$', sec.group(0), re.M)
                out[f'{arm}_tag_{tag}'] = None if not line else ('구분됨' if '→ 구분됨' in line.group(0) else '구분 안 됨')
        m = re.search(rf'^- {arm} − EXP_R: .* t ([+-][\d.]+)$', text, re.M)
        if m:
            out[arm + '_paired_t'] = float(m.group(1))
    return out


first = run(1)
open(os.path.join(HERE, 'synthetic_report_example.txt'), 'w', encoding='utf-8').write(first)
tally = collections.Counter(); sums = collections.defaultdict(list)
for seed in range(1, REPS + 1):
    r = parse(run(seed))
    for k, v in r.items():
        if isinstance(v, float):
            sums[k].append(v)
        else:
            tally[(k, v)] += 1
print(f'=== {REPS}회 반복 ===')
for arm in SPEC:
    print(f"{arm} (심은 알파 {SPEC[arm][0] * 100:+.2f}%/거래, β시장 {SPEC[arm][1]}, β모멘텀 {SPEC[arm][2]})")
    print(f"   거래 단위 Sharpe 구간(독립 가정): {dict((v, n) for (k, v), n in tally.items() if k == arm + '_naive')}")
    print(f"   거래일 단위 상태: {dict((v, n) for (k, v), n in tally.items() if k == arm + '_daily')}")
    print(f"   종합 판정(요인 알파): {dict((v, n) for (k, v), n in tally.items() if k == arm + '_verdict')}")
    print(f"   β시장 평균 {sum(sums[arm + '_bm']) / REPS:+.2f}, β모멘텀 평균 {sum(sums[arm + '_bq']) / REPS:+.2f}, 알파 |t|≥1.96 비율 {sum(abs(x) >= 1.96 for x in sums[arm + '_alpha_t']) / REPS:.1%}")
    if arm + '_paired_t' in sums:
        print(f"   대조군과 같은 날 짝지은 차이 |t|≥1.96 비율 {sum(abs(x) >= 1.96 for x in sums[arm + '_paired_t']) / REPS:.1%}")
    for tag in ('real', 'noise', 'num'):
        print(f"   꼬리표 {tag}: {dict((v, n) for (k, v), n in tally.items() if k == f'{arm}_tag_{tag}')}")
