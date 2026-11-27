"""Worker-stage measurements; pass two absolute cached snapshot paths."""
import json,resource,subprocess,sys,time
import numpy as np
import mlx.core as mx
import mlx_whisper
import transcription
from audio import Capture
from config import DEFAULTS

def rss():
    return int(subprocess.check_output(["ps","-p",str(__import__("os").getpid()),"-o","rss="]))
def memory():
    return dict(rss_KiB=rss(),active=mx.get_active_memory(),cache=mx.get_cache_memory(),peak=mx.get_peak_memory())
data=np.frombuffer(subprocess.check_output(["ffmpeg","-v","error","-i","time.aiff","-ac","1","-ar","16000","-f","f32le","-"]),dtype="<f4")
a=Capture(data,len(data)/16000,float(abs(data).max()),float(np.sqrt(np.mean(data**2))))
samples=[]
def timed(fn,key):
    def wrapper(*args,**kw):
        t=time.perf_counter()
        result=fn(*args,**kw)
        sample[key]=time.perf_counter()-t
        return result
    return wrapper
engine=transcription.Transcriber({**DEFAULTS,"model":sys.argv[1]},"GitHub, WebSocket, Python, NumPy, MLX, Jira")
transcription.analyze_speech=timed(transcription.analyze_speech,"pre")
mlx_whisper.transcribe=timed(mlx_whisper.transcribe,"mlx")
engine._transcribe_locked=timed(engine._transcribe_locked,"decode_and_post")
initial=memory()
for path,n in [(sys.argv[1],20),(sys.argv[2],5),(sys.argv[1],5)]:
    engine.select_model(path)
    sample={}
    t=time.perf_counter()
    assert engine.warm_up()
    stage=dict(model=path.split("/")[-3],load=time.perf_counter()-t,before_trials=memory(),rows=[])
    for _ in range(n):
        sample={}
        r=engine.transcribe(a)
        sample["post_plus_wrapper"]=sample["decode_and_post"]-sample["mlx"]
        sample["outcome"]=r.outcome.value
        stage["rows"].append(sample)
    stage["after_trials"]=memory()
    samples.append(stage)
print(json.dumps(dict(before=initial,stages=samples),indent=2))
