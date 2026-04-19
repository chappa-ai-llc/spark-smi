"""
Entry point for spark-smi when installed via pip.
The actual monitoring code lives in spark_smi/_core.py.
"""
import os
import runpy
import sys


def main():
    core = os.path.join(os.path.dirname(__file__), "_core.py")
    runpy.run_path(core, run_name="__main__")


if __name__ == "__main__":
    main()
