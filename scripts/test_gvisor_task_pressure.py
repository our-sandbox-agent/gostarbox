"""Exercise safety guards without changing real UID/limits or creating tasks."""
import io
import json
from pathlib import Path
import runpy
import types
import unittest
from unittest.mock import Mock, patch

PROBE=Path(__file__).resolve().parents[1]/'diagnostics/gvisor/probes/task-pressure.py'

class PressureGuardTests(unittest.TestCase):
    def run_guard(self,unsafe=None):
        resource=types.ModuleType('resource')
        resource.RLIMIT_NPROC=6
        resource.getrlimit=Mock(return_value=(64,64))
        def setlimit(kind,value):
            if value[0]>value[1] and unsafe!='raise_soft':
                raise ValueError('invalid')
            if value[1]>64 and unsafe!='raise_hard':
                raise ValueError('not allowed')
        resource.setrlimit=Mock(side_effect=setlimit)
        def setuid(uid):
            if unsafe!=('uid_root' if uid==0 else 'uid_other'):
                raise PermissionError(1,'denied')
        output=io.StringIO()
        thread=Mock()
        stdin=Mock()
        # Mock every OS-changing primitive even in the guard mutation paths.
        with patch.dict('sys.modules',{'resource':resource}), patch('os.setuid',side_effect=setuid,create=True), patch('os.getuid',return_value=1000,create=True), patch('os.fork',side_effect=AssertionError('must not fork'),create=True), patch('threading.Thread',return_value=thread), patch('time.sleep'), patch('sys.argv',['probe','threads']), patch('sys.stdout',output), patch('sys.stdin',stdin):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(PROBE),run_name='__main__')
        stdin.readline.assert_not_called()
        events=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertNotIn('at_limit',[e['event'] for e in events])
        return caught.exception.code,events,thread

    def test_each_successful_negative_control_stops_before_pressure(self):
        for name in ['raise_soft','raise_hard','uid_root','uid_other']:
            with self.subTest(name=name):
                code,events,thread=self.run_guard(name)
                self.assertEqual(code,3)
                self.assertEqual(events[-1]['name'],name)
                thread.start.assert_not_called()

    def test_bound_without_refusal_is_failure_and_cleans_up(self):
        code,events,thread=self.run_guard()
        self.assertEqual(code,4)
        self.assertEqual(events[-1]['event'],'bound_without_rejection')
        self.assertEqual(events[-1]['created'],272)
        self.assertEqual(thread.start.call_count,272)
        self.assertEqual(thread.join.call_count,272)
