import tempfile
import json
from pathlib import Path
from unittest import TestCase

from kiwoom_autotrader.config.settings import (
    PAPER_MULTI_STRATEGIES, SettingsError, _validate, default_settings,
    effective_multi_strategies, load_settings, save_settings, reset_settings,
    paper_strategy_options_path,
    effective_paper_scan, effective_entry_time_window, effective_unfilled_buy_ttl,
)


class MultiStrategySettingsTests(TestCase):
    def test_legacy_entry_pool_limit_preserves_thirty_without_rewriting_either_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'settings.json'
            shared = b'{"gdcrs_enabled":true}\n'
            path.write_bytes(shared)
            sidecar = paper_strategy_options_path(path)
            legacy = {'paper_multi_strategy_enabled': True,
                      'paper_multi_strategy_ids': list(PAPER_MULTI_STRATEGIES),
                      'gdcrs_initial_entry_pct': 50, 'gdcrs_add_on_profit_bps': 50,
                      'gdcrs_max_daily_entries_per_symbol': 2}
            sidecar.write_text(json.dumps(legacy))
            before = sidecar.read_bytes()
            value = load_settings(path)
            self.assertEqual(value['paper_strategy_entry_pool_limit'], 30)
            self.assertEqual(effective_paper_scan(value, 'paper')['entry_pool_limit'], 30)
            self.assertEqual(effective_paper_scan(value, 'real')['entry_pool_limit'], 0)
            self.assertEqual(path.read_bytes(), shared)
            self.assertEqual(sidecar.read_bytes(), before)

    def test_forty_entry_pool_is_separate_from_position_limit_and_principal(self):
        from kiwoom_autotrader.trading.paper_policy import _buy_cap, _symbol_cap
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'settings.json'
            shared = b'{"gdcrs_enabled":true}\n'
            path.write_bytes(shared)
            stocks = [{'code': f'{i:06d}', 'budget': 1_000_000} for i in range(1, 26)]
            value = save_settings({'paper_multi_strategy_enabled': True,
                'paper_multi_strategy_ids': list(PAPER_MULTI_STRATEGIES),
                'paper_dynamic_scan_enabled': True, 'paper_dynamic_candidate_limit': 20,
                'paper_strategy_stocks': stocks, 'paper_strategy_entry_pool_limit': 40,
                'paper_max_concurrent_positions': 8,
                'gdcrs_initial_entry_pct': 50, 'gdcrs_add_on_profit_bps': 50,
                'gdcrs_max_daily_entries_per_symbol': 2}, path)
            scan = effective_paper_scan(value, 'paper')
            self.assertEqual(scan['entry_pool_limit'], 40)
            self.assertEqual(scan['limit'], 20)
            self.assertEqual(len(scan['stocks']), 25)
            self.assertEqual(scan['max_positions'], 8)
            self.assertEqual((_buy_cap(value), _symbol_cap(value)), (1_000_000, 1_000_000))
            self.assertEqual(path.read_bytes(), shared)
            self.assertEqual(json.loads(paper_strategy_options_path(path).read_text())['paper_strategy_entry_pool_limit'], 40)
            save_settings({'gdcrs_short_period': 6}, path)
            self.assertNotIn('paper_strategy_entry_pool_limit', json.loads(path.read_text()))
            self.assertEqual(load_settings(path)['paper_strategy_entry_pool_limit'], 40)
            reset_settings(path)
            self.assertEqual(load_settings(path)['paper_strategy_entry_pool_limit'], 30)

    def test_entry_pool_limit_is_strict_and_cannot_silently_truncate_configured_pool(self):
        for limit in (True, False, 0, -1, 41, 40.0, '40', None):
            with self.subTest(limit=limit), self.assertRaises(SettingsError):
                _validate({**default_settings(), 'paper_strategy_entry_pool_limit': limit})
        for count, cap in ((25, 24), (26, 40)):
            stocks = [{'code': f'{i:06d}', 'budget': 1_000_000} for i in range(1, count+1)]
            with self.subTest(count=count, cap=cap), self.assertRaises(SettingsError):
                _validate({**default_settings(), 'paper_strategy_entry_pool_limit': cap,
                           'paper_strategy_stocks': stocks})
        # A small explicit pool is valid only when it can contain the configured
        # extras. The monitor additionally checks the union with gdcrs symbols.
        self.assertEqual(effective_paper_scan({'paper_strategy_entry_pool_limit': 1}, 'paper')['entry_pool_limit'], 1)

    def test_pool_upgrade_can_explicitly_preserve_existing_entry_hours_and_disabled_ttl(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'settings.json'
            shared = b'{"gdcrs_short_period":5,"gdcrs_long_period":20}\n'
            path.write_bytes(shared)
            value = save_settings({'paper_strategy_entry_pool_limit': 40,
                'gdcrs_entry_start_time': '09:00', 'gdcrs_entry_end_time': '15:30',
                'gdcrs_unfilled_buy_ttl_seconds': 0}, path)
            self.assertEqual(effective_entry_time_window(value, 'paper'), ('09:00', '15:30'))
            self.assertEqual(effective_unfilled_buy_ttl(value, 'paper'), 0)
            self.assertEqual((value['gdcrs_short_period'], value['gdcrs_long_period']), (5, 20))
            self.assertEqual(path.read_bytes(), shared)

    def test_dynamic_settings_are_optional_paper_only_and_legacy_sidecar_loads(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'settings.json'
            shared=b'{"gdcrs_enabled":true}\n';path.write_bytes(shared)
            legacy={'paper_multi_strategy_enabled':True,'paper_multi_strategy_ids':list(PAPER_MULTI_STRATEGIES),
                    'gdcrs_initial_entry_pct':50,'gdcrs_add_on_profit_bps':50,'gdcrs_max_daily_entries_per_symbol':2}
            paper_strategy_options_path(path).write_text(json.dumps(legacy))
            self.assertFalse(load_settings(path)['paper_dynamic_scan_enabled'])
            value=save_settings({'paper_dynamic_scan_enabled':True,'paper_dynamic_candidate_limit':20,
                'paper_strategy_stocks':[{'code':'005930','budget':1000000}],'paper_max_concurrent_positions':8},path)
            self.assertEqual(path.read_bytes(),shared)
            self.assertEqual(effective_paper_scan(value,'paper')['max_positions'],8)
            self.assertEqual(effective_paper_scan(value,'real')['max_positions'],0)
            self.assertEqual(effective_paper_scan(value,'real')['stocks'],())
            self.assertFalse(effective_paper_scan(value,'real')['enabled'])

    def test_dynamic_bounds_and_combined_symbol_budget_are_strict(self):
        for update in ({'paper_dynamic_scan_enabled':1},{'paper_dynamic_candidate_limit':31},
                {'paper_dynamic_candidate_limit':14},{'paper_max_concurrent_positions':0},
                {'paper_max_concurrent_positions':11},{'paper_strategy_stocks':[{'code':'005930','budget':1000001}]},
                {'paper_strategy_stocks':[{'code':'005930','budget':1}]*2}):
            with self.subTest(update=update),self.assertRaises(SettingsError):
                _validate({**default_settings(),**update})

    def test_legacy_file_is_not_migrated_or_enabled(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'settings.json'
            payload = b'{"gdcrs_enabled":true}\n'
            path.write_bytes(payload)
            value = load_settings(path)
            self.assertEqual(effective_multi_strategies(value, 'paper'), ())
            self.assertEqual(path.read_bytes(), payload)

    def test_explicit_paper_opt_in_does_not_change_real_or_budget(self):
        with tempfile.TemporaryDirectory() as root:
            value = save_settings({'paper_multi_strategy_enabled': True,
                'paper_multi_strategy_ids': list(reversed(PAPER_MULTI_STRATEGIES))},
                Path(root) / 'settings.json')
            self.assertEqual(effective_multi_strategies(value, 'paper'), PAPER_MULTI_STRATEGIES)
            self.assertEqual(effective_multi_strategies(value, 'real'), ())
            self.assertEqual(value['paper_automation_symbol_cap'], 1_000_000)
            self.assertEqual(value['gdcrs_initial_entry_pct'], 100)

    def test_malformed_or_ambiguous_configuration_fails(self):
        for update in ({'paper_multi_strategy_enabled': 1},
                       {'paper_multi_strategy_enabled': True},
                       {'paper_multi_strategy_ids': 'S2_surge_high_breakout'},
                       {'paper_multi_strategy_ids': ['not_a_strategy']},
                       {'paper_multi_strategy_ids': [None]},
                       {'paper_multi_strategy_ids': [PAPER_MULTI_STRATEGIES[0]] * 2}):
            with self.subTest(update=update), self.assertRaises(SettingsError):
                _validate({**default_settings(), **update})
        with self.assertRaises(SettingsError):
            effective_multi_strategies(default_settings(), 'unknown')

    def test_options_do_not_change_shared_collector_bytes_and_survive_unrelated_save(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'settings.json'
            original = b'{"gdcrs_enabled": true}\n'
            path.write_bytes(original)
            save_settings({'paper_multi_strategy_enabled': True,
                           'paper_multi_strategy_ids': list(PAPER_MULTI_STRATEGIES),
                           'gdcrs_initial_entry_pct':50,'gdcrs_add_on_profit_bps':50,
                           'gdcrs_max_daily_entries_per_symbol':2}, path)
            self.assertEqual(path.read_bytes(), original)
            sidecar = paper_strategy_options_path(path)
            self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
            save_settings({'gdcrs_short_period': 6}, path)
            self.assertNotIn('paper_multi_strategy_enabled', json.loads(path.read_text()))
            self.assertNotIn('gdcrs_initial_entry_pct', json.loads(path.read_text()))
            self.assertNotIn('gdcrs_add_on_profit_bps', json.loads(path.read_text()))
            self.assertNotIn('gdcrs_max_daily_entries_per_symbol', json.loads(path.read_text()))
            self.assertTrue(load_settings(path)['paper_multi_strategy_enabled'])
            reset_settings(path)
            self.assertFalse(load_settings(path)['paper_multi_strategy_enabled'])
            self.assertEqual(load_settings(path)['gdcrs_initial_entry_pct'],100)

    def test_invalid_or_symlink_options_and_mixed_write_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'settings.json'
            option = paper_strategy_options_path(path)
            option.write_text('{"paper_multi_strategy_enabled":true}')
            with self.assertRaises(SettingsError):load_settings(path)
            option.unlink()
            target = Path(root)/'target'; target.write_text('{}')
            option.symlink_to(target)
            with self.assertRaises(SettingsError):load_settings(path)
            option.unlink()
            with self.assertRaises(SettingsError):
                save_settings({'paper_multi_strategy_enabled':True,
                               'paper_multi_strategy_ids':list(PAPER_MULTI_STRATEGIES),
                               'gdcrs_enabled':True},path)

    def test_complete_dictionary_round_trip_keeps_unchanged_options_separate(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'settings.json'
            initial=load_settings(path)
            save_settings(dict(initial,gdcrs_enabled=True),path)
            self.assertTrue(load_settings(path)['gdcrs_enabled'])
            self.assertFalse(paper_strategy_options_path(path).exists())
            self.assertNotIn('paper_multi_strategy_enabled',json.loads(path.read_text()))
