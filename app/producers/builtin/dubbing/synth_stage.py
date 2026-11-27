"""配音服务逐段合成阶段（由 DubbingService 组合）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from app.engines.base import CODES_ENGINE_UNAVAILABLE, EngineError
from app.infra.loghub import CHANNEL
from app.producers.base import ProducerError
from app.producers.builtin.dubbing.helpers import (
    _cue_text,
    _engine_name,
    _params_text,
    _role_text,
    _sample_text,
    _size_text,
    role_of,
)

if TYPE_CHECKING:
    from app.usecase.library import LibraryService



class _SynthMixin:
    def start_synth(
        self,
        project_id: str,
        library: LibraryService,
        cue_ids: Sequence[str] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        # 「自动」角色在这一刻重新选一次实际引擎：调用方存的是 auto，换实例不必改配置。
        self._refresh_auto_roles(project_id, library)
        roles = self.store.roles(project_id)
        cues = self._cues_out(project_id, roles)
        stats = self._stats(cues, roles)
        targets = self._synth_targets(project_id, cue_ids)
        if not targets:
            detail = "先转录，或检查角色的处理方式是否为「配音」且已选音色。"
            if cue_ids:
                detail = "选中的段落里没有可合成的：它们可能保留原声，或角色还没有音色。"
            waiting = [
                str(role["name"])
                for role in roles
                if role.get("engine_auto")
                and role.get("voice_id")
                and not role.get("engine")
            ]
            if waiting:
                detail = (
                    f"自动引擎暂无可用实例（{'、'.join(waiting)}）："
                    "先启动一台本地引擎再开始配音。"
                )
            raise ProducerError(f"没有需要合成的段落：{detail}")
        missing = 0 if cue_ids else stats["unvoiced"]
        job = self._spawn(
            project_id,
            "synth",
            len(targets),
            lambda job_id: self._synth(job_id, library, targets, force=force),
        )
        engines = sorted({str(item["_role"]["engine"]) for item in targets})
        voices = sorted(
            {f"{item['_role']['engine']}/{item['_role']['voice_id']}" for item in targets}
        )
        # 谁、用什么声音、哪台引擎、各多少段：开始前一次说清，日志里不必猜。
        groups: dict[tuple[str, str, str], int] = {}
        for item in targets:
            role = item["_role"]
            group = (
                str(role.get("name") or "旁白"),
                str(role.get("voice_name") or role.get("voice_id") or "未选音色"),
                _engine_name(str(role.get("engine") or "")),
            )
            groups[group] = groups.get(group, 0) + 1
        summary = "；".join(f"{a} / {b} / {c} ×{count}" for (a, b, c), count in groups.items())
        note = f" · 跳过 {missing} 段缺音色" if missing else ""
        self.store.update_job(
            job["id"],
            message=f"准备合成 {len(targets)} 段{note}",
        )
        CHANNEL.info(
            f"开始配音：{len(targets)} 段{note} · {summary}"
            f" · 并发 {self.settings.concurrency}{' · 强制重配' if force else ''}",
            source="dubbing",
            project=project["id"],
            job=job["id"],
            engines=engines,
            voices=voices,
            targets=len(targets),
            skipped=missing,
            concurrency=self.settings.concurrency,
            force=force,
        )
        return job


    def _synth_targets(
        self, project_id: str, cue_ids: Sequence[str] | None
    ) -> list[dict[str, Any]]:
        """要合成的段落：配音角色名下、有音色、文本非空；指定了就只做这些。

        保留原声的角色（旁白常是这种）名下的一段都不进这个清单——它们本身
        就是成片里的声音，重念一遍只会把原声盖掉。
        """
        roles = self.store.roles(project_id)
        wanted = set(cue_ids or [])
        out: list[dict[str, Any]] = []
        for cue in self.store.cues(project_id):
            if wanted and cue["id"] not in wanted:
                continue
            if not str(cue["text"] or "").strip():
                continue
            role = role_of(cue, roles)
            if role is None or role["mode"] != "tts" or not (role["engine"] and role["voice_id"]):
                continue
            out.append({**cue, "_role": role})
        return out


    def _identity_for(self, engine_key: str) -> str:
        """本机当前引擎身份；引擎未安装时返回空串。停用不影响身份。"""
        from app.usecase import engines as engines_uc
        from app.usecase.engines import engine_identity

        try:
            # 停用的引擎仍要按它的版本/模型算身份：旧音频不该因为开关一关
            # 就整批变“待重配”，重新启用后又自己好了。
            engine = engines_uc.list_engines().get(engine_key)
        except Exception:  # noqa: BLE001 - 身份读不出来不该挡住详情
            engine = None
        return engine_identity(engine)


    def _audio_key(
        self,
        cue: dict[str, Any],
        role: dict[str, Any],
        *,
        identity: str = "",
        with_sample: bool = True,
    ) -> str:
        """当前口径的合成键：文本 / 引擎身份 / 音色 / 参数 / 本段命中的读音。

        ``identity`` 由调用方按引擎缓存后传入；这里不查注册表，避免每段都
        重新枚举引擎。空的 identity 仍可算键（测试与旧数据）。
        """
        from app.usecase import pronunciation

        key_fields: dict[str, Any] = {
            "v": 2,
            "text": cue["text"],
            "engine": role["engine"],
            "identity": identity,
            "voice": role["voice_id"],
            "params": role["params"],
            "pron": pronunciation.matches(str(cue["text"] or "")),
        }
        if with_sample and role.get("sample_mode"):
            key_fields["sample"] = f"{role['sample_mode']}:{role.get('sample_file') or ''}"
        payload = json.dumps(key_fields, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


    def _legacy_audio_key(
        self, cue: dict[str, Any], role: dict[str, Any], *, with_sample: bool = True
    ) -> str:
        """历史口径：整张读音表版本、没有引擎身份。只用于认旧产物。"""
        from app.usecase import pronunciation

        key_fields: dict[str, Any] = {
            "text": cue["text"],
            "engine": role["engine"],
            "voice": role["voice_id"],
            "params": role["params"],
            "pron": pronunciation.revision(),
        }
        if with_sample and role.get("sample_mode"):
            key_fields["sample"] = f"{role['sample_mode']}:{role.get('sample_file') or ''}"
        payload = json.dumps(key_fields, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


    def _key_matches(
        self,
        cue: dict[str, Any],
        role: dict[str, Any],
        *,
        identity: str | None = None,
    ) -> bool:
        """这条旧音频是不是当前角色参数的产物。

        新键带引擎身份与本段读音命中；旧键只带整张读音表版本。两种都认，
        旧项目不会因为升级判据整批作废；没有样例策略的角色再认一次不带
        样例格的旧键。
        """
        stored = str(cue.get("audio_key") or "")
        if not stored:
            return False
        if identity is None:
            identity = self._identity_for(str(role.get("engine") or ""))
        if stored == self._audio_key(cue, role, identity=identity):
            return True
        if stored == self._legacy_audio_key(cue, role):
            return True
        # 没有样例策略的角色才认不带样例格的旧键：用户一旦明确选了
        # 「按情绪 / 固定样例」，这一段就必须按新策略重配。
        return not role.get("sample_mode") and stored == self._legacy_audio_key(
            cue, role, with_sample=False
        )


    def _synth(
        self,
        job_id: str,
        library: LibraryService,
        targets: list[dict[str, Any]],
        *,
        force: bool = False,
    ) -> str:
        job = self.store.job(job_id)
        assert job is not None
        project_id = job["project_id"]
        self._project(project_id)
        work = self._work(project_id, "tts")
        concurrency = max(1, int(self.settings.concurrency))
        started = time.monotonic()

        async def run() -> tuple[int, int, int, int, int, int, str, dict[str, dict[str, Any]]]:
            semaphore = asyncio.Semaphore(concurrency)
            done = 0
            failed = 0
            blocked = 0
            produced = 0
            reused = 0
            written_bytes = 0
            first_error = ""
            lock = asyncio.Lock()
            # 整台做不了就停这一台：服务没起、引擎被停用、插件已卸载。
            # 后面每一段都只是白撞一次，还会把段落状态刷成满屏失败。
            halted: dict[str, dict[str, Any]] = {}
            identities: dict[str, str] = {}

            async def one(cue: dict[str, Any]) -> None:
                nonlocal done, failed, blocked, produced, reused, written_bytes
                nonlocal first_error
                if self._cancelled(job_id):
                    return
                role = cue["_role"]
                engine_key = str(role["engine"])
                engine_name = _engine_name(engine_key)
                sample_mode = str(role.get("sample_mode") or "")
                sample_file = (
                    str(role.get("sample_file") or "") if sample_mode == "fixed" else ""
                )
                cue_pos = _cue_text(cue)
                role_text = _role_text(role)
                if engine_key in halted:
                    async with lock:
                        blocked += 1
                    return
                if engine_key not in identities:
                    identities[engine_key] = self._identity_for(engine_key)
                identity = identities[engine_key]
                key = self._audio_key(cue, role, identity=identity)
                if not force and cue["audio"] and self._key_matches(
                    cue, role, identity=identity
                ):
                    existing = self.folder(project_id) / cue["audio"]
                    if existing.is_file():
                        # 键没变、文件还在：这一段本来就是好的。上一轮失败留下的
                        # failed 标记要在这里擦掉，否则它永远停在"失败"上。
                        was_failed = cue["state"] == "failed" or bool(cue["error"])
                        if cue["state"] != "ready" or cue["error"]:
                            self.store.update_cue(cue["id"], state="ready", error="")
                        if was_failed:
                            CHANNEL.info(
                                f"{cue_pos} 复用已有配音：{role_text} · "
                                f"{_size_text(existing.stat().st_size)}；"
                                "上一轮的失败标记已清掉。",
                                source="dubbing",
                                project=project_id,
                                job=job_id,
                                cue=cue["id"],
                                engine=engine_key,
                                voice=role["voice_id"],
                            )
                        else:
                            CHANNEL.debug(
                                f"{cue_pos} 跳过合成：文本、音色与样例都没变，"
                                f"复用 {_size_text(existing.stat().st_size)} 的旧产物。",
                                source="dubbing",
                                project=project_id,
                                job=job_id,
                                cue=cue["id"],
                                engine=engine_key,
                                voice=role["voice_id"],
                            )
                        async with lock:
                            done += 1
                            reused += 1
                            self.store.update_job(job_id, done=done, failed=failed)
                        return
                    CHANNEL.debug(
                        f"{cue_pos} 的旧音频文件已不在磁盘上（{cue['audio']}），"
                        "按新配音重新合成。",
                        source="dubbing",
                        project=project_id,
                        job=job_id,
                        cue=cue["id"],
                        engine=engine_key,
                    )
                else:
                    CHANNEL.debug(
                        f"{cue_pos} 需要重配："
                        + ("用户指定了强制重配。" if force else "文本、角色、音色或样例已变。"),
                        source="dubbing",
                        project=project_id,
                        job=job_id,
                        cue=cue["id"],
                        engine=engine_key,
                        voice=role["voice_id"],
                    )
                async with semaphore:
                    # 排队期间可能已有别的段撞上「连不上」或用户点了取消。
                    if self._cancelled(job_id):
                        return
                    if engine_key in halted:
                        async with lock:
                            blocked += 1
                        return
                    params = dict(role.get("params") or {})
                    emotion = str(params.pop("emotion", "") or "")
                    started_cue = time.monotonic()
                    try:
                        from app.usecase import gateway

                        result = await gateway.synthesize(
                            library,
                            engine_key=engine_key,
                            voice_id=role["voice_id"],
                            text=cue["text"],
                            emotion=emotion,
                            extra=params or None,
                            sample_file=sample_file,
                            match_emotion=sample_mode == "emotion",
                            log_failure=False,
                        )
                        suffix = (result.format or "wav").lower()
                        raw = work / f"{cue['id']}.{suffix}"
                        raw.write_bytes(result.audio)
                        # 中间件统一 44.1k 单声道 PCM：各家引擎的采样率不同，
                        # 不统一就没法按时间轴拼。
                        wav = self._to_middle(raw, cue["id"])
                        rel = wav.relative_to(self.folder(project_id)).as_posix()
                    except EngineError as exc:
                        if exc.code in CODES_ENGINE_UNAVAILABLE:
                            async with lock:
                                entry = halted.get(engine_key)
                                if entry is None:
                                    entry = halted[engine_key] = {
                                        "reason": exc.reason,
                                        "label": engine_name,
                                        "rows": 0,
                                    }
                                    CHANNEL.warning(
                                        f"{cue_pos} 撞上引擎不可用：{role_text} · "
                                        f"{engine_name}（{engine_key}）· {exc.reason}；"
                                        "这台引擎的剩余段落停下，其他引擎继续。",
                                        source="dubbing",
                                        project=project_id,
                                        job=job_id,
                                        engine=engine_key,
                                        voice=role["voice_id"],
                                        cue=cue["id"],
                                    )
                                entry["rows"] += 1
                                blocked += 1
                            return
                        kept = "；旧音频保留，仍可试听。" if cue["audio"] else "；这一段没有旧音频。"
                        async with lock:
                            failed += 1
                            if not first_error:
                                first_error = exc.reason
                            self.store.update_cue(
                                cue["id"], state="failed", error=exc.reason
                            )
                            self.store.update_job(job_id, done=done, failed=failed)
                        CHANNEL.warning(
                            f"{cue_pos} 合成失败：{role_text} · "
                            f"{len(cue['text'])} 字 · "
                            f"引擎返回 {exc.code or '错误'}：{exc.reason}{kept}",
                            source="dubbing",
                            project=project_id,
                            job=job_id,
                            engine=engine_key,
                            voice=role["voice_id"],
                            cue=cue["id"],
                            code=exc.code,
                        )
                        return
                    except Exception as exc:  # noqa: BLE001
                        # 这一步失败 = 真的没合成出来：标 failed，旧产物仍在也能放。
                        async with lock:
                            failed += 1
                            if not first_error:
                                first_error = str(exc)
                            self.store.update_cue(
                                cue["id"], state="failed", error=str(exc)
                            )
                            self.store.update_job(job_id, done=done, failed=failed)
                        kept = (
                            "；旧音频保留，仍可试听。"
                            if cue["audio"]
                            else "；这一段没有旧音频。"
                        )
                        CHANNEL.warning(
                            f"{cue_pos} 合成失败：{role_text} · "
                            f"{len(cue['text'])} 字 · {exc}{kept}",
                            source="dubbing",
                            project=project_id,
                            job=job_id,
                            engine=engine_key,
                            voice=role["voice_id"],
                            cue=cue["id"],
                        )
                        return
                    # 音频已经落盘，只有"记账"可能失败：写不进去是库的问题，不是合成
                    # 的问题。以前把它算成 failed，界面就出现"显示失败、播放却是对的"。
                    # 合成期间用户改过字或角色时，产物已经不是当前参数的产物：
                    # 照写音频，但状态记成过期；改回去时键值仍对得上。
                    latest = self.store.cue(cue["id"])
                    if latest is None:
                        wav.unlink(missing_ok=True)
                        return
                    role_now = role_of(latest, self.store.roles(project_id))
                    current = bool(
                        role_now
                        and self._audio_key(
                            latest,
                            role_now,
                            identity=self._identity_for(
                                str(role_now.get("engine") or "")
                            ),
                        )
                        == key
                    )
                    try:
                        self.store.update_cue(
                            cue["id"],
                            audio=rel,
                            audio_key=key,
                            state="ready" if current else "stale",
                            error="",
                        )
                    except Exception as exc:  # noqa: BLE001
                        CHANNEL.warning(
                            f"{cue_pos} 的配音产物已写出（{_size_text(wav.stat().st_size)}），"
                            f"但没记进库：{exc}；界面重进后会按文件重新认。",
                            source="dubbing",
                            project=project_id,
                            job=job_id,
                            cue=cue["id"],
                        )
                    elapsed_cue = time.monotonic() - started_cue
                    size = wav.stat().st_size
                    hints: list[str] = []
                    if not current:
                        hints.append("合成期间段落已改动，产物标为过期")
                    if emotion:
                        hints.append(f"情绪 {emotion}")
                    extra_note = _params_text(params)
                    if extra_note:
                        hints.append(extra_note)
                    hints.append(_sample_text(role))
                    if result.cost_note:
                        hints.append(result.cost_note)
                    for note in result.notes:
                        hints.append(f"引擎提示：{note}")
                    for _code, text in result.prep_notes:
                        hints.append(f"已自动处理：{text}")
                    CHANNEL.info(
                        f"{cue_pos} 合成完成：{role_text} · {len(cue['text'])} 字 · "
                        f"{elapsed_cue:.2f}s · {_size_text(size)}"
                        + (f" · {' · '.join(hints)}" if hints else ""),
                        source="dubbing",
                        project=project_id,
                        job=job_id,
                        cue=cue["id"],
                        engine=engine_key,
                        voice=role["voice_id"],
                        sample_mode=sample_mode,
                        sample_file=sample_file,
                        elapsed=round(elapsed_cue, 2),
                        bytes=size,
                    )
                    async with lock:
                        done += 1
                        produced += 1
                        written_bytes += size
                        self.store.update_job(job_id, done=done, failed=failed)

            tasks = [asyncio.create_task(one(cue)) for cue in targets]
            for index, task in enumerate(asyncio.as_completed(tasks), start=1):
                await task
                if self._cancelled(job_id):
                    for pending in tasks:
                        pending.cancel()
                    break
                self.store.update_job(
                    job_id,
                    done=done,
                    failed=failed,
                    message=f"合成 {index} / {len(targets)}",
                )
            return done, failed, blocked, produced, reused, written_bytes, first_error, halted

        (
            done,
            failed,
            blocked,
            produced,
            reused,
            written_bytes,
            first_error,
            halted,
        ) = asyncio.run(run())
        elapsed = time.monotonic() - started
        self.store.update_project(project_id, status="synthesized")
        parts = [f"新配 {produced} 段"]
        if reused:
            parts.append(f"复用 {reused} 段")
        if failed:
            parts.append(f"失败 {failed} 段")
        if blocked:
            for entry in halted.values():
                parts.append(f"{entry['label']} 跳过 {entry['rows']} 段：{entry['reason']}")
        if written_bytes:
            parts.append(f"产物 {_size_text(written_bytes)}")
        message = f"{' · '.join(parts)} · {elapsed:.1f}s"
        if done == 0 and (failed or blocked):
            reason = first_error or next(
                (entry["reason"] for entry in halted.values()), "全部段落合成失败。"
            )
            CHANNEL.error(
                f"配音没有完成：{message}",
                source="dubbing",
                project=project_id,
                job=job_id,
                done=done,
                failed=failed,
                blocked=blocked,
                elapsed=round(elapsed, 1),
            )
            raise ProducerError(reason)
        CHANNEL.info(
            f"配音完成：{message}",
            source="dubbing",
            project=project_id,
            job=job_id,
            done=done,
            failed=failed,
            blocked=blocked,
            produced=produced,
            reused=reused,
            bytes=written_bytes,
            elapsed=round(elapsed, 1),
        )
        self.store.update_job(
            job_id,
            done=done,
            failed=failed,
            message=message,
        )
        return message

