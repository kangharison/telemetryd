"""C++ CPython 임베딩(DESIGN.md §4)이 호출하는 얇은 JSON 계층.

pybind11 embed.h로 이 모듈을 import해서 함수를 호출하면, 중첩된 dataclass를
pybind11 type caster로 일일이 매핑할 필요 없이 JSON 문자열 하나만 주고받으면
된다. 이 파일 자체엔 조회 로직이 전혀 없다 — backend.get_backend() 호출 +
dataclasses.asdict() + json.dumps() 뿐. cpp/examples/embed_example.cpp가 이
함수들을 그대로 호출한다.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import List, Optional

from telemetryd.backend import get_backend


def list_devices_json(backend: str = "mock") -> str:
    return json.dumps(get_backend(backend).list_devices())


def get_device_snapshot_json(device: str, backend: str = "mock") -> str:
    snap = get_backend(backend).get_device_snapshot(device)
    return json.dumps(asdict(snap))


def get_queue_entries_json(
    device: str, qid: int, limit: int = 16, around_doorbell: bool = True, backend: str = "mock"
) -> str:
    entries = get_backend(backend).get_queue_entries(device, qid, limit, around_doorbell)
    return json.dumps([asdict(e) for e in entries])


def get_completion_entries_json(
    device: str, qid: int, limit: int = 16, around_doorbell: bool = True, backend: str = "mock"
) -> str:
    entries = get_backend(backend).get_completion_entries(device, qid, limit, around_doorbell)
    return json.dumps([asdict(e) for e in entries])


def get_prp_payload_json(device: str, qid: int, cid: int, backend: str = "mock") -> str:
    payload = get_backend(backend).get_prp_payload(device, qid, cid)
    d = asdict(payload)
    for p in d["pages"]:
        p["data_hex"] = p["data"].hex()  # bytes는 JSON으로 직렬화 불가 → hex 문자열로.
        del p["data"]
    return json.dumps(d)


def get_tree_node_json(device: str, path: Optional[List[str]] = None, backend: str = "mock") -> str:
    exp = get_backend(backend).get_tree_node(device, path or [])
    return json.dumps(asdict(exp))
