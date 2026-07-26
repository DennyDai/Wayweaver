import asyncio
import time
from dataclasses import dataclass
from typing import Iterable

from .adapters import factories
from .adapters.base import Adapter
from .config import TargetConfig
from .errors import CapabilityError, ConfigError
from .operations import API_VERSION, OPERATIONS
from .types import Capability


#: How stale a probe must be before a failed lookup re-runs it. Short enough
#: that a caller waiting on a recovering adapter sees it come back, long
#: enough that a burst of misses does not become a burst of probes.
_REPROBE_AFTER_MISS = 1.0


@dataclass(frozen=True, slots=True)
class AdapterStatus:
    name: str
    kind: str
    capabilities: tuple[str, ...]
    available: bool
    reason: str | None


class Router:
    def __init__(self, target: TargetConfig):
        self.target = target
        self.adapters = self._create_adapters()
        self.status: dict[str, AdapterStatus] = {}
        self.probed_at = 0.0

    def _create_adapters(self) -> dict[str, Adapter]:
        known = factories()
        adapters: dict[str, Adapter] = {}
        pending = dict(self.target.adapters)
        errors: dict[str, Exception] = {}
        while pending:
            progressed = False
            for name, config in tuple(pending.items()):
                kind = str(config.get("kind", name))
                factory = known.get(kind)
                if not factory:
                    raise ConfigError(
                        f"unknown adapter kind {kind!r} on target {self.target.name}"
                    )
                try:
                    adapters[name] = factory(name, config, adapters)
                except ConfigError as error:
                    errors[name] = error
                    continue
                del pending[name]
                errors.pop(name, None)
                progressed = True
            if not progressed:
                detail = "; ".join(str(error) for error in errors.values())
                raise ConfigError(
                    detail or f"cannot construct adapters for {self.target.name}"
                )
        return adapters

    async def probe(self) -> dict[str, AdapterStatus]:
        names = list(self.adapters)
        # An adapter that raises instead of reporting unavailable used to
        # abort the whole probe, leaving the target with no routing at all.
        results = await asyncio.gather(
            *(self.adapters[name].available() for name in names),
            return_exceptions=True,
        )
        status = {}
        for name, outcome in zip(names, results):
            if isinstance(outcome, BaseException):
                available, reason = False, f"probe raised {type(outcome).__name__}: {outcome}"
            else:
                available, reason = outcome
            adapter = self.adapters[name]
            status[name] = AdapterStatus(
                name,
                adapter.kind,
                tuple(sorted(capability.value for capability in adapter.capabilities)),
                available,
                reason,
            )
        self.status = status
        self.probed_at = time.monotonic()
        return status

    async def _ensure_probed(self) -> None:
        if not self.status or time.monotonic() - self.probed_at > self.target.probe_ttl:
            await self.probe()

    def _ordered(self) -> list[Adapter]:
        preferred = list(self.target.prefer)
        rank = {value: index for index, value in enumerate(preferred)}
        return sorted(
            self.adapters.values(),
            key=lambda adapter: min(
                rank.get(adapter.name, len(rank)), rank.get(adapter.kind, len(rank))
            ),
        )

    def _pick(
        self, requirements: set[Capability], excluded: set[str]
    ) -> Adapter | None:
        for adapter in self._ordered():
            status = self.status[adapter.name]
            if (
                adapter.name not in excluded
                and status.available
                and requirements <= adapter.capabilities
            ):
                return adapter
        return None

    async def _available(
        self,
        requirements: set[Capability],
        exclude: set[str] | None = None,
    ) -> Adapter | None:
        await self._ensure_probed()
        excluded = exclude or set()
        adapter = self._pick(requirements, excluded)
        if adapter is not None:
            return adapter
        # A momentary probe failure would otherwise be cached for the whole
        # TTL, and a caller's retries all land on the same stale snapshot --
        # a semantic layer that dropped off its bus for a second stayed
        # unavailable for thirty, so waiting for it could never succeed.
        # Re-probing only once the result has some age keeps a burst of calls
        # from turning every miss into a probe.
        if time.monotonic() - self.probed_at >= _REPROBE_AFTER_MISS:
            await self.probe()
            adapter = self._pick(requirements, excluded)
        return adapter

    async def operation_routes(
        self, include_raw: bool = False
    ) -> dict[str, dict[str, object]]:
        routes: dict[str, dict[str, object]] = {}
        for spec in OPERATIONS.values():
            if not spec.required:
                routes[spec.name] = {
                    "api_version": API_VERSION,
                    "description": spec.description,
                    "tier": spec.tier,
                    "adapter": "controller",
                    "kind": "control",
                    "params_schema": spec.params_schema,
                    "result_schema": spec.result_schema,
                    "examples": list(spec.examples),
                }
                continue
            adapter = await self._available(set(spec.required))
            fallback = False
            if adapter is None and spec.fallback:
                adapter = await self._available(set(spec.fallback))
                fallback = adapter is not None
            if adapter is not None:
                routes[spec.name] = {
                    "api_version": API_VERSION,
                    "description": spec.description,
                    "tier": "fallback" if fallback else spec.tier,
                    "adapter": adapter.name,
                    "kind": adapter.kind,
                    "params_schema": spec.params_schema,
                    "result_schema": spec.result_schema,
                    "examples": list(spec.examples),
                }
        if include_raw:
            await self._ensure_probed()
            for adapter in self._ordered():
                status = self.status[adapter.name]
                if status.available and adapter.raw_operations:
                    routes[f"raw:{adapter.name}"] = {
                        "tier": "raw",
                        "adapter": adapter.name,
                        "kind": adapter.kind,
                        "operations": dict(adapter.raw_operations),
                    }
        return routes

    async def select_operation(self, operation: str) -> tuple[Adapter, bool]:
        spec = OPERATIONS.get(operation)
        if spec is None:
            raise CapabilityError(f"unknown operation: {operation}")
        adapter = await self._available(set(spec.required))
        if adapter is not None:
            return adapter, False
        if spec.fallback:
            adapter = await self._available(set(spec.fallback))
            if adapter is not None:
                return adapter, True
        raise CapabilityError(f"target {self.target.name} does not support {operation}")

    async def select_fallback(
        self,
        operation: str,
        exclude: set[str] | None = None,
    ) -> Adapter:
        spec = OPERATIONS.get(operation)
        if spec is None or not spec.fallback:
            raise CapabilityError(f"operation {operation!r} has no fallback")
        adapter = await self._available(set(spec.fallback), exclude)
        if adapter is None:
            raise CapabilityError(
                f"target {self.target.name} has no fallback for {operation}"
            )
        return adapter

    async def raw(
        self, adapter_name: str, operation: str, params: dict[str, object]
    ) -> object:
        await self._ensure_probed()
        adapter = self.adapters.get(adapter_name)
        if adapter is None or not self.status[adapter_name].available:
            raise CapabilityError(
                f"adapter {adapter_name!r} is unavailable on {self.target.name}"
            )
        if operation not in adapter.raw_operations:
            raise CapabilityError(
                f"{adapter_name} does not expose raw operation {operation!r}"
            )
        return await adapter.raw(operation, params)

    async def select(self, required: Capability | Iterable[Capability]) -> Adapter:
        requirements = {required} if isinstance(required, Capability) else set(required)
        if adapter := await self._available(requirements):
            return adapter
        names = ", ".join(sorted(capability.value for capability in requirements))
        reasons = "; ".join(
            f"{item.name}: {item.reason}"
            for item in self.status.values()
            if item.reason
        )
        raise CapabilityError(
            f"target {self.target.name} lacks [{names}]"
            + (f" ({reasons})" if reasons else "")
        )

    async def close(self) -> None:
        # Closing is cleanup, and cleanup that stops at the first failure is
        # how a multiplexed SSH socket, a helper process and a browser
        # connection all outlive the run because one adapter raised.
        results = await asyncio.gather(
            *(adapter.close() for adapter in self.adapters.values()),
            return_exceptions=True,
        )
        for outcome in results:
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, Exception
            ):
                raise outcome
