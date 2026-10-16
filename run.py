"""One command: python run.py"""
import uvicorn

from app.config import Settings
from app.main import create_app

if __name__ == "__main__":
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_level="info")
