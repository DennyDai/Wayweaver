import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import Config, load_config
from .contracts import prepare_params, validate_params, validate_result
from .errors import ActionError, CapabilityError, ContractError, SurfaceError
from .operations import API_VERSION, OPERATIONS
from .provenance import ProvenanceStore
from .router import Router
from .runtime.deploy import manage_runtime
from .types import Capability, Frame
from .vision import Word, find_text, ocr_items, recognize, text_output


# OCR costs ~900ms on a full 1600x900 screen against ~4ms to hash the capture,
# so identical frames are recognized once. Frames are keyed by content, which
# keeps region crops and full screens in separate entries.
_OCR_CACHE_ENTRIES = 8

# Tesseract needs surrounding context: a 132x61 crop of a real desktop label
# recognized nothing while 200x80 read it correctly. Recognition cost barely
# moves across that range (64ms to 121ms up to 500x200) against ~900ms for the
# whole screen, so widening a small crop is far cheaper than missing the text.
_MIN_OCR_CROP = (240, 120)

# How long an element's reported position must hold still before a scroll counts
# as finished. Chrome animates scrolling and publishes intermediate positions
# ~100ms apart, so a shorter window reads mid-flight coordinates as settled.
_SETTLE_SECONDS = 0.25


def _ocr_crop(region: list[int]) -> tuple[list[int], list[int]]:
    """Expand a locator region to a readable crop.

    Returns the rectangle to capture and the requested region restated in that
    rectangle's coordinates, so matches outside the caller's region are still
    rejected. Capture rectangles may overhang the right and bottom edges, which
    surfaces clip; the origin stays on screen so rebasing remains exact.
    """
    left, top, right, bottom = (int(value) for value in region[:4])
    pad_x = (max(0, _MIN_OCR_CROP[0] - (right - left)) + 1) // 2
    pad_y = (max(0, _MIN_OCR_CROP[1] - (bottom - top)) + 1) // 2
    capture = [max(0, left - pad_x), max(0, top - pad_y), right + pad_x, bottom + pad_y]
    return capture, [
        left - capture[0],
        top - capture[1],
        right - capture[0],
        bottom - capture[1],
    ]


class Controller:
    def __init__(self, config: Config):
        self.config = config
        self.routers: dict[str, Router] = {}
        self.observations: dict[str, dict[str, Any]] = {}
        self.provenance = ProvenanceStore(config.cache_dir)
        self._ocr_cache: dict[tuple[str, str], list[Word]] = {}

    @classmethod
    def from_path(cls, path: str | Path | None = None) -> "Controller":
        return cls(load_config(path))

    def router(self, target: str) -> Router:
        if target not in self.routers:
            self.routers[target] = Router(self.config.target(target))
        return self.routers[target]

    async def targets(self) -> dict[str, Any]:
        result = {}
        for name in self.config.targets:
            status = await self.router(name).probe()
            result[name] = {
                "adapters": {
                    adapter_name: {
                        "kind": item.kind,
                        "capabilities": list(item.capabilities),
                        "available": item.available,
                        "reason": item.reason,
                    }
                    for adapter_name, item in status.items()
                }
            }
        return result

    async def capture(
        self, target: str, params: dict[str, Any] | None = None
    ) -> tuple[Frame, str]:
        adapter = await self.router(target).select(Capability.CAPTURE)
        return await adapter.capture(params), adapter.name

    async def _ocr(self, target: str, frame: Frame) -> list[Word]:
        key = (target, hashlib.sha256(frame.png).hexdigest())
        if (cached := self._ocr_cache.get(key)) is not None:
            return list(cached)
        shell = None
        try:
            shell = await self.router(target).select(Capability.SHELL)
        except CapabilityError:
            pass
        words = await recognize(frame, shell)
        self._ocr_cache[key] = words
        while len(self._ocr_cache) > _OCR_CACHE_ENTRIES:
            del self._ocr_cache[next(iter(self._ocr_cache))]
        return list(words)

    async def observe(self, target: str, ocr: bool = False) -> dict[str, Any]:
        frame, adapter_name = await self.capture(target)
        path = self.config.cache_dir / target / "current.png"
        frame.save(path)
        adapter = self.router(target).adapters[adapter_name]
        space = "viewport" if adapter.kind == "cdp" else "screen"
        session_id = hashlib.sha256(
            (await adapter.surface_session()).encode()
        ).hexdigest()[:24]
        surface_id = self.provenance.issue(
            "surface",
            {
                "target": target,
                "adapter": adapter_name,
                "adapter_kind": adapter.kind,
                "source": frame.source,
                "space": space,
                "session_id": session_id,
                "width": frame.width,
                "height": frame.height,
                "scale": 1.0,
            },
        )
        observation_id = self.provenance.issue(
            "observation",
            {"target": target, "surface_id": surface_id},
        )
        surface = {
            "id": surface_id,
            "session_id": session_id,
            "adapter": adapter_name,
            "source": frame.source,
            "space": space,
            "width": frame.width,
            "height": frame.height,
            "scale": 1.0,
        }
        result: dict[str, Any] = {
            "observation_id": observation_id,
            "target": target,
            "surface": surface,
            "screenshot": str(path),
        }
        router = self.router(target)
        try:
            windows_adapter = await router.select(Capability.WINDOWS)
            result["windows"] = await windows_adapter.windows()
        except CapabilityError:
            pass
        if ocr:
            words = await self._ocr(target, frame)
            result["ocr"] = text_output(words)
            result["ocr_items"] = ocr_items(words)
        self.observations[target] = {
            "observation_id": observation_id,
            "surface": surface,
        }
        self.provenance.remember(target, observation_id, surface_id)
        return result

    async def locate(
        self, target: str, locator: str | dict[str, Any], click: bool = False
    ) -> dict[str, Any]:
        options = {"text": locator} if isinstance(locator, str) else dict(locator)
        timeout = float(options.pop("timeout", 3))
        interval = float(options.pop("interval", 0.25))
        desktop = await self.router(target).select(
            {Capability.CAPTURE, Capability.POINTER} if click else Capability.CAPTURE
        )
        deadline = time.monotonic() + max(0, timeout)
        last_error: Exception | None = None
        region = options.get("region")
        # Screen-wide OCR is both imprecise and partially blind: measured
        # against regional ground truth it recovers 36-91% of the text on a
        # busy desktop, and it happily matches a taskbar entry or another
        # application's title bar. Scoping to the active window fixes both --
        # a dialog's buttons that are invisible in a full-screen pass read at
        # 94% confidence inside the dialog's own rectangle. Pass
        # `window: "screen"` to search the whole surface anyway.
        window = options.pop("window", None)
        auto_scoped = False
        if window == "screen":
            window = None
        elif region is None:
            try:
                region = await self._window_region(target, window or "active")
                # Scoping the caller did not ask for is an optimization, so it
                # must never remove reach: if the active window does not hold
                # the target, the search widens to the whole screen below.
                auto_scoped = window is None
            except CapabilityError:
                # Pixel-only surfaces report no windows; fall back to the
                # whole screen rather than refusing an unasked-for scope.
                if window is not None:
                    raise
                region = None
            if region is not None:
                options = dict(options)
                options["region"] = region
        regional_options = None
        capture_rect = None
        if region is not None:
            capture_rect, relative_region = _ocr_crop(region)
            regional_options = dict(options)
            regional_options["region"] = relative_region
            regional_options.setdefault("fuzzy", True)
        # Recognizing a crop costs ~100ms against ~1s for the whole screen, so a
        # regional locator stays regional. Widening is opt-in through
        # full_screen_fallback, or automatic when the surface cannot crop at all.
        crop = region is not None and regional_options is not None
        widen = (
            not crop or auto_scoped or bool(options.get("full_screen_fallback", False))
        )
        # OCR only ever sees the viewport, so a locator that never scrolls can
        # only find what already happens to be on screen.
        scroll_budget = int(options.get("scroll", 0) or 0)
        scrolled = 0
        while True:
            attempts: list[tuple[dict[str, Any] | None, dict[str, Any], str]] = []
            if crop:
                attempts.append(({"region": capture_rect}, regional_options, "region"))
            # Widening searches outside the requested window, so it can match a
            # word in another application entirely. Spend the scroll budget on
            # the window first: text the caller asked for is far likelier below
            # the fold than outside the window. Without this a stray match ends
            # the search before the scroll that would have found the real one.
            if widen and (not crop or scrolled >= scroll_budget):
                attempts.append((None, options, "screen"))
            widened = False
            for capture_params, locator_options, scope in attempts:
                try:
                    frame = await desktop.capture(capture_params)
                except ActionError as error:
                    last_error = error
                    if scope == "region":
                        # VNC, RDP, and ADB capture whole surfaces only. Stop
                        # cropping and search the screen, where find_text still
                        # confines matches to the requested region.
                        crop = False
                        widened = not widen
                        widen = True
                    continue
                try:
                    filename = (
                        "current-region.png" if scope == "region" else "current.png"
                    )
                    frame.save(self.config.cache_dir / target / filename)
                    words = await self._ocr(target, frame)
                    match = find_text(words, locator_options)
                except ActionError as error:
                    last_error = error
                    continue
                if scope == "region" and capture_rect is not None:
                    left, top = capture_rect[0], capture_rect[1]
                    match["x"] += left
                    match["y"] += top
                    match["box"]["left"] += left
                    match["box"]["top"] += top
                    match["region"] = list(map(int, region))
                match["ocr_scope"] = scope
                if click:
                    action = (
                        "right_click" if int(options.get("button", 1)) == 3 else "click"
                    )
                    await desktop.act(
                        action,
                        {
                            "x": match["x"],
                            "y": match["y"],
                            "button": options.get("button", 1),
                        },
                    )
                    match["clicked"] = True
                if scrolled:
                    match["scrolled"] = scrolled
                return match
            if widened:
                continue
            if scrolled < scroll_budget:
                scroller = await self.router(target).select(Capability.SCROLL)
                await scroller.act("scroll", {"direction": "down", "amount": 4})
                scrolled += 1
                await asyncio.sleep(max(0.05, interval))
                continue
            if time.monotonic() >= deadline:
                raise ActionError(str(last_error))
            await asyncio.sleep(max(0.05, interval))

    async def _window_region(self, target: str, window: Any) -> list[int]:
        """Resolve a locator's `window` to its [left, top, right, bottom] rectangle.

        Accepts "active" or a title substring; matching is case-insensitive.
        """
        try:
            adapter = await self.router(target).select(Capability.WINDOWS)
        except CapabilityError as error:
            # Pixel-only targets such as VNC and RDP report no window metadata,
            # so name the locator option rather than surfacing a bare
            # capability error the caller cannot trace back to it.
            raise CapabilityError(
                f"locator window scoping needs window metadata, which {target} "
                "does not provide; use region instead",
                details={"target": target, "window": window},
            ) from error
        windows = await adapter.windows()
        if isinstance(window, str) and window != "active":
            wanted = window.casefold()
            chosen = next(
                (
                    item
                    for item in windows
                    if wanted in str(item.get("title", "")).casefold()
                ),
                None,
            )
        else:
            chosen = next((item for item in windows if item.get("active")), None)
        if chosen is None:
            raise ActionError(
                "locator window not found",
                details={"window": window},
            )
        left, top = int(chosen["x"]), int(chosen["y"])
        return [left, top, left + int(chosen["width"]), top + int(chosen["height"])]

    async def _application_hint(self, target: str) -> str | None:
        """Name the active window's application, to scope an element search.

        An unscoped search walks every application registered on the desktop.
        Measured here with a large Java toolkit running, that is 11 seconds
        against 30ms for the same search inside one application, and the cost
        falls on whichever application happens to be slow rather than the one
        being searched. Window titles conventionally end in the application
        name, which is the only link available: a window's pid is not the pid
        the accessibility layer reports for multi-process applications.

        Only ever a hint -- the caller falls back to the whole desktop -- so a
        title that names nothing costs one refusal and loses nothing.
        """
        try:
            windows = await self.router(target).select(Capability.WINDOWS)
            listing = await windows.windows()
        except (CapabilityError, ActionError):
            return None
        active = next((item for item in listing if item.get("active")), None)
        title = str((active or {}).get("title") or "").strip()
        return title.rsplit(" - ", 1)[-1].strip() or None if title else None

    @staticmethod
    async def _find_element(
        elements: Any, options: dict[str, Any], application: str | None = None
    ) -> Any:
        """Resolve a selector, preferring what is on screen but accepting what is not.

        Element layers hide off-screen nodes so that a background window's copy
        of a button cannot shadow the visible one. That filter also turns "below
        the fold" into "no such element", which sends the caller to OCR -- and
        OCR matches the same word in surrounding prose, producing a confident
        click on nothing. Asking for the hidden ones too lets the caller scroll
        there, or refuse with the element's real position in hand.

        This is one query, not a miss followed by a retry: a search that matches
        nothing has to walk the whole desktop, which costs around sixty times a
        search that hits. The layer ranks visible matches first.
        """
        query = {**options, "include_offscreen": True}
        if application and not options.get("application"):
            try:
                return await elements.perform(
                    "element.find", {**query, "application": application}
                )
            except ActionError:
                # The hint named nothing, named more than one thing, or the
                # element lives elsewhere. Searching the desktop still answers.
                pass
        return await elements.perform("element.find", query)

    async def _await_moved(
        self,
        elements: Any,
        options: dict[str, Any],
        previous: dict[str, Any],
        timeout: float = 3.0,
        hint: str | None = None,
    ) -> bool:
        """Wait until the element settles at a position other than `previous`.

        Two things go wrong if this is rushed. Toolkits publish new coordinates a
        little after a scroll lands -- around 300ms for a browser -- so resolving
        immediately returns where the element used to be. And browsers animate
        scrolling, so an early reading catches a position the element is still
        moving through; a click there lands on whatever it happens to pass over.

        Stability is therefore measured in time, not in readings. Counting
        readings ties the answer to how fast the element layer can be queried:
        scoping searches to one application made queries fast enough that two
        consecutive replies could land inside a single animation frame and look
        settled while the element was still moving.

        Returns False if the position never changed, which means the surface has
        stopped scrolling and repeating the gesture cannot help.
        """
        def spot(bounds: dict[str, Any]) -> tuple[int, int]:
            return int(bounds.get("left", 0)), int(bounds.get("top", 0))

        clock = asyncio.get_running_loop().time
        origin = spot(previous)
        deadline = clock() + timeout
        last: tuple[int, int] | None = None
        held_since = clock()
        while True:
            await asyncio.sleep(0.05)
            try:
                found = await self._find_element(elements, options, hint)
                bounds = found.get("bounds") if isinstance(found, dict) else None
            except (CapabilityError, ActionError):
                bounds = None
            if bounds:
                current = spot(bounds)
                if current != last:
                    last, held_since = current, clock()
                elif current != origin and clock() - held_since >= _SETTLE_SECONDS:
                    return True
            if clock() >= deadline:
                return last is not None and last != origin

    async def _confirm_box(
        self,
        elements: Any,
        options: dict[str, Any],
        hint: str | None,
        box: dict[str, Any],
    ) -> dict[str, Any]:
        """Re-read an element's position before acting on it.

        A page still laying out -- images arriving, a banner appearing above
        the form -- moves an element after it resolves, and nothing in the
        request hints that anything changed. Waiting only after a scroll caught
        this by accident, so a resolve that happened not to scroll returned a
        position the element had already left, and the click missed.

        The capture taken for the surface check supplies the delay this needs,
        so confirming an element that did not move costs one more query.
        """
        def spot(bounds: dict[str, Any]) -> tuple[int, int]:
            return int(bounds.get("left", 0)), int(bounds.get("top", 0))

        async def read() -> dict[str, Any] | None:
            try:
                found = await self._find_element(elements, options, hint)
            except (CapabilityError, ActionError):
                return None
            bounds = found.get("bounds") if isinstance(found, dict) else None
            if bounds and int(bounds.get("width", 0)) > 0:
                return bounds
            return None

        current = await read()
        if current is None or spot(current) == spot(box):
            return box
        # It moved once, so it is still moving. Wait for it to stop, then take
        # the position it stopped at.
        await self._await_moved(elements, options, current, hint=hint)
        return await read() or current

    async def _element_point(
        self, target: str, options: dict[str, Any], allow_fallback: bool
    ) -> dict[str, Any]:
        """Resolve an element to a point the input layer can act on.

        Information and input do not have to come from the same adapter. A VNC
        or RDP target carries pixels and input but no element tree, while an
        AT-SPI or UIA side channel carries the tree but cannot drive the real
        input. This resolves the target through the richest layer available and
        hands back a point any pointer adapter can use, naming the layer that
        answered so a caller can tell semantic truth from a pixel guess.

        `timeout` keeps resolution retrying until the deadline. The contract
        always accepted it -- every element operation shares the schema -- but
        it was silently ignored here, so a resolve issued right after a
        navigation failed on a form that legitimately did not exist yet, and
        every caller grew its own retry loop.
        """
        router = self.router(target)
        source = tier = None
        box = None
        detail: dict[str, Any] = {}
        scroll_budget = int(options.get("scroll", 0) or 0)
        scrolled = 0
        query = dict(options)
        # `options` stays intact: the OCR fallback below re-reads the same
        # timeout, so each layer gets the budget the caller named.
        deadline = time.monotonic() + max(0.0, float(options.get("timeout", 0) or 0))
        interval = max(0.05, float(options.get("interval", 0.4)))
        query.pop("timeout", None)
        query.pop("interval", None)
        hint = await self._application_hint(target)
        elements: Any = None
        while True:
            try:
                elements = await router.select(Capability.ELEMENTS)
                found = await self._find_element(elements, query, hint)
                bounds = found.get("bounds") if isinstance(found, dict) else None
                if bounds and int(bounds.get("width", 0)) > 0:
                    box = bounds
                    source, tier = elements.name, "semantic"
                    detail = {
                        key: found[key]
                        for key in ("name", "role", "states")
                        if key in found
                    }
            except (CapabilityError, ActionError):
                box = None
            if box is None and time.monotonic() < deadline:
                await asyncio.sleep(interval)
                continue
            if box is None or scrolled >= scroll_budget:
                break
            # The element layer reports desktop coordinates, so an element below
            # the fold resolves to a point the pointer cannot reach. Scroll
            # toward it and ask again rather than acting on an unreachable point.
            #
            # Reachable means inside the window showing the element, not merely
            # on the screen: a window shorter than the screen leaves a band
            # where a point is numerically on the surface and physically on
            # whatever sits beneath the window. Measured live: a field at
            # desktop y=899 under a window ending at 890 on a 900px screen was
            # ruled reachable and the click landed on the panel below.
            frame, _ = await self.capture(target)
            top_bound, bottom_bound = 0, frame.height
            try:
                window_region = await self._window_region(target, "active")
                top_bound = max(top_bound, int(window_region[1]))
                bottom_bound = min(bottom_bound, int(window_region[3]))
            except (ActionError, CapabilityError):
                pass
            centre = int(box["top"]) + int(box["height"]) // 2
            if top_bound <= centre < bottom_bound:
                break
            previous = dict(box)
            scroller = await router.select(Capability.SCROLL)
            await scroller.act(
                "scroll",
                {"direction": "down" if centre >= bottom_bound else "up", "amount": 4},
            )
            scrolled += 1
            if not await self._await_moved(elements, query, previous, hint=hint):
                # The surface will not scroll any further, so more attempts
                # cannot help. `box` still holds the position, which is now
                # current, and the surface guard reports it if it stays off.
                break
        if box is None:
            if not allow_fallback:
                raise CapabilityError(
                    "no semantic element layer resolved this selector and "
                    "allow_fallback is false",
                    details={"target": target},
                )
            locator = dict(options)
            if "name" in locator and "text" not in locator:
                locator["text"] = locator.pop("name")
            match = await self.locate(target, locator)
            box = match["box"]
            source, tier = "ocr", "visual"
            detail = {"text": match.get("text"), "confidence": match.get("confidence")}

        # The point is only usable if it lands on the surface that will act on
        # it. Layers can disagree -- a side channel may describe a desktop the
        # framebuffer does not show -- and clicking anyway lands somewhere else.
        frame, capture_name = await self.capture(target)
        if tier == "semantic" and elements is not None:
            box = await self._confirm_box(elements, query, hint, box)
        point = {
            "x": int(box["left"]) + int(box["width"]) // 2,
            "y": int(box["top"]) + int(box["height"]) // 2,
        }
        if not (0 <= point["x"] < frame.width and 0 <= point["y"] < frame.height):
            raise SurfaceError(
                f"{tier} layer {source!r} reports a point outside the "
                f"{capture_name!r} surface",
                details={
                    "point": point,
                    "surface": {"width": frame.width, "height": frame.height},
                    "source": source,
                },
            )
        return {
            "point": point,
            "box": {key: int(box[key]) for key in ("left", "top", "width", "height")},
            "source": source,
            "tier": tier,
            "surface": {
                "adapter": capture_name,
                "width": frame.width,
                "height": frame.height,
            },
            **detail,
        }

    async def _copy_selection(
        self, adapter: Any, options: dict[str, Any]
    ) -> dict[str, Any]:
        """Copy the focused selection and return it as exact text.

        The clipboard is poisoned with a sentinel first, so a copy that never
        reached the focused widget is reported as a failure instead of silently
        returning whatever the clipboard already held.
        """
        timeout = max(0, int(options.get("settle_ms", 600))) / 1000
        sentinel = f"wayweaver-copy-{uuid4().hex}"
        previous = None
        if options.get("restore", False):
            try:
                previous = str((await adapter.perform("clipboard.read", {}))["text"])
            except (ActionError, CapabilityError, KeyError):
                previous = None
        await adapter.perform("clipboard.write", {"text": sentinel})
        if options.get("select_all", False):
            await adapter.act("key", {"keys": ["ctrl+a"]})
            await asyncio.sleep(0.1)
        await adapter.act("key", {"keys": ["ctrl+c"]})
        deadline = time.monotonic() + timeout
        text = sentinel
        while True:
            text = str((await adapter.perform("clipboard.read", {}))["text"])
            if text != sentinel or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        if previous is not None:
            await adapter.perform("clipboard.write", {"text": previous})
        if text == sentinel:
            raise ActionError(
                "copy did not reach the focused widget; the clipboard never changed",
                details={"select_all": bool(options.get("select_all", False))},
            )
        return {"text": text, "characters": len(text)}

    async def _verify_typed(self, target: str, text: str) -> dict[str, Any]:
        """Confirm typed text reached an editable control.

        Keystrokes go wherever focus happens to be, so a click that missed its
        target types into nothing and every layer still reports success. The
        accessibility tree knows what holds focus and what it now contains.
        """
        elements = await self.router(target).select(Capability.ELEMENTS)
        focused = await elements.perform("element.focused", {})
        role = str(focused.get("role", ""))
        contents = focused.get("text")
        landed = isinstance(contents, str) and text in contents
        if not landed:
            raise ActionError(
                f"typed text did not reach an editable control; focus is on "
                f"{role!r}",
                details={
                    "focused_role": role,
                    "focused_name": focused.get("name"),
                    "expected": text,
                    "found": contents,
                },
            )
        return {"typed_into": role, "name": focused.get("name")}

    async def _verify_activation(
        self,
        target: str,
        options: dict[str, Any],
        before: tuple[Frame, str] | None,
    ) -> dict[str, Any]:
        timeout = max(0, int(options.get("timeout_ms", 3000))) / 1000
        interval = max(1, int(options.get("interval_ms", 250))) / 1000
        deadline = time.monotonic() + timeout
        expected_change = options.get("surface_changed")
        text = options.get("text_disappeared")
        attempts = 0
        state: dict[str, Any] = {}
        while True:
            frame, adapter_name = await self.capture(target)
            attempts += 1
            if before is not None:
                before_frame, before_adapter = before
                if adapter_name != before_adapter:
                    raise ActionError(
                        "activation verification changed capture adapters",
                        details={
                            "before_adapter": before_adapter,
                            "after_adapter": adapter_name,
                        },
                    )
                state["surface_changed"] = frame.png != before_frame.png
            if isinstance(text, str):
                try:
                    find_text(await self._ocr(target, frame), {"text": text})
                    state["text_disappeared"] = False
                except ActionError:
                    state["text_disappeared"] = True
            change_matches = expected_change is None or (
                state.get("surface_changed") is expected_change
            )
            text_matches = text is None or state.get("text_disappeared") is True
            if change_matches and text_matches:
                return {**state, "attempts": attempts}
            if time.monotonic() >= deadline:
                raise ActionError(
                    "element activation postcondition timed out",
                    details={"verification": state, "attempts": attempts},
                )
            await asyncio.sleep(interval)

    async def _validate_surface(
        self,
        target: str,
        operation: str,
        params: dict[str, Any],
        adapter_name: str,
        adapter_kind: str,
    ) -> None:
        if not operation.startswith("pointer."):
            return
        expected_space = "viewport" if adapter_kind == "cdp" else "screen"
        space = params["space"]
        if space not in {expected_space, "surface"}:
            raise SurfaceError(
                f"{operation} uses {space!r} coordinates on a {expected_space!r} surface",
                details={"expected_space": expected_space, "actual_space": space},
            )
        observation_id = params.get("observation_id")
        surface_id = params.get("surface_id")
        if space == "surface" and not surface_id:
            raise ContractError(
                f"{operation} requires surface_id when space is 'surface'",
                details={"operation": operation},
            )
        if observation_id:
            observation = self.provenance.verify(observation_id, "observation")
            if observation.get("target") != target:
                raise SurfaceError(
                    f"{operation} references an observation from another target",
                    details={"target": target},
                )
            observed_surface_id = observation.get("surface_id")
            if surface_id and surface_id != observed_surface_id:
                raise SurfaceError(
                    f"{operation} mixes unrelated observation and surface tokens"
                )
            surface_id = str(observed_surface_id)
            latest = self.provenance.latest(target)
            if latest is None or latest["observation_id"] != observation_id:
                raise SurfaceError(
                    f"{operation} references a stale observation",
                    details={
                        "observation_id": observation_id,
                        "latest_observation_id": (
                            latest["observation_id"] if latest else None
                        ),
                    },
                )
        if not surface_id:
            return
        surface = self.provenance.verify(surface_id, "surface")
        if (
            surface.get("target") != target
            or surface.get("adapter") != adapter_name
            or surface.get("adapter_kind") != adapter_kind
            or surface.get("space") != expected_space
        ):
            raise SurfaceError(
                f"{operation} references a different control surface",
                details={
                    "target": target,
                    "adapter": adapter_name,
                    "space": expected_space,
                },
            )
        adapter = self.router(target).adapters[adapter_name]
        session_id = hashlib.sha256(
            (await adapter.surface_session()).encode()
        ).hexdigest()[:24]
        if surface.get("session_id") != session_id:
            raise SurfaceError(
                f"{operation} references a replaced desktop session",
                details={
                    "expected_session_id": session_id,
                    "actual_session_id": surface.get("session_id"),
                },
            )

    async def operations(
        self, target: str, include_raw: bool = False
    ) -> dict[str, Any]:
        return await self.router(target).operation_routes(include_raw)

    async def perform(
        self, target: str, operation: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        started = time.monotonic()
        spec = OPERATIONS.get(operation)
        if spec is None:
            raise ContractError(
                f"unknown operation: {operation}",
                details={"operation": operation},
            )
        validated = validate_params(operation, spec.params_schema, params or {})
        verification = (
            validated.pop("verify", None) if operation == "element.activate" else None
        )
        verify_typed = bool(
            validated.pop("verify", False) if operation == "keyboard.type" else False
        )
        before = (
            await self.capture(target)
            if verification is not None and "surface_changed" in verification
            else None
        )
        # Dropping from a semantic adapter to the visual fallback changes both
        # the cost and the reliability of an operation, and the switch is only
        # reported after the fact. `allow_fallback: false` refuses it instead.
        allow_fallback = bool(validated.pop("allow_fallback", True))
        fallback = False
        fallback_reason = None
        if operation == "time.sleep":
            await asyncio.sleep(validated["duration_ms"] / 1000)
            adapter_name, adapter_kind = "controller", "control"
            result = {"duration_ms": validated["duration_ms"]}
        else:
            router = self.router(target)
            adapter, fallback = await router.select_operation(operation)
            if fallback and not allow_fallback:
                raise CapabilityError(
                    f"{operation} has no semantic adapter on {target} and "
                    "allow_fallback is false",
                    details={"operation": operation, "adapter": adapter.name},
                )
            adapter_name, adapter_kind = adapter.name, adapter.kind
            await self._validate_surface(
                target, operation, validated, adapter_name, adapter_kind
            )
            prepared = prepare_params(operation, validated)
            if operation == "screen.observe":
                result = await self.observe(target, bool(prepared.get("ocr", False)))
            elif operation == "clipboard.copy":
                result = await self._copy_selection(adapter, prepared)
            elif operation == "element.point":
                result = await self._element_point(target, prepared, allow_fallback)
            elif operation in {"element.find", "element.activate"} and fallback:
                locator = dict(prepared)
                if "name" in locator and "text" not in locator:
                    locator["text"] = locator.pop("name")
                result = await self.locate(
                    target, locator, operation == "element.activate"
                )
            else:
                try:
                    result = await adapter.perform(operation, prepared)
                except ActionError as error:
                    if operation not in {"element.find", "element.activate"}:
                        raise
                    if not allow_fallback:
                        raise
                    fallback_adapter = await router.select_fallback(
                        operation, {adapter.name}
                    )
                    locator = dict(prepared)
                    if "name" in locator and "text" not in locator:
                        locator["text"] = locator.pop("name")
                    result = await self.locate(
                        target, locator, operation == "element.activate"
                    )
                    adapter_name = fallback_adapter.name
                    adapter_kind = fallback_adapter.kind
                    fallback = True
                    fallback_reason = str(error)
        if verify_typed:
            result["verification"] = await self._verify_typed(
                target, str(validated.get("text", ""))
            )
        if verification is not None:
            result["verification"] = await self._verify_activation(
                target, verification, before
            )
        validate_result(operation, spec.result_schema, result)
        backend = {
            "adapter": adapter_name,
            "kind": adapter_kind,
            "fallback": fallback,
        }
        if fallback_reason:
            backend["fallback_reason"] = fallback_reason
        return {
            "api_version": API_VERSION,
            "ok": True,
            "operation": operation,
            "backend": backend,
            "data": result,
            "timing": {
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        }

    async def runtime(
        self,
        target: str,
        action: str,
        platform: str,
        transport: str | None = None,
    ) -> dict[str, Any]:
        router = self.router(target)
        if transport is None:
            selected = await router.select(Capability.SHELL)
        else:
            selected = router.adapters.get(transport)
            if selected is None or Capability.SHELL not in selected.capabilities:
                raise CapabilityError(
                    f"target {target} has no shell transport {transport!r}"
                )
        return await manage_runtime(selected, action, platform)

    async def raw(
        self,
        target: str,
        adapter: str,
        operation: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self.router(target).raw(adapter, operation, params or {})

    async def close(self) -> None:
        results = await asyncio.gather(
            *(router.close() for router in self.routers.values()),
            return_exceptions=True,
        )
        for outcome in results:
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, Exception
            ):
                raise outcome
