# telemetryd C++ CPython 임베딩 예제 (DESIGN.md §4).
#
# telemetryd/.venv 안의 pybind11(embed 컴포넌트)로 C++ 프로세스 안에 CPython
# 인터프리터를 띄우고 telemetryd 파이썬 패키지를 그대로 import해서 호출한다.
# pybind11_DIR / Python3_EXECUTABLE 은 scripts/build_cpp.sh 가 venv 기준으로
# 자동 계산해서 넘겨준다 — 직접 configure할 때는 아래처럼 지정한다:
#
#   cmake -S cpp -B cpp/build \
#     -Dpybind11_DIR=$(../.venv/bin/python -m pybind11 --cmakedir) \
#     -DPython3_EXECUTABLE=$(readlink -f ../.venv/bin/python3)
cmake_minimum_required(VERSION 3.15)
project(telemetryd_cpp_embed CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(pybind11 REQUIRED)

add_executable(embed_example examples/embed_example.cpp)
target_link_libraries(embed_example PRIVATE pybind11::embed)
