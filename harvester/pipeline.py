"""单视频 / 合集 / 多平台 URL 处理流水线。"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .bili.client import BiliClient, BiliError
from .bili.collection import collect_target
from .bili.subtitle import (
    parse_subtitle_payload,
    pick_track,
    provider_label,
    rank_tracks,
    segments_text_hash,
)
from .bili.urls import ParsedTarget, parse_target
from .constants import ErrorCode
from .extractor import ExtractOutcome, MemeExtractor
from .models import Segment, Track, VideoInfo
from .sources.classify import classify_url
from .sources.media import MediaResolver
from .sources.webpage import WebPageResolver
from .storage import Storage
from .text import segments_to_plain, split_chunks

ProgressFn = Callable[[str, int], Awaitable[None]]

#: 单条字幕少于这个字数时，认为可能只是片头/广告占位，再看下一轨。
_MIN_SUBTITLE_CHARS = 200


def _text_length(segments: list[Segment]) -> int:
    return sum(len(segment.text) for segment in segments)


def _split_video_key(key: str) -> tuple[str, int]:
    """把 "BV..." / "av123" 统一成 (bvid, aid) 供 view 接口使用。"""
    key = (key or "").strip()
    if key.lower().startswith("av") and key[2:].isdigit():
        return "", int(key[2:])
    return key, 0


@dataclass(slots=True)
class VideoResult:
    bvid: str = ""
    video_key: str = ""
    title: str = ""
    platform: str = ""
    url: str = ""
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
            "platform": self.platform,
            "url": self.url,
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
        media_resolver: MediaResolver | None = None,
        web_resolver: WebPageResolver | None = None,
        tmp_root: Path | str | None = None,
    ) -> None:
        self.client = client
        self.storage = storage
        self.extractor = extractor
        self._settings = settings_getter
        self._logger = logger
        self._on_video_done = on_video_done
        self.media = media_resolver
        self.web = web_resolver
        self.tmp_root = Path(tmp_root) if tmp_root else None

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
        result = VideoResult(bvid=bvid, platform="bilibili")
        await self._progress(progress, "resolving", 5)

        bvid, aid = _split_video_key(bvid)
        info = await self.client.get_video_info(bvid=bvid, aid=aid)
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
                # player 接口偶发返回空轨道（登录态抖动/风控前兆），稍等重试一次。
                await asyncio.sleep(1.2)
                tracks, endpoint = await self.client.get_tracks(
                    bvid=info.bvid, cid=info.cid, aid=info.aid
                )
            if self._logger is not None:
                self._logger.debug(
                    "tracks %s: %s via %s",
                    info.bvid,
                    [(t.lan, t.ai_status, bool(t.url)) for t in tracks],
                    endpoint,
                )
            track = pick_track(tracks)
            resolved_ai = False
            if track is None:
                # 有些视频的轨道列表为空，但 AI 字幕搜索接口仍能给出地址。
                ai_url = (
                    await self.client.resolve_ai_subtitle_url(aid=info.aid, cid=info.cid)
                    if info.aid
                    else ""
                )
                if ai_url:
                    track = Track(lan="ai-zh", url=ai_url, ai_status=2, is_ai=True)
                    endpoint = "ai_stat"
                    resolved_ai = True
                else:
                    raise BiliError(
                        ErrorCode.NO_SUBTITLE,
                        "没有取到字幕轨。可能是该视频确实没有字幕，或登录态失效，可重新扫码后再试。",
                    )

            # CC 轨有时是内嵌广告/仅几秒的占位字幕，内容明显偏少时再试下一轨。
            candidates = rank_tracks(tracks) if tracks else [track]
            best: tuple[Track, list[Segment], bool] | None = None
            for candidate in candidates[:2]:
                if candidate is not track and not candidate.url and not candidate.is_ai:
                    continue
                url = candidate.url
                resolved = False
                if not url and candidate.is_ai and info.aid:
                    url = await self.client.resolve_ai_subtitle_url(aid=info.aid, cid=info.cid)
                    resolved = bool(url)
                if not url:
                    continue
                try:
                    payload = await self.client.fetch_subtitle(url)
                except BiliError as exc:
                    if self._logger is not None:
                        self._logger.debug("track %s 下载失败：%s", candidate.lan, exc)
                    continue
                if self._logger is not None:
                    body = payload.get("body") if isinstance(payload, dict) else None
                    sample = (body or [{}])[0]
                    self._logger.debug(
                        "subtitle %s（%s）entries=%s max_to=%s first=%s",
                        candidate.lan,
                        url[:70],
                        len(body or []),
                        max((float(item.get("to") or 0) for item in body or []), default=0),
                        {k: sample.get(k) for k in ("from", "to", "content")},
                    )
                candidate_segments: list[Segment] = parse_subtitle_payload(payload)
                if not candidate_segments:
                    continue
                if best is None or _text_length(candidate_segments) > _text_length(best[1]):
                    best = (candidate, candidate_segments, resolved)
                if _text_length(best[1]) >= _MIN_SUBTITLE_CHARS:
                    break

            if best is None:
                raise BiliError(ErrorCode.NO_SUBTITLE, "字幕内容为空。")
            track, segments, resolved_ai = best
            result.track_lan = track.lan
            result.segments = len(segments)
            result.provider = provider_label(track, endpoint, resolved_ai=resolved_ai)

            text_hash = segments_text_hash(segments)
            await self.storage.add_transcript(
                video_db_id, track.lan, result.provider, segments, text_hash
            )
            stored = await self.storage.get_transcript(video_db_id)
            if stored:
                result.transcript_id = int(stored.get("id") or 0)

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
            watch_url = f"https://www.bilibili.com/video/{info.bvid}"
            if info.page_no > 1:
                watch_url += f"?p={info.page_no}"
            for candidate in outcome.candidates:
                origin = watch_url
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
        force: bool = False,
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]:
        settings = self._settings()
        summary: dict[str, Any] = {
            "total": 0,
            "done": 0,
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
                result = await self.process_video(video.bvid, force=force)
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
                summary["done"] += 1
            summary["memes_created"] += result.memes_created
            summary["items"].append(result.to_dict())

        return summary


    # ------------------------------------------------------- 多平台 / 网页

    async def process_url(
        self,
        raw: str,
        *,
        page: int = 1,
        force: bool = False,
        progress: ProgressFn | None = None,
    ) -> VideoResult:
        """按链接类型分派：B 站 / 其它媒体 / Wiki / 普通网页。"""
        route = classify_url(raw)
        if route.kind == "bilibili":
            target = parse_target(route.url)
            if target.kind == "short":
                final = await self.client.resolve_short_link(route.url)
                target = parse_target(final)
            if target.kind in ("season", "series"):
                raise BiliError(
                    ErrorCode.BAD_URL, "这是合集/系列链接，请在「合集解析」页提交"
                )
            if target.kind != "video":
                raise BiliError(ErrorCode.BAD_URL, target.reason or "无法识别的 B 站链接")
            bvid = target.bvid or f"av{target.aid}"
            return await self.process_video(
                bvid, page=page or target.page, force=force, progress=progress
            )
        if route.kind == "media":
            return await self.process_media(route.url, force=force, progress=progress)
        if route.kind in ("wiki", "web"):
            return await self.process_page(route.url, force=force, progress=progress)
        raise BiliError(ErrorCode.BAD_URL, route.reason or "无法识别的链接")

    async def process_media(
        self,
        url: str,
        *,
        force: bool = False,
        progress: ProgressFn | None = None,
    ) -> VideoResult:
        if self.media is None:
            raise BiliError(ErrorCode.MEDIA_UNSUPPORTED, "媒体解析未启用")
        settings = self._settings()
        await self._progress(progress, "probing", 10)
        job_dir = self._job_dir()
        try:
            outcome = await self.media.resolve(url, job_dir)
            return await self._analyze(
                platform=outcome.platform,
                source_id=outcome.source_id,
                title=outcome.title,
                owner=outcome.uploader,
                origin=outcome.url,
                url=outcome.url,
                segments=outcome.segments,
                provider=outcome.provider,
                lang=outcome.lang,
                notes=outcome.notes,
                force=force,
                progress=progress,
            )
        finally:
            if not settings.keep_temp_files:
                shutil.rmtree(job_dir, ignore_errors=True)

    async def process_page(
        self,
        url: str,
        *,
        force: bool = False,
        progress: ProgressFn | None = None,
    ) -> VideoResult:
        if self.web is None:
            raise BiliError(ErrorCode.WEB_FETCH_FAILED, "网页抓取未启用")
        await self._progress(progress, "fetching_page", 10)
        doc = await self.web.fetch(url)
        return await self._analyze(
            platform=doc.platform,
            source_id=doc.source_id,
            title=doc.title,
            owner=doc.author,
            origin=url,
            url=url,
            segments=doc.segments,
            provider=doc.provider,
            lang=doc.lang,
            notes=doc.notes,
            force=force,
            progress=progress,
        )

    async def _analyze(
        self,
        *,
        platform: str,
        source_id: str,
        title: str,
        owner: str,
        origin: str,
        url: str,
        segments: list[Segment],
        provider: str,
        lang: str,
        notes: list[str] | None = None,
        force: bool = False,
        progress: ProgressFn | None = None,
    ) -> VideoResult:
        settings = self._settings()
        result = VideoResult(video_key=source_id, title=title, platform=platform, url=url)
        result.notes.extend(notes or [])
        result.segments = len(segments)
        result.provider = provider
        result.track_lan = lang
        await self._progress(progress, "resolving", 5)

        existing = await self.storage.get_video(source_id, platform=platform)
        if existing and existing.get("status") == "done" and not force:
            result.status = "skipped"
            result.notes.append("已解析过，跳过（可用 force 重跑）")
            return result

        info = VideoInfo(
            bvid="",
            title=title,
            owner_name=owner,
            platform=platform,
            video_id_override=source_id,
            url=url,
        )
        video_db_id = await self.storage.upsert_video(info, "processing")
        try:
            if not segments:
                raise BiliError(ErrorCode.NO_SUBTITLE, "没有可用的文本内容。")
            text_hash = segments_text_hash(segments)
            await self.storage.add_transcript(
                video_db_id, lang or provider, provider, segments, text_hash
            )
            stored = await self.storage.get_transcript(video_db_id)
            if stored:
                result.transcript_id = int(stored.get("id") or 0)

            await self._progress(progress, "extracting", 45)
            chunks = split_chunks(
                segments,
                chunk_chars=settings.chunk_chars,
                max_chunks=settings.max_chunks,
            )
            result.chunks = len(chunks)
            outcome: ExtractOutcome = await self.extractor.extract(
                title,
                owner,
                chunks,
                plain_text=segments_to_plain(segments),
            )
            if outcome.used_fallback:
                result.notes.append("未命中梗关键词，已用前两段内容兜底抽取")
            if outcome.failed_chunks:
                result.notes.append(f"{outcome.failed_chunks} 个内容块抽取失败")

            await self._progress(progress, "storing", 80)
            result.memes_total = len(outcome.candidates)
            default_status = "published" if not settings.review_required else "draft"
            for candidate in outcome.candidates:
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
                            "source": origin,
                        },
                        reason=upsert.conflict,
                        dedupe_key=f"conflict:{upsert.meme_id}:{text_hash[:8]}",
                    )
                    if review_id:
                        result.reviews += 1

            await self.storage.set_video_status(source_id, "done", platform=platform)
            await self.storage.add_log(
                "info",
                "source_done",
                f"{source_id}（{platform}）抽取 {result.memes_total} 条，新增 {result.memes_created} 条",
            )
            result.status = "done"
        except BiliError as exc:
            result.status = "failed"
            result.error = exc.code.value
            await self.storage.set_video_status(
                source_id, "failed", f"{exc.code.value}: {exc}", platform=platform
            )
            await self.storage.add_log("error", "source_failed", f"{source_id}: {exc.code.value} {exc}")
            raise
        except Exception as exc:  # noqa: BLE001
            result.status = "failed"
            result.error = ErrorCode.INTERNAL.value
            await self.storage.set_video_status(
                source_id, "failed", str(exc)[:300], platform=platform
            )
            await self.storage.add_log("error", "source_error", f"{source_id}: {exc}")
            raise

        await self._progress(progress, "done", 100)
        if self._on_video_done is not None:
            try:
                await self._on_video_done(result)
            except Exception:
                pass
        return result

    def _job_dir(self) -> Path:
        root = self.tmp_root or (Path(self.storage.db_path).parent / "tmp")
        job = Path(root) / f"job-{uuid.uuid4().hex[:8]}"
        job.mkdir(parents=True, exist_ok=True)
        return job


def build_extractor(complete_fn, settings) -> MemeExtractor:
    return MemeExtractor(complete_fn, max_memes=8, max_chunks=settings.max_chunks)


def default_db_path(data_dir: Path | str) -> Path:
    from .constants import DB_FILENAME

    return Path(data_dir) / DB_FILENAME
