#!/usr/bin/env python3
"""Tapo ONVIF detection listener.

Subscribes to each camera's ONVIF event stream and turns the raw notifications
into clean signals, as JSON lines on stdout:

  start    a detection (motion, person, ...) became active
  end      that detection ended (camera said so, or went quiet)
  settled  everything on a camera has been quiet for --settle seconds; fetch now
  sweep    periodic safety-net tick (--sweep-min), for events the camera never sent
  hook     result of the --on-settled command
  status   a camera listener changed state (listening / unavailable)

--on-settled runs a command when a camera settles (or sweeps), one run at a time
per camera. Placeholders: {cam} {ip} {date} {date_end} {start} {end} {types}
(start/end are UTC ISO and empty for a sweep; the same values are exported as
TAPO_CAM, TAPO_IP, ...; {date}/{date_end} use --timezone). Runs that overlap are
merged into one follow-up run, and a sweep wins a merge because a full scan covers
any event window. A run that exits 75 (the downloader's "clip still recording /
camera busy") is retried after --retry-delay, up to --retry-max times.

With no arguments it reads ~/.tapo-downloader/config.json, the same file tapo-downloader.py
uses (cameras come from ip_addresses as cam1..cam4 by slot; username, password, timezone,
settle_seconds and hook_timeout_minutes are picked up too; sweep_minutes only when mode is
"sweep"). Without --on-settled it just prints the JSON lines, which is handy for testing.
Command-line options override the file; --cam replaces the file's cameras.

Two logins are involved. ONVIF uses the camera account (Tapo app > Advanced Settings > Camera
Account): --user / --password / TAPO_PASSWORD, or onvif_username / onvif_password in the config.
The polling fallback logs in through pytapo with the cloud credentials tapo-downloader uses:
--tapo-user / --tapo-password / TAPO_CLOUD_PASSWORD, or username / password in the config. If only
one set is given it is used for both. The startup log says which source each login came from.

Some models (seen on the C320WS and TC43) accept the ONVIF subscription but reset every pull.
For those the listener falls back to polling the camera's own detection log through pytapo, the
same log tapo-downloader uses to label clips, and feeds the same start/end/settled signals.
Force it for a camera with --poll NAME (config: poll_cameras); --poll-interval 0 turns it off.

Needs: pip install onvif-zeep (and pytapo for the polling fallback)
"""
import argparse
import asyncio
import inspect
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ONVIF_PORT = 2020
PULLPOINT_NS = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
SUBMGR_BINDING = "{http://www.onvif.org/ver10/events/wsdl}SubscriptionManagerBinding"
WSN_NS = "{http://docs.oasis-open.org/wsn/b-2}"
WSTOP_TOPIC_ATTR = "{http://docs.oasis-open.org/wsn/t-1}topic"
SUB_LIFETIME_S = 60
SUB_RECYCLE_S = 50
UNAVAILABLE_RETRY_S = 600
MAX_CLOCK_SKEW_S = 120
EXIT_DEFERRED = 75      # hook exit code meaning "try again later" (see tapo-downloader.py)
LOCAL_TZ = None         # tzinfo for {date}/{date_end}; None = the system timezone
DEFAULT_POLL_INTERVAL = 30.0
POLL_LOOKBACK_S = 900   # how far back each poll of the camera's detection log looks
POLL_AUTH_RETRY_S = 1800   # after a rejected login wait this long; every failed login counts toward a lockout
POLL_AUTH_MAX_FAILURES = 2  # then stop polling that camera until the listener is restarted

# alarm_type codes from the camera's detection log (same table tapo-downloader uses for clip names).
POLL_DETECTION_NAMES = {
    2: "motion", 3: "tamper", 4: "line_crossing", 5: "area_intrusion", 6: "person", 7: "baby_cry",
    8: "vehicle", 9: "pet", 11: "bark", 12: "meow", 13: "glass_break", 14: "smoke",
    15: "package_delivered", 16: "package_picked_up", 20: "face", 32: "loitering",
}
# Pull settings tried in order for a camera that drops the first pull: (device port?, timeout, message limit).
PULL_ATTEMPTS = ((True, "PT10S", 100), (True, "PT5S", 10), (True, "PT1S", 1), (False, "PT10S", 100))

# Topic tail -> detection id (same ids tapo-streamer uses for clip names).
DETECTION_TYPES = {
    "CellMotionDetector/Motion": "motion",
    "PeopleDetector/People": "person",
    "IntrusionDetector/Intrusion": "area_intrusion",
    "LineCrossDetector/LineCross": "line_crossing",
    "TamperDetector/Tamper": "tamper",
    "TPSmartEventDetector/TPSmartEvent": "smart_event",
}

DOWNLOADER_CONFIG = os.path.join(os.path.expanduser("~"), ".tapo-downloader", "config.json")

log = logging.getLogger("tapo-events")


def utc_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_date(dt):
    return dt.astimezone(LOCAL_TZ).strftime("%Y%m%d")


def classify(topic, data):
    """Return (detection id, active, flagged) for a notification. flagged is True when the camera
    reports an explicit Is* state, so it will also say when the detection ends; one without an Is*
    flag is a pulse (active, unflagged) and is ended by the stale timeout instead."""
    tail = topic.split(":", 1)[-1]
    if tail.startswith("RuleEngine/"):
        tail = tail[len("RuleEngine/"):]
    detection = DETECTION_TYPES.get(tail) or tail.rsplit("/", 1)[-1].lower()
    for name, value in data.items():
        if str(name).lower().startswith("is"):
            return detection, str(value).strip().lower() == "true", True
    return detection, True, False


def event_time(utc_str, now):
    """Camera UTC time if it parses and is sane, else the local receive time."""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if abs((dt - now).total_seconds()) <= MAX_CLOCK_SKEW_S:
            return dt, "camera"
    except (AttributeError, ValueError):
        pass
    return now, "local"


class Tracker:
    """Per camera and detection start/end state, plus a settle timer per camera burst."""

    def __init__(self, emit, stale_s, settle_s, stale_flagged_s=300.0):
        self.emit = emit
        self.stale_s = stale_s                  # pulse detections: no Is* flag, ended by silence
        self.stale_flagged_s = stale_flagged_s  # flagged detections: the camera says when they end, so
        self.settle_s = settle_s                # this is only a backstop (lost "false", dropped link)
        self.lock = threading.RLock()
        self.active = {}   # (cam, detection) -> {"start", "last_seen", "last_utc"}
        self.bursts = {}   # cam -> {"start", "end", "types", "settle_at"}

    def feed(self, cam, detection, active, when, data=None, flagged=True):
        now = time.monotonic()
        with self.lock:
            state = self.active.get((cam, detection))
            if active:
                if state is None:
                    self.active[(cam, detection)] = {"start": when, "last_seen": now, "last_utc": when, "flagged": flagged}
                    burst = self.bursts.setdefault(cam, {"start": when, "end": None, "types": set(), "settle_at": None})
                    burst["types"].add(detection)
                    burst["settle_at"] = None
                    self.emit({"event": "start", "cam": cam, "detection": detection, "utc": utc_iso(when), "data": data or {}})
                else:
                    state["last_seen"] = now
                    state["last_utc"] = when
            elif state is not None:
                self._end(cam, detection, when, "camera", now)

    def _end(self, cam, detection, when, reason, now):
        state = self.active.pop((cam, detection))
        burst = self.bursts[cam]
        burst["end"] = when
        if not any(c == cam for c, _ in self.active):
            burst["settle_at"] = now + self.settle_s
        self.emit({"event": "end", "cam": cam, "detection": detection, "utc": utc_iso(when), "reason": reason,
                   "seconds": round((when - state["start"]).total_seconds(), 1)})

    def tick(self):
        now = time.monotonic()
        with self.lock:
            for (cam, detection), state in list(self.active.items()):
                limit = self.stale_flagged_s if state["flagged"] else self.stale_s
                if now - state["last_seen"] > limit:
                    self._end(cam, detection, state["last_utc"], "timeout", now)
            for cam, burst in list(self.bursts.items()):
                if burst["settle_at"] is not None and now >= burst["settle_at"]:
                    del self.bursts[cam]
                    self.emit({"event": "settled", "cam": cam, "start": utc_iso(burst["start"]), "end": utc_iso(burst["end"]),
                               "types": sorted(burst["types"])})


class HookRunner:
    """Runs the --on-settled command, one run at a time per camera; overlapping requests merge.
    A run that exits EXIT_DEFERRED is retried, and close() stops anything still running."""

    PLACEHOLDERS = ("cam", "ip", "date", "date_end", "start", "end", "types")

    def __init__(self, template, timeout_s, emit, retry_delay_s=120.0, retry_max=3):
        self.template = shlex.split(template)
        self.timeout_s = timeout_s
        self.emit = emit
        self.retry_delay_s = retry_delay_s
        self.retry_max = retry_max
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.procs = set()
        self.state = {}   # cam -> {"running", "pending"}
        self.render(dict.fromkeys(self.PLACEHOLDERS, ""))   # fail early on a bad placeholder

    def render(self, values):
        return [arg.format(**values) for arg in self.template]

    @staticmethod
    def merge(a, b):
        if a is None:
            return b
        if b is None:
            return a
        types = a["types"] | b["types"]
        if a["start"] is None or b["start"] is None:
            # A sweep is a full scan, so it already covers any event window.
            return {"start": None, "end": None, "types": types}
        return {"start": min(a["start"], b["start"]), "end": max(a["end"], b["end"]), "types": types}

    def submit(self, cam, ip, window):
        if self.closed.is_set():
            return
        with self.lock:
            state = self.state.setdefault(cam, {"running": False, "pending": None})
            if state["running"]:
                state["pending"] = self.merge(state["pending"], window)
                return
            state["running"] = True
        threading.Thread(target=self._run, args=(cam, ip, window), name=f"hook-{cam}", daemon=True).start()

    def close(self):
        """Stop accepting work and terminate any hook that is still running."""
        self.closed.set()
        with self.lock:
            procs = list(self.procs)
        for proc in procs:
            self._stop_proc(proc)

    @staticmethod
    def _stop_proc(proc):
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _run(self, cam, ip, window):
        attempt = 0
        while True:
            rc = self._exec(cam, ip, window, attempt)
            with self.lock:
                state = self.state[cam]
                pending, state["pending"] = state["pending"], None
                retry = rc == EXIT_DEFERRED and attempt < self.retry_max and not self.closed.is_set()
                if not retry and (pending is None or self.closed.is_set()):
                    state["running"] = False
                    return
            if retry:
                attempt += 1
                log.info("hook for %s deferred, retry %d/%d in %.0fs", cam, attempt, self.retry_max, self.retry_delay_s)
                if self.closed.wait(self.retry_delay_s):
                    with self.lock:
                        self.state[cam]["running"] = False
                    return
                window = self.merge(window, pending)   # what was deferred plus anything that arrived meanwhile
            else:
                attempt = 0
                window = pending

    def _exec(self, cam, ip, window, attempt=0):
        start, end = window["start"], window["end"]
        values = {
            "cam": cam, "ip": ip,
            "date": local_date(start or datetime.now(timezone.utc)),
            "date_end": local_date(end or start or datetime.now(timezone.utc)),
            "start": utc_iso(start) if start else "", "end": utc_iso(end) if end else "",
            "types": ",".join(sorted(window["types"])),
        }
        env = dict(os.environ, **{f"TAPO_{k.upper()}": v for k, v in values.items()})
        args = self.render(values)
        log.info("hook for %s: %s", cam, shlex.join(args))
        rc = -1
        try:
            proc = subprocess.Popen(args, env=env)
            with self.lock:
                self.procs.add(proc)
            try:
                rc = proc.wait(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                log.error("hook for %s timed out after %.0fs, stopping it", cam, self.timeout_s)
                self._stop_proc(proc)
            finally:
                with self.lock:
                    self.procs.discard(proc)
        except Exception as e:
            log.error("hook for %s failed: %s", cam, e)
        self.emit({"event": "hook", "cam": cam, "rc": rc, "attempt": attempt})
        return rc


class CameraListener(threading.Thread):
    """One camera's ONVIF pull-point subscription, reconnecting and recycling as needed."""

    def __init__(self, name, ip, username, password, on_notification, status, stop, fallback=None):
        super().__init__(name=f"onvif-{name}", daemon=True)
        self.fallback = fallback   # fallback(name, ip) -> True if something else now covers this camera
        self.cam_name = name
        self.ip = ip
        self.username = username
        self.password = password
        self.on_notification = on_notification
        self.status = status
        self.stop_event = stop

    class PullUnavailable(Exception):
        pass

    @staticmethod
    def host(ip):
        return ip.split("/", 1)[0].split(":", 1)[0].strip()

    def via_device_port(self, addr):
        # Same path on the camera's ONVIF port; Tapo advertises a high port that is often unreachable.
        return urlsplit(addr)._replace(netloc=f"{self.host(self.ip)}:{ONVIF_PORT}").geturl()

    def pull_addr(self, adv_addr, use_device_port):
        return self.via_device_port(adv_addr) if use_device_port else adv_addr

    def unsubscribe(self, events, adv_addr):
        # Best effort: the camera only holds a few subscriptions, so free ours.
        if not events or not adv_addr:
            return
        for addr in dict.fromkeys((self.via_device_port(adv_addr), adv_addr)):
            try:
                events.zeep_client.create_service(SUBMGR_BINDING, addr).Unsubscribe()
                return
            except Exception as e:
                log.info("%s: unsubscribe of %s failed: %s", self.cam_name, addr, e)

    def new_subscription(self, events):
        try:
            sub = events.CreatePullPointSubscription({"InitialTerminationTime": f"PT{SUB_LIFETIME_S}S"})
        except Exception as e:
            raise RuntimeError(f"camera refused a new subscription ({e}), stale ones may be using up its limit") from e
        addr = sub.SubscriptionReference.Address
        addr = getattr(addr, "_value_1", addr)
        log.info("%s: subscription at %s", self.cam_name, addr)
        return addr

    def pull_service(self, cam, addr):
        from zeep.plugins import HistoryPlugin
        cam.xaddrs[PULLPOINT_NS] = addr
        pull = cam.create_pullpoint_service()
        # zeep drops the Topic text of a notification, so the raw reply is read instead.
        history = HistoryPlugin(maxlen=1)
        pull.zeep_client.plugins.append(history)
        return pull, history

    @staticmethod
    def raw_notifications(history):
        try:
            envelope = history.last_received["envelope"]
        except (IndexError, KeyError, TypeError):
            return []
        return list(envelope.iter(WSN_NS + "NotificationMessage"))

    @staticmethod
    def parse_notification(el):
        topic = ""
        operation = utc = None
        data = {}
        for node in el.iter():
            if not isinstance(node.tag, str):
                continue
            if node.tag.endswith("}Topic"):
                topic = (node.text or "").strip()
            elif node.tag.endswith("}Message") and node.get("UtcTime"):
                utc = node.get("UtcTime")
                operation = node.get("PropertyOperation")
            elif node.tag.endswith("}SimpleItem"):
                parent = node.getparent()
                if parent is not None and str(parent.tag).endswith("}Data"):
                    data[node.get("Name")] = node.get("Value")
        return topic, operation, utc, data

    @staticmethod
    def is_dropped_poll(err):
        # The camera closes the connection instead of answering an empty long poll.
        text = str(err)
        return "RemoteDisconnected" in text or "Connection aborted" in text

    def drain(self, pull, history, limit):
        """One last short pull on a subscription that is about to be replaced, so nothing it queued is lost."""
        try:
            pull.PullMessages({"Timeout": "PT1S", "MessageLimit": limit})
        except Exception as e:
            log.debug("%s: final pull of the old subscription failed: %s", self.cam_name, e)
            return
        for el in self.raw_notifications(history):
            self.on_notification(self.cam_name, *self.parse_notification(el))

    def log_device(self, cam, events):
        try:
            info = cam.devicemgmt.GetDeviceInformation()
            log.info("%s: model=%s firmware=%s", self.cam_name, info.Model, info.FirmwareVersion)
        except Exception as e:
            log.info("%s: could not read device information: %s", self.cam_name, e)
        try:
            from lxml import etree
            found = []

            def walk(el, path):
                if not isinstance(el.tag, str):
                    return
                path = f"{path}/{etree.QName(el).localname}" if path else etree.QName(el).localname
                if el.get(WSTOP_TOPIC_ATTR) == "true":
                    found.append(path)
                for child in el:
                    walk(child, path)

            for el in (events.GetEventProperties().TopicSet._value_1 or []):
                walk(el, "")
            log.info("%s: camera advertises topics %s", self.cam_name, sorted(found))
        except Exception as e:
            log.info("%s: could not read event properties: %s", self.cam_name, e)

    def run(self):
        backoff = 5.0
        attempt = 0
        worked = False
        stop = self.stop_event
        while not stop.is_set():
            events = None
            addr = None
            next_adv = None   # replacement subscription, opened before the old one is released
            try:
                from onvif import ONVIFCamera
                from zeep.transports import Transport
                cam = ONVIFCamera(self.host(self.ip), ONVIF_PORT, self.username, self.password, adjust_time=True,
                                  transport=Transport(timeout=10, operation_timeout=25))
                # ONVIFCamera() opens a pull point of its own that is never used; free it.
                self.unsubscribe(getattr(cam, "event", None), cam.xaddrs.get(PULLPOINT_NS))
                events = cam.create_events_service()
                self.log_device(cam, events)
                log.info("%s: listening", self.cam_name)
                self.status(self.cam_name, "listening")
                while not stop.is_set():
                    adv_addr = next_adv or self.new_subscription(events)
                    next_adv = None
                    use_device_port, pull_timeout, pull_limit = PULL_ATTEMPTS[attempt]
                    addr = self.pull_addr(adv_addr, use_device_port)
                    pull, history = self.pull_service(cam, addr)
                    first_pull = True
                    dropped = 0
                    cycle_end = time.time() + SUB_RECYCLE_S
                    while time.time() < cycle_end and not stop.is_set():
                        t0 = time.time()
                        try:
                            pull.PullMessages({"Timeout": pull_timeout, "MessageLimit": pull_limit})
                        except Exception as e:
                            elapsed = time.time() - t0
                            if self.is_dropped_poll(e) and elapsed >= 3.0 and dropped < 5:
                                dropped += 1  # camera closed an empty long poll, the subscription is fine
                                first_pull = False
                                worked = True
                                stop.wait(0.5)
                                continue
                            if not first_pull:
                                raise
                            if attempt + 1 >= len(PULL_ATTEMPTS):
                                raise self.PullUnavailable(
                                    f"pull point unreachable with every setting tried (last error: {e}); "
                                    f"if a firewall sits in front of the camera, port {urlsplit(adv_addr).port} must be reachable"
                                ) from e
                            attempt += 1
                            use_device_port, pull_timeout, pull_limit = PULL_ATTEMPTS[attempt]
                            addr = self.pull_addr(adv_addr, use_device_port)
                            log.warning("%s: pull failed after %.2fs (%s), trying %s timeout=%s limit=%s",
                                        self.cam_name, elapsed, e, addr, pull_timeout, pull_limit)
                            pull, history = self.pull_service(cam, addr)
                            continue
                        first_pull = False
                        worked = True
                        dropped = 0
                        for el in self.raw_notifications(history):
                            self.on_notification(self.cam_name, *self.parse_notification(el))
                        backoff = 5.0
                        if time.time() - t0 < 0.5:
                            stop.wait(1.0)  # camera answered instantly, don't spin
                    if not stop.is_set():
                        # Hand over without a gap: open the next subscription, drain what the old one
                        # queued, then release the old one. Events during the swap are not dropped.
                        try:
                            next_adv = self.new_subscription(events)
                        except RuntimeError as e:
                            log.info("%s: cannot overlap subscriptions (%s), recycling with a short gap", self.cam_name, e)
                        else:
                            self.drain(pull, history, pull_limit)
                    self.unsubscribe(events, addr)
                    addr = None
            except self.PullUnavailable as e:
                attempt = 0
                if not stop.is_set():
                    if self.fallback:
                        log.warning("%s: %s; switching to polling the camera's event log", self.cam_name, e)
                        self.status(self.cam_name, "unavailable")
                        if self.fallback(self.cam_name, self.ip):
                            return
                    else:
                        log.warning("%s: %s; giving up for %d min", self.cam_name, e, UNAVAILABLE_RETRY_S // 60)
                        self.status(self.cam_name, "unavailable")
                    stop.wait(UNAVAILABLE_RETRY_S)
            except Exception as e:
                if not worked:
                    attempt = 0  # nothing worked yet, start the settings ladder again
                if not stop.is_set():
                    log.warning("%s: %s; retrying in %.0fs", self.cam_name, e, backoff)
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 60.0)
            finally:
                self.unsubscribe(events, addr)
                if next_adv:
                    self.unsubscribe(events, next_adv)
        log.info("%s: stopped", self.cam_name)


def utc_dt(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


class CameraPoller(threading.Thread):
    """Fallback for cameras whose ONVIF pull point does not work.

    Polls the camera's own detection log (the one tapo-downloader uses to name clips) and feeds
    the tracker the same start/end signals the ONVIF listener would. Latency is the poll interval
    plus however long the camera takes to log an event."""

    def __init__(self, name, ip, username, password, on_detection, status, stop, interval):
        super().__init__(name=f"poll-{name}", daemon=True)
        self.cam_name = name
        self.ip = ip
        self.username = username
        self.password = password
        self.on_detection = on_detection   # on_detection(cam, detection, active, when, data)
        self.status = status
        self.stop_event = stop
        self.interval = interval
        self.sample_logged = False

    @staticmethod
    def detection_name(ev):
        raw = (ev.get("type") or ev.get("detect_type") or ev.get("alarm_type")
               or ev.get("event_type") or ev.get("class"))
        if raw is None:
            return "motion"
        try:
            code = int(raw)
            return POLL_DETECTION_NAMES.get(code, f"type_{code}")
        except (ValueError, TypeError):
            return str(raw).strip().lower().replace(" ", "_").replace("-", "_")

    @staticmethod
    def call(fn, *args):
        """pytapo is synchronous in recent releases and async in older ones; accept both."""
        result = fn(*args)
        if inspect.isawaitable(result):
            async def wait(awaitable):
                return await awaitable
            result = asyncio.run(wait(result))
        return result

    def close(self, rec):
        rec["closed"] = True
        self.on_detection(self.cam_name, rec["detection"], False, utc_dt(rec["end"]), {})

    def process(self, events, seen, first, now_wall=None, now_mono=None):
        """Turn one poll of the detection log into start/end signals. seen holds our state per event."""
        now_wall = time.time() if now_wall is None else now_wall
        now_mono = time.monotonic() if now_mono is None else now_mono
        quiet = 2 * self.interval        # an event whose end has not moved for this long is finished
        finished_age = self.interval + 15  # already this old when first seen: it ended before our last poll
        present = set()
        for ev in events:
            try:
                start = int(ev.get("start_time"))
                end = max(start, int(ev["end_time"]) if ev.get("end_time") is not None else start)
            except (TypeError, ValueError, AttributeError):
                continue
            detection = self.detection_name(ev)
            key = ev["id"] if ev.get("id") is not None else (start, detection)
            present.add(key)
            old = now_wall - end > finished_age
            rec = seen.get(key)
            if rec is None:
                rec = seen[key] = {"detection": detection, "start": start, "end": end,
                                   "changed": now_mono, "closed": False}
                if first and old:
                    rec["closed"] = True   # history from before we started: not news
                    continue
                self.on_detection(self.cam_name, detection, True, utc_dt(start), {"source": "poll"})
            elif end != rec["end"]:
                rec["end"], rec["changed"], rec["closed"] = end, now_mono, False
                self.on_detection(self.cam_name, rec["detection"], True, utc_dt(rec["start"]), {"source": "poll"})
            if not rec["closed"] and (old or now_mono - rec["changed"] >= quiet):
                self.close(rec)
        for key in list(seen):   # events that dropped out of the camera's list
            if key in present:
                continue
            rec = seen[key]
            if not rec["closed"]:
                self.close(rec)
            if now_wall - rec["end"] > POLL_LOOKBACK_S:
                del seen[key]

    def run(self):
        try:
            from pytapo import Tapo
        except ImportError as e:
            log.error("%s: polling needs pytapo (%s)", self.cam_name, e)
            self.status(self.cam_name, "unavailable")
            return
        stop = self.stop_event
        tapo = None
        seen = {}
        first = True
        backoff = 5.0
        auth_failures = 0
        while not stop.is_set():
            try:
                if tapo is None:
                    host = CameraListener.host(self.ip)
                    tapo = Tapo(host=host, user=self.username, password=self.password, cloudPassword=self.password)
                    log.info("%s: polling the camera's event log every %.0fs", self.cam_name, self.interval)
                    self.status(self.cam_name, "polling")
                now = int(time.time())
                events = self.call(tapo.getEvents, now - POLL_LOOKBACK_S, now + 60) or []
                if isinstance(events, dict):
                    events = next((v for v in events.values() if isinstance(v, list)), [])
                if events and not self.sample_logged:
                    log.info("%s: sample from the camera's event log: %s", self.cam_name, events[0])
                    self.sample_logged = True
                log.debug("%s: poll returned %d events", self.cam_name, len(events))
                self.process(events, seen, first)
                first = False
                backoff = 5.0
                auth_failures = 0
                stop.wait(self.interval)
            except Exception as e:
                tapo = None
                msg = str(e)
                suspended = re.search(r"try again in (\d+) seconds", msg, re.I)
                if suspended:
                    # The camera has locked logins out; retrying does not help and may extend it.
                    wait = int(suspended.group(1)) + 60
                    log.warning("%s: camera has suspended logins (%s); not retrying for %d min",
                                self.cam_name, msg, wait // 60)
                    self.status(self.cam_name, "unavailable")
                elif re.search(r"authentication|unauthori[sz]ed|password|credential", msg, re.I):
                    auth_failures += 1
                    self.status(self.cam_name, "unavailable")
                    if auth_failures >= POLL_AUTH_MAX_FAILURES:
                        log.error("%s: login rejected %d times (%s); giving up on polling this camera until "
                                  "restarted. Check the camera account username/password pytapo logs in with.",
                                  self.cam_name, auth_failures, msg)
                        return
                    wait = POLL_AUTH_RETRY_S
                    log.error("%s: login rejected (%s); will try once more in %d min, not sooner, so the "
                              "camera does not lock logins out", self.cam_name, msg, wait // 60)
                else:
                    wait = backoff
                    backoff = min(backoff * 2, 300.0)
                    log.warning("%s: polling failed (%s); retrying in %.0fs", self.cam_name, e, wait)
                stop.wait(wait)
        log.info("%s: polling stopped", self.cam_name)


def from_downloader_config(cfg):
    """Accept tapo-downloader's config.json as is: map its keys onto this script's own."""
    out = dict(cfg)
    if cfg.get("onvif_username"):
        # The downloader's username/password are the cloud login (used for polling); ONVIF has its own.
        out["tapo_username"], out["tapo_password"] = cfg.get("username"), cfg.get("password")
        out["username"] = cfg["onvif_username"]
        out["password"] = cfg.get("onvif_password") or cfg.get("password")
    if "cams" not in out and isinstance(cfg.get("ip_addresses"), list):
        out["cams"] = {f"cam{i + 1}": ip for i, ip in enumerate(cfg["ip_addresses"]) if ip}
    if "settle" not in out and "settle_seconds" in cfg:
        out["settle"] = cfg["settle_seconds"]
    if "sweep_min" not in out and cfg.get("mode") == "sweep" and "sweep_minutes" in cfg:
        out["sweep_min"] = cfg["sweep_minutes"]
    if "poll_interval" not in out and "poll_seconds" in cfg:
        out["poll_interval"] = cfg["poll_seconds"]
    if "poll_cams" not in out and "poll_cameras" in cfg:
        out["poll_cams"] = cfg["poll_cameras"]
    if "hook_timeout" not in out and "hook_timeout_minutes" in cfg:
        out["hook_timeout"] = cfg["hook_timeout_minutes"] * 60
    return out


def load_cams(args, cfg):
    """Camera name -> IP: --cam NAME=IP / --cam IP if given, otherwise the config file's cameras."""
    if args.cam:
        cams = {}
        for item in args.cam:
            name, _, ip = item.partition("=")
            cams[name if ip else item] = ip or item
        return cams
    raw = cfg.get("cams", {})
    if isinstance(raw, list):
        raw = {ip: ip for ip in raw}
    return dict(raw)


def main():
    parser = argparse.ArgumentParser(description="Tapo ONVIF detection listener")
    parser.add_argument("--config", help="JSON config (default: ~/.tapo-downloader/config.json if it exists); a tapo-downloader config works as is")
    parser.add_argument("--user", help="ONVIF login: camera account username")
    parser.add_argument("--password", help="ONVIF login: camera account password (or TAPO_PASSWORD)")
    parser.add_argument("--tapo-user", help="polling login (pytapo): cloud username; defaults to --user")
    parser.add_argument("--tapo-password", help="polling login (pytapo): cloud password (or TAPO_CLOUD_PASSWORD); defaults to --password")
    parser.add_argument("--cam", action="append", help="camera as IP or NAME=IP, repeatable")
    parser.add_argument("--settle", type=float, help="quiet seconds before a camera is settled (default 30)")
    parser.add_argument("--stale", type=float, help="seconds of silence before a pulse detection (no Is* flag) counts as ended (default 15)")
    parser.add_argument("--stale-flagged", type=float, help="seconds without any update before a detection the camera reports "
                                                          "start/end for is force-ended; backstop only (default 300)")
    parser.add_argument("--sweep-min", type=float, help="also run the hook for every camera every N minutes (default off)")
    parser.add_argument("--on-settled", help="command to run when a camera settles or sweeps")
    parser.add_argument("--hook-timeout", type=float, help="seconds before a hook run is killed (default 3600)")
    parser.add_argument("--retry-delay", type=float, help="seconds before re-running a hook that exited 75 (default 120)")
    parser.add_argument("--retry-max", type=int, help="how many times to retry a hook that exited 75 (default 3)")
    parser.add_argument("--poll", action="append", metavar="NAME",
                        help="poll this camera's event log instead of using ONVIF, repeatable (config: poll_cameras)")
    parser.add_argument("--poll-interval", type=float,
                        help="seconds between polls; also enables the automatic fallback for cameras whose ONVIF "
                             "pull fails, 0 turns polling off (default 30; config: poll_seconds)")
    parser.add_argument("--timezone", help="IANA timezone for {date}/{date_end} (default: system timezone)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every notification")
    args = parser.parse_args()

    cfg = {}
    cfg_path = args.config or (DOWNLOADER_CONFIG if os.path.isfile(DOWNLOADER_CONFIG) else None)
    if cfg_path:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = from_downloader_config(json.load(f))
    def pick(*sources):
        for label, value in sources:
            if value:
                return value, label
        return None, None

    user, user_src = pick(("--user", args.user), ("config", cfg.get("username")))
    password, pw_src = pick(("--password", args.password), ("env TAPO_PASSWORD", os.environ.get("TAPO_PASSWORD")),
                            ("config", cfg.get("password")))
    tapo_user, tu_src = pick(("--tapo-user", args.tapo_user), ("config", cfg.get("tapo_username")),
                             ("the ONVIF username", user))
    tapo_password, tp_src = pick(("--tapo-password", args.tapo_password),
                                 ("env TAPO_CLOUD_PASSWORD", os.environ.get("TAPO_CLOUD_PASSWORD")),
                                 ("config", cfg.get("tapo_password")), ("the ONVIF password", password))
    cams = load_cams(args, cfg)
    settle = args.settle if args.settle is not None else cfg.get("settle", 30.0)
    stale = args.stale if args.stale is not None else cfg.get("stale", 15.0)
    sweep_min = args.sweep_min if args.sweep_min is not None else cfg.get("sweep_min", 0.0)
    template = args.on_settled or cfg.get("on_settled")
    hook_timeout = args.hook_timeout if args.hook_timeout is not None else cfg.get("hook_timeout", 3600.0)
    stale_flagged = args.stale_flagged if args.stale_flagged is not None else cfg.get("stale_flagged", 300.0)
    retry_delay = args.retry_delay if args.retry_delay is not None else cfg.get("retry_delay", 120.0)
    retry_max = args.retry_max if args.retry_max is not None else cfg.get("retry_max", 3)
    tz_name = args.timezone or cfg.get("timezone")
    poll_interval = args.poll_interval if args.poll_interval is not None else cfg.get("poll_interval", DEFAULT_POLL_INTERVAL)
    poll_cams = set(args.poll or [])
    if poll_interval:
        poll_cams |= set(cfg.get("poll_cams", [])) & set(cams)   # config entries only matter for cameras given
    if not (user and password and cams):
        parser.error("username, password and at least one camera are required")

    if poll_cams - set(cams):
        parser.error(f"--poll names unknown camera(s): {', '.join(sorted(poll_cams - set(cams)))}")
    if poll_cams and not poll_interval:
        parser.error("--poll needs a --poll-interval above 0")
    if tz_name:
        global LOCAL_TZ
        try:
            LOCAL_TZ = ZoneInfo(tz_name)
        except Exception:
            parser.error(f"unknown timezone {tz_name!r}")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s [%(threadName)s] %(levelname)s - %(message)s")
    if cfg_path:
        log.info("config: %s", cfg_path)
    log.info("ONVIF login: %s (username from %s, password from %s)", user, user_src, pw_src)
    if poll_interval:
        log.info("polling login: %s (username from %s, password from %s)", tapo_user, tu_src, tp_src)
    log.info("cameras: %s", ", ".join(f"{name}={ip}" for name, ip in cams.items()))
    for noisy in ("zeep", "urllib3", "asyncio", "kasa", "pytapo"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    out_lock = threading.Lock()

    def emit(evt):
        evt = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), **evt}
        with out_lock:
            print(json.dumps(evt), flush=True)

    runner = HookRunner(template, hook_timeout, emit, retry_delay, retry_max) if template else None

    def on_event(evt):
        emit(evt)
        if evt["event"] == "settled" and runner:
            start = datetime.strptime(evt["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            end = datetime.strptime(evt["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            runner.submit(evt["cam"], cams[evt["cam"]], {"start": start, "end": end, "types": set(evt["types"])})

    tracker = Tracker(on_event, stale, settle, stale_flagged)

    def on_notification(cam, topic, operation, utc, data):
        detection, active, flagged = classify(topic, data)
        when, source = event_time(utc, datetime.now(timezone.utc))
        log.debug("%s: topic=%s op=%s utc=%s data=%s (time from %s)", cam, topic, operation, utc, data, source)
        tracker.feed(cam, detection, active, when, data, flagged)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def status(cam, state):
        emit({"event": "status", "cam": cam, "state": state})

    def on_detection(cam, detection, active, when, data):
        tracker.feed(cam, detection, active, when, data, True)

    pollers = []

    def start_poller(name, ip):
        if not poll_interval:
            return False
        poller = CameraPoller(name, ip, tapo_user, tapo_password, on_detection, status, stop, poll_interval)
        pollers.append(poller)
        poller.start()
        return True

    listeners = []
    for name, ip in cams.items():
        if name in poll_cams:
            start_poller(name, ip)
        else:
            listeners.append(CameraListener(name, ip, user, password, on_notification, status, stop,
                                            fallback=start_poller if poll_interval else None))
    for listener in listeners:
        listener.start()

    next_sweep = time.monotonic() + sweep_min * 60 if sweep_min else None
    while not stop.wait(1.0):
        tracker.tick()
        if next_sweep and time.monotonic() >= next_sweep:
            next_sweep = time.monotonic() + sweep_min * 60
            for name, ip in cams.items():
                emit({"event": "sweep", "cam": name})
                if runner:
                    runner.submit(name, ip, {"start": None, "end": None, "types": set()})

    if runner:
        runner.close()   # stop any download still running instead of waiting for it
    for thread in listeners + list(pollers):
        thread.join(timeout=15)


if __name__ == "__main__":
    main()
