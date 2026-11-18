import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('tracker_server',Path(__file__).with_name('server.py'))
s=importlib.util.module_from_spec(spec); spec.loader.exec_module(s)

class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old=s.DB; s.DB=Path(self.tmp.name)/'test.sqlite3'
    def tearDown(self): s.DB=self.old; self.tmp.cleanup()
    def student(self): s.add_student({'name':'Lan','weekly_goal':3,'youtube':'https://www.youtube.com/@example'})
    def test_manual_video_and_canonical_dedup(self):
        self.student()
        date=s.dt.datetime.now(s.TZ).date().isoformat()
        s.add_video({'student_id':1,'url':'https://youtu.be/abcdefghijk?t=2','published_at':date})
        with self.assertRaises(ValueError): s.add_video({'student_id':1,'url':'https://www.youtube.com/watch?v=abcdefghijk','published_at':date})
        dash=s.dashboard(); self.assertEqual(dash['students'][0]['count_week'],1); self.assertEqual(dash['videos'][0]['verification'],'manual')
    def test_tiktok_short_link(self):
        self.student()
        s.add_video({'student_id':1,'url':'https://vm.tiktok.com/ZM123xyz/?share=1','published_at':'2026-01-01'})
        self.assertEqual(s.dashboard()['videos'][0]['url'],'https://vm.tiktok.com/ZM123xyz/')
    def test_invalid_inputs(self):
        self.student()
        for url in ('https://youtube.com.evil.test/watch?v=abcdefghijk','http://youtube.com/watch?v=abcdefghijk','https://tiktok.com/@foo'):
            with self.assertRaises(ValueError): s.add_video({'student_id':1,'url':url,'published_at':'2026-01-01'})
        with self.assertRaises(ValueError): s.add_student({'name':'','weekly_goal':0})
    def test_sync_idempotent_and_rejects_missing_dates(self):
        self.student()
        output='\n'.join(json.dumps(x) for x in [{'id':'abcdefghijk','upload_date':'20260101','title':'test'},{'id':'zyxwvutsrqp','title':'unknown'}])
        class Result: returncode=0; stdout=output
        with patch.object(s.shutil,'which',return_value='/fake/yt-dlp'),patch.object(s.subprocess,'run',return_value=Result()):
            self.assertEqual(s.sync({'student_id':1})['added'],1); self.assertEqual(s.sync({'student_id':1})['added'],0)
        self.assertEqual(len(s.dashboard()['videos']),1)
    def test_analysis_sends_no_personal_data(self):
        self.student()
        class Result: returncode=0; stdout='Phân tích thử'
        with patch.object(s.shutil,'which',return_value='/fake/codex'),patch.object(s.subprocess,'run',return_value=Result()) as run:
            self.assertEqual(s.analyze({'provider':'codex'})['report'],'Phân tích thử')
            self.assertNotIn('Lan',run.call_args.kwargs['input']); self.assertNotIn('example',run.call_args.kwargs['input'])
            self.assertFalse(run.call_args.kwargs.get('shell',False))
    def test_verify_reach_response(self):
        self.student(); s.add_video({'student_id':1,'url':'https://youtu.be/abcdefghijk','published_at':'2026-01-01'})
        class Result: stdout=json.dumps({'ok':True,'items':[{'published_at':'2026-02-02','title':'Real'}]})
        with patch.object(s.subprocess,'run',return_value=Result()): s.verify({'video_id':1})
        self.assertEqual(s.dashboard()['videos'][0]['verification'],'reach')

if __name__=='__main__': unittest.main()
