/* jsc core.js vectors.js check_core.js
   계산기의 JS 로직이 연구 엔진의 기대값과 같은지 본다. */
(function () {
  var C = LeaderCalc, total = 0, fails = 0, shown = 0, bySection = {};

  function norm(v) { return v === undefined ? null : v; }
  function same(got, exp) {
    got = norm(got); exp = norm(exp);
    if (got === null || exp === null) { return got === exp; }
    if (typeof got === 'number' && typeof exp === 'number') {
      return Math.abs(got - exp) <= 1e-9 * Math.max(1, Math.abs(got), Math.abs(exp));
    }
    if (typeof got !== typeof exp) { return false; }
    if (Array.isArray(exp)) {
      if (!Array.isArray(got) || got.length !== exp.length) { return false; }
      return exp.every(function (x, i) { return same(got[i], x); });
    }
    if (typeof exp === 'object') {
      return Object.keys(exp).every(function (k) { return same(got[k], exp[k]); });
    }
    return got === exp;
  }
  function check(section, got, exp, ctx) {
    total++;
    bySection[section] = bySection[section] || [0, 0];
    bySection[section][0]++;
    if (!same(got, exp)) {
      fails++; bySection[section][1]++;
      if (shown++ < 10) {
        print('MISMATCH ' + section + ' ' + JSON.stringify(ctx).slice(0, 500) +
          '\n  got ' + JSON.stringify(got).slice(0, 500) + '\n  exp ' + JSON.stringify(exp).slice(0, 500));
      }
    }
  }
  function pick(obj, keys) { var o = {}; keys.forEach(function (k) { o[k] = obj[k]; }); return o; }

  VECTORS.percent_rank.forEach(function (c) {
    check('percent_rank', C.percentRank(c.values, { mode: c.mode, ties: c.ties, higherIsBetter: c.hib }), c.expect, c);
  });
  VECTORS.sector.forEach(function (c) {
    check('sector', C.evaluateSectorEtf(c.args), c.expect, c.args);
  });
  VECTORS.comp.forEach(function (c) {
    var out = C.computeComp(c.raws, { regime: c.regime, penalty: c.penalty });
    check('comp', out.results, c.expect, { regime: c.regime, penalty: c.penalty, n: c.raws.length });
  });
  VECTORS.f_score.forEach(function (c) {
    var out = C.fScore(c.current, c.previous, { excludedFinancial: c.excluded, material: c.material });
    check('f_score', out, c.expect, pick(c, ['current', 'previous', 'excluded', 'material']));
  });
  VECTORS.f_composite.forEach(function (c) {
    var out = C.computeFComposite(c.rows, { regime: c.regime });
    check('f_composite', out.results, c.expect, { regime: c.regime, rows: c.rows });
  });
  VECTORS.f_grade.forEach(function (c) {
    check('f_grade', C.fGrade(c.f_core, c.f_comp, { material: c.material, accrualOk: c.accrual_ok }), c.expect, c);
  });
  VECTORS.cross.forEach(function (c) {
    check('cross', C.crossValidate(c.comp, c.f_core), c.expect, c);
  });
  VECTORS.comp_grade.forEach(function (c) {
    check('comp_grade', C.compGrade(c.score), c.expect, c);
  });
  VECTORS.gate.forEach(function (c) {
    check('gate', C.riskRewardGate(c.price, c.target, c.stop_pct), c.expect, pick(c, ['price', 'target', 'stop_pct']));
  });

  Object.keys(bySection).forEach(function (k) {
    print(k + ': ' + bySection[k][0] + ' checked, ' + bySection[k][1] + ' mismatched');
  });
  print('TOTAL ' + total + ' checked, ' + fails + ' mismatched');
})();
