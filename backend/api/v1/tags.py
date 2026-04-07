from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.api.deps import get_db, get_current_user
from backend.db.models import User, Tag
from backend.domain.errors import TagNotFound, TagAlreadyExists
from backend.schemas.common import TagCreate, TagUpdate, TagOut
import backend.services.wallet_service as svc

router = APIRouter(prefix="/tags", tags=["tags"])

SYSTEM_TAGS = {"Owner"}


def _guard_system(db: Session, tag_id: int) -> None:
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if tag and tag.name in SYSTEM_TAGS:
        raise HTTPException(status_code=400, detail=f"'{tag.name}' is a system tag and cannot be modified")


@router.get("", response_model=list[TagOut])
def list_tags(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return svc.list_tags(db)


@router.post("", response_model=TagOut, status_code=201)
def create_tag(body: TagCreate, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    if body.name in SYSTEM_TAGS:
        raise HTTPException(status_code=400, detail=f"'{body.name}' is a reserved system tag name")
    try:
        return svc.create_tag(db, body)
    except TagAlreadyExists as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.put("/{tag_id}", response_model=TagOut)
def update_tag(tag_id: int, body: TagUpdate, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    _guard_system(db, tag_id)
    try:
        return svc.update_tag(db, tag_id, body)
    except TagNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TagAlreadyExists as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.delete("/{tag_id}", status_code=204)
def delete_tag(tag_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    _guard_system(db, tag_id)
    try:
        svc.delete_tag(db, tag_id)
    except TagNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
