import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Query as QueryRow
from api.db.models import QueryFeedback, User
from api.db.session import get_db
from api.errors import ApiError
from api.services.auth import get_current_user

router = APIRouter(prefix="/api/v1/queries", tags=["feedback"])


class FeedbackRequest(BaseModel):
    rating: Literal[-1, 1]
    comment: str | None = None


class FeedbackResponse(BaseModel):
    id: uuid.UUID


@router.post("/{query_id}/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    query_id: uuid.UUID,
    body: FeedbackRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FeedbackResponse:
    query = await db.get(QueryRow, query_id)
    if query is None or query.user_id != current_user.id:
        raise ApiError(404, "query_not_found", "Запрос не найден")

    feedback = QueryFeedback(query_id=query_id, rating=body.rating, comment=body.comment)
    db.add(feedback)
    await db.commit()
    await db.refresh(feedback)

    return FeedbackResponse(id=feedback.id)
