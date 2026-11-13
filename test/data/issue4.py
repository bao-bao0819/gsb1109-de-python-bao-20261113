"""
Source data for test/issue4. Very long!
"""
import json
from os import path

MAX = 0xfe000000

memo = None
def load():
    global memo
    if memo is not None:
        return memo
    dir = path.dirname(__file__)
    fpath = path.join(dir, 'issue4.json')
    with open(fpath, 'r') as f:
        memo = json.load(f)
        return memo
