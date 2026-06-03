"""Background polling worker."""
from __future__ import annotations
import asyncio
import logging
import os

log = logging.getLogger(__name__)


async def poll_one_account(account, session_factory, service) -> int:
    if not account.imap_host or not account.imap_username:
        return 0
    from .imap_client import fetch_new_messages
    try:
        raws = await fetch_new_messages(
            host=account.imap_host, port=account.imap_port,
            username=account.imap_username, password=account.imap_password or "",
            folder=account.imap_folder, use_ssl=account.imap_use_ssl,
        )
    except Exception as e:
        log.warning("IMAP poll for %s failed: %s", account.address, e)
        return 0
    if not raws:
        return 0

    ingested = 0
    async with session_factory() as session:
        try:
            for raw in raws:
                try:
                    await service.ingest_raw(
                        session, raw, account_id=account.id,
                        account_address=account.address,
                        tenant_id=account.tenant_id,
                    )
                    ingested += 1
                except Exception as e:
                    log.warning("ingest failed: %s", e)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    log.info("polled %s: ingested=%d", account.address, ingested)
    return ingested


class EmailPollLoop:
    def __init__(self, default_interval: int = 15):
        self.default_interval = default_interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        log.info("Email poll loop started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        from sqlalchemy import select
        from case_service.db.models import EmailAccountModel
        from case_service.db.session import get_session_factory
        from case_service.mail.service import EmailService

        service = EmailService()
        factory = get_session_factory()

        while not self._stop.is_set():
            try:
                async with factory() as session:
                    q = select(EmailAccountModel).where(
                        EmailAccountModel.is_active.is_(True),
                    )
                    accounts = (await session.execute(q)).scalars().all()
                for account in accounts:
                    if self._stop.is_set():
                        break
                    try:
                        await poll_one_account(account, factory, service)
                    except Exception as e:
                        log.warning("poll error for %s: %s", account.address, e)
            except Exception as e:
                log.warning("poll loop iteration failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(5, self.default_interval))
            except asyncio.TimeoutError:
                pass


_loop: EmailPollLoop | None = None


def get_poll_loop() -> EmailPollLoop:
    global _loop
    if _loop is None:
        interval = int(os.environ.get("HELIX_CASE_EMAIL_POLL_INTERVAL", "15"))
        _loop = EmailPollLoop(default_interval=interval)
    return _loop
