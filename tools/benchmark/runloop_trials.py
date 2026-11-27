"""20 closed-loop trials with a synthetic recorder, isolated NSTextView and unposted Quartz events."""
import sys,time,json,subprocess,threading
from unittest.mock import patch
from contextlib import ExitStack
import numpy as np
import AppKit as A
import Quartz as Q
from Foundation import NSRunLoop,NSDate,NSMakeRect
import voice_to_text as app
import injection
from audio import Capture
from config import DEFAULTS
from hotkey import HotkeyAction,HotkeyEvent
from statemachine import Mode

native=A.NSApplication.sharedApplication()
native.setActivationPolicy_(A.NSApplicationActivationPolicyProhibited)
view=A.NSTextView.alloc().initWithFrame_(NSMakeRect(0,0,600,100))
clock=time.monotonic
pcm=np.frombuffer(subprocess.check_output(["ffmpeg","-v","error","-i","time.aiff","-ac","1","-ar","16000","-f","f32le","-"]),dtype="<f4")
rows=[]
class FileRecorder:
    def __init__(self,*a,**kw):pass
    def start(self,current):marks["source_ready"]=clock()
    def stop(self):return Capture(pcm,len(pcm)/16000,.5,.1)
    def abort(self):pass
def spin(predicate,seconds):
    end=clock()+seconds
    while not predicate() and clock()<end:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.002))
    if not predicate():raise TimeoutError("fixture run loop deadline")
def sink(t,event):
    if Q.CGEventGetType(event)==Q.kCGEventKeyDown:
        _,s=Q.CGEventKeyboardGetUnicodeString(event,128,None,None)
        view.insertText_(s)
        marks["field_updated"]=clock()
def destination():return injection.Destination(1, "window", "field", (0, 0))
def health(*a):return injection.PermissionState.GRANTED
with ExitStack() as stack:
    for target,value in [("voice_to_text.Recorder",FileRecorder),("voice_to_text.capture_destination",destination),("injection.capture_destination",destination),("voice_to_text.load_vocabulary",lambda:""),("injection.accessibility_state",health),("injection.event_posting_state",health),("Quartz.CGEventPostToPid",sink)]:
        stack.enter_context(patch(target,value))
    c=app.DictationController({**DEFAULTS,"model":sys.argv[1],"sound_enabled":False,"injection_mode":"unicode"})
    assert c.transcriber.warm_up()
    original=c.injector.inject_async
    def schedule(*a,**kw):marks["schedule"]=clock();original(*a,**kw)
    c.injector.inject_async=schedule
    def refresh(_):
        marks.setdefault(c.state.mode.value,clock())
    c.ui_refresh=refresh
    for _ in range(20):
        marks={};view.setString_("")
        down=clock();c.handle_hotkey_event(HotkeyEvent(HotkeyAction.OPTION_DOWN,down))
        spin(lambda:"recording" in marks and "source_ready" in marks,2)
        up=clock();c.handle_hotkey_event(HotkeyEvent(HotkeyAction.OPTION_UP,up))
        spin(lambda:c.state.mode is Mode.DONE,8)
        done=clock();assert "meeting" in str(view.string()).lower()
        rows.append(dict(down_to_refresh=marks["recording"]-down,down_to_fixture_ready=marks["source_ready"]-down,up_to_refresh=marks["transcribing"]-up,schedule_to_field=marks["field_updated"]-marks["schedule"],up_to_private_field=marks["field_updated"]-up,up_to_done=done-up))
    c.shutdown()
print(json.dumps(rows,indent=2))
