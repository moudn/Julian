"""One full autopilot pass across every org: triage new replies, then send
due sequence steps. Used by both the in-process background loop
(app/main.py) and the cron-triggered endpoint (app/routers/internal.py) —
kept here rather than in main.py so neither has to import the other.
"""

from app.database import SessionLocal
from app.services.replies import run_reply_cycle_all_orgs
from app.services.sending import run_send_cycle_all_orgs


def run_agent_cycle() -> dict:
    db = SessionLocal()
    try:
        replies_result = run_reply_cycle_all_orgs(db)
        send_result = run_send_cycle_all_orgs(db)
    finally:
        db.close()
    return {"replies": replies_result, "send": send_result}
