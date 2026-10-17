"""Make `import fizgig` work when the tests run from anywhere, without a GUI or GPU.

Run:  venv/Scripts/python.exe -I -m unittest discover -s src/fizgig/monkap/unit_tests
"""
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if SRC not in sys.path:
    sys.path.insert(0, SRC)
