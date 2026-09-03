"""Launch the local workbench. No Node.js build or model download at startup."""

import argparse


def main():
    parser = argparse.ArgumentParser(description="PaperCoding local workbench")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()
    from papercoding.presentation.api import create_app
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
