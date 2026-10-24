from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from friday.agent.approvals import ApprovalGate
from friday.audit import AuditLog
from friday.capabilities.transactions import TransactionStore
from friday.config import Settings, get_settings
from friday.db import Database
from friday.files import FileStore
from friday.handoff.manager import HandoffManager
from friday.login.diagnosis import Diagnoser
from friday.chat import ChatNotifier, ChatStore
from friday.notify import MultiNotifier, build_notifier
from friday.otp.broker import OtpBroker
from friday.google_oauth import GoogleAuth
from friday.otp.gmail import GmailClient
from friday.prefs import Prefs
from friday.sheets import SheetsClient
from friday.profiles.display import DisplayAllocator
from friday.profiles.manager import ProfileManager, SiteRegistry
from friday.runs import RunStore
from friday.sessions import SessionSupervisor
from friday.sitenotes import SiteNotes
from friday.vault.crypto import Cipher, load_master_key
from friday.vault.injector import SecretInjector
from friday.vault.masking import REDACTOR, Redactor
from friday.vault.store import SecretStore


@dataclass
class AppContext:
    settings: Settings
    db: Database
    cipher: Cipher
    redactor: Redactor
    audit: AuditLog
    vault: SecretStore
    injector: SecretInjector
    sites: SiteRegistry
    displays: DisplayAllocator
    profiles: ProfileManager
    handoffs: HandoffManager
    supervisor: SessionSupervisor
    runs: RunStore
    files: FileStore
    sitenotes: SiteNotes
    gate: ApprovalGate
    notifier: MultiNotifier
    chat: ChatNotifier
    gmail: GmailClient
    google: Any
    sheets: Any
    prefs: Any
    broker: OtpBroker
    diagnoser: Diagnoser
    transactions: TransactionStore
    jobs: Any = field(default=None)
    workspace: Any = field(default=None)
    memory: Any = field(default=None)
    tools: Any = field(default=None)
    scheduler: Any = field(default=None)
    preflight: Any = field(default=None)
    browsercheck: Any = field(default=None)
    onboarding: Any = field(default=None)
    vault_requests: Any = field(default=None)
    tasks: Any = field(default=None)
    usage: Any = field(default=None)
    playbooks: Any = field(default=None)
    spend: Any = field(default=None)
    orders: Any = field(default=None)

    def portal_link(self, fragment: str = "") -> str:
        return f"{self.settings.public_base_url.rstrip('/')}/portal#{fragment}"


def build_context(
    settings: Settings | None = None,
    *,
    master_key: bytes | None = None,
    redactor: Redactor = REDACTOR,
    llm_factory: Any = None,
    chat_llm_factory: Any = None,  # False: no assistant (fixed replies only)
) -> AppContext:
    settings = settings or get_settings()
    settings.check_safe_environment()
    cipher = Cipher(master_key if master_key is not None else load_master_key(settings))
    db = Database(settings.resolved_db_path)
    audit = AuditLog(db, redactor)
    vault = SecretStore(db, cipher, audit, redactor)
    sites = SiteRegistry(db, audit)
    displays = DisplayAllocator(settings)
    profiles = ProfileManager(settings, db, audit, vault, sites, displays)
    handoffs = HandoffManager(db, audit, base_url=settings.public_base_url, ttl_seconds=settings.handoff_ttl_seconds)
    files = FileStore(settings.data_dir.resolve() / "files", db, cipher, audit)
    chat = ChatNotifier(ChatStore(db, redactor))
    notifier = build_notifier(settings, redactor, chat)
    gate = ApprovalGate(db, audit, redactor, ttl_seconds=settings.approval_ttl_seconds)
    google_secret = settings.google_client_secret.get_secret_value() if settings.google_client_secret else None
    gmail = GmailClient(settings.google_client_id, google_secret, vault)
    google = GoogleAuth(settings.google_client_id, google_secret, vault)
    gmail.google = google
    broker = OtpBroker(
        db, audit, vault, redactor, notifier, gmail,
        owner_timeout=settings.otp_timeout_seconds, email_timeout=settings.email_otp_timeout_seconds,
        portal_url=f"{settings.public_base_url.rstrip('/')}/portal",
    )  # fmt: skip
    diagnoser = Diagnoser(settings, db, audit, files, profiles, handoffs, redactor, notifier)
    ctx = AppContext(
        settings=settings, db=db, cipher=cipher, redactor=redactor, audit=audit, vault=vault,
        injector=SecretInjector(vault, audit), sites=sites, displays=displays, profiles=profiles,
        handoffs=handoffs, supervisor=SessionSupervisor(profiles, handoffs), runs=RunStore(db, redactor),
        files=files, sitenotes=SiteNotes(db, redactor), gate=gate, notifier=notifier, chat=chat, gmail=gmail, google=google, sheets=SheetsClient(google), prefs=Prefs(db),
        broker=broker, diagnoser=diagnoser, transactions=TransactionStore(db),
    )  # fmt: skip
    gate.notify = notifier.send
    gate.link = lambda a: ctx.portal_link("approvals")
    from friday.jobs import JobRunner
    from friday.usage import UsageMeter, metered

    ctx.usage = UsageMeter(ctx)
    if llm_factory is None:
        from friday.agent.llm import default_llm

        llm_factory = lambda: default_llm(settings)  # noqa: E731
    # Every model call is metered against the configured spend cap, whatever the caller.
    llm_factory = metered(llm_factory, ctx.usage, "agent")
    ctx.jobs = JobRunner(ctx, llm_factory)
    from types import SimpleNamespace

    from friday.memory import Memory
    from friday.workspace import Drive, Gmail, GoogleAPI

    api = GoogleAPI(google)
    ctx.workspace = SimpleNamespace(api=api, gmail=Gmail(api), drive=Drive(api))
    ctx.memory = Memory(db, redactor)
    from friday.scheduler import Scheduler

    ctx.scheduler = Scheduler(ctx)
    from friday.tasks import TaskLedger

    ctx.tasks = TaskLedger(ctx)
    from friday.playbooks import PlaybookStore

    ctx.playbooks = PlaybookStore(ctx)
    from friday.spend import SpendGate

    ctx.spend = SpendGate(ctx)
    from friday.orders import OrderStore

    ctx.orders = OrderStore(ctx)
    from friday.preflight import Preflight

    ctx.preflight = Preflight(ctx)
    from friday.browsercheck import BrowserCheck

    ctx.browsercheck = BrowserCheck(ctx)
    from friday.onboarding import Onboarding
    from friday.vault_requests import VaultRequests

    ctx.onboarding = Onboarding(ctx)
    ctx.vault_requests = VaultRequests(ctx)
    if chat_llm_factory is None:
        from friday.agent.llm import AnthropicLLM

        chat_llm_factory = lambda: AnthropicLLM(  # noqa: E731
            settings.model, effort=settings.chat_effort, fallbacks=settings.model_fallbacks
        )
    chat_llm_factory = metered(chat_llm_factory, ctx.usage, "chat")
    from friday.tools import build_registry

    ctx.tools = build_registry(ctx, chat_llm_factory or None)
    if chat_llm_factory is not False:
        from friday.assistant import ChatAssistant

        chat.assistant = ChatAssistant(ctx, chat_llm_factory)
    return ctx
