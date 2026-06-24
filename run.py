"""
Entry point.

  uv run python run.py          # dev server with hot-reload
  uv run uvicorn app.main:app   # bare uvicorn (no reload)
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        reload_dirs=["app"],
    )
