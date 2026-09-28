"""Single owner of automatic MAX egress policy, driven by the inner watchdog."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class MaxEgressController:
    def __init__(self, *, adapter, config, path: Path, notify, restart, clock=time.time):
        self.adapter, self.cfg, self.path = adapter, config, path
        self.notify, self.restart, self.clock = notify, restart, clock
        self.lock = asyncio.Lock()
        self.primary = config.active
        self.fallback = config.fallback
        self.state = {
            "version": 1, "primary": self.primary, "fallback": self.fallback,
            "active": self.primary, "phase": "primary", "incident": 0, "sequence": 0,
            "failed_since": None, "healthy_since": None, "failures": 0,
            "fallback_since": None, "cooldown_until": 0, "deadline": None,
            "last_probe_at": None, "probes": {}, "pending_events": [],
            "last_reminder_at": 0, "announced": False,
        }
        if path.exists():
            try:
                saved = json.loads(path.read_text())
                if (saved.get("version") == 1 and saved.get("primary") == self.primary
                        and saved.get("fallback") == self.fallback):
                    self.state.update({k: saved[k] for k in self.state if k in saved})
            except (ValueError, OSError, TypeError):
                logger.warning("MAX egress state unreadable; starting with configured primary")
        self.state["healthy_since"] = None  # revalidate recovery after any restart
        if self.state["active"] not in {self.primary, self.fallback}:
            self.state["active"] = self.primary
        self._initialized = False

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def snapshot(self):
        now = self.clock()
        s = self.state
        return {
            k: s[k] for k in (
                "primary", "fallback", "active", "phase", "incident", "sequence",
                "failed_since", "healthy_since", "fallback_since", "cooldown_until",
                "deadline", "last_probe_at", "probes",
            )
        } | {
            "policy": "auto", "ready": self.adapter.is_ready(),
            "updated_at": now,
            "outage_seconds": max(0, int(now - s["failed_since"])) if s["failed_since"] is not None else 0,
            "fallback_seconds": max(0, int(now - s["fallback_since"])) if s["fallback_since"] is not None else 0,
            "next_action": self._next_action(),
            "wait_until": (s["failed_since"] + self.cfg.failover_after_seconds
                           if s["failed_since"] is not None else None),
        }

    def _next_action(self):
        if self.state["phase"] == "auth_blocked":
            return "Требуется проверка авторизации; смена маршрута остановлена"
        if self.state["phase"] in {"switching", "returning", "rollback"}:
            return "Ожидаем готовности MAX на выбранном маршруте"
        if self.state["active"] == self.fallback:
            return "Проверяем Channel M перед возвратом"
        if self.state["failed_since"] is not None:
            return "Ожидаем порога отказа и доступности резерва"
        return "Наблюдаем основной маршрут"

    async def _event(self, kind, text, audience="ops"):
        s = self.state
        s["sequence"] += 1
        stamp = datetime.fromtimestamp(self.clock(), ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M:%S МСК")
        event_id = f"max-egress-{s['incident']}-{s['sequence']}"
        message = (
            f"[MAX watchdog · M-{s['incident']:04d} · {s['sequence']}] {stamp}\n"
            f"{text}\nМаршрут: {s['active']}; MAX: "
            f"{'online' if self.adapter.is_ready() else 'offline'}."
        )
        s["pending_events"].append({"id": event_id, "text": message, "audience": audience})
        self._save()
        logger.info("max.egress.transition incident=%s sequence=%s event=%s active=%s phase=%s",
                    s["incident"], s["sequence"], kind, s["active"], s["phase"])
        await self._flush_events()

    async def _flush_events(self):
        while self.state["pending_events"]:
            event = self.state["pending_events"][0]
            try:
                async with asyncio.timeout(10):
                    accepted = await self.notify(event["text"], category="max_egress",
                                                 audience=event["audience"], event_id=event["id"])
                if accepted is False:
                    return
            except Exception:
                logger.warning("MAX egress notification deferred")
                return
            self.state["pending_events"].pop(0)
            self._save()

    async def _probe(self, profile):
        try:
            result = await self.adapter.probe_egress(profile)
        except Exception:
            result = {"ok": False, "stage": "probe_error"}
        # Explicit allowlist: no addresses, credentials or exception strings.
        safe = {k: result[k] for k in ("ok", "stage", "latency_ms") if k in result}
        safe["checked_at"] = self.clock()
        self.state["probes"][profile] = safe
        return bool(safe.get("ok"))

    async def _switch(self, target):
        s = self.state
        previous = s["active"]
        s["phase"] = "returning" if target == self.primary else "switching"
        s["deadline"] = self.clock() + self.cfg.drain_timeout_seconds + self.cfg.connect_timeout_seconds + 30
        s["cooldown_until"] = self.clock() + self.cfg.retry_cooldown_seconds
        await self._event("switch_started", f"Начинаем переход: {previous} → {target}.")
        try:
            await self.adapter.switch_egress(target, drain_timeout=self.cfg.drain_timeout_seconds)
        except Exception:
            await self._event("close_failed", "Не удалось безопасно закрыть MAX. Перезапускаем процесс.", "both")
            self.restart("max_egress_close_failed")
            return
        s["active"] = target
        self._save()
        if await self._wait_ready():
            s["deadline"] = None
            s["healthy_since"] = None
            if target == self.fallback:
                s["phase"] = "fallback"
                s["fallback_since"] = self.clock()
                s["last_reminder_at"] = self.clock()
                await self._event("fallback_connected", "🟠 MAX подключён через VPS. Доставка возобновлена; проверяем Channel M.", "both")
            else:
                duration = int(self.clock() - (s["fallback_since"] or self.clock()))
                s.update(phase="primary", failed_since=None, fallback_since=None, failures=0, announced=False)
                await self._event("primary_connected", f"🟢 MAX подключён через Channel M. Инцидент закрыт; на резерве {duration}с. История за простой может быть неполной.", "both")
            self._save()
            return
        s["phase"] = "rollback"
        s["deadline"] = self.clock() + self.cfg.drain_timeout_seconds + self.cfg.connect_timeout_seconds + 30
        await self._event("switch_failed", "Подключение не подтверждено. Возвращаем предыдущий маршрут.", "both")
        try:
            await self.adapter.switch_egress(previous, drain_timeout=self.cfg.drain_timeout_seconds)
        except Exception:
            self.restart("max_egress_rollback_close_failed")
            return
        s["active"] = previous
        self._save()
        ready = await self._wait_ready()
        s["phase"] = "fallback" if previous == self.fallback else "waiting"
        s["deadline"] = None
        s["healthy_since"] = None
        s["cooldown_until"] = self.clock() + self.cfg.retry_cooldown_seconds
        await self._event("rollback_finished", "Откат завершён. " + ("MAX online." if ready else "MAX offline; продолжаем reconnect и проверки."), "both")

    async def _wait_ready(self):
        deadline = time.monotonic() + self.cfg.connect_timeout_seconds
        while time.monotonic() < deadline:
            issue = self.adapter.get_last_issue()
            if issue and issue.requires_reauth:
                return False
            if self.adapter.is_ready():
                return True
            await asyncio.sleep(0.25)
        return False

    async def tick(self):
        async with self.lock:
            await self._flush_events()
            s, now = self.state, self.clock()
            first_tick = not self._initialized
            if not self._initialized:
                # The adapter starts on the configured primary. Restore durable route
                # through the same serialized close/switch path, never a second client.
                actual = self.adapter.get_egress_status()["max_egress_active"]
                if s["active"] not in {self.primary, self.fallback}:
                    s["active"] = self.primary
                if actual != s["active"]:
                    try:
                        await self.adapter.switch_egress(s["active"], drain_timeout=self.cfg.drain_timeout_seconds)
                    except Exception:
                        self.restart("max_egress_restore_close_failed")
                        return
                if s["phase"] in {"switching", "returning", "rollback"}:
                    s["phase"] = "fallback" if s["active"] == self.fallback else "waiting"
                    s["deadline"] = None
                    s["cooldown_until"] = max(s["cooldown_until"], now + self.cfg.retry_cooldown_seconds)
                    await self._event("restart_recovered", "Продолжаем наблюдение после перезапуска; доступность перепроверяется.")
                self._initialized = True
            if not first_tick and s["last_probe_at"] is not None and now - s["last_probe_at"] < self.cfg.probe_interval_seconds:
                return
            # Long monitoring gaps must not count as continuously verified failure/recovery.
            if s["last_probe_at"] is not None and now - s["last_probe_at"] > self.cfg.probe_interval_seconds * 3:
                s.update(failed_since=None, healthy_since=None, failures=0)
            s["last_probe_at"] = now
            primary_ok = await self._probe(self.primary)
            issue = self.adapter.get_last_issue()
            if issue and issue.requires_reauth:
                s["phase"] = "auth_blocked"
                s.update(failed_since=None, healthy_since=None, failures=0)
                self._save()
                return
            if s["active"] == self.primary:
                if primary_ok or self.adapter.is_ready():
                    if s["announced"] and self.adapter.is_ready():
                        await self._event("short_recovery", "🟢 Основной маршрут восстановлен без перехода на резерв.")
                        s["announced"] = False
                    s.update(failed_since=None, failures=0, phase="primary")
                else:
                    if s["failed_since"] is None:
                        s["failed_since"] = now
                        if not s["announced"]:
                            s["incident"] += 1
                            s["sequence"] = 0
                    s["failures"] += 1
                    s["phase"] = "waiting"
                    if s["failures"] >= 2 and not s["announced"]:
                        s["announced"] = True
                        await self._event("outage", f"🟡 Channel M недоступен. Ждём до {self.cfg.failover_after_seconds}с от начала отказа; затем проверим резерв.")
                    if now - s["failed_since"] >= self.cfg.failover_after_seconds and now >= s["cooldown_until"]:
                        if await self._probe(self.fallback):
                            await self._switch(self.fallback)
                        else:
                            s["cooldown_until"] = now + self.cfg.retry_cooldown_seconds
                            await self._event("both_unavailable", "🔴 Оба маршрута недоступны. MAX offline; продолжаем проверки.", "both")
            else:
                s["phase"] = "fallback"
                if primary_ok:
                    if s["healthy_since"] is None:
                        s["healthy_since"] = now
                        await self._event("recovery_started", "Channel M отвечает. Проверяем стабильность перед возвратом.")
                    if (now - s["healthy_since"] >= self.cfg.recovery_stable_seconds
                            and now - (s["fallback_since"] or 0) >= self.cfg.minimum_residence_seconds
                            and now >= s["cooldown_until"]):
                        await self._switch(self.primary)
                else:
                    s["healthy_since"] = None
                await self._probe(self.fallback)
                if now - s["last_reminder_at"] >= 4 * 3600:
                    s["last_reminder_at"] = now
                    await self._event("reminder", "MAX продолжает использовать резервный маршрут. Основной ещё не восстановлен стабильно.", "both")
            self._save()
