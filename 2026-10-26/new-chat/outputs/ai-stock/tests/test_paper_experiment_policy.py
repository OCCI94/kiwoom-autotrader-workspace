"""Paper experiment policy tests: temporary settings, no broker or tokens."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.config import settings
from kiwoom_autotrader.telegram.commands import login
from kiwoom_autotrader.trading import paper_policy
from kiwoom_autotrader.trading.order_context import automated_orders


class NoExperimentRead(dict):
    def get(self, key, *args):
        if key == 'paper_experiment_enabled':
            raise AssertionError('real mode read experiment setting')
        return super().get(key, *args)


class ExperimentSettingsTests(unittest.TestCase):
    def test_default_and_reset_are_paper_only_and_experiment_off(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            self.assertTrue(settings.load_settings(path)['paper_automation_only'])
            settings.save_settings({'paper_automation_only':False},path)
            settings.save_paper_strategy_options({'paper_experiment_enabled':True},path)
            result=settings.reset_settings(path)
            self.assertTrue(result['paper_automation_only'])
            self.assertFalse(result['paper_experiment_enabled'])
            self.assertFalse(settings.load_settings(path)['paper_experiment_enabled'])

    def test_profile_is_separate_from_shared_settings_and_default_limits(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            settings.save_settings({'gdcrs_enabled':True},path)
            original=path.read_bytes()
            result=settings.save_paper_strategy_options({'paper_experiment_enabled':True},path)
            self.assertEqual(path.read_bytes(),original)
            self.assertNotIn('paper_experiment_enabled',json.loads(original))
            self.assertEqual(result['paper_max_concurrent_positions'],8)
            self.assertEqual(settings.effective_paper_scan(result,'paper')['max_positions'],40)
            self.assertEqual(settings.effective_paper_scan(result,'paper')['limit'],30)
            self.assertEqual(settings.effective_paper_scan(result,'paper')['entry_pool_limit'],40)
            self.assertEqual(settings.effective_theme_cap(result,'paper'),5)
            self.assertEqual(settings.effective_theme_unclassified_policy(result,'paper'),'allow_unknown')
            self.assertEqual(settings.effective_b3_guards(result,'paper'),(100,0))
            self.assertEqual(settings.effective_cross_guards(result,'paper'),(10,0))
            self.assertEqual(settings.effective_unfilled_buy_ttl(result,'paper'),180)
            self.assertEqual(settings.effective_paper_experiment(result,'paper')['daily_buy_order_cap'],0)

    def test_real_profile_and_ttl_do_not_read_experiment_options(self):
        values=NoExperimentRead(paper_experiment_enabled=True)
        self.assertEqual(settings.effective_paper_experiment(values,'real'),{'enabled':False})
        self.assertEqual(settings.effective_unfilled_buy_ttl(values,'real'),0)
        self.assertEqual(paper_policy.experiment_order_sizing(values,'real',
            available_cash_krw=object(),price=object()),{'enabled':False})
        for helper in (settings.effective_cross_guards,settings.effective_b3_guards,
                       settings.effective_paper_scan,settings.effective_theme_cap,
                       settings.effective_theme_unclassified_policy):
            helper(values,'real')

    def test_real_loader_does_not_open_even_malformed_paper_sidecar(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            path.write_text('{}')
            options=settings.paper_strategy_options_path(path)
            options.write_text('DO NOT READ: invalid experimental configuration')
            original=Path.read_text
            def read(target,*args,**kwargs):
                if target==options:
                    raise AssertionError('real mode opened paper sidecar')
                return original(target,*args,**kwargs)
            with patch.object(Path,'read_text',read):
                result=settings.load_settings(path,mode='real')
            self.assertFalse(result['paper_experiment_enabled'])
            self.assertTrue(result['paper_automation_only'])

    def test_invalid_flag_is_rejected_without_saved_mutation(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            for value in (1,'true',None,[]):
                with self.subTest(value=value),self.assertRaises(settings.SettingsError):
                    settings.save_paper_strategy_options({'paper_experiment_enabled':value},path)
            self.assertFalse(path.exists())
            self.assertFalse(settings.paper_strategy_options_path(path).exists())

    def test_login_real_blocked_before_login_and_paper_unaffected(self):
        session=Mock();session.login.return_value=SimpleNamespace(message='ok')
        reporter=Mock()
        with patch.object(login,'load_settings',return_value={'paper_experiment_enabled':True}):
            self.assertIn('허용하지',login.handle_login('login_real','login real',session,reporter=reporter))
            session.login.assert_not_called();reporter.assert_not_called()
            self.assertEqual(login.handle_login('login_paper','login paper',session,reporter=reporter),'ok')
            session.login.assert_called_once_with('paper')
        session.reset_mock()
        with patch.object(login,'load_settings',side_effect=ValueError('private')):
            result=login.handle_login('login_real','login real',session,reporter=reporter)
            self.assertNotIn('private',result);session.login.assert_not_called()


class ExperimentSizingTests(unittest.TestCase):
    def size(self,cash=9000000,price=100000,existing=0,pending=0):
        return paper_policy.experiment_order_sizing({'paper_experiment_enabled':True},'paper',
            available_cash_krw=cash,price=price,existing_principal_krw=existing,pending_principal_krw=pending)

    def test_cash_formula_and_existing_pending_principal_are_combined(self):
        row=self.size(cash=3000000,price=30000,existing=50000,pending=30000)
        self.assertEqual(row['budget_krw'],66666)
        self.assertEqual(row['nominal_symbol_cap_krw'],133332)
        self.assertEqual(row['quantity'],1)
        self.assertEqual(row['principal_krw'],30000)
        self.assertFalse(row['one_share_exception'])

    def test_single_share_above_budget_and_old_million_cap_is_cash_safe(self):
        row=self.size(price=1500000)
        self.assertEqual(row['quantity'],1)
        self.assertTrue(row['one_share_exception'])
        self.assertEqual(row['effective_symbol_cap_krw'],1500000)
        self.assertEqual(self.size(cash=1499999,price=1500000)['quantity'],0)
        for existing,pending in ((1,0),(0,1),(1500000,0)):
            result=self.size(price=1500000,existing=existing,pending=pending)
            self.assertEqual(result['quantity'],0)
            self.assertFalse(result['one_share_exception'])

    def test_low_cash_and_rounding_never_exceed_available_cash(self):
        for cash in (0,1,29,30,299999,300000,9000001):
            for price in (1,29,300001,10000000):
                with self.subTest(cash=cash,price=price):
                    row=self.size(cash=cash,price=price)
                    self.assertLessEqual(row['principal_krw'],cash)
                    self.assertLessEqual(row['principal_krw'],row['effective_symbol_cap_krw'])

    def test_invalid_cash_is_not_replaced_with_estimate(self):
        for cash in (None,True,-1,'9000000',9000000.0):
            with self.subTest(cash=cash),self.assertRaises(paper_policy.PaperAutomationPolicyError):
                self.size(cash=cash)

    def test_proof_rejects_changed_quantity_or_arithmetic(self):
        row=self.size(cash=13500000)
        paper_policy.validate_experiment_order_sizing(row,quantity=3,price=100000)
        paper_policy.validate_experiment_order_sizing(row,quantity=2,price=100000)
        with self.assertRaises(paper_policy.PaperAutomationPolicyError):
            paper_policy.validate_experiment_order_sizing(row,quantity=4,price=100000)
        row['budget_krw']+=1
        with self.assertRaises(paper_policy.PaperAutomationPolicyError):
            paper_policy.validate_experiment_order_sizing(row,quantity=3,price=100000)


class ExperimentGuardTests(unittest.TestCase):
    def setUp(self):
        self.config={'paper_experiment_enabled':True,'paper_automation_only':True}
        self.tracker=Mock()
        self.tracker.assert_no_pending_order=Mock()
        self.session=SimpleNamespace(mode='paper')
        self.sizing=paper_policy.experiment_order_sizing(self.config,'paper',available_cash_krw=9000000,price=1500000)
        self.reconciled=Mock()
        self.reconciled.reconciliation_evidence={'schema_version':3,'multi_strategy_enabled':True,
                                                 'paper_experiment_sizing':self.sizing}
        self.session.paper_automation_reconcile=Mock(return_value=self.reconciled)
        patched=patch.object(paper_policy,'load_settings',side_effect=lambda:dict(self.config))
        self.loader=patched.start();self.addCleanup(patched.stop)

    def guard(self,strategy='EXP_A'):
        with automated_orders(self.tracker,strategy):
            return paper_policy.capture_automatic_paper_guard(self.session,'buy','005930','1','1500000')

    def test_explicit_paper_one_share_exception_requires_fresh_sizing_proof(self):
        guard=self.guard()
        self.assertTrue(guard.before_prepare());guard()
        self.session.paper_automation_reconcile.assert_called_once_with('005930',side='buy',quantity='1',price='1500000',strategy='EXP_A')
        self.reconciled.assert_called_once()

    def test_real_arm_rejected_before_loading_settings(self):
        self.session.mode='real';guard=self.guard()
        with self.assertRaises(paper_policy.PaperAutomationPolicyError):guard.before_prepare()
        self.loader.assert_not_called();self.session.paper_automation_reconcile.assert_not_called()

    def test_disabled_arm_and_midqueue_mode_toggle_block(self):
        guard=self.guard();guard.before_prepare()
        self.config['paper_experiment_enabled']=False
        with self.assertRaises(paper_policy.PaperAutomationPolicyError):guard()
        with self.assertRaises(paper_policy.PaperAutomationPolicyError):self.guard().before_prepare()

    def test_missing_or_malformed_sizing_does_not_pass(self):
        for value in (None,{},dict(self.sizing,one_share_exception=False)):
            self.reconciled.reconciliation_evidence['paper_experiment_sizing']=value
            with self.subTest(value=value),self.assertRaises(paper_policy.PaperAutomationPolicyError):
                self.guard().before_prepare()

    def test_experiment_sell_requires_owned_generation_schema_four(self):
        generation='1'*32
        context=SimpleNamespace(tracker=self.tracker,strategy='EXP_D',evidence=SimpleNamespace(
            as_dict=lambda:{'config':{'multi_exit_kind':'full','multi_position_generation_id':generation}}))
        self.reconciled.reconciliation_evidence={'schema_version':4}
        guard=paper_policy._AutomaticPaperGuard(context,self.session,'sell','005930','1',None)
        guard.before_prepare();guard()
        self.session.paper_automation_reconcile.assert_called_once_with('005930',side='sell',quantity='1',
            price=None,strategy='EXP_D',exit_kind='full',generation_id=generation)
        self.reconciled.reconciliation_evidence={'schema_version':1}
        guard=paper_policy._AutomaticPaperGuard(context,self.session,'sell','005930','1',None)
        with self.assertRaises(paper_policy.PaperAutomationPolicyError):guard.before_prepare()


if __name__=='__main__':unittest.main()
