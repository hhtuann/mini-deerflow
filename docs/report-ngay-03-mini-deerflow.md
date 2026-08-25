# Báo cáo Ngày 3 — Mini DeerFlow

**Hoàn tất:** 25/08/2026  
**Repository:** `https://github.com/hhtuann/mini-deerflow`  
**Trạng thái cuối:** `main` đồng bộ với `origin/main`

## Mục tiêu trong ngày

- Tách Mini DeerFlow thành repository độc lập với DeerFlow.
- Xây dựng planner có structured output và CLI có thể gọi LLM thật.
- Phân tích request lifecycle của DeerFlow để rút ra kiến trúc cho Mini DeerFlow.
- Ghi lại kết quả phân tích thành tài liệu kỹ thuật có thể kiểm chứng.

## Công việc đã hoàn thành

### 1. Tách repository Mini DeerFlow

- Chuyển dự án từ repository DeerFlow tham khảo sang:
  `D:\ViettelDigitalTalent\VAI\projects\mini-deerflow`.
- Khởi tạo Git repository riêng và kết nối GitHub:
  `https://github.com/hhtuann/mini-deerflow`.
- Giữ `.env` ngoài Git; sao chép source có kiểm chứng; không sao chép `.venv` và cache.
- Xóa bản Mini DeerFlow cũ khỏi repository tham khảo sau khi xác minh an toàn.

### 2. Xây dựng structured research planner

- Tạo `Settings` bằng Pydantic Settings để quản lý model, base URL, API key và temperature.
- Tạo `Plan` và `PlanStep` với validation:
  - cấm trường thừa;
  - giới hạn 3–7 bước;
  - bắt buộc `step_number` liên tiếp;
  - object immutable sau khi tạo.
- Tạo model factory cho `ChatOpenAI`.
- Tạo planner dùng structured output để ép LLM trả về đúng schema.
- Viết unit tests cho configuration, schema, model factory và planner.

### 3. Xây dựng CLI

- Tạo lệnh:

  ```text
  mini-deerflow "<research goal>"
  ```

- CLI thực hiện chuỗi:

  ```text
  goal → settings → model → structured planner → JSON
  ```

- Smoke test với GLM 5.3 tạo thành công kế hoạch nghiên cứu sáu bước.
- Xác nhận planner hiện tại chưa phải agent hoàn chỉnh vì mới có một model invocation,
  chưa có tool execution và vòng lặp observe/replan.

### 4. Trace request lifecycle của DeerFlow

Đã xác minh call chain production:

```text
POST /runs/stream
    → stream_run
    → start_run
    → RunManager.create_or_reject
    → asyncio.create_task(worker)
    → run_agent
    → RunManager.try_start
    → make_lead_agent
    → _make_lead_agent
    → create_agent
    → agent.astream
    → bridge.publish
    → sse_consumer
    → frontend
```

Các kết luận quan trọng:

- Gateway truyền `agent_factory` thay vì tạo sẵn agent.
- Worker chỉ tạo agent sau khi run vượt qua admission bằng `try_start()`.
- `RunManager` sở hữu vòng đời kỹ thuật của run, không chọn tool.
- LLM đưa ra quyết định ngữ nghĩa trong giới hạn do tools, middleware và runtime áp đặt.
- Production lead-agent path không đi qua `create_deerflow_agent()`; factory này là public API độc lập.
- Production path thực tế là `make_lead_agent → _make_lead_agent → create_agent`.

### 5. Tài liệu và làm sạch package

- Thêm `docs/deerflow-request-lifecycle.md` gồm sơ đồ, phân tầng trách nhiệm và ánh xạ sang Mini DeerFlow.
- Xóa entry point scaffold không sử dụng trong `mini_deerflow/__init__.py`.
- Giữ CLI duy nhất tại `mini_deerflow.cli:main`.

## Kiểm thử cuối ngày

- `ruff check`: đạt.
- `ruff format --check`: 13 file đúng định dạng.
- `pytest`: 14 test đạt.
- CLI help: hoạt động.
- Local commit và remote commit: trùng khớp.
- Working tree: sạch.

## Các commit chính

```text
3192b39 feat: scaffold structured research planner
5ec0557 feat: add structured planning CLI
a8c9b0b docs: document DeerFlow request lifecycle
b399b56 refactor: remove unused package entry point
```

## Kiến thức đã học

1. Structured output giúp giảm output sai cấu trúc, nhưng không tự biến một LLM call thành agent.
2. Một agent cần vòng lặp quyết định–hành động–quan sát và điều kiện dừng rõ ràng.
3. Dependency injection bằng `agent_factory` giúp trì hoãn khởi tạo và tách gateway khỏi agent implementation.
4. Run lifecycle và semantic decision là hai trách nhiệm khác nhau.
5. LLM chỉ nên quyết định trong một runtime bị giới hạn; không được tự kiểm soát admission, persistence hay cancellation.
6. Trace call graph từ source code đáng tin cậy hơn suy đoán từ tên thư mục hoặc tên class.

## Trạng thái Mini DeerFlow hiện tại

Đã có:

- cấu hình model;
- typed schema;
- structured planner;
- CLI;
- unit tests;
- kết nối LLM thật;
- tài liệu kiến trúc tham khảo.

Chưa có:

- agent state tổng thể;
- tool registry và tool execution;
- vòng lặp plan–act–observe;
- replanning;
- persistence/checkpoint;
- streaming và run manager.

## Ngày 4 sẽ làm gì

Ngày 4 sẽ chuyển planner đơn lẻ thành nền móng của một agent runtime:

1. Thiết kế `AgentState` lưu goal, plan, bước hiện tại, observations và trạng thái kết thúc.
2. Định nghĩa state transition hợp lệ để agent không nhảy cóc bước.
3. Viết executor interface độc lập với LLM và tool cụ thể.
4. Viết tests cho state machine trước khi nối tool thật.
5. Giữ vòng lặp đầu tiên đồng bộ và tối giản; persistence và HTTP streaming sẽ được thêm sau.

## Kết luận

Ngày 3 hoàn thành đúng mục tiêu: Mini DeerFlow đã là một repository độc lập với planner chạy được, đồng thời đã có bản đồ kiến trúc DeerFlow đủ chính xác để hướng dẫn việc xây dựng agent loop tiếp theo. Sản phẩm hiện tại là một thành phần planner đã kiểm thử, chưa phải Deep Agent hoàn chỉnh.
