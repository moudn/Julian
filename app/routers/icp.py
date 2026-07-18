from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_org
from app.database import get_db
from app.models import ICPRule, Organization
from app.routers.billing import require_active_subscription
from app.schemas import ICPRuleIn, ICPRuleOut

router = APIRouter(prefix="/icp/rules", tags=["icp"],
                   dependencies=[Depends(require_active_subscription)])


def _get_rule(db: Session, rule_id: int, org: Organization) -> ICPRule:
    record = db.get(ICPRule, rule_id)
    if record is None or record.org_id != org.id:
        raise HTTPException(status_code=404, detail=f"Rule {rule_id} not found")
    return record


@router.post("", response_model=ICPRuleOut, status_code=201)
def create_rule(
    rule: ICPRuleIn,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    record = ICPRule(**rule.model_dump(), org_id=org.id)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("", response_model=list[ICPRuleOut])
def list_rules(
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    return db.scalars(
        select(ICPRule).where(ICPRule.org_id == org.id).order_by(ICPRule.id)
    ).all()


@router.put("/{rule_id}", response_model=ICPRuleOut)
def update_rule(
    rule_id: int,
    rule: ICPRuleIn,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    record = _get_rule(db, rule_id, org)
    for key, value in rule.model_dump().items():
        setattr(record, key, value)
    db.commit()
    db.refresh(record)
    return record


@router.delete("/{rule_id}", status_code=204)
def delete_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    org: Organization = Depends(get_current_org),
):
    db.delete(_get_rule(db, rule_id, org))
    db.commit()
