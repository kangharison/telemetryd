"""drgn.Object 포인터 트리를 lazy하게 1-depth씩 펼치는 범용 워커.

struct nvme_dev 같은 커널 구조체는 필드를 따라가면 pci_dev/device/kobject
sysfs 트리까지 무한히 이어지므로 eager 직렬화는 하지 않는다(DESIGN.md §5.5).
클라이언트가 path(필드명/배열인덱스 시퀀스, 예: ["ctrl","pci_dev","bus"])를
주면, 그 노드 하나와 "바로 다음 자식들의 요약"만 돌려준다.

drgn은 `obj.member_(name)`을 포인터-투-구조체 객체에 직접 호출해도 자동으로
한 번 역참조해준다(예: 02_nvme_queues.py의 `ctrl.model_number` — ctrl은
`struct nvme_ctrl *`인데 바로 필드 접근). 배열 인덱싱 `obj[i]`도 포인터/배열
양쪽에서 동일하게 동작한다. 이 두 성질 덕분에 트리 워커는 "포인터니까 먼저
역참조해야 한다"는 특수 케이스를 직접 코드로 두지 않고 그대로 위임할 수
있다 — 다만 자식 목록을 만들 때(포인터가 가리키는 대상의 필드 이름들을
얻어야 함)는 포인터를 명시적으로 한 번 벗겨서 타입을 봐야 한다.

이 모듈은 drgn에 의존한다 — MockBackend는 이 모듈을 쓰지 않는다(§DESIGN 0).
"""
from __future__ import annotations

from typing import List, Tuple

from drgn import FaultError, Object, TypeKind

from telemetryd.models import TreeNode, TreeExpansion

MAX_DEPTH = 10          # 요청사항 6: "최대 10 depth"
MAX_ARRAY_CHILDREN = 64  # 배열/포인터-투-배열을 펼칠 때 상한 (거대 배열 폭발 방지)


def _unwrap_typedef(t):
    """typedef 체인을 실제 kind가 나올 때까지 벗긴다 (예: u16 -> unsigned short)."""
    while t.kind == TypeKind.TYPEDEF:
        t = t.type
    return t


def _kind_of(t) -> str:
    t = _unwrap_typedef(t)
    k = t.kind
    if k == TypeKind.POINTER:
        return "pointer"
    if k in (TypeKind.STRUCT, TypeKind.UNION, TypeKind.CLASS):
        return "struct"
    if k == TypeKind.ARRAY:
        return "array"
    return "scalar"  # INT/BOOL/FLOAT/ENUM 등


def _is_byte_type(t) -> bool:
    t = _unwrap_typedef(t)
    return t.kind in (TypeKind.INT, TypeKind.BOOL) and t.size == 1


def _looks_like_string(t) -> bool:
    """char* 또는 char[] 인지 — 문자열로 렌더링할지 판단."""
    t = _unwrap_typedef(t)
    if t.kind == TypeKind.POINTER:
        return _is_byte_type(t.type)
    if t.kind == TypeKind.ARRAY:
        return _is_byte_type(t.type)
    return False


_TAGGED_KIND_PREFIX = {
    TypeKind.STRUCT: "struct",
    TypeKind.UNION: "union",
    TypeKind.CLASS: "class",
    TypeKind.ENUM: "enum",
}


def _short_type_name(t) -> str:
    """struct/union/enum(및 그 배열)은 drgn의 str(t)가 필드 전체를 나열한 멀티라인
    정의 본문을 통째로 반환한다 — 실제 라이브 커널(qemu-debug 6.1.4)로 검증하다가
    발견: struct nvme_dev 노드의 type_name이 수십 줄짜리 필드 덤프로 나와버림.
    트리 노드에는 태그 이름만 있으면 충분하므로 직접 조립한다. 포인터/스칼라/
    typedef는 str(t)가 이미 간결해서 그대로 쓴다."""
    unwrapped = _unwrap_typedef(t)
    if unwrapped.kind == TypeKind.ARRAY:
        length = unwrapped.length
        elem = _short_type_name(unwrapped.type)
        return f"{elem} [{length}]" if length is not None else f"{elem} []"
    prefix = _TAGGED_KIND_PREFIX.get(unwrapped.kind)
    if prefix:
        return f"{prefix} {unwrapped.tag}" if unwrapped.tag else f"{prefix} <anonymous>"
    return str(t)


def describe(name: str, obj: Object) -> TreeNode:
    """obj 하나를 TreeNode 요약으로 변환한다. 자식은 펼치지 않는다."""
    t = obj.type_
    type_name = _short_type_name(t)
    kind = _kind_of(t)
    address = None
    is_null = False
    value_repr = ""
    expandable = False

    try:
        if kind == "pointer":
            addr_val = int(obj)
            address = addr_val
            is_null = addr_val == 0
            if _looks_like_string(t) and not is_null:
                try:
                    value_repr = obj.string_().decode(errors="replace")
                    kind = "string"
                except FaultError:
                    value_repr = hex(addr_val)
            else:
                value_repr = hex(addr_val)
                expandable = not is_null
        elif kind == "struct":
            try:
                address = int(obj.address_) if obj.address_ is not None else None
            except Exception:
                address = None
            value_repr = f"<{type_name}>"
            expandable = True
        elif kind == "array":
            if _looks_like_string(t):
                try:
                    value_repr = obj.string_().decode(errors="replace")
                    kind = "string"
                except FaultError:
                    value_repr = "<char[]>"
            else:
                length = _unwrap_typedef(t).length
                value_repr = f"<array len={length}>" if length is not None else "<array>"
                expandable = True
                try:
                    address = int(obj.address_) if obj.address_ is not None else None
                except Exception:
                    address = None
        elif _unwrap_typedef(t).kind == TypeKind.ENUM:
            # [한국어] enum은 str(obj)가 "(enum X)ENUMERATOR" 형태로 나온다(실제
            # 커널로 검증 중 nvmset_state에서 확인) — 숫자값(0)보다 "NVMSET_STATE_D"
            # 처럼 이름이 훨씬 읽기 좋아서 괄호 접두어만 잘라내고 쓴다.
            rendered = str(obj)
            close = rendered.find(")")
            value_repr = rendered[close + 1 :] if rendered.startswith("(enum") and close != -1 else rendered
        else:  # scalar
            try:
                value_repr = str(obj.value_())
            except Exception:
                value_repr = str(obj)
    except FaultError as e:
        kind = "unreadable"
        value_repr = f"<읽기 실패(FaultError): {e}>"
        expandable = False

    return TreeNode(
        name=name, type_name=type_name, kind=kind, value_repr=value_repr,
        address=address, is_null=is_null, expandable=expandable,
    )


def _child_objects(obj: Object) -> List[Tuple[str, Object]]:
    """(자식이름, 자식Object) 목록 — pointer는 대상 구조체의 필드로, struct는
    자기 필드로, array는 인덱스(MAX_ARRAY_CHILDREN 상한)로 1단계만 펼친다."""
    t = _unwrap_typedef(obj.type_)
    out: List[Tuple[str, Object]] = []

    if t.kind == TypeKind.POINTER:
        if int(obj) == 0:
            return out
        pointee = _unwrap_typedef(t.type)
        if pointee.kind in (TypeKind.STRUCT, TypeKind.UNION, TypeKind.CLASS):
            for m in pointee.members:
                if m.name is None:
                    continue
                try:
                    out.append((m.name, obj.member_(m.name)))  # drgn이 포인터를 자동 역참조
                except (FaultError, LookupError):
                    continue
        elif pointee.kind != TypeKind.VOID:
            try:
                out.append(("*", obj[0]))
            except FaultError:
                pass
    elif t.kind in (TypeKind.STRUCT, TypeKind.UNION, TypeKind.CLASS):
        for m in t.members:
            if m.name is None:
                continue
            try:
                out.append((m.name, obj.member_(m.name)))
            except (FaultError, LookupError):
                continue
    elif t.kind == TypeKind.ARRAY:
        n = t.length or 0
        for i in range(min(n, MAX_ARRAY_CHILDREN)):
            try:
                out.append((f"[{i}]", obj[i]))
            except (FaultError, LookupError):
                continue
    return out


def resolve_path(root: Object, path: List[str]) -> Object:
    """root에서 path(필드명 또는 "[idx]")를 순서대로 따라가 최종 Object를 낸다."""
    obj = root
    for step in path:
        if step.startswith("[") and step.endswith("]"):
            obj = obj[int(step[1:-1])]
        else:
            obj = obj.member_(step)  # 포인터-투-구조체면 drgn이 알아서 역참조
    return obj


def expand(root: Object, root_name: str, path: List[str]) -> TreeExpansion:
    """DESIGN.md §5.5의 lazy 1-depth expansion 본체. depth(=len(path))가
    MAX_DEPTH를 넘으면 자식을 펼치지 않고 에러로 명시한다."""
    depth = len(path)
    if depth > MAX_DEPTH:
        return TreeExpansion(
            node=TreeNode(name=path[-1], type_name="?", kind="unreadable",
                          value_repr="depth 초과", address=None, is_null=False, expandable=False),
            children=[], depth=depth,
            error=f"최대 depth({MAX_DEPTH})를 초과했습니다 (요청 depth={depth})",
        )
    try:
        obj = resolve_path(root, path)
    except (FaultError, LookupError) as e:
        return TreeExpansion(
            node=TreeNode(name=path[-1] if path else root_name, type_name="?", kind="unreadable",
                          value_repr=str(e), address=None, is_null=False, expandable=False),
            children=[], depth=depth, error=str(e),
        )

    name = path[-1] if path else root_name
    node = describe(name, obj)
    children: List[TreeNode] = []
    if node.expandable and depth < MAX_DEPTH:
        try:
            for cname, cobj in _child_objects(obj):
                children.append(describe(cname, cobj))
        except FaultError:
            pass
    return TreeExpansion(node=node, children=children, depth=depth)
