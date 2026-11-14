import types
import unittest
from coach import loop, gate_fps
from coach.prompts import repair_messages

class FunctionRepairTests(unittest.TestCase):
    def test_only_selected_function_changes(self):
        source='VALUE = 7\ndef helper():\n    return 1\n\ndef other():\n    return 9\n'
        fixed=loop.apply_function_replacements(source,'```python\ndef helper():\n    return 2\n```')
        self.assertIn('VALUE = 7\n',fixed)
        self.assertIn('def other():\n    return 9\n',fixed)
        self.assertIn('def helper():\n    return 2\n',fixed)
    def test_global_rewrite_cannot_hide_in_function_response(self):
        with self.assertRaises(loop.OmlxError):
            loop.apply_function_replacements('def f():\n    return 1\n','```python\nimport os\ndef f():\n    return 2\n```')
    def test_unknown_function_rejected(self):
        with self.assertRaises(loop.OmlxError):
            loop.apply_function_replacements('def f():\n    return 1\n','```python\ndef other():\n    return 2\n```')
    def test_diagnosis_is_invalidated_when_failure_changes(self):
        first=loop.repair_signature('context','FAIL departure: moved\nPASS aiming: hit')
        second=loop.repair_signature('context','PASS departure: held\nFAIL aiming: missed')
        self.assertNotEqual(first,second)
    def test_swallowed_live_exception_is_a_gate_failure(self):
        module=types.ModuleType('agent')
        exec("def helper():\n    raise KeyError('missing')\ndef act(o):\n    try: return helper()\n    except Exception: return {}\n",module.__dict__)
        gate_fps.monitor_empty_errors(module)
        self.assertEqual(module.act({'phase':'live','me':{'alive':True}}),{})
        self.assertIn('KeyError',str(module._gate_empty_errors))
        self.assertIn('function=helper',str(module._gate_empty_errors))
    def test_handled_exception_with_valid_action_is_not_empty_error(self):
        module=types.ModuleType('agent')
        exec("def helper():\n    raise KeyError('optional')\ndef act(o):\n    try: return helper()\n    except Exception: return {'move_forward':1}\n",module.__dict__)
        gate_fps.monitor_empty_errors(module)
        module.act({'phase':'live','me':{'alive':True}})
        self.assertEqual(module._gate_empty_errors,[])
    def test_prompt_explains_state_and_exact_peek_contract(self):
        prompt=repair_messages({},'def f(): pass','FAIL runtime_errors: KeyError','diagnosis')[0]['content']
        self.assertIn('2 direction reversals',prompt)
        self.assertIn('does not fill missing keys',prompt)
        self.assertIn('No JSON edits',prompt)

class RevisionNameTests(unittest.TestCase):
    def test_previously_admitted_name_is_renamed_even_if_not_current_best(self):
        from unittest.mock import patch
        from coach.config import load
        from coach.strategies import seed
        best={**seed('fps'),'name':'current'}
        proposal={**best,'name':'previously-admitted'}
        with patch.object(loop,'admitted_names',return_value={'previously-admitted'}),patch.object(loop,'code_book',return_value={**proposal,'name':'fresh'}) as rename:
            actual=loop._passed_book(load(),'fps',best,proposal,'def act(o): return {}','PASS syntax: ok')
        rename.assert_called_once()
        self.assertEqual(actual['name'],'fresh')

if __name__=='__main__': unittest.main()
