"""Synthetic test state; no account history or private match data required."""
from copy import deepcopy
from pathlib import Path
from coach.strategies import seed


def fps_state():
    book = seed('fps')
    code = (Path(__file__).resolve().parents[1]/'runtime/fps/agent.py').read_text()
    return {'phase':'play_A','generation':1,'history':[],'faults':[], 'plan_ready':False,
            'A':{'strategy':deepcopy(book),'code':code,'seen':[],'last':{}},
            'B':{'strategy':deepcopy(book),'code':code,'seen':[],'last':{}},
            'code_best':{'code':code,'book':deepcopy(book),'ok':True,'score':1.,'report':'PASS syntax: fixture','context':'fixture'}}
