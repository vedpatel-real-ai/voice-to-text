"""
pill.py — the floating "Dynamic Island" status pill, and the menu bar icon.

Changed (this pass):
  §19  responsiveness pass driven by user feedback ("wake/sleep should be
       instant"):
       - reveal/dismiss durations cut roughly in half (see PILL_REVEAL_SECONDS
         / PILL_DISMISS_SECONDS below) and the rise distance shortened, so
         both ends of the lifecycle read as snappy rather than leisurely.
       - the pill now respects System Settings > Accessibility > Reduce
         Motion, and exposes a FORCE_INSTANT override: when either is set,
         show()/hide()/resize skip easing entirely and snap directly to the
         target frame/alpha.
       - added an optional self-managed auto-hide: show(text, accent,
         mode=...) will schedule its own hide() after a short, per-mode
         delay (see AUTO_HIDE_SECONDS) instead of relying on the caller to
         poll and time a manual hide(). This is opt-in (only fires when
         `mode` is passed and is a key in AUTO_HIDE_SECONDS) so existing
         call sites that already manage their own hide() timing are
         unaffected. NOTE: if the "slow to sleep" complaint is actually
         caused by a long dwell timer in the caller (not in this file),
         switching that caller over to this parameter is the fix — see the
         note at the bottom of this docstring.
       - text no longer silently clips mid-word: the label is forced to
         single-line mode with tail-truncating ellipsis.
       - the pill now appears on the screen under the mouse cursor rather
         than always AppKit's "main screen", and re-clamps its position if
         the display configuration changes (e.g. an external monitor is
         connected/disconnected) while it's on screen.
       - rapid re-triggers (e.g. a fast double press) no longer stack
         conflicting animations indefinitely: an in-flight auto-hide timer
         is always cancelled before a new show()/hide() call starts one.

       IMPORTANT SCOPE NOTE: this file only owns the pill's *visual*
       lifecycle. It cannot fix latency that lives upstream of it — e.g.
       time spent recording, model inference during Mode.TRANSCRIBING, or
       a hold-timer in the caller that delays *calling* hide() in the first
       place. If the pill still feels slow to disappear after this pass,
       the remaining delay is almost certainly one of those, not the
       animation code below.

  §18  visual overhaul for a production-quality look and feel:
       - the pill is now much smaller/thinner (34pt tall, 150pt default
         width vs. the previous 52pt / 240pt), matching the compact,
         out-of-the-way footprint of the reference-quality "island" apps
         this UI takes inspiration from.
       - the flat, hand-rolled dark fill is replaced with a real
         NSVisualEffectView (HUD-style vibrancy/blur), so the pill reads
         as a native piece of macOS chrome rather than a drawn rectangle,
         plus a hairline border for edge definition on busy wallpapers.
       - reveal/dismiss is now an eased "rise + fade" (position and alpha
         animate together with an ease-out/ease-in curve) instead of a
         plain linear alpha fade, and resizes use the same easing so nothing
         in the pill's lifecycle looks mechanical.
       - bar/text proportions retuned for the smaller frame.
  §17  the pill now resizes dynamically to fit a live preview of the
       transcribed text on Mode.DONE, instead of only ever showing the
       static word "Done" — closer to the Wispr-Flow-style floating
       island the rest of this UI already imitates in spirit.
  §16  fixed the pill never appearing at all: NSPanel defaults
       hidesOnDeactivate to YES (NSWindow defaults to NO). Since this app
       is a background helper that's essentially never the frontmost app,
       the panel was hiding itself before it could ever be seen. Fixed by
       calling panel.setHidesOnDeactivate_(False) at construction.

Previously changed:
  §13  every state now has a distinct visual treatment. Previously a
       failed or empty transcription skipped straight to idle and gave
       *no* feedback at all — indistinguishable from the app being dead.
  §13  a latency readout appears while transcribing takes longer than
       usual (cold model, first-run download), so a slow run doesn't look
       like a hang.
  §9   bar animation is driven at the settings-controlled refresh rate,
       ~30fps by default instead of 12.5fps.
  §10  the generated icon is cached in Application Support, not next to
       __file__, so it survives a frozen/read-only app bundle.

Everything here is main-thread only, which the original got right by
construction and which this version keeps: nothing outside
DictationApp.refresh() touches these objects.
"""

from __future__ import annotations

import math
import os
import time
from typing import Dict, List, Tuple

from config import support_path
from diagnostics import get_logger
from statemachine import Mode

log = get_logger("ui")

# --- geometry -----------------------------------------------------------
# Deliberately small: this should sit unobtrusively near the bottom of the
# screen and never compete for attention with whatever the user is doing.
PILL_MIN_WIDTH = 150
PILL_MAX_WIDTH = 420
PILL_HEIGHT = 34
PILL_BOTTOM_MARGIN = 90

# --- motion ---------------------------------------------------------------
# Kept short on purpose: this is a status indicator the user glances at for
# a fraction of a second, not a hero animation. Longer values here were the
# #1 reported complaint ("takes a while to sleep").
PILL_RISE_DISTANCE = 5       # px the pill "rises" from on first reveal
PILL_REVEAL_SECONDS = 0.16   # rise + fade in
PILL_DISMISS_SECONDS = 0.12  # sink + fade out
PILL_RESIZE_SECONDS = 0.16   # width changes while already visible
EASE_OUT = "easeOut"
EASE_IN = "easeIn"
EASE_IN_OUT = "easeInEaseOut"

# Manual override: set True to skip all easing and snap instantly. This is
# also applied automatically at runtime if the user has System Settings >
# Accessibility > Display > Reduce Motion turned on (see
# _reduce_motion_enabled below) — that check is live, not cached, so it
# reflects the current setting on every call.
FORCE_INSTANT = False

# --- auto-hide --------------------------------------------------------------
# Opt-in: show(text, accent, mode=...) schedules its own hide() this many
# seconds later when `mode` is a key here. Modes not listed (e.g. RECORDING,
# TRANSCRIBING, or mode=None) never auto-hide — the caller is still
# responsible for calling hide() for those. Kept short for the states the
# user actually finishes on, so a normal dictation doesn't linger on screen
# after the text has already been inserted.
AUTO_HIDE_SECONDS: Dict[Mode, float] = {
    Mode.DONE: 0.55,
    Mode.NO_SPEECH: 1.1,
    Mode.CANCELLED: 0.45,
    Mode.FAILED: 1.6,
    Mode.PERMISSION_DENIED: 2.2,
}

# --- bars -----------------------------------------------------------------
BAR_WIDTH = 3
BAR_GAP = 4
BAR_COUNT = 3
BAR_START_X = 14
BAR_MIN_H = 3.0
BAR_MAX_H = 16.0
BAR_IDLE_HEIGHT = 4.0

# --- label ------------------------------------------------------------
LABEL_RIGHT_PADDING = 14
FONT_SIZE = 12.0
# Rough average glyph advance for a 12pt semibold system font. Exact
# metrics would need NSString sizing, which is overkill for a status
# pill — this just needs to be in the right ballpark so the pill doesn't
# clip text or leave huge empty space.
APPROX_CHAR_WIDTH = 6.6

# label, (r, g, b) accent for the bars, whether bars animate
STYLES: Dict[Mode, Tuple[str, Tuple[float, float, float], bool]] = {
    Mode.RECORDING: ("Listening", (1.0, 1.0, 1.0), True),
    Mode.TRANSCRIBING: ("Transcribing…", (0.62, 0.78, 1.0), True),
    Mode.DONE: ("Done", (0.45, 0.87, 0.55), False),
    Mode.NO_SPEECH: ("No speech heard", (0.98, 0.78, 0.35), False),
    Mode.LOW_CONFIDENCE: ("Low confidence — saved", (0.95, 0.62, 0.30), False),
    Mode.FAILED: ("Transcription failed", (1.0, 0.45, 0.42), False),
    Mode.PERMISSION_DENIED: ("Grant Accessibility", (1.0, 0.45, 0.42), False),
    Mode.CANCELLED: ("Cancelled", (0.7, 0.7, 0.74), False),
}


def icon_path() -> str:
    # A new filename invalidates the old cached black non-template asset.
    return support_path("menubar-icon-white-Template.png")


def _generate_icon(path: str) -> None:
    """Three ascending rounded bars, echoing the pill's waveform motif.

    White alpha-mask artwork. VoiceToText marks it as an NSImage template,
    allowing macOS to preserve the intended bright menu-bar appearance while
    retaining contrast when the system changes menu-bar style.
    """
    from PIL import Image, ImageDraw

    size = 44  # 22pt @2x
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bar_w, gap = 6, 5
    heights = [14, 27, 20]
    total_w = len(heights) * bar_w + (len(heights) - 1) * gap
    start_x = (size - total_w) // 2
    base_y = size - 9

    for i, h in enumerate(heights):
        x0 = start_x + i * (bar_w + gap)
        draw.rounded_rectangle(
            [x0, base_y - h, x0 + bar_w, base_y],
            radius=bar_w // 2,
            fill=(255, 255, 255, 255),
        )
    img.save(path)


def ensure_icon():
    path = icon_path()
    if os.path.exists(path):
        return path
    try:
        _generate_icon(path)
        return path
    except Exception as exc:
        log.warning("icon generation failed: %s", exc)
        return None


def _reduce_motion_enabled() -> bool:
    """Live check of System Settings > Accessibility > Display > Reduce
    Motion. Checked on every animation rather than cached once, since the
    user can toggle it at any time and there's no reason for this app to
    be out of sync with the rest of the system."""
    try:
        from AppKit import NSWorkspace
        return bool(
            NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
        )
    except Exception:
        return False


class FloatingPill:
    """Borderless, click-through, always-on-top status pill.

    Width is dynamic: it grows to fit whatever text is showing (up to
    PILL_MAX_WIDTH) and animates the resize, so a short state label and a
    full transcribed sentence both look intentional rather than either
    clipped or swimming in empty space.

    Visually this is a small NSVisualEffectView (HUD-material blur) with a
    hairline border, a thin waveform of bar layers, and a text label — no
    hand-drawn fill. Reveal/dismiss animate position and alpha together
    (an eased "rise" in, "sink" out) rather than a plain fade.
    """

    def __init__(self):
        self.available = False
        self.visible = False
        self._accent = (1.0, 1.0, 1.0)
        self._width = PILL_MIN_WIDTH
        self._autohide_timer = None
        self._visual_generation = 0
        try:
            self._build()
            self.available = True
            log.info("floating pill ready")
        except Exception:
            log.exception("floating pill unavailable — continuing without it")

    def _build(self) -> None:
        from AppKit import (
            NSPanel, NSScreen, NSColor, NSTextField, NSFont, NSEvent,
            NSVisualEffectView, NSVisualEffectMaterialHUDWindow,
            NSVisualEffectBlendingModeBehindWindow, NSVisualEffectStateActive,
            NSBackingStoreBuffered, NSWindowStyleMaskBorderless,
            NSWindowStyleMaskNonactivatingPanel,
            NSStatusWindowLevel, NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary, NSFontWeightSemibold,
            NSApplicationDidChangeScreenParametersNotification,
            NSLineBreakByTruncatingTail,
        )
        from Foundation import NSMakeRect, NSNotificationCenter
        from Quartz import CALayer, CAMediaTimingFunction

        self._NSMakeRect = NSMakeRect
        self._NSColor = NSColor
        self._NSScreen = NSScreen
        self._NSEvent = NSEvent
        self._CATiming = CAMediaTimingFunction

        screen = self._screen_frame()
        x = screen.origin.x + (screen.size.width - self._width) / 2.0
        y = screen.origin.y + PILL_BOTTOM_MARGIN
        rect = NSMakeRect(x, y, self._width, PILL_HEIGHT)

        # Reposition (don't animate — a re-layout on monitor changes should
        # be silent) if a display is connected/disconnected while visible,
        # so the pill can't end up centered on a screen that no longer
        # exists or off to one side of a new resolution.
        #
        # FloatingPill is a plain Python object, not an NSObject subclass,
        # so it can't be a selector-based NSNotificationCenter observer —
        # `addObserver:selector:name:object:` requires a real Objective-C
        # target and would silently no-op here. The block-based API has no
        # such requirement, which is why it's used instead. The returned
        # token is intentionally not un-registered: the pill is a
        # process-lifetime singleton, so there's no meaningful "later" in
        # which to remove it.
        try:
            NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
                NSApplicationDidChangeScreenParametersNotification, None, None,
                lambda note: self._on_screen_parameters_changed(),
            )
        except Exception:
            log.exception("pill: could not register for screen-change notifications")

        style_mask = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style_mask, NSBackingStoreBuffered, False
        )
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setIgnoresMouseEvents_(True)
        panel.setFloatingPanel_(True)
        # NSPanel defaults hidesOnDeactivate to YES (unlike NSWindow, where
        # it's NO). This app is a background helper that's essentially
        # never the frontmost app, so without this the panel hides itself
        # before it can ever be seen.
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        panel.setAlphaValue_(0.0)

        # Real vibrancy instead of a flat drawn fill — reads as native
        # macOS chrome and adapts to whatever is behind it. HUD material
        # is always dark-styled, so the white/colored text stays legible
        # regardless of the desktop content or system light/dark mode.
        effect = NSVisualEffectView.alloc().initWithFrame_(
            NSMakeRect(0, 0, self._width, PILL_HEIGHT)
        )
        effect.setMaterial_(NSVisualEffectMaterialHUDWindow)
        effect.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        effect.setState_(NSVisualEffectStateActive)
        effect.setWantsLayer_(True)
        shell = effect.layer()
        shell.setCornerRadius_(PILL_HEIGHT / 2.0)
        shell.setMasksToBounds_(True)
        shell.setBorderWidth_(0.75)
        shell.setBorderColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.14).CGColor()
        )

        # A subtle dark tint over the blur guarantees consistent contrast
        # for the text/bars no matter what's behind the pill (bright
        # wallpaper, a white document window, etc).
        tint = CALayer.layer()
        tint.setFrame_(NSMakeRect(0, 0, self._width, PILL_HEIGHT))
        tint.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.30).CGColor()
        )
        shell.addSublayer_(tint)

        self.bars: List[Tuple[object, float]] = []
        for i in range(BAR_COUNT):
            bar = CALayer.layer()
            bar.setBackgroundColor_(NSColor.whiteColor().CGColor())
            bar.setCornerRadius_(BAR_WIDTH / 2.0)
            bx = BAR_START_X + i * (BAR_WIDTH + BAR_GAP)
            bar.setFrame_(NSMakeRect(bx, PILL_HEIGHT / 2.0 - 2, BAR_WIDTH, BAR_IDLE_HEIGHT))
            shell.addSublayer_(bar)
            self.bars.append((bar, bx))

        label_x = self._label_x()
        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(label_x, 0, self._width - label_x - LABEL_RIGHT_PADDING, PILL_HEIGHT)
        )
        label.setStringValue_("Listening")
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setTextColor_(NSColor.whiteColor())
        try:
            label.setFont_(NSFont.systemFontOfSize_weight_(FONT_SIZE, NSFontWeightSemibold))
        except Exception:
            label.setFont_(NSFont.systemFontOfSize_(FONT_SIZE))
        try:
            # Without this, a transcription that overshoots PILL_MAX_WIDTH
            # (a long run-on sentence, or a script where APPROX_CHAR_WIDTH
            # under-estimates — e.g. mixed Latin/Gujarati/Devanagari text)
            # clips mid-character with no visual cue. Forcing single-line +
            # tail-truncation gives it a clean "…" instead.
            label.setUsesSingleLineMode_(True)
            label.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        except Exception:
            log.exception("pill: could not configure label truncation")
        effect.addSubview_(label)

        panel.setContentView_(effect)
        panel.orderFrontRegardless()

        self.panel = panel
        self.effect = effect
        self.shell = shell
        self.tint = tint
        self.label = label
        log.info(
            "pill built: screen=%.0fx%.0f rect=(%.0f,%.0f,%.0f,%.0f) level=%s",
            screen.size.width, screen.size.height,
            x, y, self._width, PILL_HEIGHT,
            panel.level(),
        )

    @staticmethod
    def _label_x() -> float:
        return BAR_START_X + BAR_COUNT * (BAR_WIDTH + BAR_GAP) + 8

    def _width_for_text(self, text: str) -> float:
        base = self._label_x() + LABEL_RIGHT_PADDING
        needed = base + len(text) * APPROX_CHAR_WIDTH + 12
        return max(PILL_MIN_WIDTH, min(PILL_MAX_WIDTH, needed))

    def _screen_frame(self):
        """The frame of the screen the user is actually looking at, not
        just AppKit's notion of "main screen" (which is really "the screen
        with the key window / menu bar", and can easily be the wrong one
        on a multi-monitor setup where the pill's host app has no window
        of its own). Falls back to mainScreen if anything goes wrong."""
        try:
            mouse = self._NSEvent.mouseLocation()
            for s in self._NSScreen.screens():
                f = s.frame()
                if (f.origin.x <= mouse.x <= f.origin.x + f.size.width
                        and f.origin.y <= mouse.y <= f.origin.y + f.size.height):
                    return f
        except Exception:
            log.exception("pill._screen_frame: cursor-screen lookup failed, using mainScreen")
        return self._NSScreen.mainScreen().frame()

    def _on_screen_parameters_changed(self) -> None:
        """Fires when a display is connected/disconnected or resolution
        changes. Snaps (no animation — a config change isn't a state
        transition worth animating) to a valid position on whatever screen
        is now under the cursor, so the pill can't be stranded off-screen
        or centered on geometry that no longer exists."""
        if not self.visible:
            return
        try:
            rect = self._rect_for(self._width, PILL_BOTTOM_MARGIN)
            self.panel.setFrame_display_(rect, True)
        except Exception:
            log.exception("pill: failed to reposition after screen change")

    def _rect_for(self, width: float, bottom_margin: float) -> object:
        """`bottom_margin` is relative to the bottom edge of whichever
        screen the cursor is currently on — not an absolute global y — so
        this places the pill correctly even on a secondary display whose
        origin isn't (0, 0) in the global coordinate space."""
        screen = self._screen_frame()
        x = screen.origin.x + (screen.size.width - width) / 2.0
        y = screen.origin.y + bottom_margin
        return self._NSMakeRect(x, y, width, PILL_HEIGHT)

    def _sync_content_frames(self, width: float) -> None:
        """Keep the effect view / tint layer / label in sync with the
        panel's current width. Called after any frame change."""
        try:
            self.effect.setFrame_(self._NSMakeRect(0, 0, width, PILL_HEIGHT))
            self.tint.setFrame_(self._NSMakeRect(0, 0, width, PILL_HEIGHT))
            label_x = self._label_x()
            self.label.setFrame_(
                self._NSMakeRect(label_x, 0, width - label_x - LABEL_RIGHT_PADDING, PILL_HEIGHT)
            )
        except Exception:
            log.exception("pill: failed to sync effect/tint/label frames")

    def _animate(self, duration: float, easing: str, frame=None, alpha=None) -> None:
        """Animate frame and/or alpha together in one eased group — unless
        FORCE_INSTANT is set or the user has Reduce Motion on, in which
        case this just snaps straight to the target with no easing at
        all."""
        if FORCE_INSTANT or _reduce_motion_enabled():
            try:
                if frame is not None:
                    self.panel.setFrame_display_(frame, True)
                if alpha is not None:
                    self.panel.setAlphaValue_(alpha)
            except Exception:
                log.exception("pill._animate: instant path failed")
            return
        try:
            from AppKit import NSAnimationContext

            NSAnimationContext.beginGrouping()
            ctx = NSAnimationContext.currentContext()
            ctx.setDuration_(duration)
            try:
                ctx.setTimingFunction_(self._CATiming.functionWithName_(easing))
            except Exception:
                pass
            animator = self.panel.animator()
            if frame is not None:
                animator.setFrame_display_(frame, True)
            if alpha is not None:
                animator.setAlphaValue_(alpha)
            NSAnimationContext.endGrouping()
        except Exception:
            log.exception("pill._animate: animated path failed, falling back")
            try:
                if frame is not None:
                    self.panel.setFrame_display_(frame, True)
                if alpha is not None:
                    self.panel.setAlphaValue_(alpha)
            except Exception:
                log.exception("pill._animate: non-animated fallback also failed")

    def _resize(self, width: float) -> None:
        """Animate the panel to a new width, re-centered on screen, while
        already visible."""
        if abs(width - self._width) < 1.0:
            return
        new_rect = self._rect_for(width, PILL_BOTTOM_MARGIN)
        self._animate(PILL_RESIZE_SECONDS, EASE_IN_OUT, frame=new_rect)
        self._width = width
        self._sync_content_frames(width)

    def _set_accent(self, rgb: Tuple[float, float, float]) -> None:
        if rgb == self._accent:
            return
        self._accent = rgb
        try:
            color = self._NSColor.colorWithCalibratedRed_green_blue_alpha_(
                rgb[0], rgb[1], rgb[2], 1.0
            ).CGColor()
            for bar, _ in self.bars:
                bar.setBackgroundColor_(color)
            self.label.setTextColor_(
                self._NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    rgb[0], rgb[1], rgb[2], 1.0
                )
            )
        except Exception:
            pass

    def _cancel_pending_autohide(self) -> None:
        if self._autohide_timer is not None:
            try:
                self._autohide_timer.invalidate()
            except Exception:
                pass
            self._autohide_timer = None

    def _schedule_autohide(self, delay: float) -> None:
        try:
            from Foundation import NSTimer
            self._autohide_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                delay, False, lambda _timer: self.hide()
            )
        except Exception:
            # Block-based NSTimer needs a reasonably current PyObjC/macOS;
            # if it's unavailable, fail quietly rather than crash the
            # whole pill — the caller's own hide() logic (if any) still
            # applies, we just lose the self-managed convenience.
            log.exception("pill: could not schedule auto-hide timer")

    def show(
        self,
        text: str,
        accent: Tuple[float, float, float],
        mode: Mode | None = None,
    ) -> None:
        """Show `text` with `accent`. If `mode` is passed and is a key in
        AUTO_HIDE_SECONDS, the pill schedules its own hide() after that
        many seconds — the caller doesn't need to manage a dwell timer for
        terminal states. Omit `mode` (or pass one not in that table, e.g.
        RECORDING/TRANSCRIBING) to keep the previous behavior of the
        caller being fully responsible for calling hide()."""
        if not self.available:
            return

        self._visual_generation += 1
        self._cancel_pending_autohide()

        target_width = self._width_for_text(text)
        self.label.setStringValue_(text)
        self._set_accent(accent)

        if self.visible:
            # A previous hide animation may still be interpolating alpha.
            # A direct property write supersedes it before any resize, so
            # stale dismissal work cannot make a new recording disappear.
            self.panel.setAlphaValue_(1.0)
            # Already on screen — just resize in place if needed.
            self._resize(target_width)
        else:
            self.visible = True
            self._width = target_width
            try:
                # Prime the panel at its target width, slightly below its
                # resting position, while still fully transparent — so the
                # reveal reads as one continuous "rise into place" rather
                # than a resize-then-fade.
                start_rect = self._rect_for(target_width, PILL_BOTTOM_MARGIN - PILL_RISE_DISTANCE)
                end_rect = self._rect_for(target_width, PILL_BOTTOM_MARGIN)
                self.panel.setFrame_display_(start_rect, False)
                self._sync_content_frames(target_width)
            except Exception:
                log.exception("pill.show: failed to prime initial frame")
                end_rect = self._rect_for(target_width, PILL_BOTTOM_MARGIN)

            # Visibility feedback is immediate; only the small positional
            # rise is animated. This also deterministically cancels a stale
            # fade-out from the preceding session.
            self.panel.setAlphaValue_(1.0)
            self._animate(PILL_REVEAL_SECONDS, EASE_OUT, frame=end_rect)

            try:
                log.info(
                    "pill shown: alpha=%.2f isVisible=%s frame=%s occlusionState=%s",
                    self.panel.alphaValue(),
                    self.panel.isVisible(),
                    self.panel.frame(),
                    self.panel.occlusionState(),
                )
            except Exception:
                log.exception("pill.show: state probe itself failed")

        delay = AUTO_HIDE_SECONDS.get(mode) if mode is not None else None
        if delay is not None:
            self._schedule_autohide(delay)

    def hide(self) -> None:
        self._cancel_pending_autohide()
        if not self.available or not self.visible:
            return
        self._visual_generation += 1
        self.visible = False
        try:
            f = self.panel.frame()
            sink_rect = self._NSMakeRect(
                f.origin.x, f.origin.y - PILL_RISE_DISTANCE, f.size.width, f.size.height
            )
        except Exception:
            log.exception("pill.hide: could not read current frame, fading only")
            sink_rect = None
        self._animate(PILL_DISMISS_SECONDS, EASE_IN, frame=sink_rect, alpha=0.0)

    def update_bars(self, mode: Mode, level: float) -> None:
        if not self.available or not self.visible:
            return
        style = STYLES.get(mode)
        animate = bool(style and style[2] and not _reduce_motion_enabled())
        now = time.time()
        span = BAR_MAX_H - BAR_MIN_H
        for i, (bar, bx) in enumerate(self.bars):
            if not animate:
                h = BAR_IDLE_HEIGHT
            elif mode is Mode.RECORDING:
                phase = now * 6 + i * 1.3
                wobble = (math.sin(phase) + 1) / 2.0
                h = BAR_MIN_H + level * span * (0.35 + 0.65 * wobble)
            else:
                phase = now * 4 + i * 0.9
                h = BAR_MIN_H + (math.sin(phase) + 1) / 2.0 * span * 0.55
            h = max(BAR_MIN_H, min(BAR_MAX_H, h))
            y = (PILL_HEIGHT - h) / 2.0
            bar.setFrame_(self._NSMakeRect(bx, y, BAR_WIDTH, h))


class NullPill:
    """Same interface, does nothing. Used when the pill is disabled or
    failed to build, so no call site needs a None check."""

    available = False
    visible = False

    def show(self, text: str, accent, mode=None) -> None:
        pass

    def hide(self) -> None:
        pass

    def update_bars(self, mode, level) -> None:
        pass
