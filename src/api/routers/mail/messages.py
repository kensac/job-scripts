"""A person's mailbox, message decisions, and conversations."""

from fastapi import APIRouter

from api.routers.mail import conversations, mailbox, message_assignment, message_detail

router = APIRouter()
router.include_router(mailbox.router)
router.include_router(message_assignment.router)
router.include_router(message_detail.router)
router.include_router(conversations.router)
