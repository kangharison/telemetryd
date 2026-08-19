"""아키텍처 규칙을 **기계적으로** 강제하는 테스트.

리팩토링의 진짜 위험은 "지금 깔끔한가"가 아니라 "6개월 뒤에도 깔끔한가"다.
계층 규칙을 문서에만 적어두면 바쁠 때 조용히 무너진다 — import 한 줄이면
되니까. 그래서 규칙을 실행 가능한 테스트로 박아둔다.

여기서 하는 건 소스의 import 문을 AST로 읽는 정적 검사다. 실행 없이 보므로
drgn/수집기가 없어도 항상 돌고, 위반이 생기면 그 파일과 위반 대상을 딱 집어준다.
"""
from __future__ import annotations

import ast
import pathlib
from typing import Dict, List, Set

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "telemetryd"


def _imports_of(path: pathlib.Path) -> Set[str]:
    """그 파일이 import하는 telemetryd 내부 모듈 경로들(점 표기)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("telemetryd"):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # 상대 import(level>0)는 같은 패키지 안이라 계층 위반이 될 수 없다.
            if node.level == 0 and node.module and node.module.startswith("telemetryd"):
                found.add(node.module)
    return found


def _py_files(*parts: str) -> List[pathlib.Path]:
    base = SRC.joinpath(*parts)
    return sorted(base.rglob("*.py")) if base.exists() else []


def _label(path: pathlib.Path) -> str:
    """리포트용 짧은 경로 — SRC 밖의 파일(가드 자체 검증용 임시 파일)도 다룬다."""
    try:
        return path.relative_to(SRC).as_posix()
    except ValueError:
        return path.name


def _violations(files: List[pathlib.Path], forbidden_prefix: str,
                allow: Set[str] = frozenset()) -> List[str]:
    out = []
    for f in files:
        for mod in _imports_of(f):
            if mod.startswith(forbidden_prefix) and mod not in allow:
                out.append(f"{_label(f)} -> {mod}")
    return out


# ---- 규칙 1: 플랫폼은 위를 모른다 ------------------------------------------

def test_platform_does_not_import_services():
    """platform/* 는 도메인 서비스를 몰라야 한다.

    플랫폼이 특정 서비스를 알게 되는 순간 그 서비스 없이는 못 쓰게 되고,
    "기반 설비"가 아니라 그냥 다른 이름의 결합이 된다."""
    bad = _violations(_py_files("platform"), "telemetryd.services")
    assert not bad, "플랫폼이 서비스를 import함:\n  " + "\n  ".join(bad)


def test_platform_does_not_import_transport_layers():
    """platform/* 는 gRPC/웹/CLI 같은 전송·표현 계층도 몰라야 한다."""
    bad = []
    for prefix in ("telemetryd.grpcserver", "telemetryd.web", "telemetryd.cli"):
        bad += _violations(_py_files("platform"), prefix)
    assert not bad, "플랫폼이 전송 계층을 import함:\n  " + "\n  ".join(bad)


def test_platform_has_no_domain_vocabulary_in_module_names():
    """플랫폼 모듈 이름에 도메인 어휘(nvme/queue/perf 등)가 섞이면 경계가
    흐려진 것이다 — 이름은 계층을 드러내는 가장 값싼 신호다."""
    domain_words = ("nvme", "queue", "perf", "topology", "profiler", "event")
    offenders = [_label(f) for f in _py_files("platform")
                 if any(w in f.stem.lower() for w in domain_words)]
    assert not offenders, f"플랫폼에 도메인 이름이 섞임: {offenders}"


# ---- 규칙 2: 서비스는 서로를 직접 import하지 않는다 -------------------------

def test_services_do_not_import_each_other():
    """서비스끼리 직접 import하면 같은 프로세스에 있어야만 돌아가게 되어
    마이크로서비스로 쪼갤 수 없다. 다른 서비스가 필요하면 그 서비스의
    *contract*를 생성자로 주입받아야 한다."""
    services_dir = SRC / "services"
    if not services_dir.exists():
        return
    bad: List[str] = []
    for svc_dir in sorted(p for p in services_dir.iterdir() if p.is_dir()):
        own = f"telemetryd.services.{svc_dir.name}"
        for f in sorted(svc_dir.rglob("*.py")):
            for mod in _imports_of(f):
                if not mod.startswith("telemetryd.services."):
                    continue
                if mod == own or mod.startswith(own + "."):
                    continue          # 자기 자신은 당연히 허용
                bad.append(f"{_label(f)} -> {mod}")
    assert not bad, (
        "서비스가 다른 서비스를 직접 import함(계약 주입으로 바꿀 것):\n  "
        + "\n  ".join(bad))


def test_services_do_not_import_transport_layers():
    """서비스는 gRPC/HTTP를 몰라야 한다 — proto 메시지를 만들거나 상태 코드를
    정하는 건 어댑터의 일이다. 이게 지켜져야 전송을 바꿔도 서비스가 안 바뀐다."""
    bad = []
    for prefix in ("telemetryd.grpcserver", "telemetryd.web"):
        bad += _violations(_py_files("services"), prefix)
    assert not bad, "서비스가 전송 계층을 import함:\n  " + "\n  ".join(bad)


# ---- 규칙 3: 의존 방향 ------------------------------------------------------

def test_services_actually_build_on_the_platform():
    """의존 방향이 한 방향(services -> platform)이라는 걸 양쪽에서 확인한다.

    금지 방향만 검사하면 "아무도 플랫폼을 안 쓰는" 상태에서도 통과해버려
    규칙이 유명무실해진다. 그래서 실제로 쓰고 있다는 것도 같이 고정한다."""
    uses_platform = [
        _label(f) for f in _py_files("services")
        if any(m.startswith("telemetryd.platform") for m in _imports_of(f))
    ]
    assert uses_platform, "서비스 중 아무도 플랫폼을 쓰지 않는다 — 계층이 비어 있음"
    assert not _violations(_py_files("platform"), "telemetryd.services")


def test_guards_actually_detect_violations(tmp_path):
    """가드 자체가 동작하는지 확인한다 — 위반을 절대 못 잡는 검사는
    통과해도 아무 의미가 없다(빈 디렉터리를 훑고 있었다든지)."""
    offender = tmp_path / "bad.py"
    offender.write_text("from telemetryd.services.perf import EbpfPerfService\n")
    assert _violations([offender], "telemetryd.services") == ["bad.py -> telemetryd.services.perf"]

    clean = tmp_path / "good.py"
    clean.write_text("from telemetryd.platform.ebpf import EbpfLogSource\n")
    assert _violations([clean], "telemetryd.services") == []
