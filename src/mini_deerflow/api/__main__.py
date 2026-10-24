"""Run the single-worker local API with ``python -m mini_deerflow.api``."""

import uvicorn


def main() -> None:
    uvicorn.run(
        "mini_deerflow.api:create_app", factory=True, host="127.0.0.1", port=8000
    )


if __name__ == "__main__":
    main()
