"""单视频 / 合集处理流水线。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .bili.client import BiliClient, BiliError
from .bili.collection import collect_target
from .bili.subtitle import (
    parse_subtitle_payload,
    pick_track,
    provider_label,
    segments_text_hash,
)
from .bili.urls import ParsedTarget
from .constants import ErrorCode
from .extractor import ExtractOutcome, MemeExtractor
from .models import Segment, VideoInfo
from .storage import Storage
from .text import segments_to_plain, split_chunks, truncate

ProgressFn = Callable[[str, int], Awaitable[None]]


@dataclass(slots=True)
class VideoResult:
    bvid: str = ""
    video_key: str = ""
    title: str = ""
    status: str = "done"
    error: str = ""
    provider: str = ""
    track_lan: str = ""
    segments: int = 0
    chunks: int = 0
    memes_total: int = 0
    memes_created: int = 0
    conflicts: int = 0
    reviews: int = 0
    transcript_id: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bvid": self.bvid,
            "video_key": self.video_key,
            "title": self.title,
            "status": self.status,
            "error": self.error,
            "provider": self.provider,
            "track_lan": self.track_lan,
            "segments": self.segments,
            "chunks": self.chunks,
            "memes_total": self.memes_total,
            "memes_created": self.memes_created,
            "conflicts": self.conflicts,
            "reviews": self.reviews,
            "transcript_id": self.transcript_id,
            "notes": list(self.notes),
        }


class VideoPipeline:
    def __init__(
        self,
        client: BiliClient,
        storage: Storage,
        extractor: MemeExtractor,
        settings_getter: Callable[[], Any],
        *,
        logger: Any = None,
        on_video_done: Callable[[VideoResult], Awaitable[None]] | None = None,
    ) -> None:
        self.client = client
        self.storage = storage
        self.extractor = extractor
        self._settings = settings_getter
        self._logger = logger
        self._on_video_done = on_video_done

    async def _progress(self, fn: ProgressFn | None, stage: str, pct: int) -> None:
        if fn is not None:
            try:
                await fn(stage, pct)
            except Exception:
                pass

    async def process_video(
        self,
        bvid: str,
        *,
        page: int = 1,
        force: bool = False,
        progress: ProgressFn | None = None,
    ) -> VideoResult:
        settings = self._settings()
        result = VideoResult(bvid=bvid)
        await self._progress(progress, "resolving", 5)

        info = await self.client.get_video_info(bvid=bvid)
        if page > 1 and info.pages:
            match = next((p for p in info.pages if int(p.get("page") or 0) == page), None)
            if match is not None:
                info.page_no = page
                info.cid = int(match.get("cid") or 0)
        result.video_key = info.video_key
        result.title = info.title

        existing = await self.storage.get_video(info.video_key)
        if existing and existing.get("status") == "done" and not force and existing.get("cid") == info.cid:
            result.status = "skipped"
            result.notes.append("已解析过，跳过（可用 force 重跑）")
            return result

        video_db_id = await self.storage.upsert_video(info, "pending")
        try:
            if not info.cid:
                raise BiliError(ErrorCode.NO_SUBTITLE, "视频没有可用的分 P。")
            await self._progress(progress, "fetching_subtitle", 20)

            tracks, endpoint = await self.client.get_tracks(bvid=info.bvid, cid=info.cid, aid=info.aid)
            if not tracks:
                raise BiliError(ErrorCode.NO_SUBTITLE)
            track = pick_track(tracks)
            if track is None:
                raise BiliError(ErrorCode.NO_SUBTITLE)
            result.track_lan = track.lan

            url = track.url
            resolved_ai = False
            if not url and track.is_ai and info.aid:
                url = await self.client.resolve_ai_subtitle_url(aid=info.aid, cid=info.cid)
                resolved_ai = bool(url)
            if not url:
                raise BiliError(ErrorCode.NO_SUBTITLE, f"轨道 {track.lan} 没有可用字幕地址。")

            payload = await self.client.fetch_subtitle(url)
            segments: list[Segment] = parse_subtitle_payload(payload)
            if not segments:
                raise BiliError(ErrorCode.NO_SUBTITLE, "字幕内容为空。")
            result.segments = len(segments)
            result.provider = provider_label(track, endpoint, resolved_ai=resolved_ai)

            text_hash = segments_text_hash(segments)
            await self.storage.add_transcript(
                video_db_id, track.lan, result.provider, segments, text_hash
            )

            await self._progress(progress, "extracting", 45)
            chunks = split_chunks(
                segments,
                chunk_chars=settings.chunk_chars,
                max_chunks=settings.max_chunks,
            )
            result.chunks = len(chunks)
            outcome: ExtractOutcome = await self.extractor.extract(
                info.title,
                info.owner_name,
                chunks,
                plain_text=segments_to_plain(segments),
            )
            if outcome.used_fallback:
                result.notes.append("未命中梗关键词，已用前两段字幕兜底抽取")
            if outcome.failed_chunks:
                result.notes.append(f"{outcome.failed_chunks} 个字幕块抽取失败")

            await self._progress(progress, "storing", 80)
            result.memes_total = len(outcome.candidates)
            default_status = "published" if not settings.review_required else "draft"
            for candidate in outcome.candidates:
                origin = f"BV {info.bvid}"
                upsert = await self.storage.upsert_meme(
                    candidate,
                    origin=origin,
                    video_db_id=video_db_id,
                    default_status=default_status,
                    min_confidence=settings.memewiki_min_confidence,
                )
                if upsert.created:
                    result.memes_created += 1
                if upsert.conflict:
                    result.conflicts += 1
                    review_id = await self.storage.add_review(
                        "conflict",
                        {
                            "meme_id": upsert.meme_id,
                            "term": candidate.term,
                            "incoming": candidate.to_dict(),
                            "bvid": info.bvid,
                        },
                        reason=upsert.conflict,
                        dedupe_key=f"conflict:{upsert.meme_id}:{text_hash[:8]}",
                    )
                    if review_id:
                        result.reviews += 1

            await self.storage.set_video_status(info.video_key, "done")
            await self.storage.add_log(
                "info", "video_done", f"{info.bvid} 抽取 {result.memes_total} 条，新增 {result.memes_created} 条"
            )
            result.status = "done"
        except BiliError as exc:
            result.status = "failed"
            result.error = exc.code.value
            await self.storage.set_video_status(info.video_key, "failed", f"{exc.code.value}: {exc}")
            await self.storage.add_log("error", "video_failed", f"{info.bvid}: {exc.code.value} {exc}")
            raise
        except Exception as exc:  # 未预期异常也要落状态
            result.status = "failed"
            result.error = ErrorCode.INTERNAL.value
            await self.storage.set_video_status(info.video_key, "failed", str(exc)[:300])
            await self.storage.add_log("error", "video_error", f"{info.bvid}: {exc}")
            raise

        await self._progress(progress, "done", 100)
        if self._on_video_done is not None:
            try:
                await self._on_video_done(result)
            except Exception:
                pass
        return result

    async def process_collection(
        self,
        target: ParsedTarget,
        *,
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]:
        settings = self._settings()
        summary: dict[str, Any] = {
            "total": 0,
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "memes_created": 0,
            "aborted": "",
            "items": [],
        }
        videos, total = await collect_target(
            self.client,
            target,
            page_size=settings.collection_page_size,
            max_items=settings.max_collection_items,
        )
        summary["total"] = total
        if not videos:
            raise BiliError(ErrorCode.COLLECTION_EMPTY)

        for index, video in enumerate(videos, start=1):
            if progress is not None:
                pct = min(99, int(index * 100 / max(1, len(videos))))
                await self._progress(progress, f"collection {index}/{len(videos)}", pct)
            try:
                result = await self.process_video(video.bvid)
            except BiliError as exc:
                if exc.code in (ErrorCode.RISK_CONTROL, ErrorCode.LOGIN_EXPIRED):
                    summary["aborted"] = exc.code.value
                    break
                summary["failed"] += 1
                summary["items"].append({"bvid": video.bvid, "status": "failed", "error": exc.code.value})
                continue
            if result.status == "skipped":
                summary["skipped"] += 1
            else:
                summary["processed"] += 1
            summary["memes_created"] += result.memes_created
            summary["items"].append(result.to_dict())

        return summary


def build_extractor(complete_fn, settings) -> MemeExtractor:
    return MemeExtractor(complete_fn, max_memes=8, max_chunks=settings.max_chunks)


def default_db_path(data_dir: Path | str) -> Path:
    from .constants import DB_FILENAME

    return Path(data_dir) / DB_FILENAME
