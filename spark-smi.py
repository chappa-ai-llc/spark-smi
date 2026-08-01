#!/usr/bin/env python3
# Backward-compatible launcher. The actual code lives in the spark_smi package.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from spark_smi.app import main

if __name__ == "__main__":
    main()
