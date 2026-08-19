#!/bin/bash
# cpp/examples/embed_example.cpp 빌드 + 실행.
#
# telemetryd .venv의 pybind11(embed)을 CMake로 찾게 한다. 실행 시 PYTHONPATH에
# src/를 직접 넣는다 — venv site-packages를 넣는 방식은 안 통한다: editable
# install(`pip install -e .`)은 `__editable__.telemetryd-*.pth` 파일로
# 동작하는데, .pth 처리는 site 모듈이 site-packages로 "등록"한 디렉터리에서만
# 일어나고 PYTHONPATH로 얹은 디렉터리에서는 무시된다(직접 겪음: 시스템
# /usr/bin/python3 에 PYTHONPATH로 site-packages를 줘도 import telemetryd가
# ModuleNotFoundError). src/telemetryd는 평범한 패키지라 PYTHONPATH에 src/를
# 바로 넣으면 .pth 없이도 그냥 찾는다.
#
# PYBIND11_FINDPYTHON=ON + Python3_EXECUTABLE 조합을 써야 pybind11이 실제로
# venv python을 본다 — 안 그러면 pybind11의 레거시 FindPythonInterp 경로가
# PYTHON_EXECUTABLE(구버전 변수명)만 보고 Python3_EXECUTABLE은 무시해서
# /usr/bin/python3 로 새 버린다(처음 시도에서 CMake가 "Python3_EXECUTABLE ...
# not used by the project" 경고를 냈다).
set -euo pipefail
cd "$(dirname "$0")/.."

VENV_PY="$(readlink -f .venv/bin/python3)"
PYBIND11_DIR="$(.venv/bin/python -m pybind11 --cmakedir)"
SRC_DIR="$(pwd)/src"

cmake -S cpp -B cpp/build \
  -Dpybind11_DIR="$PYBIND11_DIR" \
  -DPYBIND11_FINDPYTHON=ON \
  -DPython3_EXECUTABLE="$VENV_PY" \
  -DCMAKE_BUILD_TYPE=Release

cmake --build cpp/build -j"$(nproc)"

echo
echo "=== 실행: cpp/build/embed_example ${1:-mock} ==="
PYTHONPATH="$SRC_DIR" ./cpp/build/embed_example "${1:-mock}"
