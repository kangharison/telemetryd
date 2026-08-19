#!/bin/bash
# proto/telemetryd.proto -> src/telemetryd/grpcserver/*_pb2*.py 재생성.
# grpc_tools가 만드는 _pb2_grpc.py는 기본적으로 절대 import(`import telemetryd_pb2`)를
# 써서 패키지 안에서 깨지므로, sed로 상대 import(`from . import telemetryd_pb2`)로 고친다.
set -euo pipefail
cd "$(dirname "$0")/.."

.venv/bin/python -m grpc_tools.protoc \
  -I proto \
  --python_out=src/telemetryd/grpcserver \
  --grpc_python_out=src/telemetryd/grpcserver \
  --pyi_out=src/telemetryd/grpcserver \
  proto/telemetryd.proto

sed -i 's/^import telemetryd_pb2 as telemetryd__pb2$/from . import telemetryd_pb2 as telemetryd__pb2/' \
  src/telemetryd/grpcserver/telemetryd_pb2_grpc.py

echo "OK: src/telemetryd/grpcserver/telemetryd_pb2{,_grpc}.py 재생성 완료"
