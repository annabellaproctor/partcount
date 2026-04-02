import hashlib
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AIContextCache, AISource, Box, Component, ComponentType, Footprint, Kit, KitComponent, Manufacturer, Project

AI_SOURCE_DIR = Path(os.getenv("AI_SOURCE_DIR", "/tmp/lab-inventory-tracker/ai-sources"))
AI_SOURCE_DIR.mkdir(parents=True, exist_ok=True)

_STOPWORDS = {
    "the", "and", "or", "for", "with", "that", "this", "from", "into", "your", "you",
    "are", "have", "has", "was", "were", "will", "would", "could", "should", "need",
    "current", "data", "please", "show", "find", "make", "create", "update", "fix",
}


def _shorten(text: str, limit: int = 1800) -> str:
    blob = (text or "").strip()
    if len(blob) <= limit:
        return blob
    return blob[: limit - 32].rstrip() + "... [trimmed]"


def _source_hash(*parts: str) -> str:
    payload = "\n".join((p or "").strip() for p in parts if p)
    return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()


def _summarize_text(text: str) -> str:
    blob = re.sub(r"\s+", " ", (text or "").strip())
    if not blob:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", blob)
    chosen = []
    total = 0
    for sentence in sentences:
        if not sentence:
            continue
        chosen.append(sentence)
        total += len(sentence)
        if total >= 420:
            break
    summary = " ".join(chosen).strip()
    return _shorten(summary or blob, 520)


def extract_text_from_upload(filename: str | None, mime_type: str | None, data: bytes) -> str:
    lower_name = (filename or "").lower()
    lower_mime = (mime_type or "").lower()
    if "pdf" in lower_mime or lower_name.endswith(".pdf"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            pages = []
            for page in reader.pages[:24]:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    continue
            return "\n".join(pages).strip()
        except Exception:
            return ""

    if any(lower_name.endswith(ext) for ext in (".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".html", ".htm")) or "text/" in lower_mime:
        for encoding in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return data.decode(encoding)
            except Exception:
                continue
    return ""


async def upsert_ai_source(
    db: AsyncSession,
    *,
    source_kind: str,
    title: str,
    raw_text: str = "",
    extracted_text: str = "",
    mime_type: str | None = None,
    storage_path: str | None = None,
    metadata: dict | None = None,
) -> dict:
    title = (title or "Untitled source").strip() or "Untitled source"
    raw_text = raw_text or ""
    extracted_text = extracted_text or raw_text
    meta = metadata if isinstance(metadata, dict) else {}
    source_hash = _source_hash(source_kind, title, mime_type or "", raw_text, extracted_text, json.dumps(meta, sort_keys=True, ensure_ascii=False))
    summary = _summarize_text(extracted_text or raw_text)

    result = await db.execute(select(AISource).where(AISource.source_hash == source_hash))
    row = result.scalar_one_or_none()
    if row:
        row.source_kind = source_kind
        row.title = title
        row.mime_type = mime_type
        row.storage_path = storage_path
        row.raw_text = raw_text
        row.extracted_text = extracted_text
        row.summary = summary
        row.metadata_json = meta
        row.updated_at = datetime.utcnow()
        await db.flush()
        return serialize_ai_source(row)

    source = AISource(
        source_kind=source_kind,
        title=title,
        mime_type=mime_type,
        source_hash=source_hash,
        storage_path=storage_path,
        raw_text=raw_text,
        extracted_text=extracted_text,
        summary=summary,
        metadata_json=meta,
    )
    db.add(source)
    await db.flush()
    return serialize_ai_source(source)


def serialize_ai_source(source: AISource) -> dict:
    return {
        "id": source.id,
        "source_kind": source.source_kind,
        "title": source.title,
        "mime_type": source.mime_type,
        "storage_path": source.storage_path,
        "summary": source.summary or "",
        "metadata": source.metadata_json or {},
        "created_at": source.created_at.isoformat() if source.created_at else None,
        "updated_at": source.updated_at.isoformat() if source.updated_at else None,
    }


async def get_ai_sources(db: AsyncSession, source_ids: list[str] | None = None, limit: int = 8) -> list[dict]:
    if source_ids:
        result = await db.execute(select(AISource).where(AISource.id.in_(source_ids[:12])).order_by(AISource.created_at.desc()))
    else:
        result = await db.execute(select(AISource).order_by(AISource.created_at.desc()).limit(limit))
    return [serialize_ai_source(row) for row in result.scalars().all()]


async def get_context_cache(db: AsyncSession, cache_key: str) -> dict | None:
    result = await db.execute(select(AIContextCache).where(AIContextCache.cache_key == cache_key))
    row = result.scalar_one_or_none()
    if not row:
        return None
    row.hits = (row.hits or 0) + 1
    row.updated_at = datetime.utcnow()
    await db.flush()
    try:
        return json.loads(row.context_json)
    except Exception:
        return {"summary": row.summary or "", "raw": row.context_json}


async def set_context_cache(db: AsyncSession, cache_key: str, scope: str, context: dict, summary: str = "") -> dict:
    payload = json.dumps(context, ensure_ascii=False)
    result = await db.execute(select(AIContextCache).where(AIContextCache.cache_key == cache_key))
    row = result.scalar_one_or_none()
    if row:
        row.scope = scope
        row.context_json = payload
        row.summary = summary
        row.hits = (row.hits or 0) + 1
        row.updated_at = datetime.utcnow()
        await db.flush()
        return {"cache_key": cache_key, "summary": summary, "context": context, "hits": row.hits}

    entry = AIContextCache(cache_key=cache_key, scope=scope, context_json=payload, summary=summary, hits=1)
    db.add(entry)
    await db.flush()
    return {"cache_key": cache_key, "summary": summary, "context": context, "hits": 1}


def _search_terms(message: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9_\-]{3,}", (message or "").lower())
    out = []
    seen = set()
    for token in tokens:
        if token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out[:5]


async def build_workspace_snapshot(db: AsyncSession, message: str = "", page_context: dict | None = None, source_ids: list[str] | None = None) -> dict:
    page_context = page_context or {}
    search_terms = _search_terms(message)

    counts = {}
    for label, model in [
        ("components", Component),
        ("kits", Kit),
        ("projects", Project),
        ("boxes", Box),
        ("manufacturers", Manufacturer),
        ("types", ComponentType),
    ]:
        try:
            counts[label] = int((await db.execute(select(func.count()).select_from(model))).scalar() or 0)
        except Exception:
            counts[label] = 0

    query_hits = []
    if search_terms:
        for term in search_terms:
            like = f"%{term}%"
            comp_rows = (
                await db.execute(
                    select(Component.barcode_id, Component.name, Component.value, Component.package, Component.mpn)
                    .where(
                        (Component.name.ilike(like))
                        | (Component.value.ilike(like))
                        | (Component.package.ilike(like))
                        | (Component.description.ilike(like))
                        | (Component.mpn.ilike(like))
                    )
                    .limit(4)
                )
            ).all()
            for row in comp_rows:
                query_hits.append({"kind": "component", "term": term, "label": row.name, "barcode_id": row.barcode_id, "value": row.value, "package": row.package, "mpn": row.mpn})

            kit_rows = (
                await db.execute(
                    select(Kit.barcode_id, Kit.name, Kit.description)
                    .where((Kit.name.ilike(like)) | (Kit.description.ilike(like)) | (Kit.barcode_id.ilike(like)))
                    .limit(3)
                )
            ).all()
            for row in kit_rows:
                query_hits.append({"kind": "kit", "term": term, "label": row.name, "barcode_id": row.barcode_id, "description": row.description})

    duplicates = []
    try:
        dup_query = text(
            "SELECT barcode_id, COUNT(*) AS dup_count FROM components GROUP BY barcode_id HAVING COUNT(*) > 1 ORDER BY dup_count DESC, barcode_id ASC LIMIT 10"
        )
        dup_rows = (await db.execute(dup_query)).all()
        duplicates = [{"barcode_id": row.barcode_id, "count": int(getattr(row, 'dup_count', 0) or 0)} for row in dup_rows]
    except Exception:
        duplicates = []

    missing = {}
    try:
        missing_rows = (
            await db.execute(
                text(
                    "SELECT "
                    "COUNT(*) FILTER (WHERE mpn IS NULL OR mpn = '') AS missing_mpn, "
                    "COUNT(*) FILTER (WHERE datasheet_url IS NULL OR datasheet_url = '') AS missing_datasheet, "
                    "COUNT(*) FILTER (WHERE manufacturer_id IS NULL) AS missing_manufacturer, "
                    "COUNT(*) FILTER (WHERE type_path IS NULL OR type_path = '') AS missing_type_path "
                    "FROM components"
                )
            )
        ).first()
        if missing_rows:
            missing_map = getattr(missing_rows, "_mapping", {})
            missing = {
                "missing_mpn": int(missing_map.get("missing_mpn", 0) or 0),
                "missing_datasheet": int(missing_map.get("missing_datasheet", 0) or 0),
                "missing_manufacturer": int(missing_map.get("missing_manufacturer", 0) or 0),
                "missing_type_path": int(missing_map.get("missing_type_path", 0) or 0),
            }
    except Exception:
        missing = {}

    sources = await get_ai_sources(db, source_ids=source_ids)

    selected = {}
    component_id = page_context.get("component_id")
    kit_id = page_context.get("kit_id")
    project_id = page_context.get("project_id")
    box_id = page_context.get("box_id")

    if component_id:
        row = (await db.execute(select(Component).where(Component.id == component_id))).scalar_one_or_none()
        if row:
            selected["component"] = {
                "id": row.id,
                "barcode_id": row.barcode_id,
                "name": row.name,
                "value": row.value,
                "unit": row.unit,
                "package": row.package,
                "mpn": row.mpn,
                "manufacturer_id": row.manufacturer_id,
                "type_path": row.type_path,
                "notes": _shorten(row.notes or "", 400),
            }
    if kit_id:
        kit = (await db.execute(select(Kit).where(Kit.id == kit_id))).scalar_one_or_none()
        if kit:
            kit_rows = (await db.execute(select(KitComponent).where(KitComponent.kit_id == kit.id))).scalars().all()
            selected["kit"] = {
                "id": kit.id,
                "barcode_id": kit.barcode_id,
                "name": kit.name,
                "description": _shorten(kit.description or "", 400),
                "component_count": len(kit_rows),
            }
    if project_id:
        project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
        if project:
            selected["project"] = {
                "id": project.id,
                "name": project.name,
                "description": _shorten(project.description or "", 400),
                "status": project.status,
            }
    if box_id:
        box = (await db.execute(select(Box).where(Box.id == box_id))).scalar_one_or_none()
        if box:
            selected["box"] = {
                "id": box.id,
                "label": box.label,
                "model": box.model,
                "cell_count": box.cell_count,
                "location": box.location,
            }

    scope = page_context.get("scope") or "workspace"
    fingerprint = _source_hash(scope, message, json.dumps(page_context, sort_keys=True, ensure_ascii=False), json.dumps(source_ids or [], sort_keys=True), json.dumps(counts, sort_keys=True), json.dumps(duplicates[:6], sort_keys=True), json.dumps(missing, sort_keys=True))

    context = {
        "scope": scope,
        "page_context": page_context,
        "search_terms": search_terms,
        "counts": counts,
        "query_hits": query_hits[:16],
        "duplicates": duplicates,
        "missing": missing,
        "selected": selected,
        "sources": sources,
        "cache_key": fingerprint,
    }
    summary = _shorten(
        f"{counts.get('components', 0)} components, {counts.get('kits', 0)} kits, {counts.get('projects', 0)} projects, {len(query_hits)} query hits, {len(sources)} sources.",
        240,
    )
    cached = await get_context_cache(db, fingerprint)
    if cached:
        return {"cache_key": fingerprint, "summary": cached.get("summary", summary), "context": cached.get("context", cached), "cached": True}

    await set_context_cache(db, fingerprint, scope, context, summary)
    return {"cache_key": fingerprint, "summary": summary, "context": context, "cached": False}
