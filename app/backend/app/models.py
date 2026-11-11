from typing import Annotated, Any, Literal
from pydantic import BaseModel, Field, StrictBool


# --- Projects ---
class ProjectCounts(BaseModel):
    conversations: int = 0
    memories: int = 0
    documents: int = 0
    tasks: int = 0


class Project(BaseModel):
    id: str
    name: str
    description: str = ""
    directories: list[str] = []
    status: Literal["active", "archived"] = "active"
    created_at: str
    updated_at: str
    counts: ProjectCounts = Field(default_factory=ProjectCounts)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    directories: list[str] = []


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    directories: list[str] | None = None
    status: Literal["active", "archived"] | None = None


class ProjectMergeRequest(BaseModel):
    source_ids: list[str]
    target_id: str


class ProjectSplitRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    conversation_ids: list[str] = []
    content_ids: list[str] = []
    directories: list[str] = []


# --- Content (Memories & Documents) ---
class ContentSource(BaseModel):
    id: str | None = None
    kind: Literal["conversation", "document"]
    title: str = ""
    agent: str | None = None
    deleted: bool = False


class ContentVersion(BaseModel):
    version: int
    content: str
    title: str
    actor: str = "user"
    created_at: str


class Content(BaseModel):
    id: str
    project_id: str | None = None
    kind: Literal["memory", "document"] = "memory"
    category: Literal["preference", "background", "decision", "task", "lesson", "knowledge"] = "knowledge"
    title: str
    content: str
    tags: list[str] = []
    pinned: bool = False
    scope: Literal["global", "project"] = "project"
    source_count: int = 0
    agent: str | None = None
    status: Literal["active", "historical"] = "active"
    version: int = 1
    created_at: str
    updated_at: str
    sources: list[ContentSource] = []
    related_ids: list[str] = []
    versions: list[ContentVersion] | None = None


class ContentCreate(BaseModel):
    project_id: str | None = None
    kind: Literal["memory", "document"] = "memory"
    category: Literal["preference", "background", "decision", "task", "lesson", "knowledge"] = "knowledge"
    title: str = Field(min_length=1)
    content: str = ""
    tags: list[str] = []
    pinned: bool = False
    scope: Literal["global", "project"] | None = None
    agent: str | None = None
    source_id: str | None = None
    source_kind: Literal["conversation", "document"] | None = None
    quote: str | None = None
    related_ids: list[str] = []
    supersedes_id: str | None = None


class ContentUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    category: Literal["preference", "background", "decision", "task", "lesson", "knowledge"] | None = None
    tags: list[str] | None = None
    pinned: bool | None = None
    scope: Literal["global", "project"] | None = None
    agent: str | None = None
    status: Literal["active", "historical"] | None = None
    project_id: str | None = None
    expected_version: int = Field(ge=1)


class ContentPinRequest(BaseModel):
    pinned: bool = True
    expected_version: int | None = Field(default=None, ge=1)


class ContentConsolidateRequest(BaseModel):
    project_id: str | None = None
    scope: Literal["project", "global", "all"] = "project"


class ContentRestoreRequest(BaseModel):
    version: int
    expected_version: int = Field(ge=1)


class ContentBatchRequest(BaseModel):
    ids: list[str]
    action: Literal["delete", "retag", "recategorize"]
    tags: list[str] | None = None
    category: Literal["preference", "background", "decision", "task", "lesson", "knowledge"] | None = None
    expected_versions: dict[str, Annotated[int, Field(ge=1, strict=True)]] | None = None


class ContentMergeRequest(BaseModel):
    ids: list[str]
    title: str = Field(min_length=1)
    content: str
    project_id: str
    expected_versions: dict[str, Annotated[int, Field(ge=1, strict=True)]] | None = None


# --- Conversations ---
class ChatMessage(BaseModel):
    id: str
    role: Literal["user", "assistant", "tool", "system"]
    content: str
    timestamp: str


class Conversation(BaseModel):
    id: str
    project_id: str | None = None
    agent: str
    title: str
    source_path: str | None = None
    started_at: str
    updated_at: str
    messages: list[ChatMessage] = []
    process_summary: str = ""
    processing_status: Literal["pending", "processed", "error"] = "pending"
    processing_error: str | None = None


class ConversationCreate(BaseModel):
    project_id: str | None = None
    agent: str
    title: str
    source_path: str | None = None
    messages: list[ChatMessage] = []
    process_summary: str = ""


class ConversationUpdate(BaseModel):
    title: str | None = None
    project_id: str | None = None


# --- Connections (Hosts) ---
class Connection(BaseModel):
    id: str
    label: str
    discovered: bool = False
    enabled: bool = False
    paused: bool = False
    paths: list[str] = []
    capabilities: dict[str, dict[str, str]] = {}
    discovery: dict[str, Any] = Field(default_factory=dict)
    authorizations: dict[str, Any] = Field(default_factory=dict)
    last_sync: str | None = None
    error: str | None = None
    config_preview: str | None = None


class ConnectionPatch(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None
    paths: list[str] | None = None
    confirmed: StrictBool = False


# --- Conflicts ---
class ProposedContent(BaseModel):
    title: str
    content: str
    category: Literal["preference", "background", "decision", "task", "lesson", "knowledge"] = "knowledge"
    tags: list[str] = []
    quote: str | None = None


class Conflict(BaseModel):
    id: str
    project_id: str | None = None
    existing_id: str | None = None
    proposed: ProposedContent
    reason: str
    status: Literal["pending", "resolved"] = "pending"
    created_at: str
    resolution: str | None = None


class ConflictResolveRequest(BaseModel):
    resolution: Literal["keep", "replace", "both", "merge"]
    expected_version: int | None = None
    merged_title: str | None = None
    merged_content: str | None = None
    merged_category: Literal["preference", "background", "decision", "task", "lesson", "knowledge"] | None = None


# --- Handoffs ---
class HandoffCard(BaseModel):
    goal: str
    progress: str
    decisions: str
    evidence: str
    blockers: str
    next_steps: str


class HandoffReadback(BaseModel):
    goal: str
    progress: str
    constraints: str
    scene: str
    blockers: str
    next: str


class Handoff(BaseModel):
    id: str
    project_id: str | None = None
    source_agent: str
    target_agent: str
    status: Literal["awaiting_confirmation", "confirmed", "cancelled", "stale"] = "awaiting_confirmation"
    stale_reason: str | None = None
    confirmation_review_required: bool = False
    system_text_language: Literal['zh', 'en', 'original'] = 'original'
    card: HandoffCard
    readback: HandoffReadback
    source_ids: list[str] = []
    source_revisions: dict[str, Any] = {}


class HandoffCreateRequest(BaseModel):
    language: Literal['zh', 'en'] = 'zh'
    project_id: str | None = None
    target_agent: str
    task_id: str | None = None
    conversation_id: str | None = None
    query: str | None = None
    source_agent: str | None = None

class HandoffConfirmRequest(BaseModel):
    confirmed: StrictBool


# --- Audit ---
class Audit(BaseModel):
    id: str
    actor: str
    action: str
    target: str
    result: str
    created_at: str


# --- Status & Settings ---
class ServiceStatus(BaseModel):
    running: bool
    autostart: bool
    autostart_installed: bool
    platform: str
    stop_status: str | None = None
    stop_error: str | None = None


class CountsStatus(BaseModel):
    projects: int = 0
    conversations: int = 0
    memories: int = 0
    documents: int = 0
    pending_conflicts: int = 0


class ProcessingStatus(BaseModel):
    pending: int = 0
    errors: int = 0


class UsageStatus(BaseModel):
    tokens: int | None = None
    requests: int | None = None
    note: str = "Host-reported metrics"


class StatusResponse(BaseModel):
    ok: bool = True
    version: str
    service: ServiceStatus
    counts: CountsStatus
    processing: ProcessingStatus
    usage: UsageStatus


class SettingsResponse(BaseModel):
    autostart: bool = True
    autostart_installed: bool = False
    platform: str = "darwin"
    compute_host: Literal["omp"] | None = None
    compute_authorized: bool = False


class SettingsPatch(BaseModel):
    autostart: bool | None = None
    install_launch_agent: bool | None = None
    compute_host: str | None = None
    compute_authorized: bool | None = None
    compute_confirmed: StrictBool | None = None

class ServiceStopRequest(BaseModel):
    confirmed: StrictBool
