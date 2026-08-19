// telemetryd 파이썬 라이브러리를 C++ 프로세스에 CPython으로 임베딩해서 호출하는
// 예제 (DESIGN.md §4 — "1차 배포 형태"의 두 번째: pure python library를 C++
// 프로그램에서 쓰는 형태).
//
// pybind11::embed 로 인터프리터를 프로세스 안에 띄우고, telemetryd.ffi 모듈의
// JSON 반환 함수들을 그대로 호출한다. 중첩된 dataclass(DeviceSnapshot 등)를
// pybind11 type caster로 하나하나 매핑하는 대신 JSON 문자열만 주고받아 C++
// 쪽 의존성을 없앴다 — 이 예제는 JSON을 파싱하지 않고 원문을 그대로 출력만
// 한다(실제로 쓰려면 nlohmann/json 등으로 파싱하면 됨).
//
// telemetryd 패키지는 venv에 editable install돼 있으므로, 실행 전에 그
// site-packages를 PYTHONPATH에 넣어줘야 embedded interpreter가 찾는다
// (scripts/build_cpp.sh 가 빌드+실행 스크립트를 같이 만들어준다).
#include <pybind11/embed.h>

#include <iostream>
#include <string>

namespace py = pybind11;

int main(int argc, char** argv) {
    std::string backend = (argc > 1) ? argv[1] : "mock";  // mock: root 불필요(기본), drgn: sudo -E 필요

    py::scoped_interpreter guard{};  // CPython 인터프리터 시작 — 프로세스 생애주기 동안 1회

    try {
        py::module_ ffi = py::module_::import("telemetryd.ffi");

        std::cout << "=== list_devices_json(backend=" << backend << ") ===\n";
        std::cout << ffi.attr("list_devices_json")(backend).cast<std::string>() << "\n\n";

        std::cout << "=== get_device_snapshot_json(\"nvme0\") ===\n";
        std::cout << ffi.attr("get_device_snapshot_json")("nvme0", backend).cast<std::string>() << "\n\n";

        std::cout << "=== get_queue_entries_json(\"nvme0\", qid=1, limit=2) ===\n";
        std::cout << ffi.attr("get_queue_entries_json")("nvme0", 1, 2, backend).cast<std::string>() << "\n\n";

        std::cout << "=== get_prp_payload_json(\"nvme0\", qid=1, cid=1) (앞 300자) ===\n";
        std::string prp = ffi.attr("get_prp_payload_json")("nvme0", 1, 1, backend).cast<std::string>();
        std::cout << prp.substr(0, 300) << (prp.size() > 300 ? "...\n\n" : "\n\n");

        std::cout << "=== get_tree_node_json(\"nvme0\", path=[]) ===\n";
        py::list empty_path;
        std::cout << ffi.attr("get_tree_node_json")("nvme0", empty_path, backend).cast<std::string>() << "\n";
    } catch (const py::error_already_set& e) {
        std::cerr << "[Python 예외] " << e.what() << std::endl;
        return 1;
    }

    std::cout << "\n[OK] C++ -> CPython 임베딩 -> telemetryd 파이썬 라이브러리 왕복 성공\n";
    return 0;
}
