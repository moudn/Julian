from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ICPRule
from app.schemas import ICPRuleIn, ICPRuleOut

router = APIRouter(prefix="/icp/rules", tags=["icp"])


@router.post("", response_model=ICPRuleOut, status_code=201)
def create_rule(rule: ICPRuleIn, db: Session = Depends(get_db)):
    record = ICPRule(**rule.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("", response_model=list[ICPRuleOut])
def list_rules(db: Session = Depends(get_db)):
    return db.scalars(select(ICPRule).order_by(ICPRule.id)).all()


@router.put("/{rule_id}", response_model=ICPRuleOut)
def update_rule(rule_id: int, rule: ICPRuleIn, db: Session = Depends(get_db)):
    record = db.get(ICPRule, rule_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Rule {rule_id} not found")
    for key, value in rule.model_dump().items():
        setattr(record, key, value)
    db.commit()
    db.refresh(record)
    return record


@router.delete("/{rule_id}", status_code=204)
def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    record = db.get(ICPRule, rule_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Rule {rule_id} not found")
    db.delete(record)
    db.commit()
