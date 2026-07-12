import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import init_db
from app.routers import apollo, auth, bookings, icp, integrations, leads

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="AI Sales Agent Core",
    description=(
        "Lead import, ICP scoring, LLM outreach drafting, and a human-approved "
        "meeting scheduling workflow."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(integrations.router)
app.include_router(leads.router)
app.include_router(icp.router)
app.include_router(apollo.router)
app.include_router(bookings.router)


@app.get("/health")
def health():
    return {"status": "ok"}
