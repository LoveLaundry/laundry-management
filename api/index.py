from src.main import app

# Vercel / Now Python serverless entry point
# All API routes are served under /api as handled by FastAPI in src/main.py
# The vercel.json rewrites route all traffic to this module.