import datetime as dt
import unittest
from notify import triage, request_enrichment, commands

NOW = dt.datetime(2026,10,4,12,tzinfo=dt.UTC).timestamp()
W = '0x' + 'a'*40


def case(**changes):
    f = dict(id='case1',key=W,tier='digest',signal='10',ts=NOW-600,episode_first='2026-10-04 10:00',
             episode_last='2026-10-04 11:50',pending_send=True,value=.59)
    f.update(changes)
    return f


class TriageTests(unittest.TestCase):
    def test_observation_is_private_and_not_a_human_queue_item(self):
        f=case();s={'open':{'case1':f}}
        triage.prepare(s,NOW)
        self.assertFalse(f['pending_send']);self.assertTrue(f['needs_triage'])
        self.assertFalse(triage.queue_case(f,NOW));self.assertEqual(request_enrichment.pending(s),[f])
        triage.prepare(s,NOW);self.assertEqual(f['triage_generation'],1)

    def test_existing_fresh_unhandled_case_enters_automatic_verification(self):
        f=case(pending_send=False,last_sent='2026-10-04T11:00:00Z',tier='notify',signal='S-G')
        s={'open':{'case1':f}};triage.prepare(s,NOW)
        self.assertTrue(f['needs_triage']);self.assertFalse(triage.queue_case(f,NOW))

    def test_wallet_exception_waits_for_private_identity_before_notification(self):
        f=case(signal='CAP',tier='notify');s={'open':{'case1':f}}
        triage.prepare(s,NOW)
        self.assertFalse(f['pending_send']);self.assertFalse(triage.queue_case(f,NOW))
        triage.apply_results(s,{'case1':{'generation':1,'decision':'exception','message_id':123,'retry':False,'at':'2026-10-04T12:00:00Z'}})
        self.assertTrue(triage.queue_case(f,NOW));self.assertEqual(f['tg_message_id'],123)
        self.assertNotIn('human_email',f)

    def test_receipt_cannot_ack_a_new_generation_or_import_identity(self):
        f=case(triage_generation=2,needs_triage=True);s={'open':{'case1':f}}
        triage.apply_results(s,{'case1':{'generation':1,'decision':'expected','human_email':'private' + '@' + 'example.com'}})
        self.assertTrue(f['needs_triage']);self.assertNotIn('human_email',f)
        triage.apply_results(s,{'case1':{'generation':2,'decision':'observe','retry':False,'human_email':'private' + '@' + 'example.com'}})
        self.assertFalse(f['needs_triage']);self.assertNotIn('human_email',f)

    def test_old_unacked_and_manually_watched_cases_archive_without_all_clear(self):
        for status in (None,'watching','reported','contained'):
            f=case(ts=NOW-90000,status=status,tier='notify',signal='S-F')
            s={'open':{'case1':f}};triage.prepare(s,NOW)
            self.assertEqual(f['status'],'archived');self.assertFalse(f['pending_send'])
            self.assertIn('evidence retained',f['status_note']);self.assertFalse(triage.queue_case(f,NOW))

    def test_new_episode_reopens_automatic_archive_but_respects_manual_close(self):
        new=case(episode_first='2026-10-04 10:00')
        old=case(ts=NOW-90000,status='archived',episode_last='2026-10-02 10:00',triage_generation=1,ack_by='operator')
        self.assertTrue(triage.reopen(old,new,NOW));self.assertNotIn('ack_by',old)
        self.assertFalse(triage.reopen(case(status='closed',status_by='operator'),new,NOW))

    def test_platform_exception_keeps_immediate_path_and_context_is_quiet(self):
        f=case(key='platform',tier='page');s={'open':{'case1':f}}
        triage.prepare(s,NOW);self.assertTrue(f['pending_send']);self.assertTrue(triage.queue_case(f,NOW))
        f['tier']='digest';triage.prepare(s,NOW);self.assertFalse(f['pending_send'])

    def test_cashout_and_composite_are_routed_privately(self):
        for f in (case(key='exit:'+W,signal='S-X',tier='notify'),case(key='hash',entities=[W],signal='composite',tier='page')):
            s={'open':{'case1':f}};triage.prepare(s,NOW)
            self.assertTrue(f['needs_triage']);self.assertFalse(f['pending_send'])

    def test_material_growth_requests_one_new_generation(self):
        f=case(pending_send=False,triage_generation=1,triage_value=.3,value=.6,needs_triage=False)
        s={'open':{'case1':f}};triage.prepare(s,NOW);triage.prepare(s,NOW)
        self.assertEqual(f['triage_generation'],2)

    def test_handled_group_and_late_receipt_cannot_reopen_review_queue(self):
        f=case(triage_generation=1,delivery_route='private',needs_decision=True,tg_message_id=100)
        other=case(id='case2',key='0x'+'b'*40,triage_generation=1,delivery_route='private',needs_decision=True,tg_message_id=100)
        s={'triage_version':1,'open':{'case1':f,'case2':other}}
        commands.set_status(s,'case1','reported','operator')
        self.assertFalse(other['needs_decision']);self.assertEqual(other['status'],'reported')
        triage.apply_results(s,{'case1':{'generation':1,'decision':'exception','retry':False}})
        self.assertFalse(f['needs_decision'])

    def test_overdue_independent_warning_has_one_fallback(self):
        f=case(signal='S-X',tier='notify');s={'open':{'case1':f}}
        triage.prepare(s,NOW);self.assertFalse(f['pending_send'])
        triage.prepare(s,NOW+3601);self.assertTrue(f['pending_send'])
        f.update(pending_send=False,fallback_sent=True)
        triage.prepare(s,NOW+3602);self.assertFalse(f['pending_send'])

    def test_cases_excludes_closed_and_automatic_observations(self):
        f=case(triage_generation=1,delivery_route='private',needs_decision=False)
        text=commands.cases_text({'open':{'case1':f},'triage_version':1})
        self.assertIn('Nothing is waiting',text)

if __name__=='__main__': unittest.main()
